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


@router.get("/trades")
async def pnl_trades(
    days: int = Query(30, le=365),
    db: Database = Depends(get_db),
) -> list[dict]:
    """One data point per closed trade, ordered chronologically."""
    since = date.today() - timedelta(days=days)
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.closed_at,
                p.realized_pnl,
                p.exchange::text,
                p.signal_type::text,
                p.side,
                p.cost_basis_usd,
                m.title AS market_title
            FROM positions p
            JOIN markets m ON m.id = p.market_id
            WHERE p.status = 'closed'
              AND p.closed_at::date >= $1
            ORDER BY p.closed_at ASC
            """,
            since,
        )

    cum = 0.0
    results = []
    for r in rows:
        pnl = float(r["realized_pnl"] or 0)
        cum += pnl
        results.append({
            "timestamp": r["closed_at"].isoformat(),
            "realized_pnl": round(pnl, 2),
            "cumulative_pnl": round(cum, 2),
            "exchange": r["exchange"],
            "signal_type": r["signal_type"],
            "side": r["side"],
            "cost_basis_usd": float(r["cost_basis_usd"]),
            "market_title": r["market_title"],
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
