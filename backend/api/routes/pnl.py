from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database
from backend.db.models import Exchange, TradingMode

from .deps import get_db

router = APIRouter(prefix="/pnl", tags=["pnl"])


@router.get("/daily")
async def daily_pnl(
    days: int = Query(30, le=365),
    db: Database = Depends(get_db),
) -> list[dict]:
    since = date.today() - timedelta(days=days)
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                closed_at::date AS date,
                exchange::text,
                mode::text,
                COALESCE(SUM(realized_pnl), 0) AS realized_pnl,
                COALESCE(SUM(unrealized_pnl), 0) AS unrealized_pnl,
                COUNT(*) AS num_positions,
                COUNT(*) FILTER (WHERE realized_pnl > 0) AS num_wins,
                COUNT(*) FILTER (WHERE realized_pnl <= 0) AS num_losses
            FROM positions
            WHERE status = 'closed'
              AND closed_at::date >= $1
            GROUP BY closed_at::date, exchange, mode
            ORDER BY date DESC
            """,
            since,
        )

    results = []
    for r in rows:
        results.append({
            "date": str(r["date"]),
            "exchange": r["exchange"],
            "mode": r["mode"],
            "realized_pnl": float(r["realized_pnl"]),
            "unrealized_pnl": float(r["unrealized_pnl"]),
            "num_positions": r["num_positions"],
            "num_wins": r["num_wins"],
            "num_losses": r["num_losses"],
        })

    # If no closed positions yet, include today with open position unrealized
    if not results:
        unreal = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(unrealized_pnl), 0) AS unrealized,
                   COUNT(*) AS positions
            FROM positions WHERE status IN ('open', 'pending_close')
            """
        )
        if unreal and unreal["positions"] > 0:
            results.append({
                "date": str(date.today()),
                "exchange": "both",
                "mode": "paper",
                "realized_pnl": 0.0,
                "unrealized_pnl": float(unreal["unrealized"]),
                "num_positions": unreal["positions"],
                "num_wins": 0,
                "num_losses": 0,
            })

    return results


@router.get("/summary")
async def pnl_summary(
    db: Database = Depends(get_db),
) -> dict:
    async with db._pool.acquire() as conn:
        closed = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(realized_pnl), 0) AS total_realized,
                COUNT(*) AS total_trades,
                COUNT(*) FILTER (WHERE realized_pnl > 0) AS wins,
                COUNT(*) FILTER (WHERE realized_pnl <= 0) AS losses,
                COALESCE(SUM(cost_basis_usd), 0) AS total_cost
            FROM positions
            WHERE status = 'closed'
            """
        )
        open_pos = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(unrealized_pnl), 0) AS total_unrealized,
                COUNT(*) AS open_count,
                COALESCE(SUM(cost_basis_usd), 0) AS open_exposure
            FROM positions
            WHERE status IN ('open', 'pending_close')
            """
        )

    total_trades = closed["total_trades"] if closed else 0
    wins = closed["wins"] if closed else 0
    losses = closed["losses"] if closed else 0
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    return {
        "total_realized_pnl": float(closed["total_realized"]) if closed else 0.0,
        "total_unrealized_pnl": float(open_pos["total_unrealized"]) if open_pos else 0.0,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "total_wins": wins,
        "total_losses": losses,
        "open_positions": open_pos["open_count"] if open_pos else 0,
        "open_exposure": float(open_pos["open_exposure"]) if open_pos else 0.0,
    }
