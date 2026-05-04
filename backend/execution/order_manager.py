from __future__ import annotations

"""
OrderManager — central dispatcher for order submission and cancellation.

The scanner creates Order records in the DB with status='pending'.
The OrderManager picks them up and routes to the correct exchange executor.
The PositionTracker handles post-submission monitoring (fill polling, MTM).

Responsibilities:
  - submit_pending()     : submit all pending orders to their exchange
  - submit_order()       : submit a single order by DB id
  - cancel_order()       : cancel on exchange + update DB
  - cancel_all_for_position() : cancel all open orders for a position

The executors (KalshiExecutor, PolymarketExecutor) are injected at
construction time so this module doesn't import exchange specifics.
"""

import asyncio
import logging
from typing import Optional, Protocol
from uuid import UUID

from backend.db.database import Database
from backend.db.models import Exchange, Order, OrderStatus

logger = logging.getLogger(__name__)

# Re-try a failed submission up to this many times before giving up
_MAX_SUBMIT_RETRIES = 3


class ExchangeExecutor(Protocol):
    """Interface that KalshiExecutor and PolymarketExecutor must satisfy."""

    async def submit(self, order: Order) -> Optional[str]:
        """
        Submit the order to the exchange.
        Returns the exchange-assigned order ID on success, None on failure.
        """
        ...

    async def cancel(self, external_order_id: str) -> bool:
        """Cancel an open order on the exchange. Returns True if cancelled."""
        ...


class OrderManager:
    def __init__(
        self,
        db: Database,
        kalshi_executor: Optional[ExchangeExecutor] = None,
        poly_executor: Optional[ExchangeExecutor] = None,
    ) -> None:
        self._db = db
        self._executors: dict[Exchange, Optional[ExchangeExecutor]] = {
            Exchange.KALSHI: kalshi_executor,
            Exchange.POLYMARKET: poly_executor,
        }

    # ── Bulk submission ───────────────────────────────────────────────────────

    async def submit_pending(self) -> int:
        """
        Find all pending (un-submitted) orders and send them to their exchange.
        Returns the number of successfully submitted orders.
        """
        orders = await self._db.get_pending_orders()
        pending = [o for o in orders if o.status == OrderStatus.PENDING]

        if not pending:
            return 0

        results = await asyncio.gather(
            *[self.submit_order(o.id) for o in pending],
            return_exceptions=True,
        )

        success = sum(1 for r in results if r is True)
        if success:
            logger.info("OrderManager: submitted %d / %d pending orders", success, len(pending))
        return success

    # ── Single order submission ───────────────────────────────────────────────

    async def submit_order(self, order_id: UUID) -> bool:
        """
        Submit one order to its exchange.  Updates DB with external_id on
        success; marks failed on unrecoverable error.
        Returns True if the order reached the exchange.
        """
        order = await self._db.get_order(order_id)
        if order is None:
            logger.warning("submit_order: order %s not found", order_id)
            return False

        if order.status not in (OrderStatus.PENDING, OrderStatus.FAILED):
            return False  # already submitted or terminal

        executor = self._executors.get(order.exchange)
        if executor is None:
            logger.warning(
                "submit_order: no executor for %s (mode=%s) — order %s left pending",
                order.exchange.value, order.mode.value, order_id,
            )
            return False

        for attempt in range(1, _MAX_SUBMIT_RETRIES + 1):
            try:
                external_id = await executor.submit(order)
            except Exception as exc:
                if attempt == _MAX_SUBMIT_RETRIES:
                    await self._db.fail_order(order_id, f"submit error: {exc}")
                    logger.error(
                        "submit_order: failed after %d attempts for %s: %s",
                        attempt, order_id, exc,
                    )
                    return False
                logger.warning(
                    "submit_order: attempt %d failed for %s: %s — retrying",
                    attempt, order_id, exc,
                )
                await asyncio.sleep(2 ** (attempt - 1))
                continue

            if external_id:
                await self._db.set_order_placed(order_id, external_id)
                logger.debug(
                    "submit_order: %s → external_id=%s", order_id, external_id
                )
                return True
            else:
                # Executor returned None (rejected by exchange, non-retryable)
                await self._db.fail_order(order_id, "exchange rejected order")
                return False

        return False

    # ── Cancellation ──────────────────────────────────────────────────────────

    async def cancel_order(self, order_id: UUID) -> bool:
        """
        Cancel a specific order.  Calls the exchange if the order has been
        submitted; always updates DB status to 'cancelled'.
        """
        order = await self._db.get_order(order_id)
        if order is None:
            return False

        if order.is_complete:
            return True  # already terminal

        cancelled_on_exchange = False
        if order.external_id:
            executor = self._executors.get(order.exchange)
            if executor:
                try:
                    cancelled_on_exchange = await executor.cancel(order.external_id)
                except Exception as exc:
                    logger.warning(
                        "cancel_order: exchange cancel failed for %s: %s",
                        order.external_id, exc,
                    )

        await self._db.cancel_order(order_id)
        logger.info(
            "cancel_order: %s (exchange_cancelled=%s)", order_id, cancelled_on_exchange
        )
        return True

    async def cancel_all_for_position(self, position_id: UUID) -> int:
        """Cancel all open orders linked to a position. Returns count cancelled."""
        async with self._db._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id FROM orders
                WHERE position_id = $1
                  AND status IN ('pending', 'open')
                """,
                position_id,
            )

        cancelled = 0
        for row in rows:
            if await self.cancel_order(row["id"]):
                cancelled += 1

        return cancelled

    # ── Inspection ────────────────────────────────────────────────────────────

    async def get_order(self, order_id: UUID) -> Optional[Order]:
        return await self._db.get_order(order_id)

    async def has_open_orders_for_market(self, market_id: UUID) -> bool:
        async with self._db._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM orders o
                JOIN positions p ON p.id = o.position_id
                WHERE p.market_id = $1
                  AND o.status IN ('pending','open')
                LIMIT 1
                """,
                market_id,
            )
        return row is not None
