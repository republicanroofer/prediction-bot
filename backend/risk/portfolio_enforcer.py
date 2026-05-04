from __future__ import annotations

"""
PortfolioEnforcer — sequential 4-gate pre-trade firewall.

Every proposed trade passes through all four gates in order.  The first
failure short-circuits and the trade is rejected with an audit record.

Gate 1 — category_score
    The category composite score must be >= BLOCK_THRESHOLD (30).
    Hard-blocked categories cannot be traded regardless of signal strength.

Gate 2 — allocation_cap
    Total open exposure in this (exchange, category) must not exceed the
    category's allocation_pct × portfolio_usd.

Gate 3 — position_size
    The proposed trade size must not exceed max_position_pct × portfolio_usd.

Gate 4 — sector_concentration
    Total open exposure in this category (across all signals/positions) must
    not exceed max_sector_concentration_pct × portfolio_usd.

All rejections are written to the blocked_trades table for monitoring.
Approvals are not logged (high-frequency path; keep it fast).
"""

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from backend.config.settings import get_settings
from backend.db.database import Database
from backend.db.models import (
    BlockedTradeInsert,
    CategoryScore,
    Exchange,
    Market,
    SignalType,
    TradingMode,
)
from backend.signals.category_scorer import BLOCK_THRESHOLD, CategoryScorer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnforcerResult:
    approved: bool
    gate: Optional[str]      # which gate fired, or None if approved
    reason: Optional[str]    # human-readable explanation


_APPROVED = EnforcerResult(approved=True, gate=None, reason=None)


class PortfolioEnforcer:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._scorer = CategoryScorer(db)
        self._cfg = get_settings()

    async def check(
        self,
        *,
        exchange: Exchange,
        market: Market,
        side: str,
        size_usd: float,
        contracts: float,
        entry_price: float,
        signal_type: SignalType,
        portfolio_usd: float,
        cat_score: Optional[CategoryScore] = None,
    ) -> EnforcerResult:
        """
        Run all four gates.  Fetch category score if not supplied.
        Returns EnforcerResult(approved=True) or the first gate that failed.
        """
        category = (market.category or "unknown").lower()

        if cat_score is None:
            cat_score = await self._scorer.get(exchange, category)

        # ── Gate 1: category composite score ──────────────────────────────────
        result = self._gate_category_score(cat_score, category)
        if not result.approved:
            await self._audit(exchange, market, side, size_usd, contracts,
                              entry_price, signal_type, result)
            return result

        # ── Gate 2: category allocation cap ───────────────────────────────────
        result = await self._gate_allocation_cap(
            exchange, category, size_usd, portfolio_usd, cat_score
        )
        if not result.approved:
            await self._audit(exchange, market, side, size_usd, contracts,
                              entry_price, signal_type, result)
            return result

        # ── Gate 3: per-position size cap ─────────────────────────────────────
        result = self._gate_position_size(size_usd, portfolio_usd)
        if not result.approved:
            await self._audit(exchange, market, side, size_usd, contracts,
                              entry_price, signal_type, result)
            return result

        # ── Gate 4: sector concentration ──────────────────────────────────────
        result = await self._gate_sector_concentration(
            exchange, category, size_usd, portfolio_usd
        )
        if not result.approved:
            await self._audit(exchange, market, side, size_usd, contracts,
                              entry_price, signal_type, result)
            return result

        return _APPROVED

    # ── Gate implementations ──────────────────────────────────────────────────

    def _gate_category_score(
        self,
        cat_score: Optional[CategoryScore],
        category: str,
    ) -> EnforcerResult:
        if cat_score is None:
            return _APPROVED  # unknown category — allow cautiously

        score = float(cat_score.composite_score)
        if cat_score.is_blocked or score < BLOCK_THRESHOLD:
            return EnforcerResult(
                approved=False,
                gate="category_score",
                reason=(
                    f"'{category}' score {score:.1f} < threshold {BLOCK_THRESHOLD} "
                    f"(blocked={cat_score.is_blocked})"
                ),
            )
        return _APPROVED

    async def _gate_allocation_cap(
        self,
        exchange: Exchange,
        category: str,
        size_usd: float,
        portfolio_usd: float,
        cat_score: Optional[CategoryScore],
    ) -> EnforcerResult:
        if cat_score is None or cat_score.allocation_pct is None:
            return _APPROVED

        alloc_pct = float(cat_score.allocation_pct)
        if alloc_pct <= 0:
            return EnforcerResult(
                approved=False,
                gate="allocation_cap",
                reason=f"'{category}' allocation_pct=0 (category near-blocked)",
            )

        max_usd = portfolio_usd * alloc_pct
        current_usd = await self._db.get_category_exposure_usd(exchange, category)

        if current_usd + size_usd > max_usd:
            return EnforcerResult(
                approved=False,
                gate="allocation_cap",
                reason=(
                    f"'{category}' exposure ${current_usd:.0f} + ${size_usd:.0f} "
                    f"= ${current_usd + size_usd:.0f} > cap ${max_usd:.0f} "
                    f"({alloc_pct*100:.0f}% of ${portfolio_usd:.0f})"
                ),
            )
        return _APPROVED

    def _gate_position_size(
        self, size_usd: float, portfolio_usd: float
    ) -> EnforcerResult:
        max_usd = portfolio_usd * self._cfg.max_position_pct
        if size_usd > max_usd:
            return EnforcerResult(
                approved=False,
                gate="position_size",
                reason=(
                    f"size ${size_usd:.2f} > max ${max_usd:.2f} "
                    f"({self._cfg.max_position_pct*100:.1f}% of ${portfolio_usd:.0f})"
                ),
            )
        return _APPROVED

    async def _gate_sector_concentration(
        self,
        exchange: Exchange,
        category: str,
        size_usd: float,
        portfolio_usd: float,
    ) -> EnforcerResult:
        max_usd = portfolio_usd * self._cfg.max_sector_concentration_pct
        current_usd = await self._db.get_category_exposure_usd(exchange, category)

        if current_usd + size_usd > max_usd:
            return EnforcerResult(
                approved=False,
                gate="sector_concentration",
                reason=(
                    f"'{category}' sector ${current_usd + size_usd:.0f} "
                    f"> max ${max_usd:.0f} "
                    f"({self._cfg.max_sector_concentration_pct*100:.0f}% of ${portfolio_usd:.0f})"
                ),
            )
        return _APPROVED

    # ── Audit logging ─────────────────────────────────────────────────────────

    async def _audit(
        self,
        exchange: Exchange,
        market: Market,
        side: str,
        size_usd: float,
        contracts: float,
        entry_price: float,
        signal_type: SignalType,
        result: EnforcerResult,
    ) -> None:
        try:
            await self._db.insert_blocked_trade(
                BlockedTradeInsert(
                    exchange=exchange,
                    market_id=market.id,
                    external_market_id=market.external_id,
                    side=side,
                    proposed_contracts=contracts,
                    proposed_price=entry_price,
                    signal_type=signal_type,
                    block_gate=result.gate or "unknown",
                    block_reason=result.reason or "",
                    mode=TradingMode(self._cfg.trading_mode.value),
                )
            )
            logger.debug(
                "Blocked [%s] %s: gate=%s reason=%s",
                exchange.value, market.external_id, result.gate, result.reason,
            )
        except Exception:
            logger.warning("Failed to write blocked_trade audit record", exc_info=True)
