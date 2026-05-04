from __future__ import annotations

"""
KalshiExecutor — submits and cancels orders on Kalshi.

Kalshi order format:
  - ticker      : market ticker (e.g. "KXBTC-25DEC-T100000")
  - side        : "yes" or "no"
  - action      : "buy" or "sell"
  - count       : integer number of contracts (1 contract = $1 max payout)
  - type        : "limit" or "market"
  - yes_price   : limit price in cents (1–99) for YES side
  - no_price    : limit price in cents (1–99) for NO side

Position side mapping:
  - Opening a YES position → action="buy",  side="yes", yes_price=...
  - Opening a NO  position → action="buy",  side="no",  no_price=...
  - Closing a YES position → action="sell", side="yes", yes_price=...
  - Closing a NO  position → action="sell", side="no",  no_price=...

Limit price strategy (passive-aggressive):
  - For an opening limit order, use the mid-price rounded to the nearest cent.
    This is inside the spread so it may not fill immediately, but avoids
    paying the full ask.  A 30-minute TTL is set; the position tracker
    cancels it if unfilled by then.
  - For closing orders (stop-loss, take-profit), use a market-aggressive
    price (ask for buys, bid for sells) to guarantee execution.
"""

import logging
import uuid
from typing import Optional

from backend.clients.kalshi_client import KalshiAPIError, KalshiClient
from backend.db.models import Order, OrderType

logger = logging.getLogger(__name__)

_AGGRESSIVE_OFFSET_CENTS = 2  # cross the spread by 2¢ on closing orders


class KalshiExecutor:
    """
    Implements the ExchangeExecutor protocol for Kalshi.
    KalshiClient must already be inside an async context manager when this
    executor is used.
    """

    def __init__(self, client: KalshiClient) -> None:
        self._client = client

    # ── ExchangeExecutor protocol ─────────────────────────────────────────────

    async def submit(self, order: Order) -> Optional[str]:
        """
        Submit an order to Kalshi.
        Returns the Kalshi order_id string on success, None on hard failure.
        """
        try:
            ticker = order.external_market_id
            price_f = float(order.requested_price)
            contracts = int(round(float(order.requested_contracts)))

            if contracts < 1:
                logger.warning(
                    "KalshiExecutor: order %s has < 1 contract — skipping", order.id
                )
                return None

            side, action, yes_price, no_price = _build_kalshi_params(
                position_side=order.side,
                is_opening=order.is_opening,
                price_f=price_f,
                order_type=order.order_type,
            )

            client_order_id = f"pb-{order.id}"

            resp = await self._client.place_order(
                ticker=ticker,
                side=side,
                action=action,
                count=contracts,
                order_type=order.order_type.value,
                yes_price=yes_price,
                no_price=no_price,
                client_order_id=client_order_id,
            )

            external_id = (
                resp.get("order", {}).get("order_id")
                or resp.get("order_id")
            )
            if not external_id:
                logger.error(
                    "KalshiExecutor: no order_id in response for %s: %s",
                    order.id, resp,
                )
                return None

            logger.info(
                "KalshiExecutor: placed %s %s %s %d contracts @ %d¢ → %s",
                action, side, ticker, contracts,
                yes_price or no_price or 0, external_id,
            )
            return external_id

        except KalshiAPIError as exc:
            if exc.status_code in (400, 422):
                # Validation error — do not retry
                logger.error(
                    "KalshiExecutor: order %s rejected (HTTP %d): %s",
                    order.id, exc.status_code, exc,
                )
                return None
            raise  # 429 / 5xx → OrderManager will retry

    async def cancel(self, external_order_id: str) -> bool:
        try:
            await self._client.cancel_order(external_order_id)
            return True
        except KalshiAPIError as exc:
            if exc.status_code == 404:
                return True  # already gone
            logger.warning(
                "KalshiExecutor: cancel %s failed: %s", external_order_id, exc
            )
            return False

    # ── Price helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def to_cents(price_f: float) -> int:
        """Convert probability (0–1) to Kalshi cents (1–99), clamped."""
        return max(1, min(99, round(price_f * 100)))

    @staticmethod
    def aggressive_close_price(
        side: str, current_price_f: float
    ) -> int:
        """
        For closing orders, cross the spread slightly to ensure execution.
        YES close (sell YES): price slightly below best bid.
        NO close  (sell NO):  price slightly below best NO bid.
        """
        cents = KalshiExecutor.to_cents(current_price_f)
        if side in ("yes",):
            return max(1, cents - _AGGRESSIVE_OFFSET_CENTS)
        else:
            return max(1, cents - _AGGRESSIVE_OFFSET_CENTS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_kalshi_params(
    position_side: str,  # 'yes' or 'no'
    is_opening: bool,
    price_f: float,
    order_type: OrderType,
) -> tuple[str, str, Optional[int], Optional[int]]:
    """
    Return (side, action, yes_price_cents, no_price_cents).
    Opening = buy the side.  Closing = sell the side.
    """
    side = position_side.lower()
    action = "buy" if is_opening else "sell"

    if order_type == OrderType.MARKET:
        return side, action, None, None

    cents = max(1, min(99, round(price_f * 100)))

    if side == "yes":
        return side, action, cents, None
    else:
        return side, action, None, cents
