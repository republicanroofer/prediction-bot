from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from backend.db.database import Database
from backend.db.models import Exchange

from .deps import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/funnel")
async def funnel_metrics(
    hours: int = Query(24, le=168),
    db: Database = Depends(get_db),
) -> dict:
    async with db._pool.acquire() as conn:
        h = str(hours)
        markets = await conn.fetchval(
            "SELECT COUNT(*) FROM markets WHERE is_active = TRUE"
        )
        signals = await conn.fetchval(
            "SELECT COUNT(*) FROM news_signals WHERE created_at >= NOW() - ($1 || ' hours')::interval",
            h,
        )
        blocked = await conn.fetchval(
            "SELECT COUNT(*) FROM blocked_trades WHERE created_at >= NOW() - ($1 || ' hours')::interval",
            h,
        )
        executed = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE status = 'filled' AND created_at >= NOW() - ($1 || ' hours')::interval",
            h,
        )
    return {
        "markets_scanned": markets,
        "signals_generated": signals,
        "trades_blocked": blocked,
        "trades_executed": executed,
        "period_hours": hours,
    }


@router.get("/opportunities")
async def opportunities(
    limit: int = Query(20, le=100),
    db: Database = Depends(get_db),
) -> list[dict]:
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT DISTINCT ON (ns.market_id)
                    ns.market_id,
                    ns.relevance_score,
                    ns.sentiment_score,
                    ns.direction,
                    ns.headline,
                    ns.source,
                    m.title,
                    m.exchange,
                    m.category,
                    m.yes_bid,
                    m.yes_ask,
                    m.volume_24h_usd,
                    m.close_time,
                    m.external_id
                FROM news_signals ns
                JOIN markets m ON m.id = ns.market_id
                WHERE ns.created_at >= NOW() - interval '24 hours'
                  AND m.is_active = TRUE
                  AND m.close_time > NOW() + interval '1 day'
                  AND NOT EXISTS (
                      SELECT 1 FROM positions p
                      WHERE p.market_id = ns.market_id
                        AND p.status IN ('open', 'pending_close')
                  )
                ORDER BY ns.market_id, ns.relevance_score DESC
            )
            SELECT *,
                CASE WHEN yes_bid IS NOT NULL AND yes_ask IS NOT NULL
                     THEN (yes_bid + yes_ask) / 2.0
                     ELSE NULL END AS yes_mid,
                EXTRACT(DAY FROM close_time - NOW()) AS days_to_close
            FROM ranked
            WHERE relevance_score >= 0.3
            ORDER BY relevance_score DESC
            LIMIT $1
            """,
            limit,
        )
    results = []
    for r in rows:
        d = dict(r)
        mid = float(d["yes_mid"]) if d.get("yes_mid") else None
        rel = float(d.get("relevance_score") or 0)
        sent = float(d.get("sentiment_score") or 0)
        confidence = min(0.65, 0.35 + rel * 0.20 + abs(sent) * 0.15) if mid else 0
        edge = (confidence - mid) if mid and mid > 0 else 0
        results.append({
            "market_id": str(d["market_id"]),
            "external_id": d["external_id"],
            "title": d["title"],
            "exchange": d["exchange"],
            "category": d.get("category"),
            "yes_mid": round(float(mid), 4) if mid else None,
            "confidence": round(confidence, 4),
            "edge": round(edge, 4),
            "signal_type": "news",
            "signal_headline": d.get("headline"),
            "signal_source": d.get("source"),
            "relevance": round(rel, 4),
            "sentiment": round(sent, 4),
            "volume_24h": round(float(d["volume_24h_usd"]), 2) if d.get("volume_24h_usd") else None,
            "days_to_close": round(float(d["days_to_close"]), 1) if d.get("days_to_close") else None,
        })
    results.sort(key=lambda x: x["edge"], reverse=True)
    return results


@router.get("/decisions")
async def decision_log(
    hours: int = Query(24, le=168),
    limit: int = Query(100, le=500),
    db: Database = Depends(get_db),
) -> list[dict]:
    async with db._pool.acquire() as conn:
        h = str(hours)
        accepted = await conn.fetch(
            """
            SELECT
                p.opened_at AS ts,
                p.exchange::text,
                m.title AS market_title,
                'accepted' AS decision,
                p.signal_type::text,
                p.side,
                p.cost_basis_usd AS size_usd,
                p.avg_entry_price AS price,
                NULL AS gate,
                NULL AS block_reason
            FROM positions p
            JOIN markets m ON m.id = p.market_id
            WHERE p.opened_at >= NOW() - ($1 || ' hours')::interval
            ORDER BY p.opened_at DESC
            LIMIT $2
            """,
            h, limit,
        )
        rejected = await conn.fetch(
            """
            SELECT
                bt.created_at AS ts,
                bt.exchange::text,
                COALESCE(m.title, bt.external_market_id) AS market_title,
                'rejected' AS decision,
                bt.signal_type::text,
                bt.side,
                (bt.proposed_contracts * bt.proposed_price) AS size_usd,
                bt.proposed_price AS price,
                bt.block_gate AS gate,
                bt.block_reason AS block_reason
            FROM blocked_trades bt
            LEFT JOIN markets m ON m.id = bt.market_id
            WHERE bt.created_at >= NOW() - ($1 || ' hours')::interval
            ORDER BY bt.created_at DESC
            LIMIT $2
            """,
            h, limit,
        )
    combined = [dict(r) for r in accepted] + [dict(r) for r in rejected]
    combined.sort(key=lambda x: str(x.get("ts", "")), reverse=True)
    return combined[:limit]


@router.get("/decisions/live")
async def decisions_live(
    limit: int = Query(500, le=2000),
    exchange: Optional[str] = Query(None),
    decision: Optional[str] = Query(None),
    signal_type: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    db: Database = Depends(get_db),
) -> list[dict]:
    rows = await db.get_evaluation_decisions(
        limit=limit,
        exchange=exchange,
        decision=decision,
        signal_type=signal_type,
        reason_prefix=reason,
    )
    for r in rows:
        for k, v in r.items():
            if isinstance(v, UUID):
                r[k] = str(v)
    return rows


@router.get("/decisions/summary")
async def decisions_summary(
    db: Database = Depends(get_db),
) -> dict:
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                substring(reason from '^[^:]+') AS reason_bucket,
                exchange,
                decision,
                count(*) AS cnt
            FROM evaluation_decisions
            WHERE created_at >= NOW() - interval '24 hours'
            GROUP BY 1, 2, 3
            ORDER BY cnt DESC
            """
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM evaluation_decisions WHERE created_at >= NOW() - interval '24 hours'"
        )
    buckets: list[dict] = []
    for r in rows:
        buckets.append({
            "reason": r["reason_bucket"],
            "exchange": r["exchange"],
            "decision": r["decision"],
            "count": r["cnt"],
        })
    return {"total": total, "buckets": buckets}


@router.get("/exposure")
async def category_exposure(
    db: Database = Depends(get_db),
) -> list[dict]:
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(m.category, 'unknown') AS category,
                m.exchange::text AS exchange,
                COUNT(*) AS positions_count,
                COALESCE(SUM(p.cost_basis_usd), 0) AS exposure_usd,
                COALESCE(SUM(p.unrealized_pnl), 0) AS unrealized_pnl
            FROM positions p
            JOIN markets m ON m.id = p.market_id
            WHERE p.status IN ('open', 'pending_close')
            GROUP BY m.category, m.exchange
            ORDER BY exposure_usd DESC
            """
        )
    return [dict(r) for r in rows]
