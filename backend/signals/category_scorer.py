from __future__ import annotations

"""
CategoryScorer — composite per-category performance scoring.

Composite score (0–100):
    score = 40*roi_c + 20*sample_c + 25*trend_c + 15*winrate_c
    (each component normalised to [0,1] before weighting)

Components:
  roi_c    : ROI ∈ [-0.25, +0.25] → [0, 1]
  sample_c : log-scaled, saturates at 200 closed trades
  trend_c  : last-10-trade avg ROI ∈ [-0.15, +0.15] → [0, 1]
  winrate_c: win_rate ∈ [0.30, 0.70] → [0, 1]

Allocation tiers (max fraction of portfolio per category):
    >= 80  →  20%
    60–79  →  10%
    40–59  →   5%
    30–39  →   2%
     < 30  →  BLOCKED

Unknown / new categories default to score 45 (cautious trading allowed).
Scores are rebuilt from closed positions on each hourly maintenance cycle.
"""

import logging
import math
from typing import Optional

from backend.db.database import Database
from backend.db.models import CategoryScore, Exchange

logger = logging.getLogger(__name__)

BLOCK_THRESHOLD = 30.0
_DEFAULT_SCORE = 45.0

_TIERS: list[tuple[float, float]] = [
    (80.0, 0.20),
    (60.0, 0.10),
    (40.0, 0.05),
    (30.0, 0.02),
    (0.0,  0.00),
]


class CategoryScorer:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Query helpers ─────────────────────────────────────────────────────────

    async def get(
        self, exchange: Exchange, category: Optional[str]
    ) -> Optional[CategoryScore]:
        if not category:
            return None
        return await self._db.get_category_score(exchange, category.lower())

    def is_blocked(self, score: Optional[CategoryScore]) -> bool:
        """Return True if trading in this category is forbidden."""
        if score is None:
            return False  # unknown category → cautious entry allowed
        return bool(score.is_blocked) or float(score.composite_score) < BLOCK_THRESHOLD

    def allocation_pct(self, score: Optional[CategoryScore]) -> float:
        """Max portfolio fraction allowed for this category."""
        if score is None:
            return tier_allocation(_DEFAULT_SCORE)
        return tier_allocation(float(score.composite_score))

    # ── Score rebuild ─────────────────────────────────────────────────────────

    async def rebuild(self) -> None:
        """
        Recompute composite scores from all closed positions and update
        the category_scores table.  Safe to call concurrently — uses
        separate pool connections per category update.
        """
        async with self._db._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    p.exchange,
                    m.category,
                    COUNT(*)                                                  AS n,
                    AVG(CASE WHEN p.realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                    AVG(p.realized_pnl / NULLIF(p.cost_basis_usd, 0))        AS avg_roi,
                    AVG(recent.roi)                                           AS recent_trend
                FROM positions p
                JOIN markets m ON m.id = p.market_id
                LEFT JOIN LATERAL (
                    SELECT p2.realized_pnl / NULLIF(p2.cost_basis_usd, 0) AS roi
                    FROM positions p2
                    JOIN markets m2 ON m2.id = p2.market_id
                    WHERE p2.exchange = p.exchange
                      AND m2.category = m.category
                      AND p2.status = 'closed'
                    ORDER BY p2.closed_at DESC
                    LIMIT 10
                ) recent ON TRUE
                WHERE p.status = 'closed'
                  AND m.category IS NOT NULL
                GROUP BY p.exchange, m.category
                """
            )

        updated = 0
        for row in rows:
            n = int(row["n"] or 0)
            win_rate = float(row["win_rate"] or 0.5)
            avg_roi = float(row["avg_roi"] or 0.0)
            trend = float(row["recent_trend"] or 0.0)

            cs = compute_composite(avg_roi, n, trend, win_rate)
            alloc = tier_allocation(cs)

            async with self._db._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE category_scores
                    SET composite_score = $3,
                        roi             = $4,
                        win_rate        = $5,
                        sample_size     = $6,
                        recent_trend    = $7,
                        allocation_pct  = $8,
                        is_blocked      = $9,
                        scored_at       = NOW()
                    WHERE exchange = $1 AND category = $2
                    """,
                    row["exchange"],
                    row["category"],
                    cs,
                    avg_roi,
                    win_rate,
                    n,
                    trend,
                    alloc,
                    cs < BLOCK_THRESHOLD,
                )
            updated += 1

        logger.info("CategoryScorer: rebuilt %d category scores", updated)


# ── Pure functions (also used by scanner inline logic) ────────────────────────

def compute_composite(
    roi: float,
    sample_size: int,
    recent_trend: float,
    win_rate: float,
) -> float:
    """Return composite score ∈ [0, 100]."""
    roi_c    = _norm(roi, lo=-0.25, hi=0.25)
    sample_c = min(1.0, math.log1p(sample_size) / math.log1p(200))
    trend_c  = _norm(recent_trend, lo=-0.15, hi=0.15)
    wr_c     = _norm(win_rate, lo=0.30, hi=0.70)
    return round(40.0 * roi_c + 20.0 * sample_c + 25.0 * trend_c + 15.0 * wr_c, 2)


def tier_allocation(score: float) -> float:
    """Return the portfolio-fraction cap for a given composite score."""
    for threshold, alloc in _TIERS:
        if score >= threshold:
            return alloc
    return 0.0


def _norm(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))
