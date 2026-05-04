from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class ActiveExchange(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    BOTH = "both"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Trading mode ────────────────────────────────────────────────────────
    trading_mode: TradingMode = TradingMode.PAPER
    active_exchange: ActiveExchange = ActiveExchange.BOTH

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql://prediction_bot:prediction_bot@localhost:5432/prediction_bot"
    )
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10

    # ── Kalshi ──────────────────────────────────────────────────────────────
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: Path = Path("kalshi_private_key.pem")
    kalshi_base_url: str = "https://api.elections.kalshi.com"
    kalshi_request_delay_ms: int = 200     # stay under 5 req/s
    kalshi_max_retries: int = 5
    kalshi_timeout_s: int = 30

    # ── Polymarket ──────────────────────────────────────────────────────────
    polygon_wallet_private_key: str = ""
    polygon_rpc_url: str = "https://polygon-rpc.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_chain_id: int = 137

    # ── Goldsky (on-chain Polymarket whale data) ─────────────────────────────
    goldsky_endpoint: str = (
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw"
        "/subgraphs/orderbook-subgraph/0.0.1/gn"
    )
    goldsky_page_size: int = 1000
    whale_ingest_interval_s: int = 30

    # ── News ────────────────────────────────────────────────────────────────
    newsapi_key: str = ""
    gdelt_base_url: str = "https://api.gdeltproject.org/api/v2/doc/doc"
    news_scan_interval_s: int = 300

    # ── Telegram ────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_error_chat_id: str = ""      # separate channel for errors/alerts

    # ── LLM ─────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-6"
    llm_temperature: float = 0.0
    daily_llm_budget_usd: float = 10.0

    # ── Position sizing ─────────────────────────────────────────────────────
    kelly_fraction: float = 0.25           # quarter-Kelly
    max_position_pct: float = 0.03         # max 3% of portfolio per position
    max_sector_concentration_pct: float = 0.30
    min_edge_cents: float = 3.0            # minimum edge in cents to trade
    min_confidence: float = 0.55

    # ── Whale mirroring ─────────────────────────────────────────────────────
    whale_mirror_delay_s: int = 45         # seconds to wait before mirroring
    whale_min_trade_usd: float = 500.0     # only mirror trades above this size
    whale_min_score: float = 60.0          # minimum composite score to follow
    whale_score_interval_s: int = 3600     # re-rank every hour

    # ── Market scanning ─────────────────────────────────────────────────────
    scan_interval_s: int = 60
    min_market_volume_usd: float = 1000.0
    min_days_to_expiry: int = 1
    max_days_to_expiry: int = 90

    # ── Risk / exits ────────────────────────────────────────────────────────
    max_drawdown_pct: float = 0.20         # halt if portfolio drops 20%
    stop_loss_pct: float = 0.40            # exit if position down 40%
    take_profit_pct: float = 0.80          # exit if position up 80%
    max_hold_hours: int = 240              # 10-day hard cap

    # ── Paper trading ────────────────────────────────────────────────────────
    paper_starting_balance: float = 10000.0

    # ── Workers ─────────────────────────────────────────────────────────────
    position_track_interval_s: int = 15
    daily_summary_hour_utc: int = 9        # send daily Telegram summary at 9am UTC

    # ── Validators ──────────────────────────────────────────────────────────

    @field_validator("kalshi_private_key_path", mode="before")
    @classmethod
    def expand_key_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @field_validator("kelly_fraction", mode="after")
    @classmethod
    def kelly_in_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("kelly_fraction must be in (0, 1]")
        return v

    @field_validator("max_position_pct", "max_sector_concentration_pct", mode="after")
    @classmethod
    def pct_in_range(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("percentage values must be in (0, 1)")
        return v

    @model_validator(mode="after")
    def check_live_credentials(self) -> "Settings":
        if self.trading_mode == TradingMode.LIVE:
            missing: list[str] = []
            if self.active_exchange in (ActiveExchange.KALSHI, ActiveExchange.BOTH):
                if not self.kalshi_api_key_id:
                    missing.append("KALSHI_API_KEY_ID")
                if not self.kalshi_private_key_path.exists():
                    missing.append(f"kalshi key file at {self.kalshi_private_key_path}")
            if self.active_exchange in (ActiveExchange.POLYMARKET, ActiveExchange.BOTH):
                if not self.polygon_wallet_private_key:
                    missing.append("POLYGON_WALLET_PRIVATE_KEY")
            if missing:
                raise ValueError(
                    f"LIVE mode is missing required credentials: {', '.join(missing)}"
                )
        return self


_instance: Optional[Settings] = None


def get_settings() -> Settings:
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance
