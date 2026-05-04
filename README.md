# Prediction Bot

Automated trading bot for [Kalshi](https://kalshi.com) and [Polymarket](https://polymarket.com) prediction markets.

## Features

- **Dual-exchange**: trades on Kalshi and/or Polymarket from a unified codebase
- **Whale mirroring**: tracks top traders via Goldsky GraphQL, mirrors their positions after a configurable delay
- **News signals**: aggregates GDELT + NewsAPI articles, scores sentiment with recency decay
- **Kelly Criterion sizing**: quarter-Kelly position sizing with per-category allocation caps
- **4-gate risk firewall**: category score → allocation cap → position size → sector concentration
- **Paper mode**: full simulation with instant fills and P&L tracking — no real money at risk
- **Live dashboard**: React + Recharts frontend with real-time WebSocket updates
- **Telegram alerts**: trade opens/closes, drawdown warnings, error notifications
- **VPS deploy**: one-command Hetzner/Ubuntu deploy via systemd

## Architecture

```
backend/
├── config/settings.py          # Pydantic BaseSettings, env-driven
├── db/
│   ├── migrations/001_initial.sql
│   ├── models.py               # Pydantic models for all DB rows
│   └── database.py             # asyncpg pool, typed query methods
├── clients/
│   ├── kalshi_client.py        # RSA-PSS signed REST client
│   └── polymarket_client.py    # py_clob_client wrapper (async)
├── workers/
│   ├── scanner.py              # Market scan → signal detection → order creation
│   ├── whale_ingester.py       # Goldsky GraphQL → whale_trades table
│   ├── news_analyzer.py        # GDELT + NewsAPI → news_signals table
│   └── position_tracker.py     # Fill polling, stop-loss, take-profit, resolution
├── signals/
│   ├── kelly.py                # Quarter-Kelly position sizing
│   ├── category_scorer.py      # ROI/trend/winrate composite, allocation tiers
│   ├── whale_scorer.py         # Big-win-rate composite from trade history
│   ├── whale_mirror.py         # Mirror signal generator from queued trades
│   └── news_signal.py          # Sentiment aggregation with recency decay
├── risk/
│   └── portfolio_enforcer.py   # 4-gate pre-trade firewall
├── execution/
│   ├── order_manager.py        # Dispatcher with retry logic
│   ├── kalshi_executor.py      # Kalshi CLOB order submission
│   └── polymarket_executor.py  # Polymarket CLOB order submission
├── alerts/telegram.py          # Async Telegram notifier
├── paper/paper_engine.py       # Simulated fill engine
├── api/
│   ├── websocket.py            # Live dashboard WebSocket
│   └── routes/                 # REST endpoints
├── orchestrator.py             # Worker lifecycle + APScheduler
└── main.py                     # FastAPI entry point

frontend/
├── src/
│   ├── App.tsx                 # Dashboard layout
│   ├── components/             # StatusBar, PositionsTable, PnLChart, StatCard
│   └── lib/                    # API client, useWebSocket hook
└── package.json
```

## Prerequisites

- Python 3.12+
- PostgreSQL 14+
- Node.js 20+ (frontend build only)

## Local Setup

### 1. Clone and configure

```bash
git clone https://github.com/republicanroofer/prediction-bot.git
cd prediction-bot
cp .env.example .env
# Edit .env with your credentials
```

### 2. Database

```bash
createdb predbot
psql predbot < backend/db/migrations/001_initial.sql
```

### 3. Python environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Frontend (optional)

```bash
cd frontend
npm install
npm run dev   # dev server at http://localhost:5173
```

### 5. Run the bot

```bash
# Start with paper trading (default)
uvicorn backend.main:app --reload --port 8000
```

The API and dashboard are at `http://localhost:8000`.

## Configuration

All configuration is via environment variables (`.env` file). Key settings:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | required |
| `TRADING_MODE` | `paper` or `live` | `paper` |
| `ACTIVE_EXCHANGE` | `kalshi`, `polymarket`, or `both` | `both` |
| `KALSHI_API_KEY_ID` | Kalshi API key ID | live mode only |
| `KALSHI_PRIVATE_KEY_PATH` | Path to RSA private key PEM | live mode only |
| `POLYMARKET_WALLET_PRIVATE_KEY` | EVM wallet private key (0x…) | live mode only |
| `KELLY_FRACTION` | Kelly multiplier (0.25 = quarter-Kelly) | `0.25` |
| `MAX_POSITION_PCT` | Max single position as % of portfolio | `0.03` |
| `STOP_LOSS_PCT` | Stop-loss threshold | `0.40` |
| `TAKE_PROFIT_PCT` | Take-profit threshold | `0.80` |
| `MIRROR_DELAY_S` | Delay before mirroring whale trades (seconds) | `45` |
| `MIRROR_MIN_SCORE` | Minimum whale composite score to mirror | `60` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | optional |
| `TELEGRAM_CHAT_ID` | Main alerts chat ID | optional |

See `.env.example` for the full list.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/control/status` | Bot health + config snapshot |
| `POST` | `/api/v1/control/close-all` | Queue close for all open positions |
| `GET` | `/api/v1/positions` | List open positions |
| `DELETE` | `/api/v1/positions/{id}` | Manually close a position |
| `GET` | `/api/v1/orders` | List orders |
| `GET` | `/api/v1/markets` | List tracked markets |
| `GET` | `/api/v1/pnl/daily` | Daily P&L history |
| `GET` | `/api/v1/pnl/summary` | All-time P&L stats |
| `GET` | `/api/v1/signals/news` | Recent news signals |
| `GET` | `/api/v1/signals/category-scores` | Category composite scores |
| `GET` | `/api/v1/whales/scores` | Whale trader leaderboard |
| `WS` | `/ws` | Live dashboard feed (5s snapshots) |

## VPS Deployment (Hetzner / Ubuntu 24.04)

### First-time server setup

```bash
# On the VPS as root
DB_PASS=your_secure_password bash scripts/setup_vps.sh
```

### Deploy / update

```bash
# From your local machine
VPS_HOST=your.server.ip ./scripts/deploy.sh
```

This builds the frontend, rsyncs the code, runs migrations, and restarts the systemd service. The bot runs on port 8000; put nginx in front of it for TLS.

## Signal Logic

### Whale mirroring
Polymarket on-chain trades are indexed via Goldsky GraphQL. Wallets are scored using a composite of big-win-rate (≥70% gain trades), median gain, and overall win rate. Trades from wallets scoring ≥60 are queued for mirroring after a 45-second delay (configurable).

### News signals
Articles from GDELT and NewsAPI are scored for relevance (keyword overlap with market title) and sentiment (positive/negative word lists + GDELT tone field). Signals are aggregated with exponential recency decay (2-hour half-life). Net sentiment must exceed ±0.20 to produce a trade signal; confidence is capped at 0.70.

### Kelly sizing
Position size = `portfolio × kelly_fraction × full_kelly`, where `full_kelly = p - (1-p)×m/(1-m)` for win probability `p` and market odds `m`. Capped at `max_position_pct × portfolio` and the category allocation tier.

### Risk gates (in order)
1. **Category score** ≥ 30 (composite of ROI, sample size, trend, win rate)
2. **Allocation cap** — category exposure must not exceed its tier allocation
3. **Position size** ≤ `max_position_pct × portfolio`
4. **Sector concentration** ≤ `max_sector_concentration_pct × portfolio`

## Disclaimer

This software is for educational purposes. Prediction market trading involves real financial risk. Always start in paper mode and understand the strategy before going live.
