from __future__ import annotations

"""
Whale ingester — polls Polymarket public Data API for large recent trades.

Replaces the Goldsky GraphQL ingester (which required a private API key).
The Polymarket Data API is fully public and requires no authentication.

On each tick (every 30 seconds):
  1. Fetch recent trades from data-api.polymarket.com/trades, paginated
     by timestamp using the `after` cursor.
  2. Compute USD value (price × size) and discard trades below
     whale_min_trade_usd.
  3. For qualifying trades, check if the maker has a whale score >=
     whale_min_score and, if so, queue the trade for mirroring.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx

from backend.clients.polymarket_client import PLATFORM_WALLETS
from backend.config.settings import get_settings
from backend.db.database import Database
from backend.db.models import WhaleTradeInsert

logger = logging.getLogger(__name__)

_DATA_API_BASE = "https://data-api.polymarket.com"
_LOOKBACK_MINUTES = 35   # initial lookback on first run
_PAGE_SIZE = 500         # max records per API call


class WhaleIngesterWorker:
    def __init__(
        self,
        db: Database,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = db
        self._stop = stop_event or asyncio.Event()
        self._cfg = get_settings()
        self._cursor: Optional[datetime] = None  # newest trade timestamp seen

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info(
            "Whale ingester started (Polymarket Data API, interval=%ds)",
            self._cfg.whale_ingest_interval_s,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as http:
            self._http = http
            while not self._stop.is_set():
                try:
                    await self._ingest()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Whale ingester error — will retry next tick")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._cfg.whale_ingest_interval_s,
                    )
                except asyncio.TimeoutError:
                    pass

        logger.info("Whale ingester stopped")

    # ── Main ingest ───────────────────────────────────────────────────────────

    async def _ingest(self) -> None:
        now = datetime.now(timezone.utc)

        if self._cursor:
            # Small overlap so we don't miss trades at the boundary
            after = self._cursor - timedelta(minutes=2)
        else:
            after = now - timedelta(minutes=_LOOKBACK_MINUTES)

        trades = await self._fetch_trades(after)
        if not trades:
            return

        inserted = 0
        newest: Optional[datetime] = self._cursor
        for trade in trades:
            try:
                stored = await self._process_trade(trade, now)
                if stored:
                    inserted += 1
            except Exception:
                logger.exception("Failed to process trade %s", trade.get("transactionHash"))

            # Track newest timestamp for cursor advance (field is Unix int)
            ts_raw = trade.get("timestamp")
            if ts_raw:
                try:
                    ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
                    if newest is None or ts > newest:
                        newest = ts
                except (ValueError, TypeError):
                    pass

        if newest:
            self._cursor = newest

        if inserted:
            logger.info("Whale ingester: stored %d new large trades", inserted)

    # ── API fetch ─────────────────────────────────────────────────────────────

    async def _fetch_trades(self, after: datetime) -> list[dict]:
        params = {
            "limit": _PAGE_SIZE,
            "after": after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for attempt in range(3):
            try:
                resp = await self._http.get(f"{_DATA_API_BASE}/trades", params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(5)
                    continue
                if resp.status_code == 200:
                    return resp.json() or []
                logger.warning("Polymarket trades API returned %d", resp.status_code)
                return []
            except (httpx.RequestError, ValueError) as exc:
                if attempt == 2:
                    logger.warning("Polymarket trades API request failed: %s", exc)
                    return []
                await asyncio.sleep(2 ** attempt)
        return []

    # ── Trade processing ──────────────────────────────────────────────────────

    async def _process_trade(self, trade: dict, now: datetime) -> bool:
        # Polymarket Data API field names differ from internal names
        tx_hash = trade.get("transactionHash") or trade.get("id") or ""
        maker = (trade.get("proxyWallet") or "").lower()

        if not tx_hash or not maker:
            return False

        try:
            price = float(trade.get("price") or 0)
            size = float(trade.get("size") or 0)
        except (TypeError, ValueError):
            return False

        usd_amount = price * size
        if usd_amount < self._cfg.whale_min_trade_usd:
            return False

        # Timestamp is a Unix int
        ts_raw = trade.get("timestamp")
        try:
            block_time = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc) if ts_raw else now
        except (ValueError, TypeError):
            block_time = now

        # Resolve market — conditionId is the Polymarket condition ID
        condition_id = trade.get("conditionId") or ""
        token_id = trade.get("asset") or ""
        market_id: Optional[UUID] = None

        if condition_id:
            market = await self._db.get_market_by_condition(condition_id)
            if market:
                market_id = market.id
        elif token_id:
            market_id, condition_id = await self._resolve_market_by_token(token_id)

        # Direction
        side_raw = (trade.get("side") or "").upper()
        maker_direction = "buy" if side_raw == "BUY" else "sell"
        taker_direction = "sell" if maker_direction == "buy" else "buy"

        is_platform = maker in PLATFORM_WALLETS

        # Queue for mirroring if whale-scored
        mirror_queued_at: Optional[datetime] = None
        if not is_platform:
            whale_score = await self._db.get_whale_score(maker)
            if whale_score and float(whale_score.composite_score or 0) >= self._cfg.whale_min_score:
                mirror_queued_at = now
                logger.info(
                    "Mirror queued: %s %s $%.0f @ %.3f (score=%.0f)",
                    maker_direction,
                    (condition_id or token_id)[:16],
                    usd_amount,
                    price,
                    float(whale_score.composite_score or 0),
                )

        trade_id = await self._db.insert_whale_trade(
            WhaleTradeInsert(
                tx_hash=tx_hash,
                block_timestamp=block_time,
                maker_address=maker,
                taker_address="",
                market_id=market_id,
                condition_id=condition_id or None,
                token_id=token_id or None,
                maker_direction=maker_direction,
                taker_direction=taker_direction,
                price=round(price, 6),
                usd_amount=round(usd_amount, 2),
                token_amount=round(size, 6),
                is_platform_tx=is_platform,
                mirror_queued_at=mirror_queued_at,
            )
        )

        return trade_id is not None

    async def _resolve_market_by_token(
        self, token_id: str
    ) -> tuple[Optional[UUID], Optional[str]]:
        if not token_id:
            return None, None
        async with self._db._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, external_id FROM markets
                WHERE (token_id_yes = $1 OR token_id_no = $1)
                  AND exchange = 'polymarket'
                LIMIT 1
                """,
                token_id,
            )
            if row:
                return row["id"], row["external_id"]
        return None, None
