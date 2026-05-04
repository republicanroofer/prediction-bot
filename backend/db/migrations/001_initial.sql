-- prediction-bot: initial schema
-- PostgreSQL 14+

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Enums ────────────────────────────────────────────────────────────────────

CREATE TYPE exchange_t        AS ENUM ('kalshi', 'polymarket');
CREATE TYPE trading_mode_t    AS ENUM ('paper', 'live');
CREATE TYPE order_side_t      AS ENUM ('yes', 'no', 'buy', 'sell');
CREATE TYPE order_type_t      AS ENUM ('limit', 'market', 'fok');
CREATE TYPE order_status_t    AS ENUM (
    'pending', 'open', 'filled', 'partially_filled',
    'cancelled', 'expired', 'failed'
);
CREATE TYPE position_status_t AS ENUM ('pending', 'open', 'pending_close', 'closed');
CREATE TYPE signal_type_t     AS ENUM (
    'whale_mirror', 'news', 'llm_directional', 'safe_compounder', 'manual'
);
CREATE TYPE close_reason_t    AS ENUM (
    'stop_loss', 'take_profit', 'time_limit', 'manual', 'expiry', 'resolved'
);

-- ── updated_at trigger ────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── markets ───────────────────────────────────────────────────────────────────
-- Unified market record for both exchanges. One row per tradeable market.

CREATE TABLE markets (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange            exchange_t   NOT NULL,
    -- Exchange-native identifiers
    external_id         TEXT         NOT NULL,   -- Kalshi ticker / Poly condition_id
    event_ticker        TEXT,                    -- Kalshi event ticker
    token_id_yes        TEXT,                    -- Polymarket CLOB YES token
    token_id_no         TEXT,                    -- Polymarket CLOB NO token
    -- Display
    title               TEXT         NOT NULL,
    category            TEXT,
    sub_category        TEXT,
    -- Live pricing (updated by scanner every 60s)
    yes_bid             NUMERIC(10,4),
    yes_ask             NUMERIC(10,4),
    no_bid              NUMERIC(10,4),
    no_ask              NUMERIC(10,4),
    last_price          NUMERIC(10,4),
    volume_24h_usd      NUMERIC(18,2),
    volume_total_usd    NUMERIC(18,2),
    open_interest       NUMERIC(18,2),
    liquidity_usd       NUMERIC(18,2),
    -- Lifecycle
    close_time          TIMESTAMPTZ,
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    is_resolved         BOOLEAN      NOT NULL DEFAULT FALSE,
    resolution          TEXT,                    -- 'yes', 'no', or outcome string
    -- Raw payload for debugging
    raw                 JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (exchange, external_id)
);

CREATE INDEX idx_markets_exchange    ON markets (exchange);
CREATE INDEX idx_markets_category    ON markets (category);
CREATE INDEX idx_markets_close_time  ON markets (close_time);
CREATE INDEX idx_markets_active      ON markets (is_active, is_resolved);
CREATE INDEX idx_markets_updated     ON markets (updated_at DESC);

CREATE TRIGGER markets_updated_at
    BEFORE UPDATE ON markets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── positions ─────────────────────────────────────────────────────────────────
-- One row per open/closed trade. A position may span multiple fill orders.

