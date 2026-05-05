from __future__ import annotations

"""
Market scanner — runs every 60 seconds.

On each tick:
  1. Fetch all open Kalshi events and Polymarket markets.
  2. Upsert prices into the markets table.
  3. For each market that passes basic filters:
       - category score gate (skip blocked categories)
       - volume / days-to-close filters
       - no existing open position
  4. Check pre-computed signals (whale mirror queue + recent news signals).
  5. Compute quarter-Kelly position size against available capital.
  6. Run the 4-gate PortfolioEnforcer check.
  7. In paper mode: create DB records only.
     In live mode: send to the exchange executor.
"""

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import anthropic

from backend.clients.kalshi_client import KalshiClient, KalshiAPIError
from backend.clients.polymarket_client import GammaClient, PolymarketClobClient
from backend.config.settings import ActiveExchange, TradingMode, get_settings
from backend.db.database import Database
from backend.db.models import (
    BlockedTradeInsert,
    CategoryScore,
    Exchange,
    LLMQueryInsert,
    Market,
    OrderCreate,
    OrderType,
    PositionCreate,
    SignalType,
)

logger = logging.getLogger(__name__)

# Minimum required composite category score to allow any trade
_BLOCK_THRESHOLD = 30.0
# Seconds of news signal recency required to count as "actionable"
_NEWS_SIGNAL_RECENCY_HOURS = 24
# Hard cap on new positions opened in a single scan tick (prevents mass-entry bursts)
_MAX_NEW_POSITIONS_PER_SCAN = 3
# Hard cap on new positions opened per calendar day (resets at midnight UTC)
_MAX_NEW_POSITIONS_PER_DAY = 10
# Minimum 24h volume to justify an LLM API call — filters illiquid markets
_LLM_MIN_VOLUME_USD = 5_000.0
# LLM must exceed this confidence AND edge to trade (raised from 0.65/0.10)
_LLM_MIN_CONFIDENCE = 0.75
_LLM_MIN_EDGE = 0.15


