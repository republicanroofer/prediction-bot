from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database
from backend.db.models import Exchange, NewsSignal

from .deps import get_db

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/news", response_model=list[NewsSignal])
async def list_news_signals(
    market_id: Optional[UUID] = Query(None),
    hours: int = Query(24, le=168),
    limit: int = Query(50, le=200),
    db: Database = Depends(get_db),
) -> list[NewsSignal]:
    async with db._pool.acquire() as conn:
        conditions = ["created_at >= NOW() - ($1 || ' hours')::interval"]
        params: list = [str(hours)]

        if market_id:
            params.append(market_id)
            conditions.append(f"market_id = ${len(params)}")

        where = f"WHERE {' AND '.join(conditions)}"
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT * FROM news_signals {where} ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [NewsSignal.model_validate(dict(r)) for r in rows]


@router.get("/blocked-trades")
async def list_blocked_trades(
    exchange: Optional[Exchange] = Query(None),
    gate: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Database = Depends(get_db),
) -> list[dict]:
    async with db._pool.acquire() as conn:
        conditions = []
        params: list = []

        if exchange:
            params.append(exchange.value)
            conditions.append(f"exchange = ${len(params)}::exchange_t")

        if gate:
            params.append(gate)
            conditions.append(f"block_gate = ${len(params)}")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT * FROM blocked_trades {where} ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


@router.get("/whale", response_model=list[dict])
async def list_whale_mirror_signals(
    hours: int = Query(24, le=168),
    limit: int = Query(50, le=200),
    db: Database = Depends(get_db),
) -> list[dict]:
    """Recent whale trades that were queued for mirroring."""
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT wt.*, ws.composite_score AS whale_score, m.title AS market_title
            FROM whale_trades wt
            LEFT JOIN whale_scores ws ON ws.address = wt.maker_address
            LEFT JOIN markets m ON m.id = wt.market_id
            WHERE wt.mirror_queued_at >= NOW() - ($1 || ' hours')::interval
              AND wt.is_platform_tx = FALSE
            ORDER BY wt.mirror_queued_at DESC
            LIMIT $2
            """,
            str(hours),
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/category-scores")
async def category_scores(
    exchange: Optional[Exchange] = Query(None),
    db: Database = Depends(get_db),
) -> list[dict]:
    async with db._pool.acquire() as conn:
        if exchange:
            rows = await conn.fetch(
                "SELECT * FROM category_scores WHERE exchange = $1::exchange_t ORDER BY composite_score DESC",
                exchange.value,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM category_scores ORDER BY exchange, composite_score DESC"
            )
    return [dict(r) for r in rows]
