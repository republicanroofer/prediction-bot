from __future__ import annotations

"""
Position tracker — runs every 15 seconds.

Two responsibilities:

A) Order fill monitoring
   For every pending/open order, poll the exchange for current status.
   When an order fills, activate the linked position and record the fill.
   When an order has been open > ORDER_EXPIRY_MINUTES without filling,
   cancel it and mark the position failed.

B) Mark-to-market + exit logic
   For every open position, fetch the current mid-price from the exchange,
   update unrealized P&L, and trigger a close if any exit condition fires:
     - Stop-loss:  unrealized P&L < -stop_loss_pct of cost basis
     - Take-profit: unrealized P&L > +take_profit_pct of cost basis
     - Time limit: position age > max_hold_hours
     - Resolution: market is_resolved = TRUE

   Closing a live position creates a closing order (executor handles submission).
   In paper mode, the close is simulated immediately at current mid-price.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from backend.clients.kalshi_client import KalshiClient, KalshiAPIError
from backend.clients.polymarket_client import PolymarketClobClient
from backend.config.settings import TradingMode, get_settings
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
)

logger = logging.getLogger(__name__)

ORDER_EXPIRY_MINUTES = 30
PAPER_FEE_RATE = 0.0  # no fees in paper mode
KALSHI_FEE_RATE = 0.07  # 7% of profit on resolution
POLY_FEE_RATE = 0.02  # 2% of trade notional


class PositionTrackerWorker:
    def __init__(
        self,
        db: Database,
        kalshi: Optional[KalshiClient],
        clob: Optional[PolymarketClobClient],
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = db
        self._kalshi = kalshi
        self._clob = clob
        self._stop = stop_event or asyncio.Event()
        self._cfg = get_settings()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info(
            "Position tracker started (interval=%ds)",
            self._cfg.position_track_interval_s,
        )
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Position tracker error — will retry next tick")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._cfg.position_track_interval_s,
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Position tracker stopped")

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        # Run fill monitoring and MTM update concurrently
        await asyncio.gather(
            self._monitor_orders(),
            self._update_positions(),
            return_exceptions=True,
        )

    # ── A: Order fill monitoring ───────────────────────────────────────────────

    async def _monitor_orders(self) -> None:
        orders = await self._db.get_pending_orders()
        for order in orders:
            try:
                await self._check_order(order)
            except Exception:
                logger.exception("Error checking order %s", order.id)

    async def _check_order(self, order: Order) -> None:
        mode = order.mode
        now = datetime.now(timezone.utc)

        # Paper mode: opening orders are auto-filled by the scanner.
        # Only closing orders may arrive here in paper mode.
        if mode == TradingMode.PAPER:
            await self._paper_fill_order(order, now)
            return

        # Live mode: query exchange
        if order.exchange == Exchange.KALSHI:
            await self._check_kalshi_order(order, now)
        else:
            await self._check_poly_order(order, now)

    async def _paper_fill_order(self, order: Order, now: datetime) -> None:
        """Simulate immediate fill for closing orders in paper mode."""
        if not order.is_opening:
            await self._db.fill_order(
                order.id,
                filled_contracts=float(order.requested_contracts),
                avg_fill_price=float(order.requested_price),
                fees_usd=0.0,
            )
            if order.position_id:
                await self._finalize_close(order, float(order.requested_price))

    async def _check_kalshi_order(self, order: Order, now: datetime) -> None:
        if not self._kalshi or not order.external_id:
            # Not yet submitted; check for expiry
            if self._order_is_stale(order, now):
                await self._db.fail_order(order.id, "Order was never submitted to exchange")
            return

        try:
            raw = await self._kalshi.get_order(order.external_id)
        except KalshiAPIError as exc:
            if exc.status_code == 404:
                await self._db.cancel_order(order.id)
            return

        kal_order = raw.get("order", {})
        status = kal_order.get("status", "")
        filled_count = int(kal_order.get("filled_count", 0))
        remaining = int(kal_order.get("remaining_count", 0))
        avg_price_cents = kal_order.get("no_price") or kal_order.get("yes_price") or 50

        if status == "canceled":
            await self._db.cancel_order(order.id)
            return

        if filled_count > 0:
            avg_price = avg_price_cents / 100.0
            fees = filled_count * avg_price * KALSHI_FEE_RATE if not order.is_opening else 0.0
            await self._db.fill_order(order.id, float(filled_count), avg_price, fees)
            await self._db.record_fill(
                order.id, Exchange.KALSHI,
                float(filled_count), avg_price, fees,
            )
            if order.position_id:
                if order.is_opening:
                    await self._db.activate_position(order.position_id)
                    logger.info(
                        "Kalshi order %s filled: %d contracts @ %.3f",
                        order.external_id, filled_count, avg_price,
                    )
                else:
                    await self._finalize_close(order, avg_price)
            return

        if self._order_is_stale(order, now):
            try:
                await self._kalshi.cancel_order(order.external_id)
            except KalshiAPIError:
                pass
            await self._db.cancel_order(order.id)

    async def _check_poly_order(self, order: Order, now: datetime) -> None:
        if not self._clob or not order.external_id:
            if self._order_is_stale(order, now):
                await self._db.fail_order(order.id, "Order was never submitted to exchange")
            return

        try:
            raw = await self._clob.get_order(order.external_id)
        except Exception as exc:
            logger.warning("Poly get_order error: %s", exc)
            return

        status = raw.get("status", "").lower()
        filled_size = float(raw.get("size_matched", 0) or 0)
        avg_price = float(raw.get("average_price", 0) or order.requested_price)

        if status in ("cancelled", "canceled"):
            await self._db.cancel_order(order.id)
            return

        if status == "matched" or filled_size > 0:
            fees = filled_size * avg_price * POLY_FEE_RATE
            await self._db.fill_order(order.id, filled_size, avg_price, fees)
            await self._db.record_fill(
                order.id, Exchange.POLYMARKET,
                filled_size, avg_price, fees,
            )
            if order.position_id:
                if order.is_opening:
                    await self._db.activate_position(order.position_id)
                else:
                    await self._finalize_close(order, avg_price)
            return

        if self._order_is_stale(order, now):
            try:
                await self._clob.cancel_order(order.external_id)
            except Exception:
                pass
            await self._db.cancel_order(order.id)

    @staticmethod
    def _order_is_stale(order: Order, now: datetime) -> bool:
        if order.placed_at is None:
            ref = order.created_at
        else:
            ref = order.placed_at
        ref_utc = ref if ref.tzinfo else ref.replace(tzinfo=timezone.utc)
        return (now - ref_utc) > timedelta(minutes=ORDER_EXPIRY_MINUTES)

    # ── B: Mark-to-market + exit logic ────────────────────────────────────────

    async def _update_positions(self) -> None:
        positions = await self._db.get_open_positions()
        for pos in positions:
            if pos.status != PositionStatus.OPEN:
                continue  # still pending fill — don't evaluate exits yet
            try:
                await self._update_position(pos)
            except Exception:
                logger.exception("Error updating position %s", pos.id)

    async def _update_position(self, pos: Position) -> None:
        cfg = self._cfg

        # Fetch current market price
        current_price = await self._fetch_current_price(pos)
        if current_price is None:
            return

        # Compute MTM
        entry = float(pos.avg_entry_price)
        contracts = float(pos.contracts)

        if pos.side in ("yes", "buy"):
            unrealized = (current_price - entry) * contracts
        else:
            # NO side: we profit when YES price drops (NO price rises)
            # entry was the NO price; current_price should be the NO price too
            # (caller fetches NO mid for NO positions)
            unrealized = (current_price - entry) * contracts

        market_value = current_price * contracts
        await self._db.update_position_mtm(
            pos.id, current_price, unrealized, market_value
        )

        # ── Exit checks ────────────────────────────────────────────────────────
        # Rebuild position with updated P&L for accurate checks
        updated_pos = pos.model_copy(
            update={
                "current_price": current_price,  # type: ignore[arg-type]
                "unrealized_pnl": unrealized,     # type: ignore[arg-type]
                "market_value_usd": market_value, # type: ignore[arg-type]
            }
        )

        # Check market resolution first (best exit)
        market = await self._db.get_market(pos.market_id)
        if market and market.is_resolved:
            resolution_price = _resolution_price(market.resolution, pos.side)
            await self._close_position(
                pos, resolution_price, CloseReason.RESOLVED
            )
            return

        # Time limit
        if updated_pos.is_past_time_limit():
            await self._close_position(pos, current_price, CloseReason.TIME_LIMIT)
            return

        # Stop-loss
        if updated_pos.should_stop_loss(cfg.stop_loss_pct):
            logger.info(
                "STOP LOSS triggered: %s %s P&L=%.1f%%",
                pos.exchange.value, pos.external_market_id,
                (updated_pos.unrealized_pnl_pct() or 0) * 100,
            )
            await self._close_position(pos, current_price, CloseReason.STOP_LOSS)
            return

        # Take-profit
        if updated_pos.should_take_profit(cfg.take_profit_pct):
            logger.info(
                "TAKE PROFIT triggered: %s %s P&L=+%.1f%%",
                pos.exchange.value, pos.external_market_id,
                (updated_pos.unrealized_pnl_pct() or 0) * 100,
            )
            await self._close_position(pos, current_price, CloseReason.TAKE_PROFIT)
            return

    # ── Price fetching ─────────────────────────────────────────────────────────

    async def _fetch_current_price(self, pos: Position) -> Optional[float]:
        try:
            if pos.exchange == Exchange.KALSHI:
                return await self._kalshi_price(pos)
            else:
                return await self._poly_price(pos)
        except Exception as exc:
            logger.warning(
                "Price fetch failed for %s: %s", pos.external_market_id, exc
            )
            return None

    async def _kalshi_price(self, pos: Position) -> Optional[float]:
        if not self._kalshi:
            # Paper fallback: use stored current_price or entry price
            return float(pos.current_price or pos.avg_entry_price)

        book = await self._kalshi.get_orderbook(pos.external_market_id, depth=1)
        ob = book.get("orderbook", {})

        if pos.side == "yes":
            bids = ob.get("yes", [])
            asks = ob.get("no", [])  # Kalshi orderbook structure
            best_bid = bids[0][0] / 100 if bids else None
            best_ask = (100 - asks[0][0]) / 100 if asks else None
        else:
            bids = ob.get("no", [])
            best_bid = bids[0][0] / 100 if bids else None
            best_ask = best_bid  # approximate

        if best_bid and best_ask:
            return (best_bid + best_ask) / 2
        if best_bid:
            return best_bid
        return float(pos.current_price or pos.avg_entry_price)

    async def _poly_price(self, pos: Position) -> Optional[float]:
        if not self._clob:
            return float(pos.current_price or pos.avg_entry_price)

        market = await self._db.get_market(pos.market_id)
        if not market:
            return None

        token_id = (
            market.token_id_yes
            if pos.side in ("yes", "buy")
            else market.token_id_no
        )
        if not token_id:
            return None

        return await self._clob.get_orderbook_mid(token_id)

    # ── Position closing ───────────────────────────────────────────────────────

    async def _close_position(
        self,
        pos: Position,
        exit_price: float,
        reason: CloseReason,
    ) -> None:
        mode = pos.mode

        if mode == TradingMode.PAPER:
            # Simulate close immediately at exit_price
            await self._execute_paper_close(pos, exit_price, reason)
        else:
            # Live: create closing order record (executor submits to exchange)
            await self._queue_live_close(pos, exit_price, reason)

    async def _execute_paper_close(
        self,
        pos: Position,
        exit_price: float,
        reason: CloseReason,
    ) -> None:
        entry = float(pos.avg_entry_price)
        contracts = float(pos.contracts)

        if pos.side in ("yes", "buy"):
            realized = (exit_price - entry) * contracts
        else:
            realized = (exit_price - entry) * contracts

        await self._db.close_position(pos.id, realized, reason.value)

        logger.info(
            "[PAPER] Closed %s %s %s @ %.3f → P&L $%.2f (%s)",
            pos.exchange.value,
            pos.external_market_id,
            pos.side,
            exit_price,
            realized,
            reason.value,
        )

    async def _queue_live_close(
        self,
        pos: Position,
        exit_price: float,
        reason: CloseReason,
    ) -> None:
        # Determine closing side (opposite of opening side)
        close_side = "no" if pos.side == "yes" else "yes"
        if pos.side in ("buy", "sell"):
            close_side = "sell" if pos.side == "buy" else "buy"

        order_id = await self._db.create_order(
            OrderCreate(
                position_id=pos.id,
                exchange=pos.exchange,
                market_id=pos.market_id,
                external_market_id=pos.external_market_id,
                side=close_side,
                order_type=OrderType.LIMIT,
                mode=TradingMode.LIVE,
                is_opening=False,
                requested_contracts=float(pos.contracts),
                requested_price=exit_price,
            )
        )

        logger.info(
            "[LIVE] Queued close order %s for position %s (%s) @ %.3f — %s",
            order_id, pos.id, pos.external_market_id, exit_price, reason.value
        )
        # TODO: executor picks up and submits this order

    async def _finalize_close(self, order: Order, fill_price: float) -> None:
        """Called when a closing order has been filled."""
        if not order.position_id:
            return
        pos = await self._db.get_position(order.position_id)
        if not pos:
            return

        entry = float(pos.avg_entry_price)
        contracts = float(order.filled_contracts)

        if pos.side in ("yes", "buy"):
            realized = (fill_price - entry) * contracts
        else:
            realized = (fill_price - entry) * contracts

        # Subtract fees
        fees = float(order.fees_paid_usd or 0)
        realized -= fees

        # Determine close reason from position state
        reason = CloseReason.MANUAL
        if pos.stop_loss_price and fill_price <= float(pos.stop_loss_price):
            reason = CloseReason.STOP_LOSS
        elif pos.take_profit_price and fill_price >= float(pos.take_profit_price):
            reason = CloseReason.TAKE_PROFIT

        await self._db.close_position(pos.id, realized, reason.value)

        logger.info(
            "[LIVE] Position %s closed: %s %s @ %.3f → P&L $%.2f",
            pos.id, pos.exchange.value, pos.external_market_id, fill_price, realized,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolution_price(resolution: Optional[str], side: str) -> float:
    """
    Return the settlement price for a position given market resolution.
    Kalshi/Polymarket: YES resolution → YES = $1.00, NO = $0.00
    """
    if not resolution:
        return 0.5  # unknown — use mid as fallback
    res = resolution.strip().lower()
    if side in ("yes", "buy"):
        return 1.0 if res in ("yes", "true", "1") else 0.0
    else:
        # NO side
        return 1.0 if res in ("no", "false", "0") else 0.0
