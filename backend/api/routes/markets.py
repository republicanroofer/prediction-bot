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
        active_clause = "is_active = TRUE" if active_only else "TRUE"

        if exchange:
            # Single-exchange filter: simple query
            conditions = [active_clause, f"exchange = $1::exchange_t"]
            if category:
                conditions.append("LOWER(category) = $2")
                rows = await conn.fetch(
                    f"SELECT * FROM markets WHERE {' AND '.join(conditions)} "
                    f"ORDER BY volume_24h_usd DESC NULLS LAST LIMIT $3",
                    exchange.value, category.lower(), limit,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT * FROM markets WHERE {' AND '.join(conditions)} "
                    f"ORDER BY volume_24h_usd DESC NULLS LAST LIMIT $2",
                    exchange.value, limit,
                )
        else:
            # No exchange filter: UNION ALL so both exchanges always get representation
            half = limit // 2
            cat_clause = f"AND LOWER(category) = $1" if category else ""
            cat_params = [category.lower()] if category else []
            n = len(cat_params)
            rows = await conn.fetch(
                f"""
                (SELECT * FROM markets WHERE {active_clause} AND exchange = 'kalshi'::exchange_t {cat_clause}
                 ORDER BY volume_24h_usd DESC NULLS LAST LIMIT ${n+1})
                UNION ALL
                (SELECT * FROM markets WHERE {active_clause} AND exchange = 'polymarket'::exchange_t {cat_clause}
                 ORDER BY volume_24h_usd DESC NULLS LAST LIMIT ${n+2})
                ORDER BY volume_24h_usd DESC NULLS LAST
                """,
                *cat_params, half, limit - half,
            )
    return [Market.model_validate(dict(r)) for r in rows]


@router.get("/{market_id}", response_model=Market)
async def get_market(market_id: UUID, db: Database = Depends(get_db)) -> Market:
    market = await db.get_market(market_id)
    if market is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Market not found")
    return market
