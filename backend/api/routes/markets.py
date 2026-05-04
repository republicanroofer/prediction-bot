from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database
from backend.db.models import Exchange, Market

from .deps import get_db

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/", response_model=list[Market])
async def list_markets(
    exchange: Optional[Exchange] = Query(None),
    category: Optional[str] = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(100, le=500),
    db: Database = Depends(get_db),
) -> list[Market]:
    async with db._pool.acquire() as conn:
        conditions = []
        params: list = []

        if active_only:
            conditions.append("is_active = TRUE")

        if exchange:
            params.append(exchange.value)
            conditions.append(f"exchange = ${len(params)}::exchange_t")

        if category:
            params.append(category.lower())
            conditions.append(f"LOWER(category) = ${len(params)}")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT * FROM markets {where} ORDER BY volume_24h_usd DESC NULLS LAST LIMIT ${len(params)}",
            *params,
        )
    return [Market.model_validate(dict(r)) for r in rows]


@router.get("/{market_id}", response_model=Market)
async def get_market(market_id: UUID, db: Database = Depends(get_db)) -> Market:
    market = await db.get_market(market_id)
    if market is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Market not found")
    return market
