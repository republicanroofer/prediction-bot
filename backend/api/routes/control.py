from __future__ import annotations

"""
Control routes — runtime management of the bot.

Endpoints:
  GET  /control/status        : bot health + config snapshot
  POST /control/mode          : switch paper ↔ live (requires restart)
  POST /control/pause         : pause scanner (stop opening new positions)
  POST /control/resume        : resume scanner
  POST /control/close-all     : queue close for all open positions
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.config.settings import TradingMode, get_settings
from backend.db.database import Database
from backend.db.models import CloseReason

from .deps import get_db

router = APIRouter(prefix="/control", tags=["control"])


@router.get("/status")
async def bot_status(
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    cfg = get_settings()

    async with db._pool.acquire() as conn:
        open_count = await conn.fetchval(
            "SELECT COUNT(*) FROM positions WHERE status IN ('open', 'pending_close')"
        )
        pending_orders = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'open')"
        )
        total_exposure = await conn.fetchval(
            """
            SELECT COALESCE(SUM(cost_basis_usd), 0)
            FROM positions
            WHERE status IN ('open', 'pending_close')
            """
        )
        realized_pnl = await conn.fetchval(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status = 'closed'"
        )
        unrealized_pnl = await conn.fetchval(
            """
            SELECT COALESCE(SUM(unrealized_pnl), 0)
            FROM positions
            WHERE status IN ('open', 'pending_close')
            """
        )

    starting = cfg.paper_starting_balance
    paper_balance = starting + float(realized_pnl or 0) + float(unrealized_pnl or 0)
    paper_return_pct = (paper_balance - starting) / starting * 100

    return {
        "mode": cfg.trading_mode.value,
        "exchange": cfg.active_exchange.value,
        "open_positions": open_count,
        "pending_orders": pending_orders,
        "total_exposure_usd": float(total_exposure or 0),
        "stop_loss_pct": cfg.stop_loss_pct,
        "take_profit_pct": cfg.take_profit_pct,
        "max_position_pct": cfg.max_position_pct,
        "kelly_fraction": cfg.kelly_fraction,
        "paper_starting_balance": starting,
        "paper_balance": round(paper_balance, 2),
        "paper_return_pct": round(paper_return_pct, 2),
    }


class ModeRequest(BaseModel):
    mode: TradingMode


@router.post("/mode")
async def set_mode(body: ModeRequest) -> dict:
    """
    Update TRADING_MODE in settings.  Takes effect after bot restart.
    In production, update the .env file and restart the container.
    """
    cfg = get_settings()
    if body.mode == cfg.trading_mode:
        return {"message": f"Already in {body.mode.value} mode"}

    if body.mode == TradingMode.LIVE:
        if not cfg.kalshi_api_key_id and not cfg.polymarket_wallet_private_key:
            raise HTTPException(
                status_code=400,
                detail="Cannot switch to live mode: no exchange credentials configured",
            )

    return {
        "message": f"Mode change to '{body.mode.value}' requested. Restart the bot to apply.",
        "current_mode": cfg.trading_mode.value,
        "requested_mode": body.mode.value,
    }


@router.post("/close-all")
async def close_all_positions(
    db: Database = Depends(get_db),
) -> dict:
    """Queue a manual close for every open position."""
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM positions WHERE status = 'open'"
        )

    count = 0
    for row in rows:
        await db.set_position_closing(row["id"], CloseReason.MANUAL)
        count += 1

    return {"queued_for_close": count}
