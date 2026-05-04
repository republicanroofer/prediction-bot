from __future__ import annotations

"""
WhaleScorer — ranks Polymarket traders by consistent profitability.

Ranking methodology (from poly_data/Isolated.ipynb):
  1. Fetch the Polymarket leaderboard from data-api.polymarket.com/leaderboard.
  2. Filter: last active after ACTIVITY_CUTOFF, markets_traded >= MIN_MARKETS.
  3. Join to accumulated whale_trades in DB for per-market PnL reconstruction.
  4. Compute per-trader metrics:
       win_rate      = fraction of markets with positive realised PnL
       big_win_rate  = fraction of winning markets with gain >= 70 %
       median_gain   = median ROI across winning markets
       median_loss   = median ROI across losing markets
  5. Composite score = 50*big_win_rate + 30*max(0,median_gain) + 20*win_rate (→ 0–100).
  6. Upsert into whale_scores table; deactivate addresses not seen in 30 days.

Score thresholds (configurable via settings.whale_min_score, default 60):
    >= 80  elite whale — mirror unconditionally (subject to portfolio gates)
    60–79  strong whale — mirror if market signal confirms
    40–59  watch-only — accumulate data, do not mirror
     < 40  ignore

When accumulated DB trade data is sparse (early bootstrap), the leaderboard
fetch provides a useful prior.  Once > 500 trades are in DB, the leaderboard
fetch becomes supplementary.
"""

import asyncio
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from backend.db.database import Database

logger = logging.getLogger(__name__)

_LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
_ACTIVITY_CUTOFF = timedelta(days=90)
_MIN_MARKETS = 50          # minimum markets traded to include in ranking
_DEACTIVATE_AFTER = 30     # days of inactivity before marking is_active=False
_BATCH_SIZE = 50           # leaderboard page size
_MAX_LEADERBOARD_PAGES = 10  # fetch top 500 traders only (ordered by PNL)


