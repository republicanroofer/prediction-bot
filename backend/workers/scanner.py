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
_NEWS_SIGNAL_RECENCY_HOURS = 4


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
                    event.get("category") or market.get("category")
                ),
                sub_category=event.get("sub_title"),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                last_price=_safe_float(market.get("last_price_dollars") or market.get("last_price")),
                volume_24h_usd=_safe_float(market.get("volume_24h")),
                volume_total_usd=_safe_float(market.get("volume")),
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
                category=_normalise_category(raw.get("category") or raw.get("type")),
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

        # ── Basic filters ─────────────────────────────────────────────────────
        if not await self._passes_basic_filters(market):
            return

        # ── Existing position check ───────────────────────────────────────────
        existing = await self._db.get_positions_for_market(market.id)
        if existing:
            return  # already positioned

        # ── Category score gate ───────────────────────────────────────────────
        cat_score = await self._db.get_category_score(
            market.exchange, market.category or "unknown"
        )
        if cat_score and cat_score.is_blocked:
            return  # hard block

        # ── Signal detection ──────────────────────────────────────────────────
        signal = await self._detect_signal(market)
        if signal is None:
            return  # no actionable signal

        signal_type, side, confidence, whale_meta = signal

        # ── Mid-price ─────────────────────────────────────────────────────────
        entry_price = market.yes_mid if side == "yes" else market.no_mid
        if entry_price is None or not (0.05 <= entry_price <= 0.95):
            return  # price too extreme

        # ── Kelly position sizing ─────────────────────────────────────────────
        portfolio_value = await self._estimate_portfolio_value()
        if portfolio_value <= 0:
            return

        size_usd = self._kelly_size(
            confidence=confidence,
            entry_price=entry_price,
            portfolio_usd=portfolio_value,
            cat_score=cat_score,
        )
        if size_usd < 1.0:
            return  # too small to bother

        contracts = size_usd / entry_price  # Kalshi: 1 contract = $1 max payout

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

    async def _passes_basic_filters(self, market: Market) -> bool:
        cfg = self._cfg
        vol = float(market.volume_24h_usd or 0)
        if vol < cfg.min_market_volume_usd:
            return False
        days = market.days_to_close
        if days is None:
            return False
        if days < cfg.min_days_to_expiry or days > cfg.max_days_to_expiry:
            return False
        return True

    async def _detect_signal(
        self, market: Market
    ) -> Optional[tuple[SignalType, str, float, dict]]:
        """
        Check pre-computed signals stored by other workers.
        Returns (signal_type, side, confidence, meta) or None.
        Priority: whale_mirror > news.
        """
        # ── Whale mirror signal ───────────────────────────────────────────────
        # Check if a queued mirror trade exists for this market
        if market.exchange == Exchange.POLYMARKET:
            queued = await self._db.get_queued_mirror_trades(
                min_delay_s=self._cfg.whale_mirror_delay_s,
                min_score=self._cfg.whale_min_score,
            )
            for trade in queued:
                if (
                    trade.get("condition_id") == market.external_id
                    or trade.get("market_id") == market.id
                ):
                    side = trade["maker_direction"]  # 'buy' → buy YES
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

        # ── News signal ───────────────────────────────────────────────────────
        recent_signals = await self._db.get_recent_signals_for_market(
            market.id, hours=_NEWS_SIGNAL_RECENCY_HOURS
        )
        if recent_signals:
            best = recent_signals[0]  # already sorted by relevance DESC
            relevance = float(best.relevance_score or 0)
            sentiment = float(best.sentiment_score or 0)
            if relevance >= 0.6 and abs(sentiment) >= 0.3:
                direction = best.direction or ("yes" if sentiment > 0 else "no")
                side = "yes" if "yes" in direction else "no"
                confidence = min(0.70, 0.40 + relevance * 0.20 + abs(sentiment) * 0.15)
                return (
                    SignalType.NEWS,
                    side,
                    confidence,
                    {"headline": best.headline, "sentiment": sentiment},
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

        # Gate 4: total sector concentration
        total_exposure = await self._db.get_total_exposure_usd()
        cat_exposure = await self._db.get_category_exposure_usd(
            market.exchange, category
        )
        max_sector_usd = portfolio_usd * cfg.max_sector_concentration_pct
        if cat_exposure + size_usd > max_sector_usd:
            return (
                "sector_concentration",
                f"{category} sector ${cat_exposure + size_usd:.0f} > max ${max_sector_usd:.0f}",
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


def _normalise_category(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
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
    return mapping.get(cleaned, cleaned.lower().split()[0] if cleaned else None)
