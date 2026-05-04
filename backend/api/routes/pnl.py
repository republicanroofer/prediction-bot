from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database
from backend.db.models import DailyPnL, Exchange, TradingMode

from .deps import get_db

router = APIRouter(prefix="/pnl", tags=["pnl"])


@router.get("/daily", response_model=list[DailyPnL])
async def daily_pnl(
    exchange: Optional[Exchange] = Query(None),
    mode: Optional[TradingMode] = Query(None),
    days: int = Query(30, le=365),
    db: Database = Depends(get_db),
) -> list[DailyPnL]:
    since = date.today() - timedelta(days=days)

    async with db._pool.acquire() as conn:
        conditions = ["date >= $1"]
        params: list = [since]

        if exchange:
            params.append(exchange.value)
            conditions.append(f"exchange = ${len(params)}::exchange_t")

        if mode:
            params.append(mode.value)
            conditions.append(f"mode = ${len(params)}::trading_mode_t")

        where = f"WHERE {' AND '.join(conditions)}"
        rows = await conn.fetch(
            f"SELECT * FROM daily_pnl {where} ORDER BY date DESC",
            *params,
        )
    return [DailyPnL.model_validate(dict(r)) for r in rows]


@router.get("/summary")
async def pnl_summary(
    exchange: Optional[Exchange] = Query(None),
    mode: Optional[TradingMode] = Query(None),
    db: Database = Depends(get_db),
) -> dict:
    """Aggregate P&L stats across all time."""
    async with db._pool.acquire() as conn:
        conditions = []
        params: list = []

        if exchange:
            params.append(exchange.value)
            conditions.append(f"exchange = ${len(params)}::exchange_t")

        if mode:
            params.append(mode.value)
            conditions.append(f"mode = ${len(params)}::trading_mode_t")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = await conn.fetchrow(
            f"""
            SELECT
                SUM(realized_pnl)    AS total_realized,
                SUM(unrealized_pnl)  AS total_unrealized,
                SUM(num_positions)   AS total_positions,
                SUM(num_wins)        AS total_wins,
                SUM(num_losses)      AS total_losses
            FROM daily_pnl {where}
            """,
            *params,
        )

    if row is None:
        return {}

    total_trades = (row["total_wins"] or 0) + (row["total_losses"] or 0)
    win_rate = (row["total_wins"] or 0) / total_trades if total_trades else 0.0

    return {
        "total_realized_pnl": float(row["total_realized"] or 0),
        "total_unrealized_pnl": float(row["total_unrealized"] or 0),
        "total_positions": row["total_positions"] or 0,
        "win_rate": round(win_rate, 4),
        "total_wins": row["total_wins"] or 0,
        "total_losses": row["total_losses"] or 0,
    }