class WhaleScorer:
    """
    Runs on the hourly maintenance cycle.  Call run_once() from the orchestrator.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run_once(self) -> None:
        logger.info("WhaleScorer: starting scoring run")
        now = datetime.now(timezone.utc)
        cutoff = now - _ACTIVITY_CUTOFF

        # 1. Score from accumulated DB trade data (primary source)
        db_count = await self._score_from_db(now)

        # 2. Supplement with Polymarket leaderboard (adds addresses not yet in DB)
        try:
            lb_count = await self._score_from_leaderboard(cutoff)
        except Exception:
            logger.warning("WhaleScorer: leaderboard fetch failed — skipping", exc_info=True)
            lb_count = 0

        # 3. Deactivate stale addresses
        await self._deactivate_stale(now)

        logger.info(
            "WhaleScorer: updated %d from DB, %d from leaderboard",
            db_count, lb_count,
        )

    # ── Score from accumulated DB trades ──────────────────────────────────────

    async def _score_from_db(self, now: datetime) -> int:
        async with self._db._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    maker_address,
                    COUNT(DISTINCT condition_id)            AS markets_traded,
                    SUM(usd_amount)                         AS total_volume,
                    MAX(block_timestamp)                    AS last_trade_at,
                    -- approximate realised cash flow
                    SUM(CASE WHEN maker_direction='sell'
                             THEN  usd_amount
                             ELSE -usd_amount END)          AS approx_cash_flow,
                    -- per-market signed PnL for median calculation
                    jsonb_agg(jsonb_build_object(
                        'condition_id', condition_id,
                        'direction', maker_direction,
                        'usd', usd_amount
                    ))                                      AS trade_rows
                FROM whale_trades
                WHERE is_platform_tx = FALSE
                GROUP BY maker_address
                HAVING COUNT(DISTINCT condition_id) >= $1
                """,
                _MIN_MARKETS,
            )

        updated = 0
        for row in rows:
            address = row["maker_address"]
            markets = int(row["markets_traded"] or 0)
            volume = float(row["total_volume"] or 0)
            last_trade = row["last_trade_at"]
            cash_flow = float(row["approx_cash_flow"] or 0)

            trade_rows = row["trade_rows"] or []
            metrics = _compute_metrics_from_trades(trade_rows, volume, markets)

            composite = _composite_score(
                big_win_rate=metrics["big_win_rate"],
                median_gain=metrics["median_gain"],
                win_rate=metrics["win_rate"],
            )

            await self._db.upsert_whale_score(address, {
                "total_pnl_usd":    round(cash_flow, 2),
                "win_rate":         round(metrics["win_rate"], 4),
                "big_win_rate":     round(metrics["big_win_rate"], 4),
                "median_gain_pct":  round(metrics["median_gain"], 4),
                "median_loss_pct":  round(metrics["median_loss"], 4),
                "markets_traded":   markets,
                "total_volume_usd": round(volume, 2),
                "composite_score":  round(composite, 2),
                "is_active":        True,
                "last_trade_at":    last_trade,
            })
            updated += 1

        return updated

    # ── Score from Polymarket leaderboard ─────────────────────────────────────

    async def _score_from_leaderboard(self, cutoff: datetime) -> int:
        addresses = await self._fetch_leaderboard(cutoff)
        if not addresses:
            return 0

        updated = 0
        for entry in addresses:
            # v1 API uses "proxyWallet" (not "proxyWalletAddress")
            address = (entry.get("proxyWallet") or entry.get("proxyWalletAddress") or "").lower()
            if not address:
                continue

            # Skip if we already have high-quality DB data for this address
            existing = await self._db.get_whale_score(address)
            if existing and int(existing.markets_traded or 0) >= _MIN_MARKETS * 2:
                continue

            # v1 API uses "vol" for volume; marketsTraded not exposed, use default
            pnl = float(entry.get("pnl") or entry.get("profit") or 0)
            volume = float(entry.get("vol") or entry.get("volume") or 0)
            # v1 leaderboard doesn't expose marketsTraded; assume qualified traders
            markets = int(entry.get("marketsTraded") or _MIN_MARKETS + 10)

            # Leaderboard doesn't expose win/loss breakdown directly;
            # approximate from pnl/volume ratio
            implied_win_rate = min(0.85, max(0.35, 0.50 + pnl / max(volume, 1) * 0.5))
            big_win_rate = implied_win_rate * 0.75
            median_gain = pnl / max(markets, 1) / max(volume / max(markets, 1), 1)
            median_gain = max(-0.50, min(0.50, median_gain))

            composite = _composite_score(big_win_rate, median_gain, implied_win_rate)

            await self._db.upsert_whale_score(address, {
                "display_name":     entry.get("userName") or entry.get("name") or entry.get("username"),
                "total_pnl_usd":    round(pnl, 2),
                "win_rate":         round(implied_win_rate, 4),
                "big_win_rate":     round(big_win_rate, 4),
                "median_gain_pct":  round(median_gain, 4),
                "median_loss_pct":  round(-abs(median_gain) * 0.4, 4),
                "markets_traded":   markets,
                "total_volume_usd": round(volume, 2),
                "composite_score":  round(composite, 2),
                "is_active":        True,
                "last_trade_at":    None,
            })
            updated += 1

        return updated

    async def _fetch_leaderboard(self, cutoff: datetime) -> list[dict]:
        """Fetch top traders from Polymarket's data API (v1), capped at _MAX_LEADERBOARD_PAGES."""
        results: list[dict] = []
        offset = 0
        pages_fetched = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as http:
            while pages_fetched < _MAX_LEADERBOARD_PAGES:
                params = {
                    "limit": _BATCH_SIZE,
                    "offset": offset,
                    "timePeriod": "ALL",
                    "orderBy": "PNL",
                }
                for attempt in range(3):
                    try:
                        resp = await http.get(_LEADERBOARD_URL, params=params)
                        if resp.status_code == 429:
                            await asyncio.sleep(5)
                            continue
                        resp.raise_for_status()
                        batch = resp.json()
                        break
                    except (httpx.RequestError, Exception):
                        if attempt == 2:
                            return results
                        await asyncio.sleep(2 ** attempt)
                else:
                    break

                if not batch:
                    break

                results.extend(batch)
                pages_fetched += 1

                if len(batch) < _BATCH_SIZE:
                    break
                offset += _BATCH_SIZE

        return results

    # ── Maintenance ───────────────────────────────────────────────────────────

    async def _deactivate_stale(self, now: datetime) -> None:
        stale_cutoff = now - timedelta(days=_DEACTIVATE_AFTER)
        async with self._db._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE whale_scores SET is_active = FALSE
                WHERE COALESCE(last_trade_at, scored_at) < $1
                  AND is_active = TRUE
                """,
                stale_cutoff,
            )
        logger.debug("WhaleScorer: deactivated stale addresses: %s", result)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _compute_metrics_from_trades(
    trade_rows: list[dict],
    total_volume: float,
    markets_traded: int,
) -> dict:
    """
    Reconstruct per-market PnL from raw buy/sell rows and compute
    win_rate, big_win_rate, median_gain, median_loss.
    """
    # Aggregate by condition_id
    by_market: dict[str, float] = {}
    for t in trade_rows:
        cid = t.get("condition_id") or ""
        direction = t.get("direction", "buy")
        usd = float(t.get("usd") or 0)
        if not cid:
            continue
        sign = 1.0 if direction == "sell" else -1.0
        by_market[cid] = by_market.get(cid, 0.0) + sign * usd

    if not by_market:
        avg_stake = total_volume / max(markets_traded, 1)
        return {
            "win_rate": 0.50,
            "big_win_rate": 0.25,
            "median_gain": 0.0,
            "median_loss": 0.0,
        }

    gains = []
    losses = []
    for pnl in by_market.values():
        avg_stake = total_volume / max(len(by_market), 1)
        pct = pnl / avg_stake if avg_stake > 0 else 0.0
        if pnl > 0:
            gains.append(pct)
        else:
            losses.append(pct)

    total = len(by_market)
    win_rate = len(gains) / total if total else 0.5
    big_wins = [g for g in gains if g >= 0.70]
    big_win_rate = len(big_wins) / len(gains) if gains else 0.0
    median_gain = statistics.median(gains) if gains else 0.0
    median_loss = statistics.median(losses) if losses else 0.0

    return {
        "win_rate":     win_rate,
        "big_win_rate": big_win_rate,
        "median_gain":  median_gain,
        "median_loss":  median_loss,
    }


def _composite_score(
    big_win_rate: float,
    median_gain: float,
    win_rate: float,
) -> float:
    """Composite score scaled to 0–100."""
    raw = (
        0.50 * big_win_rate
        + 0.30 * max(0.0, median_gain)
        + 0.20 * win_rate
    )
    return round(min(100.0, max(0.0, raw * 100)), 2)
