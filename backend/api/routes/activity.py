from __future__ import annotations

"""
Activity feed — unified stream of recent bot decisions.

Aggregates across four event sources and returns them sorted newest-first:
  - position_opened   : a new position was entered
  - position_closed   : a position was exited (with P&L + reason)
  - trade_blocked     : PortfolioEnforcer rejected a trade (with gate + reason)
  - whale_queued      : a whale trade was queued for mirroring
"""

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database

from .deps import get_db

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/")
async def get_activity(
    hours: int = Query(24, le=168),
    limit: int = Query(100, le=500),
    db: Database = Depends(get_db),
) -> list[dict]:
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM (

                SELECT
                    'position_opened'         AS event_type,
                    p.opened_at               AS ts,
                    p.exchange::TEXT          AS exchange,
                    p.side,
                    p.signal_type::TEXT       AS signal_type,
                    m.title                   AS market_title,
                    p.cost_basis_usd          AS size_usd,
                    NULL::NUMERIC             AS pnl,
                    NULL::TEXT                AS reason,
                    NULL::TEXT                AS gate,
                    NULL::TEXT                AS address
                FROM positions p
                JOIN markets m ON m.id = p.market_id
                WHERE p.opened_at >= NOW() - ($1 || ' hours')::interval

                UNION ALL

                SELECT
                    'position_closed'         AS event_type,
                    p.closed_at               AS ts,
                    p.exchange::TEXT,
                    p.side,
                    p.signal_type::TEXT,
                    m.title,
                    p.cost_basis_usd,
                    p.realized_pnl,
                    p.close_reason::TEXT,
                    NULL,
                    NULL
                FROM positions p
                JOIN markets m ON m.id = p.market_id
                WHERE p.closed_at >= NOW() - ($1 || ' hours')::interval
                  AND p.status = 'closed'

                UNION ALL

                SELECT
                    'trade_blocked'           AS event_type,
                    bt.created_at             AS ts,
                    bt.exchange::TEXT,
                    bt.side,
                    bt.signal_type::TEXT,
                    m.title,
                    (bt.proposed_contracts * bt.proposed_price),
                    NULL,
                    bt.block_reason,
                    bt.block_gate,
                    NULL
                FROM blocked_trades bt
                LEFT JOIN markets m ON m.id = bt.market_id
                WHERE bt.created_at >= NOW() - ($1 || ' hours')::interval

                UNION ALL

                SELECT
                    'whale_queued'            AS event_type,
                    wt.mirror_queued_at       AS ts,
                    'polymarket'              AS exchange,
                    wt.maker_direction        AS side,
                    'whale_mirror'            AS signal_type,
                    m.title,
                    wt.usd_amount,
                    NULL,
                    NULL,
                    NULL,
                    wt.maker_address
                FROM whale_trades wt
                LEFT JOIN markets m ON m.id = wt.market_id
                WHERE wt.mirror_queued_at >= NOW() - ($1 || ' hours')::interval

            ) events
            WHERE ts IS NOT NULL
            ORDER BY ts DESC
            LIMIT $2
            """,
            str(hours),
            limit,
        )

    return [
        {
            "event_type": r["event_type"],
            "ts": r["ts"].isoformat() if r["ts"] else None,
            "exchange": r["exchange"],
            "side": r["side"],
            "signal_type": r["signal_type"],
            "market_title": r["market_title"],
            "size_usd": float(r["size_usd"]) if r["size_usd"] is not None else None,
            "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            "reason": r["reason"],
            "gate": r["gate"],
            "address": r["address"],
        }
        for r in rows
    ]
