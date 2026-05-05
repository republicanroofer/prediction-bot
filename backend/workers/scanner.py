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
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from backend.clients.kalshi_client import KalshiClient, KalshiAPIError
from backend.clients.polymarket_client import GammaClient
from backend.config.settings import ActiveExchange, TradingMode, get_settings
from backend.db.database import Database
from backend.db.models import (
    BlockedTradeInsert,
    CategoryScore,
    Exchange,
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
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = db
        self._kalshi = kalshi
        self._gamma = gamma
        self._stop = stop_event or asyncio.Event()
        self._cfg = get_settings()

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

    # ── Kalshi ingestion ──────────────────────────────────────────────────────

    async def _scan_kalshi(self) -> None:
        assert self._kalshi
        try:
            events = await self._kalshi.get_all_events(status="open")
        except KalshiAPIError as exc:
            logger.warning("Kalshi event fetch failed: %s", exc)
            return

        upserted = 0
        for event in events:
            for market in event.get("markets", []):
                await self._upsert_kalshi_market(event, market)
                upserted += 1

        logger.debug("Kalshi: upserted %d markets from %d events", upserted, len(events))

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
        markets = await self._db.get_active_markets()
        cfg = self._cfg

        for market in markets:
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
        """Check news and volume-momentum signals."""
        # ── News signal ───────────────────────────────────────────────────────
        recent_signals = await self._db.get_recent_signals_for_market(
            market.id, hours=_NEWS_SIGNAL_RECENCY_HOURS
        )
        if recent_signals:
            best = recent_signals[0]  # already sorted by relevance DESC
            relevance = float(best.relevance_score or 0)
            sentiment = float(best.sentiment_score or 0)
            if relevance >= 0.3:
                direction = best.direction or ("yes" if sentiment >= 0 else "no")
                side = "yes" if direction in ("bullish_yes", "yes") else ("no" if direction in ("bullish_no", "no") else "yes")
                confidence = min(0.65, 0.35 + relevance * 0.20 + abs(sentiment) * 0.15)
                return (
                    SignalType.NEWS,
                    side,
                    confidence,
                    {"headline": best.headline, "sentiment": sentiment},
                )

        # ── Volume-momentum signal ───────────────────────────────────────────
        # Markets with unusually high 24h volume relative to total lifetime
        # volume suggest active price discovery — trade toward the mid-price
        # direction (yes if mid > 0.5, no if mid < 0.5).
        vol_24h = float(market.volume_24h_usd or 0)
        vol_total = float(market.volume_total_usd or 0)
        days = market.days_to_close or 30
        if vol_24h >= 5000 and vol_total > 0:
            vol_ratio = vol_24h / vol_total
            if vol_ratio >= 0.03:  # 24h vol is >= 3% of all-time
                mid = market.yes_mid
                if mid is not None and 0.15 <= mid <= 0.85:
                    side = "yes" if mid >= 0.5 else "no"
                    confidence = min(0.58, 0.40 + vol_ratio * 2.0 + (0.01 if days <= 14 else 0))
                    return (
                        SignalType.LLM_DIRECTIONAL,
                        side,
                        confidence,
                        {"volume_ratio": round(vol_ratio, 4), "vol_24h": vol_24h},
                    )

        return None

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
        In live mode, query exchange balances (TODO: wire up after executors built).
        """
        if self._cfg.trading_mode == TradingMode.PAPER:
            exposure = await self._db.get_total_exposure_usd()
            return max(0.0, self._cfg.paper_starting_balance - exposure)
        # Live: TODO query Kalshi balance + Poly USDC balance
        return self._cfg.paper_starting_balance


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
