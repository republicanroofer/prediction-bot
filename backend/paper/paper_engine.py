from __future__ import annotations

"""
PaperEngine — simulated fill engine for paper-trading mode.

In paper mode the real exchange clients are never called.  Instead, this
engine:
  1. Immediately fills opening orders at the requested price.
  2. Tracks open paper positions in DB (same tables as live).
  3. Handles closing orders (stop-loss / take-profit / resolution) the same
     way — instant fill at current mid price.

Paper fills record a zero-fee rate and do not go through OrderManager's
exchange executor path.  The scanner and position_tracker check
`cfg.trading_mode == TradingMode.PAPER` and call PaperEngine directly.

Portfolio P&L is recomputed each tick from current mid prices, stored in
daily_pnl with mode='paper' for dashboard display.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from backend.config.settings import get_settings
from backend.db.database import Database
from backend.db.models import (
    CloseReason,
    Exchange,
    Order,
    OrderCreate,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TradingMode,
)

logger = logging.getLogger(__name__)


class PaperEngine:
    """
    Simulates order fills and position lifecycle without touching real exchanges.
    One instance is shared across scanner + position_tracker.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cfg = get_settings()

    # ── Opening fills ─────────────────────────────────────────────────────────

    async def fill_opening_order(self, order_id: UUID) -> bool:
        """
        Immediately fill an opening paper order at its requested price.
        Updates order → filled, position → open.
        Returns True on success.
        """
        order = await self._db.get_order(order_id)
        if order is None:
            logger.warning("PaperEngine: order %s not found", order_id)
            return False

        if order.status not in (OrderStatus.PENDING, OrderStatus.OPEN):
            return False

        price = float(order.requested_price)
        contracts = float(order.requested_contracts)

        await self._db.record_fill(
            order_id=order_id,
            fill_price=price,
            fill_contracts=contracts,
            fee=0.0,
        )
        await self._db.set_order_filled(order_id, price, contracts)

        if order.position_id:
            await self._db.open_position(
                order.position_id,
                fill_price=price,
                fill_contracts=contracts,
            )
            logger.info(
                "PaperEngine: opened position %s via order %s @ %.3f × %.2f",
                order.position_id, order_id, price, contracts,
            )

        return True

    # ── Closing fills ─────────────────────────────────────────────────────────

    async def fill_closing_order(
        self,
        order_id: UUID,
        fill_price: float,
    ) -> bool:
        """
        Fill a closing order at the given price (typically current mid).
        Finalises the position: computes realized P&L and marks it closed.
        """
        order = await self._db.get_order(order_id)
        if order is None:
            logger.warning("PaperEngine: closing order %s not found", order_id)
            return False

        contracts = float(order.requested_contracts)

        await self._db.record_fill(
            order_id=order_id,
            fill_price=fill_price,
            fill_contracts=contracts,
            fee=0.0,
        )
        await self._db.set_order_filled(order_id, fill_price, contracts)

        if order.position_id:
            position = await self._db.get_position(order.position_id)
            if position:
                pnl = _compute_pnl(position, fill_price, contracts)
                await self._db.close_position(
                    order.position_id,
                    close_price=fill_price,
                    realized_pnl=pnl,
                )
                logger.info(
                    "PaperEngine: closed position %s @ %.3f  P&L=%.2f",
                    order.position_id, fill_price, pnl,
                )

        return True

    # ── P&L snapshot ─────────────────────────────────────────────────────────

    async def snapshot_daily_pnl(
        self,
        exchange: Exchange,
        realized_pnl: float,
        unrealized_pnl: float,
        num_positions: int,
        num_wins: int,
    ) -> None:
        """Write or update today's paper P&L summary row."""
        today = datetime.now(timezone.utc).date()
        await self._db.upsert_daily_pnl(
            date=today,
            exchange=exchange,
            mode=TradingMode.PAPER,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            num_positions=num_positions,
            num_wins=num_wins,
            num_losses=num_positions - num_wins,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_pnl(position: Position, close_price: float, contracts: float) -> float:
    """
    P&L for a binary prediction market position:
      YES long: (close - entry) * contracts
      NO  long: (entry - close) * contracts  ← NO mid moves inversely
    """
    entry = float(position.avg_entry_price or 0)
    if position.side.lower() == "yes":
        return (close_price - entry) * contracts
    else:
        return (entry - close_price) * contracts
