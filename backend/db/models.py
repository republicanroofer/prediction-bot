from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ── Enums ─────────────────────────────────────────────────────────────────────

class Exchange(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    FOK = "fok"


class PositionStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"


class SignalType(str, Enum):
    WHALE_MIRROR = "whale_mirror"
    NEWS = "news"
    LLM_DIRECTIONAL = "llm_directional"
    SAFE_COMPOUNDER = "safe_compounder"
    MANUAL = "manual"


class CloseReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_LIMIT = "time_limit"
    MANUAL = "manual"
    EXPIRY = "expiry"
    RESOLVED = "resolved"


_RO = ConfigDict(from_attributes=True)


# ── Domain models ─────────────────────────────────────────────────────────────

class Market(BaseModel):
    model_config = _RO

    id: UUID
    exchange: Exchange
    external_id: str
    event_ticker: Optional[str] = None
    token_id_yes: Optional[str] = None
    token_id_no: Optional[str] = None
    title: str
    category: Optional[str] = None
    sub_category: Optional[str] = None
    yes_bid: Optional[Decimal] = None
    yes_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None
    last_price: Optional[Decimal] = None
    volume_24h_usd: Optional[Decimal] = None
    volume_total_usd: Optional[Decimal] = None
    open_interest: Optional[Decimal] = None
    liquidity_usd: Optional[Decimal] = None
    close_time: Optional[datetime] = None
    is_active: bool = True
    is_resolved: bool = False
    resolution: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @property
    def yes_mid(self) -> Optional[float]:
        if self.yes_bid is not None and self.yes_ask is not None:
            return float((self.yes_bid + self.yes_ask) / 2)
        return float(self.last_price) if self.last_price is not None else None

    @property
    def no_mid(self) -> Optional[float]:
        if self.no_bid is not None and self.no_ask is not None:
            return float((self.no_bid + self.no_ask) / 2)
        m = self.yes_mid
        return round(1.0 - m, 4) if m is not None else None

    @property
    def days_to_close(self) -> Optional[float]:
        if not self.close_time:
            return None
        from datetime import timezone
        now = datetime.now(timezone.utc)
        ct = (
            self.close_time
            if self.close_time.tzinfo
            else self.close_time.replace(tzinfo=timezone.utc)
        )
        return max(0.0, (ct - now).total_seconds() / 86400)


class Position(BaseModel):
    model_config = _RO

    id: UUID
    exchange: Exchange
    market_id: UUID
    external_market_id: str
    side: str
    mode: TradingMode
    status: PositionStatus
    signal_type: SignalType
    contracts: Decimal
    avg_entry_price: Decimal
    cost_basis_usd: Decimal
    current_price: Optional[Decimal] = None
    market_value_usd: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    fees_paid_usd: Decimal = Decimal("0")
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    kelly_fraction_used: Optional[Decimal] = None
    whale_address: Optional[str] = None
    whale_score: Optional[Decimal] = None
    mirror_delay_s: Optional[int] = None
    whale_trade_id: Optional[UUID] = None
    opened_at: datetime
    closed_at: Optional[datetime] = None
    close_reason: Optional[CloseReason] = None
    max_hold_until: datetime
    created_at: datetime
    updated_at: datetime

    def unrealized_pnl_pct(self) -> Optional[float]:
        if self.unrealized_pnl is None or self.cost_basis_usd == 0:
            return None
        return float(self.unrealized_pnl / self.cost_basis_usd)

    def should_stop_loss(self, stop_loss_pct: float) -> bool:
        pct = self.unrealized_pnl_pct()
        return pct is not None and pct <= -stop_loss_pct

    def should_take_profit(self, take_profit_pct: float) -> bool:
        pct = self.unrealized_pnl_pct()
        return pct is not None and pct >= take_profit_pct

    def is_past_time_limit(self) -> bool:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        limit = (
            self.max_hold_until
            if self.max_hold_until.tzinfo
            else self.max_hold_until.replace(tzinfo=timezone.utc)
        )
        return now >= limit


class Order(BaseModel):
    model_config = _RO

    id: UUID
    position_id: Optional[UUID] = None
    exchange: Exchange
    external_id: Optional[str] = None
    market_id: UUID
    external_market_id: str
    side: str
    order_type: OrderType
    status: OrderStatus
    mode: TradingMode
    is_opening: bool
    requested_contracts: Decimal
    requested_price: Decimal
    filled_contracts: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    fees_paid_usd: Optional[Decimal] = None
    placed_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @property
    def is_complete(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        )

    @property
    def fill_pct(self) -> float:
        if self.requested_contracts == 0:
            return 0.0
        return float(self.filled_contracts / self.requested_contracts)


class Fill(BaseModel):
    model_config = _RO

    id: UUID
    order_id: UUID
    exchange: Exchange
    external_fill_id: Optional[str] = None
    contracts: Decimal
    price: Decimal
    fees_usd: Optional[Decimal] = None
    filled_at: datetime
    created_at: datetime


class WhaleTrade(BaseModel):
    model_config = _RO

    id: UUID
    tx_hash: str
    block_timestamp: datetime
    maker_address: str
    taker_address: str
    market_id: Optional[UUID] = None
    condition_id: Optional[str] = None
    token_id: Optional[str] = None
    maker_direction: str
    taker_direction: str
    price: Decimal
    usd_amount: Decimal
    token_amount: Decimal
    is_platform_tx: bool
    mirrored: bool
    mirror_position_id: Optional[UUID] = None
    mirror_queued_at: Optional[datetime] = None
    mirror_executed_at: Optional[datetime] = None
    created_at: datetime


class WhaleScore(BaseModel):
    model_config = _RO

    id: UUID
    address: str
    display_name: Optional[str] = None
    total_pnl_usd: Optional[Decimal] = None
    win_rate: Optional[Decimal] = None
    big_win_rate: Optional[Decimal] = None
    median_gain_pct: Optional[Decimal] = None
    median_loss_pct: Optional[Decimal] = None
    markets_traded: Optional[int] = None
    total_volume_usd: Optional[Decimal] = None
    composite_score: Optional[Decimal] = None
    is_active: bool = True
    last_trade_at: Optional[datetime] = None
    scored_at: datetime
    created_at: datetime


class NewsSignal(BaseModel):
    model_config = _RO

    id: UUID
    market_id: Optional[UUID] = None
    external_market_id: Optional[str] = None
    source: str
    headline: str
    url: Optional[str] = None
    published_at: Optional[datetime] = None
    sentiment_score: Optional[Decimal] = None
    relevance_score: Optional[Decimal] = None
    direction: Optional[str] = None
    keywords: Optional[list[str]] = None
    created_at: datetime


class CategoryScore(BaseModel):
    model_config = _RO

    id: UUID
    exchange: Exchange
    category: str
    composite_score: Decimal
    roi: Optional[Decimal] = None
    win_rate: Optional[Decimal] = None
    sample_size: int = 0
    recent_trend: Optional[Decimal] = None
    allocation_pct: Optional[Decimal] = None
    is_blocked: bool = False
    scored_at: datetime


class DailyPnL(BaseModel):
    model_config = _RO

    id: UUID
    date: date
    exchange: Optional[Exchange] = None
    mode: TradingMode
    starting_balance: Optional[Decimal] = None
    ending_balance: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    fees_paid: Optional[Decimal] = None
    trades_opened: int = 0
    trades_closed: int = 0
    win_count: int = 0
    loss_count: int = 0
    created_at: datetime


class LLMQuery(BaseModel):
    model_config = _RO

    id: UUID
    model: str
    purpose: str
    market_id: Optional[UUID] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[Decimal] = None
    response_action: Optional[str] = None
    response_confidence: Optional[Decimal] = None
    was_traded: Optional[bool] = None
    trade_outcome_pnl: Optional[Decimal] = None
    latency_ms: Optional[int] = None
    created_at: datetime


# ── Create / Update DTOs ──────────────────────────────────────────────────────

class MarketUpsert(BaseModel):
    exchange: Exchange
    external_id: str
    event_ticker: Optional[str] = None
    token_id_yes: Optional[str] = None
    token_id_no: Optional[str] = None
    title: str
    category: Optional[str] = None
    sub_category: Optional[str] = None
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    last_price: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    volume_total_usd: Optional[float] = None
    open_interest: Optional[float] = None
    liquidity_usd: Optional[float] = None
    close_time: Optional[datetime] = None
    is_active: bool = True
    raw: Optional[dict] = None


class PositionCreate(BaseModel):
    exchange: Exchange
    market_id: UUID
    external_market_id: str
    side: str
    mode: TradingMode
    signal_type: SignalType
    contracts: float
    avg_entry_price: float
    cost_basis_usd: float
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    kelly_fraction_used: Optional[float] = None
    whale_address: Optional[str] = None
    whale_score: Optional[float] = None
    mirror_delay_s: Optional[int] = None
    whale_trade_id: Optional[UUID] = None
    max_hold_until: datetime


class OrderCreate(BaseModel):
    position_id: Optional[UUID] = None
    exchange: Exchange
    market_id: UUID
    external_market_id: str
    side: str
    order_type: OrderType = OrderType.LIMIT
    mode: TradingMode
    is_opening: bool = True
    requested_contracts: float
    requested_price: float
    expires_at: Optional[datetime] = None


class WhaleTradeInsert(BaseModel):
    tx_hash: str
    block_timestamp: datetime
    maker_address: str
    taker_address: str
    market_id: Optional[UUID] = None
    condition_id: Optional[str] = None
    token_id: Optional[str] = None
    maker_direction: str
    taker_direction: str
    price: float
    usd_amount: float
    token_amount: float
    is_platform_tx: bool = False
    mirror_queued_at: Optional[datetime] = None


class NewsSignalInsert(BaseModel):
    market_id: Optional[UUID] = None
    external_market_id: Optional[str] = None
    source: str
    headline: str
    url: Optional[str] = None
    published_at: Optional[datetime] = None
    sentiment_score: Optional[float] = None
    relevance_score: Optional[float] = None
    direction: Optional[str] = None
    keywords: Optional[list[str]] = None
    raw: Optional[dict] = None


class LLMQueryInsert(BaseModel):
    model: str
    purpose: str
    market_id: Optional[UUID] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    response_action: Optional[str] = None
    response_confidence: Optional[float] = None
    latency_ms: Optional[int] = None


class BlockedTradeInsert(BaseModel):
    exchange: Exchange
    market_id: Optional[UUID] = None
    external_market_id: Optional[str] = None
    side: Optional[str] = None
    proposed_contracts: Optional[float] = None
    proposed_price: Optional[float] = None
    signal_type: Optional[SignalType] = None
    block_gate: str
    block_reason: str
    mode: TradingMode