class ScannerWorker:
    """
    Long-running asyncio task. Call run() to start the loop; cancel the task
    or set the stop event to shut it down.
    """

    def __init__(
        self,
        db: Database,
        kalshi: Optional[KalshiClient],
        gamma: Optional[GammaClient],
        clob: Optional["PolymarketClobClient"] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = db
        self._kalshi = kalshi
        self._gamma = gamma
        self._clob = clob
        self._stop = stop_event or asyncio.Event()
        self._cfg = get_settings()
        self._new_positions_this_scan: int = 0
        self._scan_exposure_usd: float = 0.0
        # LLM evaluation cache: market_id → (expires_at, signal_tuple_or_None)
        # None means "LLM evaluated and abstained" — don't re-call for 30 min.
        self._llm_cache: dict[UUID, tuple[datetime, Optional[tuple]]] = {}
        self._llm_client: Optional[anthropic.AsyncAnthropic] = None
        # Set True on account-level errors (no credits, invalid key) to stop retrying
        self._llm_account_error: bool = False
        # Historical win rates cached per scan (refreshed every tick)
        self._hist_rates: dict[tuple[str, str, str], dict] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("Scanner worker started (interval=%ds)", self._cfg.scan_interval_s)
        while not self._stop.is_set():
            try:
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scanner error — will retry next tick")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._cfg.scan_interval_s
                )
            except asyncio.TimeoutError:
                pass
        logger.info("Scanner worker stopped")

    # ── Main scan ─────────────────────────────────────────────────────────────

    async def _scan(self) -> None:
        tasks = []
        if self._kalshi and self._cfg.active_exchange in (
            ActiveExchange.KALSHI, ActiveExchange.BOTH
        ):
            tasks.append(self._scan_kalshi())
        if self._gamma and self._cfg.active_exchange in (
            ActiveExchange.POLYMARKET, ActiveExchange.BOTH
        ):
            tasks.append(self._scan_polymarket())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # After upserts, evaluate signals on all active markets
        await self._evaluate_all_markets()
        # Cross-exchange arbitrage (runs after market eval so it can use position cap)
        await self._scan_arbitrage()

    # ── Kalshi ingestion ──────────────────────────────────────────────────────

    async def _scan_kalshi(self) -> None:
        assert self._kalshi
        try:
            events = await self._kalshi.get_all_events(status="open")
        except KalshiAPIError as exc:
            logger.warning("Kalshi event fetch failed: %s", exc)
            return

        upserted = skipped = 0
        for event in events:
            for market in event.get("markets", []):
                # Skip new zero-volume markets — they won't pass the scanner
                # filter and would just bloat the DB indefinitely.
                vol = _kalshi_volume_usd(market)
                if not vol or vol < 1.0:
                    skipped += 1
                    continue
                await self._upsert_kalshi_market(event, market)
                upserted += 1

        logger.debug(
            "Kalshi: upserted %d markets (%d zero-vol skipped) from %d events",
            upserted, skipped, len(events),
        )

    async def _upsert_kalshi_market(self, event: dict, market: dict) -> None:
        ticker = market.get("ticker", "")
        if not ticker:
            return

        # Price normalisation: Kalshi API returns dollar amounts in new format
        yes_bid = _safe_float(
            market.get("yes_bid_dollars") or market.get("yes_bid")
        )
        yes_ask = _safe_float(
            market.get("yes_ask_dollars") or market.get("yes_ask")
        )
        # Legacy cent-format: values >1 are cents, divide by 100
        if yes_bid and yes_bid > 1:
            yes_bid /= 100
        if yes_ask and yes_ask > 1:
            yes_ask /= 100

        no_bid = round(1 - yes_ask, 4) if yes_ask is not None else None
        no_ask = round(1 - yes_bid, 4) if yes_bid is not None else None

        close_time: Optional[datetime] = None
        if raw_close := market.get("close_time"):
            try:
                close_time = datetime.fromisoformat(
                    raw_close.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        from backend.db.models import MarketUpsert

        await self._db.upsert_market(
            MarketUpsert(
                exchange=Exchange.KALSHI,
                external_id=ticker,
                event_ticker=event.get("event_ticker"),
                title=market.get("title") or event.get("title") or ticker,
                category=_normalise_category(
                    event.get("category") or market.get("category"),
                    title=market.get("title") or event.get("title") or "",
                ),
                sub_category=event.get("sub_title"),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                last_price=_safe_float(market.get("last_price_dollars") or market.get("last_price")),
                volume_24h_usd=_kalshi_volume_usd(market),
                volume_total_usd=_kalshi_volume_usd(market, field="volume_fp"),
                open_interest=_safe_float(market.get("open_interest")),
                close_time=close_time,
                is_active=market.get("status") == "active",
                raw={"event_ticker": event.get("event_ticker"), "status": market.get("status")},
            )
        )

    # ── Polymarket ingestion ──────────────────────────────────────────────────

    async def _scan_polymarket(self) -> None:
        assert self._gamma
        try:
            raw_markets = await self._gamma.get_clob_tradable_markets()
        except Exception as exc:
            logger.warning("Polymarket market fetch failed: %s", exc)
            return

        upserted = 0
        for raw in raw_markets:
            await self._upsert_polymarket_market(raw)
            upserted += 1

        logger.debug("Polymarket: upserted %d markets", upserted)

    async def _upsert_polymarket_market(self, raw: dict) -> None:
        condition_id = raw.get("conditionId") or raw.get("id", "")
        if not condition_id:
            return

        from backend.clients.polymarket_client import GammaClient as _G

        token_yes, token_no = _G.parse_token_ids(raw)
        yes_price, no_price = _G.parse_outcome_prices(raw)

        close_time: Optional[datetime] = None
        if raw_close := raw.get("endDateIso") or raw.get("closedTime"):
            try:
                close_time = datetime.fromisoformat(
                    str(raw_close).replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        # Spread is not available from Gamma directly; treat price as mid
        spread = 0.02
        yes_bid = max(0.01, yes_price - spread / 2)
        yes_ask = min(0.99, yes_price + spread / 2)

        from backend.db.models import MarketUpsert

        await self._db.upsert_market(
            MarketUpsert(
                exchange=Exchange.POLYMARKET,
                external_id=condition_id,
                token_id_yes=token_yes,
                token_id_no=token_no,
                title=raw.get("question") or raw.get("title") or condition_id,
                category=_normalise_category(
                    raw.get("category") or raw.get("type"),
                    title=raw.get("question") or raw.get("title") or "",
                ),
                yes_bid=round(yes_bid, 4),
                yes_ask=round(yes_ask, 4),
                no_bid=round(1 - yes_ask, 4),
                no_ask=round(1 - yes_bid, 4),
                last_price=yes_price,
                volume_24h_usd=_safe_float(raw.get("volume24hr")),
                volume_total_usd=_safe_float(raw.get("volume")),
                liquidity_usd=_safe_float(raw.get("liquidity")),
                close_time=close_time,
                is_active=bool(raw.get("active")),
                raw={"slug": raw.get("marketSlug"), "neg_risk": raw.get("negRisk")},
            )
        )

    # ── Signal evaluation ─────────────────────────────────────────────────────

    async def _evaluate_all_markets(self) -> None:
        markets = await self._db.get_active_markets(
            min_volume_usd=self._cfg.min_market_volume_usd
        )
        cfg = self._cfg
        self._new_positions_this_scan = 0
        self._scan_exposure_usd = 0.0

        # Check daily position cap before evaluating any markets
        try:
            daily_count = await self._db.count_positions_opened_today()
        except Exception:
            daily_count = 0
        if daily_count >= _MAX_NEW_POSITIONS_PER_DAY:
            logger.info(
                "Daily position cap reached (%d/%d) — skipping evaluation",
                daily_count, _MAX_NEW_POSITIONS_PER_DAY,
            )
            return

        # Refresh historical win rates for the pattern scorer
        try:
            rates = await self._db.get_historical_win_rates(min_trades=5)
            self._hist_rates = {
                (r["category"], r["exchange"], r["price_range"]): r
                for r in rates
            }
        except Exception:
            pass

        for market in markets:
            if self._new_positions_this_scan >= _MAX_NEW_POSITIONS_PER_SCAN:
                logger.info(
                    "Scan cap reached (%d new positions) — skipping remaining markets",
                    _MAX_NEW_POSITIONS_PER_SCAN,
                )
                break
            try:
                await self._evaluate_market(market)
            except Exception:
                logger.exception(
                    "Error evaluating market %s (%s)", market.external_id, market.title[:40]
                )

    async def _evaluate_market(self, market: Market) -> None:
        cfg = self._cfg
        mkt_base = {
            "market_id": market.id,
            "external_market_id": market.external_id,
            "market_title": market.title[:120],
            "exchange": market.exchange.value,
        }

        # ── Volume filter (always required) ───────────────────────────────────
        vol = float(market.volume_24h_usd or 0)
        if vol < cfg.min_market_volume_usd:
            await self._log_decision(mkt_base, "rejected", f"volume too low: ${vol:.0f} < ${cfg.min_market_volume_usd:.0f}")
            return

        # ── Existing position check ───────────────────────────────────────────
        existing = await self._db.get_positions_for_market(market.id)
        if existing:
            await self._log_decision(mkt_base, "rejected", "already positioned")
            return

        # ── Whale mirror signal (checked before expiry filter — short-dated OK) ─
        whale_signal = await self._detect_whale_signal(market)

        # ── Expiry filters (relaxed for whale signals) ────────────────────────
        days = market.days_to_close
        if whale_signal is None:
            if days is None:
                await self._log_decision(mkt_base, "rejected", "no close date")
                return
            if days < cfg.min_days_to_expiry:
                await self._log_decision(mkt_base, "rejected", f"expires too soon: {days:.1f}d < {cfg.min_days_to_expiry}d")
                return
            if days > cfg.max_days_to_expiry:
                await self._log_decision(mkt_base, "rejected", f"expires too far: {days:.0f}d > {cfg.max_days_to_expiry}d")
                return
        else:
            # Whale signal: only reject if market already closed
            if days is not None and days <= 0:
                await self._log_decision(mkt_base, "rejected", f"market already closed (whale signal)")
                return

        # ── Category score gate ───────────────────────────────────────────────
        cat_score = await self._db.get_category_score(
            market.exchange, market.category or "unknown"
        )
        if cat_score and cat_score.is_blocked:
            await self._log_decision(
                mkt_base, "rejected",
                f"category blocked: {market.category}",
            )
            return

        # ── Signal detection (use whale signal if found, else check others) ───
        signal = whale_signal or await self._detect_other_signals(market)
        if signal is None:
            await self._log_decision(mkt_base, "rejected", "no signal")
            return

        signal_type, side, confidence, whale_meta = signal
        mkt_base["signal_type"] = signal_type.value
        mkt_base["side"] = side
        mkt_base["confidence"] = round(confidence, 4)

        # ── Mid-price ─────────────────────────────────────────────────────────
        entry_price = market.yes_mid if side == "yes" else market.no_mid
        if entry_price is None or not (0.05 <= entry_price <= 0.95):
            await self._log_decision(
                mkt_base, "rejected",
                f"price too extreme: {entry_price}",
            )
            return

        mkt_base["entry_price"] = round(entry_price, 6)
        edge = confidence - entry_price
        mkt_base["edge"] = round(edge, 6)

        # ── Kelly position sizing ─────────────────────────────────────────────
        portfolio_value = await self._estimate_portfolio_value()
        if portfolio_value <= 0:
            await self._log_decision(mkt_base, "rejected", "portfolio value <= 0")
            return

        size_usd = self._kelly_size(
            confidence=confidence,
            entry_price=entry_price,
            portfolio_usd=portfolio_value,
            cat_score=cat_score,
        )
        mkt_base["kelly_size_usd"] = round(size_usd, 2)

        if edge <= 0:
            await self._log_decision(
                mkt_base, "rejected",
                f"edge too low: {edge*100:.1f}%",
            )
            return

        if size_usd < 1.0:
            await self._log_decision(
                mkt_base, "rejected",
                f"kelly size too small: ${size_usd:.2f}",
            )
            return

        contracts = size_usd / entry_price

        # ── Portfolio enforcer (4 gates) ──────────────────────────────────────
        block = await self._portfolio_enforcer_check(
            market=market,
            side=side,
            contracts=contracts,
            entry_price=entry_price,
            size_usd=size_usd,
            signal_type=signal_type,
            portfolio_usd=portfolio_value,
            cat_score=cat_score,
        )
        if block:
            gate, reason = block
            await self._log_decision(
                mkt_base, "rejected",
                f"risk gate [{gate}]: {reason}",
            )
            await self._db.insert_blocked_trade(
                BlockedTradeInsert(
                    exchange=market.exchange,
                    market_id=market.id,
                    external_market_id=market.external_id,
                    side=side,
                    proposed_contracts=contracts,
                    proposed_price=entry_price,
                    signal_type=signal_type,
                    block_gate=gate,
                    block_reason=reason,
                    mode=TradingMode(cfg.trading_mode.value),
                )
            )
            return

        # ── Accepted — build reason string ────────────────────────────────────
        signals_desc = signal_type.value.replace("_", " ")
        if whale_meta.get("whale_score"):
            signals_desc += f" (score {whale_meta['whale_score']:.0f})"
        accept_reason = (
            f"signal fired: {signals_desc} | "
            f"edge {edge*100:.1f}% | "
            f"kelly ${size_usd:.2f} | "
            f"conf {confidence*100:.0f}%"
        )
        await self._log_decision(mkt_base, "accepted", accept_reason)

        # ── Create position + order records ───────────────────────────────────
        await self._open_position(
            market=market,
            side=side,
            contracts=contracts,
            entry_price=entry_price,
            size_usd=size_usd,
            signal_type=signal_type,
            confidence=confidence,
            whale_meta=whale_meta,
        )

    async def _log_decision(
        self, base: dict, decision: str, reason: str,
    ) -> None:
        try:
            await self._db.insert_evaluation_decision({
                **base,
                "decision": decision,
                "reason": reason,
            })
        except Exception:
            logger.debug("Failed to log evaluation decision", exc_info=True)


    async def _detect_whale_signal(
        self, market: Market
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        """Check for queued whale mirror trades matching this market."""
        if market.exchange != Exchange.POLYMARKET:
            return None
        queued = await self._db.get_queued_mirror_trades(
            min_delay_s=self._cfg.whale_mirror_delay_s,
            min_score=self._cfg.whale_min_score,
        )
        for trade in queued:
            if (
                trade.get("condition_id") == market.external_id
                or trade.get("market_id") == market.id
            ):
                side = trade["maker_direction"]
                return (
                    SignalType.WHALE_MIRROR,
                    "yes" if side == "buy" else "no",
                    min(0.75, 0.5 + float(trade.get("composite_score", 60)) / 200),
                    {
                        "whale_address": trade["maker_address"],
                        "whale_score": float(trade.get("composite_score", 0)),
                        "whale_trade_id": trade["id"],
                        "usd_amount": float(trade["usd_amount"]),
                    },
                )
        return None

    async def _detect_other_signals(
        self, market: Market
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        """Check structural signals before falling back to LLM eval."""
        # Fetch recent news signals once for reuse across checks
        recent_signals = await self._db.get_recent_signals_for_market(
            market.id, hours=_NEWS_SIGNAL_RECENCY_HOURS
        )

        # ── Order book imbalance (Polymarket only, requires CLOB) ────────────
        ob_signal = await self._detect_order_book_imbalance(market)
        if ob_signal:
            return ob_signal

        # ── Late money signal ────────────────────────────────────────────────
        lm_signal = self._detect_late_money(market)
        if lm_signal:
            return lm_signal

        # ── Social/news sentiment aggregation ─────────────────────────────────
        ss_signal = self._detect_sentiment_signal(market, recent_signals)
        if ss_signal:
            return ss_signal

        # ── Historical pattern ────────────────────────────────────────────────
        hp_signal = self._detect_historical_pattern(market)
        if hp_signal:
            return hp_signal

        # ── LLM evaluation (fallback) ────────────────────────────────────────
        news_hint = None
        if recent_signals:
            best = recent_signals[0]
            if float(best.relevance_score or 0) >= 0.3:
                news_hint = best.headline

        return await self._llm_evaluate_market(market, news_hint=news_hint)

    # ── Order book imbalance ──────────────────────────────────────────────────

    async def _detect_order_book_imbalance(
        self, market: Market
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        if market.exchange != Exchange.POLYMARKET or not self._clob:
            return None

        vol_24h = float(market.volume_24h_usd or 0)
        if vol_24h < _LLM_MIN_VOLUME_USD:
            return None

        yes_token = market.external_id
        if not yes_token:
            return None

        try:
            book = await self._clob.get_orderbook(yes_token)
        except Exception:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None

        bid_depth = sum(float(b.get("size", 0)) for b in bids[:10])
        ask_depth = sum(float(a.get("size", 0)) for a in asks[:10])

        if bid_depth == 0 or ask_depth == 0:
            return None

        ratio = bid_depth / ask_depth
        inv_ratio = ask_depth / bid_depth

        _OB_MIN_RATIO = 3.0

        if ratio >= _OB_MIN_RATIO:
            side = "yes"
            imbalance = ratio
            confidence = min(0.75, 0.5 + (ratio - _OB_MIN_RATIO) * 0.05)
        elif inv_ratio >= _OB_MIN_RATIO:
            side = "no"
            imbalance = inv_ratio
            confidence = min(0.75, 0.5 + (inv_ratio - _OB_MIN_RATIO) * 0.05)
        else:
            return None

        signal_confidence = confidence
        meta = {
            "bid_depth": round(bid_depth, 2),
            "ask_depth": round(ask_depth, 2),
            "imbalance_ratio": round(imbalance, 2),
        }

        logger.info(
            "ORDER_BOOK [polymarket] %s | %s imbalance=%.1f:1 bids=%.0f asks=%.0f",
            market.title[:40], side, imbalance, bid_depth, ask_depth,
        )

        return (SignalType.ORDER_BOOK, side, signal_confidence, meta)

    # ── Late money signal ─────────────────────────────────────────────────────

    def _detect_late_money(
        self, market: Market
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        days = market.days_to_close
        if days is None or days > 2.0 or days < 0.1:
            return None

        vol_24h = float(market.volume_24h_usd or 0)
        vol_total = float(market.volume_total_usd or 0)

        if vol_total < 1000 or vol_24h < 500:
            return None

        avg_daily_vol = vol_total / max(1, 30)
        vol_spike = vol_24h / max(1, avg_daily_vol)

        if vol_spike < 2.0:
            return None

        mid = market.yes_mid
        if mid is None or 0.40 <= mid <= 0.60:
            return None

        side = "yes" if mid >= 0.60 else "no"
        confidence = min(0.80, 0.55 + (vol_spike - 2.0) * 0.03 + abs(mid - 0.5) * 0.3)
        signal_confidence = confidence if side == "yes" else (1.0 - (1.0 - confidence))

        meta = {
            "vol_spike": round(vol_spike, 2),
            "vol_24h": round(vol_24h, 0),
            "days_to_close": round(days, 2),
        }

        logger.info(
            "LATE_MONEY [%s] %s | %s spike=%.1fx vol=$%.0f days=%.1f",
            market.exchange.value, market.title[:40], side,
            vol_spike, vol_24h, days,
        )

        return (SignalType.LATE_MONEY, side, signal_confidence, meta)

    # ── Social sentiment signal ─────────────────────────────────────────────

    def _detect_sentiment_signal(
        self, market: Market, recent_signals: Optional[list],
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        if not recent_signals or len(recent_signals) < 3:
            return None

        sentiments = []
        for sig in recent_signals[:10]:
            s = float(sig.sentiment_score or 0)
            r = float(sig.relevance_score or 0)
            if r >= 0.3:
                sentiments.append(s)

        if len(sentiments) < 3:
            return None

        avg_sentiment = sum(sentiments) / len(sentiments)
        if abs(avg_sentiment) < 0.15:
            return None

        if avg_sentiment > 0.15:
            side = "yes"
            confidence = min(0.75, 0.50 + avg_sentiment * 0.5)
        else:
            side = "no"
            confidence = min(0.75, 0.50 + abs(avg_sentiment) * 0.5)

        signal_confidence = confidence if side == "yes" else (1.0 - (1.0 - confidence))

        meta = {
            "avg_sentiment": round(avg_sentiment, 4),
            "signal_count": len(sentiments),
            "headlines": [s.headline[:60] for s in recent_signals[:3]],
        }

        logger.info(
            "SOCIAL_SENT [%s] %s | %s avg_sent=%.2f from %d signals",
            market.exchange.value, market.title[:40], side,
            avg_sentiment, len(sentiments),
        )

        return (SignalType.SOCIAL_SENTIMENT, side, signal_confidence, meta)

    # ── Historical pattern scoring ────────────────────────────────────────────

    def _detect_historical_pattern(
        self, market: Market
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        if not self._hist_rates:
            return None

        mid = market.yes_mid
        if mid is None:
            return None

        price_range = "low" if mid < 0.3 else ("mid" if mid < 0.7 else "high")
        category = market.category or "unknown"
        exchange = market.exchange.value

        key = (category, exchange, price_range)
        hist = self._hist_rates.get(key)
        if not hist:
            return None

        total = int(hist["total"])
        wins = int(hist["wins"])
        win_rate = wins / total if total > 0 else 0

        if total < 10 or win_rate < 0.55:
            return None

        side = "yes" if mid >= 0.5 else "no"
        confidence = min(0.80, 0.50 + win_rate * 0.3)
        signal_confidence = confidence if side == "yes" else (1.0 - (1.0 - confidence))

        avg_pnl = float(hist["avg_pnl"])
        meta = {
            "hist_win_rate": round(win_rate, 4),
            "hist_total": total,
            "hist_avg_pnl": round(avg_pnl, 4),
            "price_range": price_range,
        }

        logger.info(
            "HIST_PATTERN [%s] %s | cat=%s range=%s wr=%.0f%% (%d trades)",
            exchange, market.title[:40], category, price_range,
            win_rate * 100, total,
        )

        return (SignalType.HISTORICAL_PATTERN, side, signal_confidence, meta)

    # ── Cross-market arbitrage ────────────────────────────────────────────────

    async def _scan_arbitrage(self) -> None:
        """Find markets listed on both exchanges with a >5% price gap."""
        pairs = await self._db.get_cross_exchange_pairs(
            min_gap=0.05, min_volume=200.0,
        )
        for pair in pairs:
            if self._new_positions_this_scan >= _MAX_NEW_POSITIONS_PER_SCAN:
                break

            k_mid = (float(pair["kb"] or 0) + float(pair["ka"] or 0)) / 2
            p_mid = (float(pair["pb"] or 0) + float(pair["pa"] or 0)) / 2
            gap = abs(k_mid - p_mid)

            if k_mid < p_mid:
                cheap_id, cheap_exchange, cheap_price = pair["kalshi_id"], Exchange.KALSHI, k_mid
            else:
                cheap_id, cheap_exchange, cheap_price = pair["poly_id"], Exchange.POLYMARKET, p_mid

            existing = await self._db.get_positions_for_market(cheap_id)
            if existing:
                continue

            market = await self._db.get_market(cheap_id)
            if not market:
                continue

            confidence = min(0.85, 0.5 + gap * 2)
            side = "yes" if cheap_price < 0.5 else "no"
            entry_price = market.yes_mid if side == "yes" else market.no_mid
            if entry_price is None or not (0.05 <= entry_price <= 0.95):
                continue

            signal_meta = {
                "gap_pct": round(gap * 100, 2),
                "kalshi_mid": round(k_mid, 4),
                "poly_mid": round(p_mid, 4),
                "cheap_exchange": cheap_exchange.value,
            }

            logger.info(
                "ARB [%s] %s | gap=%.1f%% K=%.2f P=%.2f → buy %s at %.2f",
                cheap_exchange.value, pair["title"][:40],
                gap * 100, k_mid, p_mid,
                side, entry_price,
            )

            mkt_base = {
                "market_id": market.id,
                "external_market_id": market.external_id,
                "market_title": market.title[:120],
                "exchange": market.exchange.value,
                "signal_type": SignalType.ARBITRAGE.value,
                "side": side,
                "confidence": round(confidence, 4),
                "entry_price": round(entry_price, 6),
                "edge": round(confidence - entry_price, 6),
            }

            edge = confidence - entry_price
            if edge <= 0:
                await self._log_decision(mkt_base, "rejected", f"arb edge too low: {edge*100:.1f}%")
                continue

            portfolio_value = await self._estimate_portfolio_value()
            if portfolio_value <= 0:
                continue

            cat_score = await self._db.get_category_score(
                market.exchange, market.category or "unknown"
            )

            size_usd = self._kelly_size(
                confidence=confidence,
                entry_price=entry_price,
                portfolio_usd=portfolio_value,
                cat_score=cat_score,
            )
            mkt_base["kelly_size_usd"] = round(size_usd, 2)

            if size_usd < 1.0:
                await self._log_decision(mkt_base, "rejected", f"arb kelly too small: ${size_usd:.2f}")
                continue

            contracts = size_usd / entry_price

            block = await self._portfolio_enforcer_check(
                market=market, side=side, contracts=contracts,
                entry_price=entry_price, size_usd=size_usd,
                signal_type=SignalType.ARBITRAGE,
                portfolio_usd=portfolio_value, cat_score=cat_score,
            )
            if block:
                gate, reason = block
                await self._log_decision(mkt_base, "rejected", f"risk gate [{gate}]: {reason}")
                continue

            accept_reason = (
                f"signal fired: arbitrage gap {gap*100:.1f}% | "
                f"edge {edge*100:.1f}% | kelly ${size_usd:.2f}"
            )
            await self._log_decision(mkt_base, "accepted", accept_reason)

            await self._open_position(
                market=market, side=side, contracts=contracts,
                entry_price=entry_price, size_usd=size_usd,
                signal_type=SignalType.ARBITRAGE,
                confidence=confidence, whale_meta=signal_meta,
            )

    async def _llm_evaluate_market(
        self,
        market: Market,
        news_hint: Optional[str] = None,
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        """
        Call Claude to evaluate the market and return a signal if edge exists.

        Returns a signal tuple if Claude finds actionable edge, None to abstain.
        Results are cached per market_id for 30 minutes to avoid redundant calls.
        """
        cfg = self._cfg

        if not cfg.anthropic_api_key or self._llm_account_error:
            return None

        # ── Volume pre-filter: only LLM-evaluate genuinely active markets ─────
        # Prevents burning budget on low-liquidity markets where the signal
        # quality won't justify the cost.
        vol_24h = float(market.volume_24h_usd or 0)
        if vol_24h < _LLM_MIN_VOLUME_USD:
            return None

        # ── Skip live sports matches: LLM has no edge on real-time outcomes ──
        days = float(market.days_to_close or 30)
        cat = (market.category or "").lower()
        if cat in ("sports", "") and days < 1.0:
            return None

        # ── Cache check ───────────────────────────────────────────────────────
        cached = self._llm_cache.get(market.id)
        if cached is not None:
            expires_at, cached_result = cached
            if datetime.now(timezone.utc) < expires_at:
                return cached_result  # type: ignore[return-value]
            del self._llm_cache[market.id]

        # ── Daily budget gate ─────────────────────────────────────────────────
        daily_cost = await self._db.get_llm_daily_cost_usd()
        if daily_cost >= cfg.daily_llm_budget_usd:
            logger.warning(
                "Daily LLM budget $%.2f exhausted (spent $%.2f) — skipping",
                cfg.daily_llm_budget_usd, daily_cost,
            )
            return None

        # ── Build and send prompt ─────────────────────────────────────────────
        yes_price = float(market.yes_mid or 0.50)
        days = float(market.days_to_close or 30)

        prompt = _build_llm_prompt(
            title=market.title,
            yes_price=yes_price,
            days=days,
            category=market.category or "unknown",
            news_hint=news_hint,
        )

        if self._llm_client is None:
            self._llm_client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)

        t0 = time.monotonic()
        try:
            response = await self._llm_client.messages.create(
                model=cfg.llm_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            msg = str(exc).lower()
            if "credit balance" in msg or "invalid api key" in msg or "authentication" in msg:
                # Account-level error — stop all LLM calls until restart
                self._llm_account_error = True
                logger.error(
                    "LLM account error — disabling LLM signals for this session: %s", exc
                )
            else:
                logger.warning("LLM API error for %s: %s", market.external_id[:30], exc)
            return None
        latency_ms = int((time.monotonic() - t0) * 1000)

        # ── Parse response ────────────────────────────────────────────────────
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()

        try:
            parsed = json.loads(raw)
            llm_conf = float(parsed["confidence"])
            reasoning = str(parsed.get("reasoning", "")).strip()
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(
                "LLM parse failed for %s: %s | raw=%s",
                market.external_id[:30], exc, raw[:120],
            )
            return None

        if not (0.0 <= llm_conf <= 1.0):
            logger.warning("LLM returned out-of-range confidence %.3f for %s", llm_conf, market.external_id[:30])
            return None

        # ── Cost tracking ─────────────────────────────────────────────────────
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        # claude-sonnet-4-6: $3/M input, $15/M output
        cost_usd = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000

        # ── Decide action from confidence vs market price ─────────────────────
        edge_yes = llm_conf - yes_price            # positive = LLM thinks YES is cheap
        edge_no  = yes_price - llm_conf            # positive = LLM thinks NO is cheap

        action = "abstain"
        side: Optional[str] = None
        if llm_conf >= _LLM_MIN_CONFIDENCE and edge_yes >= _LLM_MIN_EDGE:
            action, side = "yes", "yes"
        elif (1.0 - llm_conf) >= _LLM_MIN_CONFIDENCE and edge_no >= _LLM_MIN_EDGE:
            action, side = "no", "no"

        edge_display = edge_yes if action == "yes" else (-edge_yes if action == "no" else edge_yes)

        logger.info(
            "LLM [%s] %s | action=%s conf=%.0f%% mkt=%.0f%% edge=%+.0f%% | $%.4f %dms | %s",
            market.exchange.value,
            market.title[:45],
            action,
            llm_conf * 100,
            yes_price * 100,
            edge_display * 100,
            cost_usd,
            latency_ms,
            reasoning[:80],
        )

        # ── Persist cost log ──────────────────────────────────────────────────
        await self._db.insert_llm_query(LLMQueryInsert(
            model=cfg.llm_model,
            purpose="market_analysis",
            market_id=market.id,
            prompt_tokens=in_tok,
            completion_tokens=out_tok,
            cost_usd=cost_usd,
            response_action=action,
            response_confidence=llm_conf,
            latency_ms=latency_ms,
        ))

        # ── Cache and return ──────────────────────────────────────────────────
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

        if action == "abstain":
            await self._log_decision(
                {
                    "market_id": market.id,
                    "external_market_id": market.external_id,
                    "market_title": market.title[:120],
                    "exchange": market.exchange.value,
                },
                "abstain",
                f"LLM abstain: conf={llm_conf:.0%} mkt={yes_price:.0%} "
                f"edge_yes={edge_yes:+.0%} | {reasoning[:120]}",
            )
            self._llm_cache[market.id] = (expires_at, None)
            return None

        # For NO trades, confidence must represent P(NO wins) = 1 - llm_conf so
        # the existing edge formula (confidence - entry_price) stays correct for
        # both sides: YES edge = llm_conf - yes_mid, NO edge = (1-llm_conf) - no_mid.
        signal_confidence = llm_conf if side == "yes" else (1.0 - llm_conf)

        signal_meta = {
            "reasoning": reasoning,
            "llm_confidence": round(llm_conf, 4),   # raw YES probability from LLM
            "market_price": round(yes_price, 4),
            "edge": round(edge_display, 4),
            "cost_usd": round(cost_usd, 6),
            "latency_ms": latency_ms,
        }
        result: tuple = (SignalType.LLM_DIRECTIONAL, side, signal_confidence, signal_meta)
        self._llm_cache[market.id] = (expires_at, result)
        return result

    def _kelly_size(
        self,
        confidence: float,
        entry_price: float,
        portfolio_usd: float,
        cat_score: Optional[CategoryScore],
    ) -> float:
        """
        Quarter-Kelly position size, capped at max_position_pct of portfolio.
        Kelly fraction = (edge / odds) * kelly_fraction
          edge = confidence - entry_price  (expected edge as probability)
          odds = (1 - entry_price) / entry_price  (payout ratio)
        """
        if entry_price <= 0 or entry_price >= 1:
            return 0.0

        edge = confidence - entry_price
        if edge <= 0:
            return 0.0

        odds = (1.0 - entry_price) / entry_price
        full_kelly = edge / odds
        fraction = full_kelly * self._cfg.kelly_fraction  # quarter-Kelly

        # Apply category allocation cap
        if cat_score and cat_score.allocation_pct:
            fraction = min(fraction, float(cat_score.allocation_pct))

        # Hard cap
        fraction = min(fraction, self._cfg.max_position_pct)

        return round(portfolio_usd * fraction, 2)

    async def _portfolio_enforcer_check(
        self,
        market: Market,
        side: str,
        contracts: float,
        entry_price: float,
        size_usd: float,
        signal_type: SignalType,
        portfolio_usd: float,
        cat_score: Optional[CategoryScore],
    ) -> Optional[tuple[str, str]]:
        """
        4-gate check. Returns (gate_name, reason) if blocked, None if approved.

        Gate 1: category composite score >= BLOCK_THRESHOLD
        Gate 2: category allocation cap
        Gate 3: per-position size cap (max_position_pct)
        Gate 4: sector concentration cap (max_sector_concentration_pct)
        """
        category = market.category or "unknown"
        cfg = self._cfg

        # Gate 1: category blocked?
        if cat_score and cat_score.composite_score < _BLOCK_THRESHOLD:
            return (
                "category_score",
                f"{category} score {float(cat_score.composite_score):.1f} < threshold {_BLOCK_THRESHOLD}",
            )

        # Gate 2: category allocation cap
        if cat_score and cat_score.allocation_pct:
            cat_exposure = await self._db.get_category_exposure_usd(
                market.exchange, category
            )
            max_cat_usd = portfolio_usd * float(cat_score.allocation_pct)
            if cat_exposure + size_usd > max_cat_usd:
                return (
                    "allocation_cap",
                    f"{category} exposure ${cat_exposure:.0f} + ${size_usd:.0f} > cap ${max_cat_usd:.0f}",
                )

        # Gate 3: per-position size cap
        max_pos_usd = portfolio_usd * cfg.max_position_pct
        if size_usd > max_pos_usd:
            return (
                "position_size",
                f"Size ${size_usd:.2f} > max ${max_pos_usd:.2f} ({cfg.max_position_pct*100:.1f}% of portfolio)",
            )

        # Gate 4: category concentration (cross-exchange)
        cat_exposure_total = await self._db.get_category_exposure_all_exchanges(category)
        max_sector_usd = portfolio_usd * cfg.max_sector_concentration_pct
        if cat_exposure_total + size_usd > max_sector_usd:
            return (
                "category_concentration",
                f"{category} total ${cat_exposure_total:.0f} + ${size_usd:.0f} > {cfg.max_sector_concentration_pct*100:.0f}% cap ${max_sector_usd:.0f}",
            )

        return None  # all gates passed

    async def _open_position(
        self,
        market: Market,
        side: str,
        contracts: float,
        entry_price: float,
        size_usd: float,
        signal_type: SignalType,
        confidence: float,
        whale_meta: dict,
    ) -> None:
        cfg = self._cfg
        mode = TradingMode(cfg.trading_mode.value)
        now = datetime.now(timezone.utc)
        max_hold = now + timedelta(hours=cfg.max_hold_hours)

        # Stop-loss / take-profit absolute prices
        stop_price = round(entry_price * (1 - cfg.stop_loss_pct), 4)
        tp_price = round(min(0.99, entry_price * (1 + cfg.take_profit_pct)), 4)

        pos_id = await self._db.create_position(
            PositionCreate(
                exchange=market.exchange,
                market_id=market.id,
                external_market_id=market.external_id,
                side=side,
                mode=mode,
                signal_type=signal_type,
                contracts=contracts,
                avg_entry_price=entry_price,
                cost_basis_usd=size_usd,
                stop_loss_price=stop_price,
                take_profit_price=tp_price,
                kelly_fraction_used=size_usd / (await self._estimate_portfolio_value() or 1),
                whale_address=whale_meta.get("whale_address"),
                whale_score=whale_meta.get("whale_score"),
                mirror_delay_s=cfg.whale_mirror_delay_s if signal_type == SignalType.WHALE_MIRROR else None,
                whale_trade_id=whale_meta.get("whale_trade_id"),
                max_hold_until=max_hold,
            )
        )

        order_id = await self._db.create_order(
            OrderCreate(
                position_id=pos_id,
                exchange=market.exchange,
                market_id=market.id,
                external_market_id=market.external_id,
                side=side,
                order_type=OrderType.LIMIT,
                mode=mode,
                is_opening=True,
                requested_contracts=contracts,
                requested_price=entry_price,
            )
        )

        if signal_type == SignalType.WHALE_MIRROR and whale_meta.get("whale_trade_id"):
            await self._db.mark_whale_trade_mirrored(
                whale_meta["whale_trade_id"], pos_id
            )

        self._new_positions_this_scan += 1
        self._scan_exposure_usd += size_usd

        logger.info(
            "[%s][%s] %s %s %.0f contracts @ %.3f (%.2f USD) signal=%s conf=%.0f%%",
            mode.value.upper(),
            market.exchange.value,
            side.upper(),
            market.external_id,
            contracts,
            entry_price,
            size_usd,
            signal_type.value,
            confidence * 100,
        )

        if mode == TradingMode.LIVE:
            # Executor picks up the order record and submits to exchange.
            # TODO: call execution.kalshi_executor or execution.polymarket_executor
            logger.info("LIVE order queued (order_id=%s) — executor will submit", order_id)
        else:
            # Paper mode: activate position immediately (simulated fill at mid)
            await self._db.set_order_placed(order_id, f"paper-{order_id}")
            await self._db.fill_order(order_id, contracts, entry_price)
            await self._db.activate_position(pos_id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _estimate_portfolio_value(self) -> float:
        """
        In paper mode, use a synthetic $10,000 starting balance minus open exposure.
        Adds _scan_exposure_usd to account for positions opened earlier in this scan
        that haven't committed to the DB yet.
        In live mode, query exchange balances (TODO: wire up after executors built).
        """
        if self._cfg.trading_mode == TradingMode.PAPER:
            exposure = await self._db.get_total_exposure_usd()
            return max(0.0, self._cfg.paper_starting_balance - exposure - self._scan_exposure_usd)
        # Live: TODO query Kalshi balance + Poly USDC balance
        return self._cfg.paper_starting_balance


# ── LLM prompt ────────────────────────────────────────────────────────────────

def _build_llm_prompt(
    title: str,
    yes_price: float,
    days: float,
    category: str,
    news_hint: Optional[str],
) -> str:
    news_line = f"\nRecent news: {news_hint}" if news_hint else ""
    return f"""You are evaluating a prediction market. Estimate the TRUE probability it resolves YES.

Market: {title}
Category: {category}
Current YES price: {yes_price:.0%}  (this is the market's current implied probability)
Days until resolution: {days:.0f}{news_line}

Your task: using your knowledge of world events, base rates, and the context above, estimate the real probability this market resolves YES.

Return JSON only — no other text, no markdown:
{{"confidence": <float 0.0–1.0>, "reasoning": "<1-2 sentences>"}}

Rules:
- "confidence" is YOUR probability estimate, not the market price
- If you lack meaningful information to estimate this (market too obscure, outcome genuinely uncertain, or price already efficient), set confidence within ±0.05 of the market price
- Be conservative and honest about uncertainty — wrong bets lose money
- A {yes_price:.0%} market price already reflects crowd wisdom; you need a real reason to deviate"""


# ── Utilities ─────────────────────────────────────────────────────────────────

def _safe_float(v: object) -> Optional[float]:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _kalshi_volume_usd(market: dict, field: str = "volume_24h_fp") -> Optional[float]:
    """Kalshi reports volume in contracts (field ends in _fp); multiply by mid-price to get USD."""
    contracts = _safe_float(market.get(field))
    if contracts is None:
        return None
    yes_bid = _safe_float(market.get("yes_bid_dollars") or market.get("yes_bid"))
    yes_ask = _safe_float(market.get("yes_ask_dollars") or market.get("yes_ask"))
    if yes_bid and yes_bid > 1:
        yes_bid /= 100
    if yes_ask and yes_ask > 1:
        yes_ask /= 100
    mid = ((yes_bid or 0) + (yes_ask or 0)) / 2 or 0.5
    return round(contracts * mid, 2)


def _normalise_category(raw: Optional[str], title: str = "") -> Optional[str]:
    if raw:
        mapping = {
            "Political & Geopolitical": "politics",
            "Economics & Finance": "economics",
            "Financials": "economics",
            "Science & Technology": "technology",
            "Crypto": "crypto",
            "Cryptocurrency": "crypto",
            "Sports": "sports",
            "Entertainment": "entertainment",
            "Weather & Environment": "weather",
            "Climate & Environment": "weather",
        }
        cleaned = raw.strip()
        result = mapping.get(cleaned, cleaned.lower().split()[0] if cleaned else None)
        if result:
            return result

    if not title:
        return None
    t = title.lower()
    if any(kw in t for kw in ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol price")):
        return "crypto"
    if any(kw in t for kw in ("nba", "nfl", "mlb", "nhl", "premier league", " fc ", "vs.", "winner?",
                               "game ", "playoffs", "lakers", "yankees", "mets", "padres", "braves",
                               "red sox", "guardians", "phillies", "brewers", "cubs", "reds",
                               "angels", "tigers", "royals", "cardinals", "orioles", "mariners",
                               "giants", "rays", "blue jays", "white sox", "astros", "dodgers",
                               "rangers", "twins", "pirates", "rockies", "nationals", "marlins",
                               "atletico", "barcelona", "real madrid", "manchester", "chelsea",
                               "arsenal", "liverpool", "tottenham", "inter", "juventus", "roma",
                               "bayern", "dortmund", "psg", "serie a", "la liga", "bundesliga",
                               "cricket", "ipl", "tennis", "atp", "wta", "esports", "lol:",
                               "dota", "csgo", "ufc", "boxing", "f1", "formula",
                               " win on 2026", " win on 2027", "o/u ", "over/under",
                               "points?", "spread", "handicap", "1st inning")):
        return "sports"
    if any(kw in t for kw in ("trump", "biden", "election", "congress", "senate", "president",
                               "governor", "democrat", "republican", "poll", "vote", "party",
                               "legislation", "bill pass", "supreme court")):
        return "politics"
    if any(kw in t for kw in ("iran", "ukraine", "russia", "china", "war", "ceasefire",
                               "military", "nato", "sanctions", "invasion", "missile",
                               "airspace", "blockade", "strait", "nuclear")):
        return "geopolitics"
    if any(kw in t for kw in ("oil", "wti", "crude", "gold", "silver", "commodity",
                               "natural gas", "brent")):
        return "commodities"
    if any(kw in t for kw in ("s&p", "nasdaq", "dow", "stock", "fed ", "interest rate",
                               "inflation", "gdp", "unemployment", "tariff")):
        return "economics"
    if any(kw in t for kw in ("weather", "hurricane", "temperature", "earthquake", "flood")):
        return "weather"
    if any(kw in t for kw in ("movie", "album", "oscar", "grammy", "spotify",
                               "youtube", "tiktok", "mrbeast", "views", "streaming")):
        return "entertainment"
    return None
