from __future__ import annotations

"""
PolymarketExecutor — submits and cancels orders on the Polymarket CLOB.

Polymarket order format (via py_clob_client):
  - token_id  : ERC-1155 conditional token ID (YES or NO token)
  - price     : probability 0.01–0.99
  - size      : USDC amount to trade
  - side      : "BUY" or "SELL"
  - type      : GTC (limit) or FOK (market / fill-or-kill)

Side resolution:
  - Opening a YES position  → BUY the YES token
  - Opening a NO  position  → BUY the NO token
  - Closing a YES position  → SELL the YES token
  - Closing a NO  position  → SELL the NO token

The USDC size for a closing order equals:
    contracts × current_price  (since each contract = $1 face, price = fraction)

Minimum tick size is 0.01 (1¢); prices are rounded to the nearest cent.

Aggressive pricing for closing orders:
  - Sell YES at mid - 1¢   (slightly below mid to guarantee a taker match)
  - Sell NO  at mid - 1¢

Market (FOK) orders are used when signal_type == STOP_LOSS to guarantee
immediate execution regardless of price impact.
"""

import logging
from typing import Optional

from backend.clients.polymarket_client import PolymarketClobClient
from backend.db.database import Database
from backend.db.models import CloseReason, Market, Order, OrderType

logger = logging.getLogger(__name__)

_AGGRESSIVE_OFFSET = 0.01   # 1¢ below mid for closing limit orders
_MIN_TICK = 0.01
_MIN_SIZE_USDC = 1.0         # Polymarket CLOB minimum order size


class PolymarketExecutor:
    """
    Implements the ExchangeExecutor protocol for Polymarket.
    PolymarketClobClient must already be inside an async context manager.
    """

    def __init__(self, clob: PolymarketClobClient, db: Database) -> None:
        self._clob = clob
        self._db = db

    # ── ExchangeExecutor protocol ─────────────────────────────────────────────

    async def submit(self, order: Order) -> Optional[str]:
        """
        Submit an order to the Polymarket CLOB.
        Returns the CLOB order_id on success, None on hard rejection.
        """
        market = await self._db.get_market(order.market_id)
        if market is None:
            logger.error("PolymarketExecutor: market %s not found", order.market_id)
            return None

        token_id = _resolve_token_id(market, order.side, order.is_opening)
        if not token_id:
            logger.error(
                "PolymarketExecutor: no token_id for market %s side=%s",
                order.external_market_id, order.side,
            )
            return None

        price = _round_to_tick(float(order.requested_price))
        contracts = float(order.requested_contracts)
        size_usdc = round(contracts * price, 2)

        if size_usdc < _MIN_SIZE_USDC:
            logger.warning(
                "PolymarketExecutor: order %s too small (%.2f USDC) — skipping",
                order.id, size_usdc,
            )
            return None

        clob_side = _clob_side(order.side, order.is_opening)

        try:
            if order.order_type == OrderType.FOK:
                resp = await self._clob.place_market_order(
                    token_id=token_id,
                    amount=size_usdc,
                    side=clob_side,
                )
            else:
                # Closing orders: use aggressive price to ensure fill
                if not order.is_opening:
                    price = max(
                        _MIN_TICK,
                        _round_to_tick(price - _AGGRESSIVE_OFFSET),
                    )

                resp = await self._clob.place_limit_order(
                    token_id=token_id,
                    price=price,
                    size=size_usdc,
                    side=clob_side,
                )

            order_id = (
                resp.get("orderID")
                or resp.get("order_id")
                or resp.get("id")
            )
            if not order_id:
                logger.error(
                    "PolymarketExecutor: no order_id in response for %s: %s",
                    order.id, resp,
                )
                return None

            logger.info(
                "PolymarketExecutor: placed %s %s token=%s... %.2f USDC @ %.3f → %s",
                clob_side,
                "open" if order.is_opening else "close",
                token_id[:12],
                size_usdc,
                price,
                order_id,
            )
            return order_id

        except Exception as exc:
            # Distinguish between validation errors (don't retry) and transient errors
            err = str(exc).lower()
            if any(k in err for k in ("invalid", "rejected", "insufficient", "bad request")):
                logger.error(
                    "PolymarketExecutor: order %s hard-rejected: %s", order.id, exc
                )
                return None
            raise  # transient — OrderManager will retry

    async def cancel(self, external_order_id: str) -> bool:
        try:
            await self._clob.cancel_order(external_order_id)
            return True
        except Exception as exc:
            err = str(exc).lower()
            if "not found" in err or "does not exist" in err:
                return True  # already gone
            logger.warning(
                "PolymarketExecutor: cancel %s failed: %s",
                external_order_id, exc,
            )
            return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_token_id(
    market: Market, side: str, is_opening: bool
) -> Optional[str]:
    """
    Opening a YES position or closing a NO position → YES token.
    Opening a NO  position or closing a YES position → NO token.
    """
    want_yes = (
        (side in ("yes", "buy") and is_opening)
        or (side in ("no", "sell") and not is_opening)
    )
    return market.token_id_yes if want_yes else market.token_id_no


def _clob_side(position_side: str, is_opening: bool) -> str:
    """
    Map position side + opening/closing to CLOB BUY/SELL.
    Opening any side = BUY the corresponding token.
    Closing any side = SELL the corresponding token.
    """
    return "BUY" if is_opening else "SELL"


def _round_to_tick(price: float) -> float:
    return round(round(price / _MIN_TICK) * _MIN_TICK, 4)