CREATE TABLE positions (
    id                  UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange            exchange_t       NOT NULL,
    market_id           UUID             NOT NULL REFERENCES markets(id),
    external_market_id  TEXT             NOT NULL,
    side                TEXT             NOT NULL,  -- 'yes'|'no' (Kalshi) or 'buy'|'sell' (Poly)
    mode                trading_mode_t   NOT NULL,
    status              position_status_t NOT NULL DEFAULT 'pending',
    signal_type         signal_type_t    NOT NULL,
    -- Sizing
    contracts           NUMERIC(18,6)    NOT NULL,
    avg_entry_price     NUMERIC(10,4)    NOT NULL,
    cost_basis_usd      NUMERIC(18,2)    NOT NULL,
    -- Mark-to-market (updated by position_tracker)
    current_price       NUMERIC(10,4),
    market_value_usd    NUMERIC(18,2),
    unrealized_pnl      NUMERIC(18,2),
    realized_pnl        NUMERIC(18,2),
    fees_paid_usd       NUMERIC(10,4)    NOT NULL DEFAULT 0,
    -- Risk params captured at entry time
    stop_loss_price     NUMERIC(10,4),
    take_profit_price   NUMERIC(10,4),
    kelly_fraction_used NUMERIC(6,4),
    -- Whale source (populated if signal_type = 'whale_mirror')
    whale_address       TEXT,
    whale_score         NUMERIC(8,2),
    mirror_delay_s      INT,
    whale_trade_id      UUID,           -- FK set after whale_trades row exists
    -- Timing
    opened_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    close_reason        close_reason_t,
    max_hold_until      TIMESTAMPTZ     NOT NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_positions_market     ON positions (market_id);
CREATE INDEX idx_positions_status     ON positions (status);
CREATE INDEX idx_positions_exchange   ON positions (exchange);
CREATE INDEX idx_positions_mode       ON positions (mode);
CREATE INDEX idx_positions_opened     ON positions (opened_at DESC);
CREATE INDEX idx_positions_signal     ON positions (signal_type);

CREATE TRIGGER positions_updated_at
    BEFORE UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── orders ────────────────────────────────────────────────────────────────────
-- Every order sent to an exchange. Many orders can belong to one position
-- (e.g. partial fills + retry, or separate open/close orders).

CREATE TABLE orders (
    id                  UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID             REFERENCES positions(id),
    exchange            exchange_t       NOT NULL,
    external_id         TEXT,            -- exchange-assigned order ID
    market_id           UUID             NOT NULL REFERENCES markets(id),
    external_market_id  TEXT             NOT NULL,
    side                TEXT             NOT NULL,
    order_type          order_type_t     NOT NULL DEFAULT 'limit',
    status              order_status_t   NOT NULL DEFAULT 'pending',
    mode                trading_mode_t   NOT NULL,
    is_opening          BOOLEAN          NOT NULL DEFAULT TRUE,  -- FALSE = closing order
    -- Amounts
    requested_contracts NUMERIC(18,6)    NOT NULL,
    requested_price     NUMERIC(10,4)    NOT NULL,
    filled_contracts    NUMERIC(18,6)    NOT NULL DEFAULT 0,
    avg_fill_price      NUMERIC(10,4),
    fees_paid_usd       NUMERIC(10,4),
    -- Timing
    placed_at           TIMESTAMPTZ,
    filled_at           TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    -- Debug
    error_message       TEXT,
    raw_response        JSONB,
    created_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_orders_position       ON orders (position_id);
CREATE INDEX idx_orders_ext_id         ON orders (exchange, external_id);
CREATE INDEX idx_orders_status         ON orders (status);
CREATE INDEX idx_orders_placed_at      ON orders (placed_at DESC);

CREATE TRIGGER orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── fills ─────────────────────────────────────────────────────────────────────
-- Individual fill events within an order (handles partial fills correctly).

CREATE TABLE fills (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID         NOT NULL REFERENCES orders(id),
    exchange            exchange_t   NOT NULL,
    external_fill_id    TEXT,
    contracts           NUMERIC(18,6) NOT NULL,
    price               NUMERIC(10,4) NOT NULL,
    fees_usd            NUMERIC(10,4),
    filled_at           TIMESTAMPTZ  NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_fills_order      ON fills (order_id);
CREATE INDEX idx_fills_filled_at  ON fills (filled_at DESC);

-- ── whale_trades ──────────────────────────────────────────────────────────────
-- On-chain trade events scraped from Goldsky (Polymarket).
-- maker_address is the true trader identity per Goldsky event structure.

CREATE TABLE whale_trades (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tx_hash             TEXT         NOT NULL UNIQUE,
    block_timestamp     TIMESTAMPTZ  NOT NULL,
    maker_address       TEXT         NOT NULL,
    taker_address       TEXT         NOT NULL,
    market_id           UUID         REFERENCES markets(id),
    condition_id        TEXT,        -- Polymarket condition_id (pre-join lookup)
    token_id            TEXT,        -- which token was traded
    maker_direction     TEXT         NOT NULL,   -- 'buy' or 'sell'
    taker_direction     TEXT         NOT NULL,
    price               NUMERIC(10,6) NOT NULL,
    usd_amount          NUMERIC(18,2) NOT NULL,
    token_amount        NUMERIC(18,6) NOT NULL,
    -- Platform wallet transactions are excluded from signal generation
    is_platform_tx      BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Mirror tracking
    mirrored            BOOLEAN      NOT NULL DEFAULT FALSE,
    mirror_position_id  UUID         REFERENCES positions(id),
    mirror_queued_at    TIMESTAMPTZ,
    mirror_executed_at  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_whale_maker       ON whale_trades (maker_address);
CREATE INDEX idx_whale_timestamp   ON whale_trades (block_timestamp DESC);
CREATE INDEX idx_whale_condition   ON whale_trades (condition_id);
CREATE INDEX idx_whale_mirrored    ON whale_trades (mirrored);
CREATE INDEX idx_whale_platform    ON whale_trades (is_platform_tx);

-- ── whale_scores ──────────────────────────────────────────────────────────────
-- Ranked whale leaderboard. Rebuilt hourly from accumulated whale_trades.
-- Uses big-win-rate ranking: weight on consistency of large gains, not raw PnL.

CREATE TABLE whale_scores (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    address             TEXT         NOT NULL UNIQUE,
    display_name        TEXT,        -- from Polymarket profile if available
    -- Performance metrics
    total_pnl_usd       NUMERIC(18,2),
    win_rate            NUMERIC(6,4),
    big_win_rate        NUMERIC(6,4), -- fraction of wins with ≥70% gain
    median_gain_pct     NUMERIC(8,4),
    median_loss_pct     NUMERIC(8,4),
    markets_traded      INT,
    total_volume_usd    NUMERIC(18,2),
    -- Composite ranking score (higher = better)
    composite_score     NUMERIC(8,2),
    -- Tracking
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    last_trade_at       TIMESTAMPTZ,
    scored_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_whale_scores_score   ON whale_scores (composite_score DESC);
CREATE INDEX idx_whale_scores_active  ON whale_scores (is_active);

-- ── news_signals ──────────────────────────────────────────────────────────────

CREATE TABLE news_signals (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id           UUID         REFERENCES markets(id),
    external_market_id  TEXT,
    source              TEXT         NOT NULL,   -- 'gdelt' or 'newsapi'
    headline            TEXT         NOT NULL,
    url                 TEXT,
    published_at        TIMESTAMPTZ,
    sentiment_score     NUMERIC(6,4),            -- -1.0 to 1.0
    relevance_score     NUMERIC(6,4),            -- 0.0 to 1.0
    direction           TEXT,                    -- 'bullish_yes' | 'bullish_no' | 'neutral'
    keywords            TEXT[],
    raw                 JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_news_market    ON news_signals (market_id);
CREATE INDEX idx_news_created   ON news_signals (created_at DESC);
CREATE INDEX idx_news_source    ON news_signals (source);

-- ── category_scores ───────────────────────────────────────────────────────────
-- Per-exchange per-category performance scores used by CategoryScorer.
-- Seeded with prior knowledge; updated from resolved position outcomes.

CREATE TABLE category_scores (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange            exchange_t   NOT NULL,
    category            TEXT         NOT NULL,
    composite_score     NUMERIC(6,2) NOT NULL,   -- 0-100
    roi                 NUMERIC(8,4),
    win_rate            NUMERIC(6,4),
    sample_size         INT          NOT NULL DEFAULT 0,
    recent_trend        NUMERIC(8,4),            -- last-10-trade ROI trend
    allocation_pct      NUMERIC(6,4),            -- allowed portfolio fraction
    is_blocked          BOOLEAN      NOT NULL DEFAULT FALSE,  -- score < 30
    scored_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (exchange, category)
);

-- ── blocked_trades ────────────────────────────────────────────────────────────
-- Audit log from PortfolioEnforcer. Every rejected trade is recorded here.

CREATE TABLE blocked_trades (
    id                  UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange            exchange_t     NOT NULL,
    market_id           UUID           REFERENCES markets(id),
    external_market_id  TEXT,
    side                TEXT,
    proposed_contracts  NUMERIC(18,6),
    proposed_price      NUMERIC(10,4),
    signal_type         signal_type_t,
    block_gate          TEXT           NOT NULL,  -- 'category_score'|'allocation_cap'|'position_size'|'sector_concentration'
    block_reason        TEXT           NOT NULL,
    mode                trading_mode_t NOT NULL,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_blocked_created  ON blocked_trades (created_at DESC);
CREATE INDEX idx_blocked_gate     ON blocked_trades (block_gate);
CREATE INDEX idx_blocked_exchange ON blocked_trades (exchange);

-- ── daily_pnl ─────────────────────────────────────────────────────────────────
-- End-of-day snapshot. exchange = NULL means aggregate across both.

CREATE TABLE daily_pnl (
    id                  UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
    date                DATE           NOT NULL,
    exchange            exchange_t,               -- NULL = aggregate
    mode                trading_mode_t NOT NULL,
    starting_balance    NUMERIC(18,2),
    ending_balance      NUMERIC(18,2),
    realized_pnl        NUMERIC(18,2),
    unrealized_pnl      NUMERIC(18,2),
    fees_paid           NUMERIC(18,2),
    trades_opened       INT            NOT NULL DEFAULT 0,
    trades_closed       INT            NOT NULL DEFAULT 0,
    win_count           INT            NOT NULL DEFAULT 0,
    loss_count          INT            NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW(),

    UNIQUE (date, exchange, mode)
);

CREATE INDEX idx_daily_pnl_date ON daily_pnl (date DESC);

-- ── llm_queries ───────────────────────────────────────────────────────────────
-- Cost tracking for every Anthropic API call.

CREATE TABLE llm_queries (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    model               TEXT         NOT NULL,
    purpose             TEXT         NOT NULL,   -- 'market_analysis'|'whale_filter'|etc.
    market_id           UUID         REFERENCES markets(id),
    prompt_tokens       INT,
    completion_tokens   INT,
    cost_usd            NUMERIC(10,6),
    -- Outcome tracking
    response_action     TEXT,
    response_confidence NUMERIC(6,4),
    was_traded          BOOLEAN,
    trade_outcome_pnl   NUMERIC(18,2),  -- filled in when position closes
    latency_ms          INT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_llm_created  ON llm_queries (created_at DESC);
CREATE INDEX idx_llm_purpose  ON llm_queries (purpose);

-- ── Seed: category_scores ─────────────────────────────────────────────────────
-- Prior knowledge from kalshi-ai-trading-bot historical data.
-- Sample sizes are conservative; will be overwritten as real data accumulates.

INSERT INTO category_scores
    (exchange, category, composite_score, roi, win_rate, sample_size, recent_trend, allocation_pct, is_blocked)
VALUES
    ('kalshi',     'politics',       72, 0.18,  0.58, 80,  0.05,  0.10, FALSE),
    ('kalshi',     'economics',      28,-0.12,  0.38, 40, -0.08,  0.00, TRUE),
    ('kalshi',     'sports',         45, 0.04,  0.50, 60, -0.02,  0.05, FALSE),
    ('kalshi',     'entertainment',  31,-0.05,  0.42, 25, -0.04,  0.00, TRUE),
    ('kalshi',     'crypto',         55, 0.09,  0.52, 35,  0.03,  0.05, FALSE),
    ('kalshi',     'weather',        62, 0.12,  0.55, 28,  0.01,  0.08, FALSE),
    ('polymarket', 'politics',       75, 0.20,  0.60, 95,  0.06,  0.12, FALSE),
    ('polymarket', 'crypto',         58, 0.10,  0.53, 55,  0.04,  0.06, FALSE),
    ('polymarket', 'sports',         42, 0.02,  0.48, 45, -0.03,  0.03, FALSE),
    ('polymarket', 'economics',      35,-0.08,  0.44, 30, -0.05,  0.02, FALSE)
ON CONFLICT (exchange, category) DO NOTHING;
