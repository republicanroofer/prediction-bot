from __future__ import annotations

"""
TelegramAlerter — sends structured notifications to Telegram.

Two channels:
  - main chat_id     : trade events (open/close, P&L, whale mirrors)
  - error_chat_id    : errors, drawdown warnings, budget alerts

Message types:
  - position_opened  : exchange, side, size, entry price, confidence
  - position_closed  : realized P&L, reason (stop_loss/take_profit/resolution/etc.)
  - whale_mirrored   : address, market, size
  - drawdown_alert   : current drawdown %, threshold
  - error            : component, message
  - budget_alert     : LLM daily spend, limit

Rate limiting: Telegram allows ~30 messages/second per bot but enforces
per-chat flood control.  We apply a 1-second minimum between consecutive
messages to the same chat to avoid 429s without complex queue management.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from backend.config.settings import get_settings

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_MIN_INTERVAL_S = 1.0  # minimum seconds between messages to same chat


class TelegramAlerter:
    """
    Async Telegram notifier.  Safe to use from multiple coroutines; internal
    lock prevents concurrent requests to the same chat.
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._token = cfg.telegram_bot_token
        self._chat_id = cfg.telegram_chat_id
        self._error_chat_id = cfg.telegram_error_chat_id or cfg.telegram_chat_id
        self._enabled = bool(self._token and self._chat_id)
        self._lock = asyncio.Lock()
        self._last_sent: dict[str, float] = {}

    # ── High-level events ─────────────────────────────────────────────────────

    async def position_opened(
        self,
        *,
        exchange: str,
        market_title: str,
        side: str,
        size_usd: float,
        entry_price: float,
        confidence: float,
        signal_type: str,
        mode: str,
    ) -> None:
        emoji = "🟢" if side.lower() == "yes" else "🔴"
        mode_tag = f"[{mode.upper()}] " if mode != "live" else ""
        text = (
            f"{mode_tag}{emoji} *Position Opened* — {exchange.upper()}\n"
            f"Market: {_esc(market_title[:60])}\n"
            f"Side: {side.upper()}  |  Size: ${size_usd:.2f}\n"
            f"Entry: {entry_price:.3f}  |  Conf: {confidence:.0%}\n"
            f"Signal: `{signal_type}`"
        )
        await self._send(self._chat_id, text)

    async def position_closed(
        self,
        *,
        exchange: str,
        market_title: str,
        side: str,
        realized_pnl: float,
        close_reason: str,
        mode: str,
    ) -> None:
        pnl_emoji = "✅" if realized_pnl >= 0 else "❌"
        sign = "+" if realized_pnl >= 0 else ""
        mode_tag = f"[{mode.upper()}] " if mode != "live" else ""
        text = (
            f"{mode_tag}{pnl_emoji} *Position Closed* — {exchange.upper()}\n"
            f"Market: {_esc(market_title[:60])}\n"
            f"Side: {side.upper()}  |  Reason: `{close_reason}`\n"
            f"P&L: *{sign}${realized_pnl:.2f}*"
        )
        await self._send(self._chat_id, text)

    async def whale_mirrored(
        self,
        *,
        address: str,
        market_title: str,
        side: str,
        size_usd: float,
        whale_score: float,
        mode: str,
    ) -> None:
        mode_tag = f"[{mode.upper()}] " if mode != "live" else ""
        text = (
            f"{mode_tag}🐋 *Whale Mirror*\n"
            f"Address: `{address[:10]}…`  |  Score: {whale_score:.0f}\n"
            f"Market: {_esc(market_title[:60])}\n"
            f"Side: {side.upper()}  |  Size: ${size_usd:.2f}"
        )
        await self._send(self._chat_id, text)

    async def drawdown_alert(
        self,
        *,
        current_drawdown_pct: float,
        threshold_pct: float,
        portfolio_usd: float,
    ) -> None:
        text = (
            f"⚠️ *Drawdown Warning*\n"
            f"Current drawdown: *{current_drawdown_pct:.1%}*\n"
            f"Threshold: {threshold_pct:.1%}  |  Portfolio: ${portfolio_usd:,.0f}"
        )
        await self._send(self._error_chat_id, text)

    async def budget_alert(
        self,
        *,
        spent_usd: float,
        limit_usd: float,
    ) -> None:
        text = (
            f"💸 *LLM Budget Alert*\n"
            f"Daily spend: ${spent_usd:.2f} / ${limit_usd:.2f}\n"
            f"Further LLM calls paused until midnight UTC."
        )
        await self._send(self._error_chat_id, text)

    async def error(
        self,
        *,
        component: str,
        message: str,
    ) -> None:
        text = (
            f"🚨 *Bot Error* — `{_esc(component)}`\n"
            f"{_esc(message[:400])}"
        )
        await self._send(self._error_chat_id, text)

    async def info(self, message: str) -> None:
        await self._send(self._chat_id, f"ℹ️ {_esc(message)}")

    # ── Low-level send ────────────────────────────────────────────────────────

    async def _send(self, chat_id: str, text: str) -> None:
        if not self._enabled:
            logger.debug("Telegram not configured — suppressed: %s", text[:80])
            return

        async with self._lock:
            now = time.monotonic()
            since = now - self._last_sent.get(chat_id, 0.0)
            if since < _MIN_INTERVAL_S:
                await asyncio.sleep(_MIN_INTERVAL_S - since)

            url = _API_BASE.format(token=self._token)
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                        logger.warning("Telegram 429 — retry after %ds", retry_after)
                        await asyncio.sleep(retry_after)
                        resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                self._last_sent[chat_id] = time.monotonic()
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)


def _esc(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Module-level singleton ────────────────────────────────────────────────────

_alerter: Optional[TelegramAlerter] = None


def get_alerter() -> TelegramAlerter:
    global _alerter
    if _alerter is None:
        _alerter = TelegramAlerter()
    return _alerter
