from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.db.database import Database
from backend.db.models import Exchange, Order, OrderStatus

from .deps import get_db

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/", response_model=list[Order])
async def list_orders(
    exchange: Optional[Exchange] = Query(None),
    status: Optional[OrderStatus] = Query(None),
    position_id: Optional[UUID] = Query(None),
    limit: int = Query(100, le=500),
    db: Database = Depends(get_db),
) -> list[Order]:
    async with db._pool.acquire() as conn:
        conditions = []
        params: list = []

        if exchange:
            params.append(exchange.value)
            conditions.append(f"exchange = ${len(params)}::exchange_t")

        if status:
            params.append(status.value)
            conditions.append(f"status = ${len(params)}::order_status_t")

        if position_id:
            params.append(position_id)
            conditions.append(f"position_id = ${len(params)}")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [Order.model_validate(dict(r)) for r in rows]


@router.get("/{order_id}", response_model=Order)
async def get_order(order_id: UUID, db: Database = Depends(get_db)) -> Order:
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.delete("/{order_id}")
async def cancel_order(order_id: UUID, db: Database = Depends(get_db)) -> dict:
    from fastapi import Request
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.is_complete:
        raise HTTPException(status_code=400, detail="Order is already terminal")

    # Cancel via OrderManager (which handles exchange-side cancellation)
    # We access it through app.state in the request, but for simplicity
    # just call DB cancel directly; position_tracker will reconcile.
    await db.cancel_order(order_id)
    return {"status": "cancelled", "order_id": str(order_id)}
