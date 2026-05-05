from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg

from backend.config.settings import get_settings
from backend.db.models import (
    BlockedTradeInsert,
    CategoryScore,
    CloseReason,
    DailyPnL,
    Exchange,
    LLMQuery,
    LLMQueryInsert,
    Market,
    MarketUpsert,
    NewsSignal,
    NewsSignalInsert,
    Order,
    OrderCreate,
    OrderStatus,
    Position,
    PositionCreate,
    PositionStatus,
    TradingMode,
    WhaleTrade,
    WhaleScore,
    WhaleTradeInsert,
)

logger = logging.getLogger(__name__)


def _to_json(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v) if isinstance(v, dict) else v


class Database:
    """
    asyncpg-backed repository for all bot state.

    Create once at startup via Database.create(), share the instance across
    all workers, and call close() on shutdown.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str | None = None) -> "Database":
        cfg = get_settings()
        url = dsn or cfg.database_url
        pool = await asyncpg.create_pool(
            url,
            min_size=cfg.db_pool_min_size,
            max_size=cfg.db_pool_max_size,
            # Return dicts for asyncpg records automatically
            init=_set_type_codecs,
        )
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # ── Markets ───────────────────────────────────────────────────────────────

    async def upsert_market(self, data: MarketUpsert) -> UUID:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO markets (
                    exchange, external_id, event_ticker, token_id_yes, token_id_no,
                    title, category, sub_category,
                    yes_bid, yes_ask, no_bid, no_ask, last_price,
                    volume_24h_usd, volume_total_usd, open_interest, liquidity_usd,
                    close_time, is_active, raw
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20
                )
                ON CONFLICT (exchange, external_id) DO UPDATE SET
                    event_ticker    = EXCLUDED.event_ticker,
                    token_id_yes    = EXCLUDED.token_id_yes,
                    token_id_no     = EXCLUDED.token_id_no,
                    title           = EXCLUDED.title,
                    category        = COALESCE(EXCLUDED.category, markets.category),
                    sub_category    = COALESCE(EXCLUDED.sub_category, markets.sub_category),
                    yes_bid         = EXCLUDED.yes_bid,
                    yes_ask         = EXCLUDED.yes_ask,
                    no_bid          = EXCLUDED.no_bid,
                    no_ask          = EXCLUDED.no_ask,
                    last_price      = EXCLUDED.last_price,
                    volume_24h_usd  = EXCLUDED.volume_24h_usd,
                    volume_total_usd= EXCLUDED.volume_total_usd,
                    open_interest   = EXCLUDED.open_interest,
                    liquidity_usd   = EXCLUDED.liquidity_usd,
                    close_time      = EXCLUDED.close_time,
                    is_active       = EXCLUDED.is_active,
                    raw             = EXCLUDED.raw,
                    updated_at      = NOW()
                RETURNING id
                """,
                data.exchange.value,
                data.external_id,
                data.event_ticker,
                data.token_id_yes,
                data.token_id_no,
                data.title,
                data.category,
                data.sub_category,
                data.yes_bid,
                data.yes_ask,
                data.no_bid,
                data.no_ask,
                data.last_price,
                data.volume_24h_usd,
                data.volume_total_usd,
                data.open_interest,
                data.liquidity_usd,
                data.close_time,
                data.is_active,
                _to_json(data.raw),
            )
            return row["id"]

    async def get_market(self, market_id: UUID) -> Optional[Market]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM markets WHERE id = $1", market_id
            )
            return Market.model_validate(dict(row)) if row else None

    async def get_market_by_external(
        self, exchange: Exchange, external_id: str
    ) -> Optional[Market]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM markets WHERE exchange = $1 AND external_id = $2",
                exchange.value,
                external_id,
            )
            return Market.model_validate(dict(row)) if row else None

    async def get_active_markets(
        self,
        exchange: Optional[Exchange] = None,
        min_volume_usd: float = 1.0,
    ) -> list[Market]:
        async with self._pool.acquire() as conn:
            if exchange:
                rows = await conn.fetch(
                    "SELECT * FROM markets"
                    " WHERE is_active = TRUE AND is_resolved = FALSE"
                    " AND exchange = $1"
                    " AND COALESCE(volume_24h_usd, 0) >= $2"
                    " ORDER BY volume_24h_usd DESC NULLS LAST",
                    exchange.value,
                    min_volume_usd,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM markets"
                    " WHERE is_active = TRUE AND is_resolved = FALSE"
                    " AND COALESCE(volume_24h_usd, 0) >= $1"
                    " ORDER BY volume_24h_usd DESC NULLS LAST",
                    min_volume_usd,
                )
            return [Market.model_validate(dict(r)) for r in rows]

    async def get_cross_exchange_pairs(
        self, min_gap: float = 0.05, min_volume: float = 1000.0,
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT k.id AS kalshi_id, p.id AS poly_id,
                       k.title, k.category,
                       k.yes_bid AS kb, k.yes_ask AS ka,
                       p.yes_bid AS pb, p.yes_ask AS pa,
                       k.volume_24h_usd AS k_vol, p.volume_24h_usd AS p_vol,
                       k.close_time, k.external_id AS k_ext, p.external_id AS p_ext
                FROM markets k
                JOIN markets p ON LOWER(k.title) = LOWER(p.title)
                WHERE k.exchange::text = 'kalshi' AND p.exchange::text = 'polymarket'
                  AND k.is_active AND p.is_active
                  AND NOT k.is_resolved AND NOT p.is_resolved
                  AND COALESCE(k.volume_24h_usd, 0) >= $1
                  AND COALESCE(p.volume_24h_usd, 0) >= $1
                  AND ABS(
                      (COALESCE(k.yes_bid,0)+COALESCE(k.yes_ask,0))/2
                    - (COALESCE(p.yes_bid,0)+COALESCE(p.yes_ask,0))/2
                  ) >= $2
                ORDER BY ABS(
                    (COALESCE(k.yes_bid,0)+COALESCE(k.yes_ask,0))/2
                  - (COALESCE(p.yes_bid,0)+COALESCE(p.yes_ask,0))/2
                ) DESC
                LIMIT 50
                """,
                min_volume,
                min_gap,
            )
            return [dict(r) for r in rows]

    async def count_positions_opened_today(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM positions WHERE opened_at >= CURRENT_DATE"
            )

    async def get_market_by_condition(self, condition_id: str) -> Optional[Market]:
        """Look up Polymarket market by condition_id (= external_id)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM markets WHERE external_id = $1 AND exchange = 'polymarket'",
                condition_id,
            )
            return Market.model_validate(dict(row)) if row else None

    async def mark_market_resolved(
        self, market_id: UUID, resolution: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE markets
                SET is_resolved = TRUE, resolution = $2, is_active = FALSE, updated_at = NOW()
                WHERE id = $1
                """,
                market_id,
                resolution,
            )

    # ── Positions ─────────────────────────────────────────────────────────────

    async def create_position(self, data: PositionCreate) -> UUID:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO positions (
                    exchange, market_id, external_market_id, side, mode, status,
                    signal_type, contracts, avg_entry_price, cost_basis_usd,
                    stop_loss_price, take_profit_price, kelly_fraction_used,
                    whale_address, whale_score, mirror_delay_s, whale_trade_id,
                    max_hold_until
                ) VALUES (
                    $1,$2,$3,$4,$5,'pending',
                    $6,$7,$8,$9,
                    $10,$11,$12,
                    $13,$14,$15,$16,
                    $17
                )
                RETURNING id
                """,
                data.exchange.value,
                data.market_id,
                data.external_market_id,
                data.side,
                data.mode.value,
                data.signal_type.value,
                data.contracts,
                data.avg_entry_price,
                data.cost_basis_usd,
                data.stop_loss_price,
                data.take_profit_price,
                data.kelly_fraction_used,
                data.whale_address,
                data.whale_score,
                data.mirror_delay_s,
                data.whale_trade_id,
                data.max_hold_until,
            )
            return row["id"]

    async def get_position(self, position_id: UUID) -> Optional[Position]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM positions WHERE id = $1", position_id
            )
            return Position.model_validate(dict(row)) if row else None

    async def get_open_positions(
        self, exchange: Optional[Exchange] = None
    ) -> list[Position]:
        async with self._pool.acquire() as conn:
            if exchange:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE status IN ('pending','open')"
                    " AND exchange = $1 ORDER BY opened_at DESC",
                    exchange.value,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE status IN ('pending','open')"
                    " ORDER BY opened_at DESC"
                )
            return [Position.model_validate(dict(r)) for r in rows]

    async def get_positions_for_market(
        self, market_id: UUID
    ) -> list[Position]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM positions WHERE market_id = $1 AND status IN ('pending','open')",
                market_id,
            )
            return [Position.model_validate(dict(r)) for r in rows]

    async def activate_position(self, position_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status = 'open', opened_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND status = 'pending'
                """,
                position_id,
            )

    async def update_position_mtm(
        self,
        position_id: UUID,
        current_price: float,
        unrealized_pnl: float,
        market_value: float,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET current_price = $2, unrealized_pnl = $3,
                    market_value_usd = $4, updated_at = NOW()
                WHERE id = $1
                """,
                position_id,
                current_price,
                unrealized_pnl,
                market_value,
            )

    async def close_position(
        self,
        position_id: UUID,
        realized_pnl: float,
        close_reason: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status = 'closed', realized_pnl = $2,
                    close_reason = $3, closed_at = NOW(),
                    unrealized_pnl = 0, updated_at = NOW()
                WHERE id = $1
                """,
                position_id,
                realized_pnl,
                close_reason,
            )

    async def set_position_closing(
        self,
        position_id: UUID,
        close_reason: "CloseReason",
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status = 'pending_close',
                    close_reason = $2,
                    updated_at = NOW()
                WHERE id = $1 AND status = 'open'
                """,
                position_id,
                close_reason.value,
            )

    async def get_closed_positions_today(
        self, mode: TradingMode
    ) -> list[Position]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM positions
                WHERE status = 'closed'
                  AND mode = $1
                  AND closed_at >= CURRENT_DATE
                ORDER BY closed_at DESC
                """,
                mode.value,
            )
            return [Position.model_validate(dict(r)) for r in rows]

    # ── Orders ────────────────────────────────────────────────────────────────

    async def create_order(self, data: OrderCreate) -> UUID:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO orders (
                    position_id, exchange, market_id, external_market_id,
                    side, order_type, status, mode, is_opening,
                    requested_contracts, requested_price, expires_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,'pending',$7,$8,$9,$10,$11
                )
                RETURNING id
                """,
                data.position_id,
                data.exchange.value,
                data.market_id,
                data.external_market_id,
                data.side,
                data.order_type.value,
                data.mode.value,
                data.is_opening,
                data.requested_contracts,
                data.requested_price,
                data.expires_at,
            )
            return row["id"]

    async def get_order(self, order_id: UUID) -> Optional[Order]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM orders WHERE id = $1", order_id
            )
            return Order.model_validate(dict(row)) if row else None

    async def get_pending_orders(self) -> list[Order]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE status IN ('pending','open') ORDER BY created_at ASC"
            )
            return [Order.model_validate(dict(r)) for r in rows]

    async def set_order_placed(
        self, order_id: UUID, external_id: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE orders
                SET external_id = $2, status = 'open', placed_at = NOW(), updated_at = NOW()
                WHERE id = $1
                """,
                order_id,
                external_id,
            )

    async def fill_order(
        self,
        order_id: UUID,
        filled_contracts: float,
        avg_fill_price: float,
        fees_usd: float = 0.0,
    ) -> None:
        async with self._pool.acquire() as conn:
            status = (
                "filled"
                if filled_contracts > 0
                else "cancelled"
            )
            await conn.execute(
                """
                UPDATE orders
                SET status = $2, filled_contracts = $3, avg_fill_price = $4,
                    fees_paid_usd = $5, filled_at = NOW(), updated_at = NOW()
                WHERE id = $1
                """,
                order_id,
                status,
                filled_contracts,
                avg_fill_price,
                fees_usd,
            )

    async def fail_order(self, order_id: UUID, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE orders
                SET status = 'failed', error_message = $2, updated_at = NOW()
                WHERE id = $1
                """,
                order_id,
                error,
            )

    async def cancel_order(self, order_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled', cancelled_at = NOW(), updated_at = NOW()
                WHERE id = $1
                """,
                order_id,
            )

    async def record_fill(
        self,
        order_id: UUID,
        exchange: Exchange,
        contracts: float,
        price: float,
        fees_usd: float = 0.0,
        external_fill_id: Optional[str] = None,
        filled_at: Optional[datetime] = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fills
                    (order_id, exchange, external_fill_id, contracts, price, fees_usd, filled_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT DO NOTHING
                """,
                order_id,
                exchange.value,
                external_fill_id,
                contracts,
                price,
                fees_usd,
                filled_at or datetime.now(timezone.utc),
            )

    # ── Whale trades ──────────────────────────────────────────────────────────

    async def insert_whale_trade(self, data: WhaleTradeInsert) -> Optional[UUID]:
        """Insert a whale trade. Returns None on duplicate tx_hash (idempotent)."""
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO whale_trades (
                        tx_hash, block_timestamp, maker_address, taker_address,
                        market_id, condition_id, token_id,
                        maker_direction, taker_direction,
                        price, usd_amount, token_amount,
                        is_platform_tx, mirror_queued_at
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14
                    )
                    RETURNING id
                    """,
                    data.tx_hash,
                    data.block_timestamp,
                    data.maker_address,
                    data.taker_address,
                    data.market_id,
                    data.condition_id,
                    data.token_id,
                    data.maker_direction,
                    data.taker_direction,
                    data.price,
                    data.usd_amount,
                    data.token_amount,
                    data.is_platform_tx,
                    data.mirror_queued_at,
                )
                return row["id"]
            except asyncpg.UniqueViolationError:
                return None

    async def get_queued_mirror_trades(
        self, min_delay_s: int, min_score: float
    ) -> list[dict]:
        """
        Return whale trades ready to be mirrored (queued but not yet executed,
        delay has elapsed, and the maker has a sufficiently high score).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT wt.*, ws.composite_score
                FROM whale_trades wt
                JOIN whale_scores ws ON ws.address = wt.maker_address
                WHERE wt.mirrored = FALSE
                  AND wt.is_platform_tx = FALSE
                  AND wt.mirror_queued_at IS NOT NULL
                  AND wt.mirror_queued_at <= NOW() - ($1 || ' seconds')::INTERVAL
                  AND ws.composite_score >= $2
                  AND ws.is_active = TRUE
                ORDER BY wt.block_timestamp ASC
                LIMIT 20
                """,
                str(min_delay_s),
                min_score,
            )
            return [dict(r) for r in rows]

    async def mark_whale_trade_mirrored(
        self, trade_id: UUID, position_id: UUID
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE whale_trades
                SET mirrored = TRUE, mirror_position_id = $2,
                    mirror_executed_at = NOW()
                WHERE id = $1
                """,
                trade_id,
                position_id,
            )

    async def get_latest_whale_trade_timestamp(self) -> Optional[int]:
        """Return the unix timestamp of the most recent ingested whale trade."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT MAX(EXTRACT(EPOCH FROM block_timestamp)::BIGINT) AS ts FROM whale_trades"
            )
            return int(row["ts"]) if row and row["ts"] else None

    # ── Whale scores ──────────────────────────────────────────────────────────

    async def get_whale_score(self, address: str) -> Optional[WhaleScore]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM whale_scores WHERE address = $1", address.lower()
            )
            return WhaleScore.model_validate(dict(row)) if row else None

    async def get_top_whales(
        self, min_score: float = 60.0, limit: int = 50
    ) -> list[WhaleScore]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM whale_scores
                WHERE composite_score >= $1 AND is_active = TRUE
                ORDER BY composite_score DESC
                LIMIT $2
                """,
                min_score,
                limit,
            )
            return [WhaleScore.model_validate(dict(r)) for r in rows]

    async def upsert_whale_score(self, address: str, data: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO whale_scores (
                    address, display_name, total_pnl_usd, win_rate, big_win_rate,
                    median_gain_pct, median_loss_pct, markets_traded,
                    total_volume_usd, composite_score, is_active,
                    last_trade_at, scored_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW())
                ON CONFLICT (address) DO UPDATE SET
                    display_name    = COALESCE(EXCLUDED.display_name, whale_scores.display_name),
                    total_pnl_usd   = EXCLUDED.total_pnl_usd,
                    win_rate        = EXCLUDED.win_rate,
                    big_win_rate    = EXCLUDED.big_win_rate,
                    median_gain_pct = EXCLUDED.median_gain_pct,
                    median_loss_pct = EXCLUDED.median_loss_pct,
                    markets_traded  = EXCLUDED.markets_traded,
                    total_volume_usd= EXCLUDED.total_volume_usd,
                    composite_score = EXCLUDED.composite_score,
                    is_active       = EXCLUDED.is_active,
                    last_trade_at   = EXCLUDED.last_trade_at,
                    scored_at       = NOW()
                """,
                address.lower(),
                data.get("display_name"),
                data.get("total_pnl_usd"),
                data.get("win_rate"),
                data.get("big_win_rate"),
                data.get("median_gain_pct"),
                data.get("median_loss_pct"),
                data.get("markets_traded"),
                data.get("total_volume_usd"),
                data.get("composite_score"),
                data.get("is_active", True),
                data.get("last_trade_at"),
            )

    # ── News signals ──────────────────────────────────────────────────────────

    async def insert_news_signal(self, data: NewsSignalInsert) -> Optional[UUID]:
        """Insert a news signal; returns None (silently) if the URL already exists."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO news_signals (
                    market_id, external_market_id, source, headline, url,
                    published_at, sentiment_score, relevance_score,
                    direction, keywords, raw
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (url) WHERE url IS NOT NULL DO NOTHING
                RETURNING id
                """,
                data.market_id,
                data.external_market_id,
                data.source,
                data.headline,
                data.url,
                data.published_at,
                data.sentiment_score,
                data.relevance_score,
                data.direction,
                data.keywords,
                _to_json(data.raw),
            )
            return row["id"] if row else None

    async def get_recent_signal_urls(self, hours: int = 24) -> set[str]:
        """Return the set of URLs already stored in the last N hours."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT url FROM news_signals
                WHERE url IS NOT NULL
                  AND created_at >= NOW() - ($1 || ' hours')::INTERVAL
                """,
                str(hours),
            )
        return {r["url"] for r in rows}

    async def get_recent_signals_for_market(
        self, market_id: UUID, hours: int = 24
    ) -> list[NewsSignal]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM news_signals
                WHERE market_id = $1
                  AND created_at >= NOW() - ($2 || ' hours')::INTERVAL
                ORDER BY relevance_score DESC NULLS LAST, created_at DESC
                """,
                market_id,
                str(hours),
            )
            return [NewsSignal.model_validate(dict(r)) for r in rows]

    async def has_recent_signal_for_market(
        self, market_id: UUID, source: str, hours: int = 6
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM news_signals
                WHERE market_id = $1 AND source = $2
                  AND created_at >= NOW() - ($3 || ' hours')::INTERVAL
                LIMIT 1
                """,
                market_id,
                source,
                str(hours),
            )
            return row is not None

    # ── Category scores ───────────────────────────────────────────────────────

    async def get_category_score(
        self, exchange: Exchange, category: str
    ) -> Optional[CategoryScore]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM category_scores WHERE exchange = $1 AND category = $2",
                exchange.value,
                category.lower(),
            )
            return CategoryScore.model_validate(dict(row)) if row else None

    async def get_all_category_scores(
        self, exchange: Optional[Exchange] = None
    ) -> list[CategoryScore]:
        async with self._pool.acquire() as conn:
            if exchange:
                rows = await conn.fetch(
                    "SELECT * FROM category_scores WHERE exchange = $1 ORDER BY composite_score DESC",
                    exchange.value,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM category_scores ORDER BY composite_score DESC"
                )
            return [CategoryScore.model_validate(dict(r)) for r in rows]

    # ── Portfolio checks ──────────────────────────────────────────────────────

    async def get_category_exposure_usd(
        self, exchange: Exchange, category: str
    ) -> float:
        """Sum of cost_basis_usd for all open positions in this category."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(p.cost_basis_usd), 0) AS total
                FROM positions p
                JOIN markets m ON m.id = p.market_id
                WHERE p.status IN ('pending','open')
                  AND p.exchange = $1
                  AND m.category = $2
                """,
                exchange.value,
                category.lower(),
            )
            return float(row["total"])

    async def get_total_exposure_usd(self) -> float:
        """Sum of cost_basis_usd for all open positions."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_basis_usd), 0) AS total"
                " FROM positions WHERE status IN ('pending','open')"
            )
            return float(row["total"])

    async def get_category_exposure_all_exchanges(self, category: str) -> float:
        """Sum of cost_basis_usd for all open positions in this category across ALL exchanges."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(p.cost_basis_usd), 0) AS total
                FROM positions p
                JOIN markets m ON m.id = p.market_id
                WHERE p.status IN ('pending','open')
                  AND m.category = $1
                """,
                category.lower(),
            )
            return float(row["total"])

    # ── Blocked trades ────────────────────────────────────────────────────────

    async def insert_blocked_trade(self, data: BlockedTradeInsert) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO blocked_trades (
                    exchange, market_id, external_market_id, side,
                    proposed_contracts, proposed_price, signal_type,
                    block_gate, block_reason, mode
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                data.exchange.value,
                data.market_id,
                data.external_market_id,
                data.side,
                data.proposed_contracts,
                data.proposed_price,
                data.signal_type.value if data.signal_type else None,
                data.block_gate,
                data.block_reason,
                data.mode.value,
            )

    # ── Evaluation decisions ─────────────────────────────────────────────────

    async def insert_evaluation_decision(self, data: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO evaluation_decisions (
                    market_id, external_market_id, market_title, exchange,
                    signal_type, side, entry_price, edge, confidence,
                    kelly_size_usd, decision, reason
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                """,
                data.get("market_id"),
                data.get("external_market_id"),
                data.get("market_title"),
                data["exchange"],
                data.get("signal_type"),
                data.get("side"),
                data.get("entry_price"),
                data.get("edge"),
                data.get("confidence"),
                data.get("kelly_size_usd"),
                data["decision"],
                data["reason"],
            )

    async def get_evaluation_decisions(
        self,
        limit: int = 500,
        exchange: Optional[str] = None,
        decision: Optional[str] = None,
        signal_type: Optional[str] = None,
        reason_prefix: Optional[str] = None,
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            clauses = ["TRUE"]
            params: list[Any] = []
            idx = 1

            if exchange:
                clauses.append(f"exchange = ${idx}")
                params.append(exchange)
                idx += 1
            if decision:
                clauses.append(f"decision = ${idx}")
                params.append(decision)
                idx += 1
            if signal_type:
                clauses.append(f"signal_type = ${idx}")
                params.append(signal_type)
                idx += 1
            if reason_prefix:
                clauses.append(f"reason LIKE ${idx}")
                params.append(reason_prefix + "%")
                idx += 1

            clauses.append("created_at >= NOW() - interval '24 hours'")

            where = " AND ".join(clauses)
            params.append(limit)

            rows = await conn.fetch(
                f"""
                SELECT * FROM evaluation_decisions
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx}
                """,
                *params,
            )
            return [dict(r) for r in rows]

    async def prune_old_evaluation_decisions(self, days: int = 7) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM evaluation_decisions WHERE created_at < NOW() - ($1 || ' days')::interval",
                str(days),
            )
            return int(result.split()[-1])

    # ── Historical pattern scoring ──────────────────────────────────────────────

    async def get_historical_win_rates(self, min_trades: int = 5) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT COALESCE(m.category, 'unknown') AS category,
                       p.exchange::text AS exchange,
                       CASE
                         WHEN p.avg_entry_price < 0.3 THEN 'low'
                         WHEN p.avg_entry_price < 0.7 THEN 'mid'
                         ELSE 'high'
                       END AS price_range,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE p.realized_pnl > 0) AS wins,
                       COALESCE(AVG(p.realized_pnl), 0) AS avg_pnl
                FROM positions p
                JOIN markets m ON m.id = p.market_id
                WHERE p.status = 'closed' AND p.realized_pnl IS NOT NULL
                GROUP BY category, p.exchange, price_range
                HAVING COUNT(*) >= $1
                ORDER BY COUNT(*) DESC
                """,
                min_trades,
            )
            return [dict(r) for r in rows]

    # ── LLM cost tracking ─────────────────────────────────────────────────────

    async def get_llm_daily_cost_usd(self) -> float:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total"
                " FROM llm_queries WHERE created_at >= CURRENT_DATE"
            )
            return float(row["total"])

    async def insert_llm_query(self, data: LLMQueryInsert) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO llm_queries (
                    model, purpose, market_id, prompt_tokens, completion_tokens,
                    cost_usd, response_action, response_confidence, latency_ms
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                data.model,
                data.purpose,
                data.market_id,
                data.prompt_tokens,
                data.completion_tokens,
                data.cost_usd,
                data.response_action,
                data.response_confidence,
                data.latency_ms,
            )

    # ── Daily PnL ─────────────────────────────────────────────────────────────

    async def upsert_daily_pnl(
        self,
        report_date: date,
        mode: TradingMode,
        exchange: Optional[Exchange],
        **fields: Any,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_pnl (
                    date, mode, exchange,
                    starting_balance, ending_balance,
                    realized_pnl, unrealized_pnl, fees_paid,
                    trades_opened, trades_closed, win_count, loss_count
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (date, exchange, mode) DO UPDATE SET
                    ending_balance  = EXCLUDED.ending_balance,
                    realized_pnl    = EXCLUDED.realized_pnl,
                    unrealized_pnl  = EXCLUDED.unrealized_pnl,
                    fees_paid       = EXCLUDED.fees_paid,
                    trades_opened   = EXCLUDED.trades_opened,
                    trades_closed   = EXCLUDED.trades_closed,
                    win_count       = EXCLUDED.win_count,
                    loss_count      = EXCLUDED.loss_count
                """,
                report_date,
                mode.value,
                exchange.value if exchange else None,
                fields.get("starting_balance"),
                fields.get("ending_balance"),
                fields.get("realized_pnl"),
                fields.get("unrealized_pnl"),
                fields.get("fees_paid"),
                fields.get("trades_opened", 0),
                fields.get("trades_closed", 0),
                fields.get("win_count", 0),
                fields.get("loss_count", 0),
            )

    async def get_daily_pnl_history(self, days: int = 30) -> list[DailyPnL]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM daily_pnl
                WHERE date >= CURRENT_DATE - ($1 || ' days')::INTERVAL
                  AND exchange IS NULL
                ORDER BY date DESC
                """,
                str(days),
            )
            return [DailyPnL.model_validate(dict(r)) for r in rows]


async def _set_type_codecs(conn: asyncpg.Connection) -> None:
    """Register JSON codec so asyncpg returns dicts for JSONB columns."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
