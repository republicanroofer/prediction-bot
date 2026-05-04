from __future__ import annotations

"""
Whale ingester — runs every 30 seconds.

Scrapes on-chain Polymarket order-fill events from the Goldsky GraphQL
subgraph, resolves them to DB market records, scores the maker address
against the whale leaderboard, and queues high-confidence trades for
the scanner to mirror after the configured delay.

Sticky-cursor pagination pattern (from poly_data):
  - Normal mode: query timestamp_gte = last_known_ts, id_gt = None
  - Sticky mode: when a full page all share the same timestamp, lock that
    timestamp and paginate by id_gt to avoid missing events.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx

from backend.clients.polymarket_client import PLATFORM_WALLETS
from backend.config.settings import get_settings
from backend.db.database import Database
from backend.db.models import Exchange, WhaleTradeInsert

logger = logging.getLogger(__name__)

_GOLDSKY_QUERY = """
query GetFills($ts_gte: BigInt!, $id_gt: String, $first: Int!) {
  orderFilledEvents(
    first: $first
    orderBy: timestamp
    orderDirection: asc
    where: {
      timestamp_gte: $ts_gte
      %s
    }
  ) {
    id
    timestamp
    maker
    makerAssetId
    makerAmountFilled
    taker
    takerAssetId
    takerAmountFilled
    transactionHash
  }
}
"""

_USDC_ASSET_ID = "0"


class WhaleIngesterWorker:
    def __init__(
        self,
        db: Database,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = db
        self._stop = stop_event or asyncio.Event()
        self._cfg = get_settings()
        # In-memory cursor state; falls back to DB on startup
        self._last_timestamp: Optional[int] = None
        self._sticky_ts: Optional[int] = None
        self._sticky_last_id: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info(
            "Whale ingester started (interval=%ds)", self._cfg.whale_ingest_interval_s
        )
        # Bootstrap cursor from DB
        self._last_timestamp = await self._db.get_latest_whale_trade_timestamp()
        if self._last_timestamp:
            logger.info("Resuming from timestamp %d", self._last_timestamp)

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
        start_ts = self._last_timestamp or 0
        page_size = self._cfg.goldsky_page_size
        new_trades = 0

        while True:
            events = await self._fetch_page(start_ts, page_size)
            if not events:
                break

            inserted = await self._process_events(events)
            new_trades += inserted

            # Update cursor
            last_event = events[-1]
            last_ts = int(last_event["timestamp"])
            last_id = last_event["id"]

            if len(events) == page_size:
                # Check if all events share the same timestamp (sticky mode)
                all_same_ts = all(int(e["timestamp"]) == last_ts for e in events)
                if all_same_ts:
                    # Lock timestamp and paginate by id to avoid missing events
                    self._sticky_ts = last_ts
                    self._sticky_last_id = last_id
                    start_ts = last_ts  # same timestamp, id_gt advances
                    logger.debug("Sticky cursor activated at ts=%d id=%s", last_ts, last_id)
                    continue
                else:
                    # Normal advance
                    self._sticky_ts = None
                    self._sticky_last_id = None
                    self._last_timestamp = last_ts
                    start_ts = last_ts
            else:
                # Partial page = caught up
                self._sticky_ts = None
                self._sticky_last_id = None
                self._last_timestamp = last_ts
                break

        if new_trades:
            logger.info("Whale ingester: inserted %d new trades", new_trades)

    async def _fetch_page(
        self, ts_gte: int, first: int
    ) -> list[dict]:
        id_filter = ""
        variables: dict = {"ts_gte": str(ts_gte), "first": first}

        if self._sticky_ts is not None and self._sticky_last_id is not None:
            id_filter = "id_gt: $id_gt"
            variables["id_gt"] = self._sticky_last_id
            query = _GOLDSKY_QUERY % id_filter
        else:
            query = _GOLDSKY_QUERY % ""

        for attempt in range(5):
            try:
                resp = await self._http.post(
                    self._cfg.goldsky_endpoint,
                    json={"query": query, "variables": variables},
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    logger.warning("Goldsky GraphQL errors: %s", data["errors"])
                    return []
                return data.get("data", {}).get("orderFilledEvents", [])
            except httpx.RequestError as exc:
                if attempt == 4:
                    logger.error("Goldsky request failed: %s", exc)
                    return []
                await asyncio.sleep(2 ** attempt)

        return []

    # ── Event processing ──────────────────────────────────────────────────────

    async def _process_events(self, events: list[dict]) -> int:
        inserted = 0
        now = datetime.now(timezone.utc)

        for event in events:
            try:
                result = await self._process_single(event, now)
                if result:
                    inserted += 1
            except Exception:
                logger.exception("Failed to process event %s", event.get("id"))

        return inserted

    async def _process_single(self, event: dict, now: datetime) -> bool:
        tx_hash = event.get("transactionHash", "")
        maker = (event.get("maker") or "").lower()
        taker = (event.get("taker") or "").lower()

        if not tx_hash or not maker:
            return False

        ts = int(event.get("timestamp", 0))
        block_time = datetime.fromtimestamp(ts, tz=timezone.utc)

        # Determine which side is USDC and which is the prediction token
        maker_asset = event.get("makerAssetId", "")
        taker_asset = event.get("takerAssetId", "")
        maker_amount = int(event.get("makerAmountFilled", 0))
        taker_amount = int(event.get("takerAmountFilled", 0))

        if maker_asset == _USDC_ASSET_ID:
            # Maker paid USDC, received tokens → maker is BUY
            usdc_amount = maker_amount
            token_amount = taker_amount
            token_id = taker_asset
            maker_direction = "buy"
            taker_direction = "sell"
        elif taker_asset == _USDC_ASSET_ID:
            # Taker paid USDC, received tokens → maker is SELL
            usdc_amount = taker_amount
            token_amount = maker_amount
            token_id = maker_asset
            maker_direction = "sell"
            taker_direction = "buy"
        else:
            # Token-to-token swap — skip
            return False

        usd_amount = usdc_amount / 1_000_000
        token_qty = token_amount / 1_000_000

        if token_qty <= 0:
            return False

        price = usd_amount / token_qty

        is_platform = (
            maker in PLATFORM_WALLETS or taker in PLATFORM_WALLETS
        )

        # Try to resolve condition_id from token_id
        market_id, condition_id = await self._resolve_market(token_id)

        # Decide whether to queue for mirroring
        mirror_queued_at: Optional[datetime] = None
        if not is_platform and usd_amount >= self._cfg.whale_min_trade_usd:
            whale_score = await self._db.get_whale_score(maker)
            if whale_score and float(whale_score.composite_score or 0) >= self._cfg.whale_min_score:
                mirror_queued_at = now
                logger.info(
                    "Mirror queued: %s %s $%.0f @ %.3f (score=%.0f)",
                    maker_direction,
                    token_id[:12],
                    usd_amount,
                    price,
                    float(whale_score.composite_score or 0),
                )

        trade_id = await self._db.insert_whale_trade(
            WhaleTradeInsert(
                tx_hash=tx_hash,
                block_timestamp=block_time,
                maker_address=maker,
                taker_address=taker,
                market_id=market_id,
                condition_id=condition_id,
                token_id=token_id,
                maker_direction=maker_direction,
                taker_direction=taker_direction,
                price=round(price, 6),
                usd_amount=round(usd_amount, 2),
                token_amount=round(token_qty, 6),
                is_platform_tx=is_platform,
                mirror_queued_at=mirror_queued_at,
            )
        )

        return trade_id is not None  # None = duplicate

    async def _resolve_market(
        self, token_id: str
    ) -> tuple[Optional[UUID], Optional[str]]:
        """
        Attempt to find the condition_id and market UUID for a given token_id.
        Polymarket stores token_id_yes / token_id_no in the markets table.
        """
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


# ── Whale scorer (runs hourly, called from orchestrator) ──────────────────────

class WhaleScorer:
    """
    Re-ranks the whale leaderboard from accumulated whale_trades data.
    Uses the big-win-rate ranking from poly_data:
      - Only include addresses with >= 300 markets traded (configurable)
      - Score = 0.5 * big_win_rate + 0.3 * median_gain_pct + 0.2 * win_rate
    """

    MIN_MARKETS = 50  # lowered from 300 for bootstrap; raise as data accumulates

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run_once(self) -> None:
        logger.info("Whale scorer running")
        async with self._db._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    maker_address                                          AS address,
                    COUNT(DISTINCT condition_id)                          AS markets_traded,
                    SUM(usd_amount)                                       AS total_volume,
                    MAX(block_timestamp)                                  AS last_trade_at,
                    -- approximate PnL: buys cost USD, sells return USD
                    SUM(CASE WHEN maker_direction='sell' THEN usd_amount ELSE -usd_amount END)
                                                                          AS approx_pnl
                FROM whale_trades
                WHERE is_platform_tx = FALSE
                GROUP BY maker_address
                HAVING COUNT(DISTINCT condition_id) >= $1
                """,
                self.MIN_MARKETS,
            )

        for row in rows:
            address = row["address"]
            markets = int(row["markets_traded"] or 0)
            volume = float(row["total_volume"] or 0)
            approx_pnl = float(row["approx_pnl"] or 0)
            last_trade = row["last_trade_at"]

            # Derive synthetic scores from available data
            win_rate = min(0.65, max(0.35, 0.50 + approx_pnl / max(volume, 1) * 0.5))
            big_win_rate = win_rate * 0.6  # conservative estimate without per-trade resolution
            median_gain = approx_pnl / max(markets, 1) / max(volume / max(markets, 1), 1)
            median_gain = max(-0.5, min(0.5, median_gain))

            composite = (
                0.50 * big_win_rate
                + 0.30 * max(0, median_gain)
                + 0.20 * win_rate
            ) * 100  # scale to 0–100

            await self._db.upsert_whale_score(
                address,
                {
                    "total_pnl_usd": approx_pnl,
                    "win_rate": round(win_rate, 4),
                    "big_win_rate": round(big_win_rate, 4),
                    "median_gain_pct": round(median_gain, 4),
                    "median_loss_pct": round(-abs(median_gain) * 0.5, 4),
                    "markets_traded": markets,
                    "total_volume_usd": volume,
                    "composite_score": round(composite, 2),
                    "is_active": True,
                    "last_trade_at": last_trade,
                },
            )

        logger.info("Whale scorer updated %d addresses", len(rows))
