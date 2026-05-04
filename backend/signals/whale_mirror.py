from __future__ import annotations

"""
WhaleMirrorSignal — generates trade signals by mirroring top whale traders.

Workflow:
  1. WhaleIngester queues mirror candidates in whale_trades (mirror_queued_at set).
  2. Database.get_queued_mirror_trades() returns trades where:
       - delay has elapsed (now - mirror_queued_at >= mirror_delay_s)
       - whale score >= mirror_min_score
       - not yet mirrored (mirrored_at IS NULL)
  3. WhaleMirrorSignal.get_signals() fetches the queue, resolves each trade to
     a MirrorSignal, and marks each whale_trade.mirrored_at = now.

The scanner calls get_signals() every tick and opens positions for any
returned signals that pass the portfolio enforcer.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from backend.config.settings import get_settings
from backend.db.database import Database
from backend.db.models import WhaleTrade

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MirrorSignal:
    whale_trade_id: UUID
    market_id: UUID
    external_market_id: str
    side: str           # 'yes' or 'no'
    size_usd: float
    price: float        # entry price at time of whale trade
    whale_address: str
    whale_score: float
    confidence: float   # derived from whale score (0–1)


class WhaleMirrorSignal:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cfg = get_settings()

    async def get_signals(self) -> list[MirrorSignal]:
        """
        Return all pending mirror signals and mark them as mirrored in DB.
        Filters out trades below min_trade_usd and min_score thresholds.
        """
        trades = await self._db.get_queued_mirror_trades(
            delay_s=self._cfg.mirror_delay_s,
            min_score=self._cfg.mirror_min_score,
        )

        signals: list[MirrorSignal] = []
        for trade in trades:
            sig = _trade_to_signal(trade, self._cfg.mirror_min_trade_usd)
            if sig is not None:
                signals.append(sig)
                await self._db.mark_whale_trade_mirrored(trade.id)

        if signals:
            logger.info("WhaleMirror: %d mirror signals queued", len(signals))

        return signals


def _trade_to_signal(
    trade: WhaleTrade,
    min_trade_usd: float,
) -> Optional[MirrorSignal]:
    if trade.market_id is None:
        return None

    size_usd = float(trade.size_usd or 0)
    if size_usd < min_trade_usd:
        return None

    price = float(trade.price or 0)
    if not (0.01 <= price <= 0.99):
        return None

    whale_score = float(getattr(trade, "whale_score", 0) or 0)
    # Scale confidence: score 60→0.50, score 100→0.90 (linear interpolation)
    confidence = min(0.90, 0.50 + (whale_score - 60) * (0.40 / 40))
    confidence = max(0.30, confidence)

    return MirrorSignal(
        whale_trade_id=trade.id,
        market_id=trade.market_id,
        external_market_id=trade.external_market_id or "",
        side=trade.side.lower(),
        size_usd=size_usd,
        price=price,
        whale_address=trade.maker_address,
        whale_score=whale_score,
        confidence=round(confidence, 4),
    )
