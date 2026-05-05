from __future__ import annotations

"""
WebSocket endpoint — pushes live state updates to the dashboard.

Clients connect to /ws and receive JSON frames every PUSH_INTERVAL_S seconds:

  {
    "type": "snapshot",
    "positions": [...],    // open positions with current P&L
    "orders":    [...],    // pending/open orders
    "pnl": {               // today's P&L totals
      "realized":   float,
      "unrealized": float
    },
    "ts": "ISO8601"
  }

Each connected client gets its own push loop; stale connections are cleaned
up on the next failed send.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.db.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

PUSH_INTERVAL_S = 5.0


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    db: Database = websocket.app.state.db

    if db is None:
        await websocket.send_text(json.dumps({"error": "DB not ready"}))
        await websocket.close()
        return

    logger.debug("WebSocket client connected: %s", websocket.client)
    try:
        while True:
            payload = await _build_snapshot(db)
            await websocket.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(PUSH_INTERVAL_S)
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected: %s", websocket.client)
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)


async def _build_snapshot(db: Database) -> dict:
    async with db._pool.acquire() as conn:
        pos_rows = await conn.fetch(
            "SELECT * FROM positions WHERE status IN ('open', 'pending_close') ORDER BY opened_at DESC LIMIT 50"
        )
        ord_rows = await conn.fetch(
            "SELECT * FROM orders WHERE status IN ('pending', 'open') ORDER BY created_at DESC LIMIT 50"
        )
        pnl_row = await conn.fetchrow(
            """
            SELECT
                (SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status = 'closed') AS r,
                (SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions WHERE status IN ('open', 'pending_close')) AS u
            """
        )

    return {
        "type": "snapshot",
        "positions": [dict(r) for r in pos_rows],
        "orders": [dict(r) for r in ord_rows],
        "pnl": {
            "realized": float(pnl_row["r"] or 0) if pnl_row else 0.0,
            "unrealized": float(pnl_row["u"] or 0) if pnl_row else 0.0,
        },
        "ts": datetime.now(timezone.utc).isoformat(),
    }
