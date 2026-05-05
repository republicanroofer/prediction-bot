from __future__ import annotations

import logging
from decimal import Decimal

from backend.alerts.telegram import get_alerter
from backend.config.settings import get_settings
from backend.db.database import Database

logger = logging.getLogger(__name__)


async def send_daily_recap(db: Database) -> None:
    alerter = get_alerter()
    if not alerter._enabled:
        return

    cfg = get_settings()

    try:
        async with db._pool.acquire() as conn:
            # Closed positions in last 24h
            closed_rows = await conn.fetch(
                """
                SELECT market_id, external_market_id, side, signal_type,
                       cost_basis_usd, realized_pnl, close_reason
                FROM positions
                WHERE status = 'closed'
                  AND closed_at >= NOW() - INTERVAL '24 hours'
                ORDER BY realized_pnl DESC
                """
            )

            # Open positions
            open_count = await conn.fetchval(
                "SELECT COUNT(*) FROM positions WHERE status IN ('pending','open')"
            )
            exposure = await conn.fetchval(
                "SELECT COALESCE(SUM(cost_basis_usd), 0) FROM positions"
                " WHERE status IN ('pending','open')"
            )
            unrealized = await conn.fetchval(
                "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions"
                " WHERE status IN ('pending','open')"
            )

            # Funnel: markets scanned, signals, trades
            markets_scanned = await conn.fetchval(
                "SELECT COUNT(*) FROM markets WHERE is_active = TRUE"
            )
            signals_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM news_signals"
                " WHERE created_at >= NOW() - INTERVAL '24 hours'"
            )
            trades_executed = await conn.fetchval(
                "SELECT COUNT(*) FROM orders"
                " WHERE status = 'filled'"
                "   AND created_at >= NOW() - INTERVAL '24 hours'"
            )

            # Whale mirrors
            whale_count = await conn.fetchval(
                "SELECT COUNT(*) FROM positions"
                " WHERE signal_type = 'whale_mirror'"
                "   AND opened_at >= NOW() - INTERVAL '24 hours'"
            )

            # LLM stats
            llm_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE response_action = 'abstain') AS abstains,
                       COALESCE(SUM(cost_usd), 0) AS cost
                FROM llm_queries
                WHERE created_at >= NOW() - INTERVAL '24 hours'
                """
            )
            llm_total = int(llm_row["total"])
            llm_abstains = int(llm_row["abstains"])
            llm_cost = float(llm_row["cost"])
            abstain_rate = (llm_abstains / llm_total * 100) if llm_total > 0 else 0

            # Market titles for top winners/losers
            market_titles: dict[str, str] = {}
            if closed_rows:
                market_ids = list({r["market_id"] for r in closed_rows})
                title_rows = await conn.fetch(
                    "SELECT id, title FROM markets WHERE id = ANY($1::uuid[])",
                    market_ids,
                )
                market_titles = {str(r["id"]): r["title"] for r in title_rows}

        # Compute P&L from closed positions
        realized_pnl = sum(float(r["realized_pnl"] or 0) for r in closed_rows)
        wins = [r for r in closed_rows if float(r["realized_pnl"] or 0) > 0]
        losses = [r for r in closed_rows if float(r["realized_pnl"] or 0) < 0]
        total_closed = len(closed_rows)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_closed * 100) if total_closed > 0 else 0

        # Top 3 winners and losers
        sorted_by_pnl = sorted(closed_rows, key=lambda r: float(r["realized_pnl"] or 0), reverse=True)
        top_winners = sorted_by_pnl[:3]
        top_losers = sorted_by_pnl[-3:][::-1] if len(sorted_by_pnl) > 0 else []
        top_losers = [r for r in top_losers if float(r["realized_pnl"] or 0) < 0]

        def _clean(text: str) -> str:
            for ch in "*_`[":
                text = text.replace(ch, "")
            return text

        def fmt_position(r: dict) -> str:
            pnl = float(r["realized_pnl"] or 0)
            sign = "+" if pnl >= 0 else ""
            title = market_titles.get(str(r["market_id"]), r["external_market_id"][:25])
            return f"  {sign}${pnl:.2f} - {_clean(title[:40])}"

        winners_text = "\n".join(fmt_position(r) for r in top_winners) if top_winners else "  None"
        losers_text = "\n".join(fmt_position(r) for r in top_losers) if top_losers else "  None"

        pnl_emoji = "📈" if realized_pnl >= 0 else "📉"
        pnl_sign = "+" if realized_pnl >= 0 else ""

        text = (
            f"📊 *Daily Trading Recap*\n"
            f"\n"
            f"{pnl_emoji} *P&L*\n"
            f"  Realized: *{pnl_sign}${realized_pnl:.2f}*\n"
            f"  Unrealized: ${float(unrealized):.2f}\n"
            f"\n"
            f"🎯 *Win Rate*\n"
            f"  {win_rate:.1f}% - {win_count}W / {loss_count}L ({total_closed} closed)\n"
            f"\n"
            f"🔬 *Pipeline*\n"
            f"  Markets scanned: {markets_scanned:,}\n"
            f"  Signals: {signals_24h:,}\n"
            f"  Trades executed: {trades_executed}\n"
            f"\n"
            f"🏆 *Top Winners*\n"
            f"{winners_text}\n"
            f"\n"
            f"💀 *Top Losers*\n"
            f"{losers_text}\n"
            f"\n"
            f"🐋 Whale mirrors: {whale_count}\n"
            f"🤖 LLM: {llm_total} calls, {abstain_rate:.0f}% abstain, ${llm_cost:.2f} spent\n"
            f"📦 Open positions: {open_count} (${float(exposure):,.0f} exposure)"
        )

        await alerter._send(alerter._chat_id, text)
        logger.info("Daily recap sent to Telegram")

    except Exception as exc:
        logger.error("Failed to send daily recap: %s", exc)
