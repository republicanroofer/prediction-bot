from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.db.database import Database
from backend.db.models import Exchange, Position, PositionStatus

from .deps import get_db

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/", response_model=list[Position])
async def list_positions(
    exchange: Optional[Exchange] = Query(None),
    status: Optional[PositionStatus] = Query(None),
    limit: int = Query(100, le=500),
    db: Database = Depends(get_db),
) -> list[Position]:
    async with db._pool.acquire() as conn:
        conditions = []
        params: list = []

        if exchange:
            params.append(exchange.value)
            conditions.append(f"exchange = ${len(params)}::exchange_t")

        if status:
            params.append(status.value)
            conditions.append(f"status = ${len(params)}::position_status_t")
        else:
            # default: open + pending_close
            conditions.append("status IN ('open', 'pending_close')")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT * FROM positions {where} ORDER BY opened_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [Position.model_validate(dict(r)) for r in rows]


@router.get("/{position_id}", response_model=Position)
async def get_position(position_id: UUID, db: Database = Depends(get_db)) -> Position:
    pos = await db.get_position(position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    return pos


@router.delete("/{position_id}")
async def close_position(
    position_id: UUID,
    db: Database = Depends(get_db),
) -> dict:
    """
    Queue a manual close for a position.
    The position_tracker will execute the close on its next tick.
    """
    from backend.db.models import CloseReason
    pos = await db.get_position(position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    if pos.status not in ("open", "pending_close"):
        raise HTTPException(status_code=400, detail=f"Position status is '{pos.status}'")

    await db.set_position_closing(position_id, CloseReason.MANUAL)
    return {"status": "closing", "position_id": str(position_id)}
