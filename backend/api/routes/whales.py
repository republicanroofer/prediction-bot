from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database
from backend.db.models import WhaleScore, WhaleTrade

from .deps import get_db

router = APIRouter(prefix="/whales", tags=["whales"])


@router.get("/scores", response_model=list[WhaleScore])
async def list_whale_scores(
    min_score: float = Query(0.0),
    active_only: bool = Query(True),
    limit: int = Query(50, le=200),
    db: Database = Depends(get_db),
) -> list[WhaleScore]:
    async with db._pool.acquire() as conn:
        conditions = [f"composite_score >= $1"]
        params: list = [min_score]

        if active_only:
            conditions.append("is_active = TRUE")

        where = f"WHERE {' AND '.join(conditions)}"
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT *, COALESCE(last_trade_at, scored_at) AS last_trade_at"
            f" FROM whale_scores {where} ORDER BY composite_score DESC LIMIT ${len(params)}",
            *params,
        )
    return [WhaleScore.model_validate(dict(r)) for r in rows]


@router.get("/trades", response_model=list[WhaleTrade])
async def list_whale_trades(
    address: Optional[str] = Query(None),
    market_id: Optional[str] = Query(None),
    mirrored_only: bool = Query(False),
    limit: int = Query(50, le=200),
    db: Database = Depends(get_db),
) -> list[WhaleTrade]:
    async with db._pool.acquire() as conn:
        conditions = []
        params: list = []

        if address:
            params.append(address.lower())
            conditions.append(f"maker_address = ${len(params)}")

        if market_id:
            params.append(market_id)
            conditions.append(f"external_market_id = ${len(params)}")

        if mirrored_only:
            conditions.append("mirrored_at IS NOT NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT * FROM whale_trades {where} ORDER BY block_timestamp DESC LIMIT ${len(params)}",
            *params,
        )
    return [WhaleTrade.model_validate(dict(r)) for r in rows]
