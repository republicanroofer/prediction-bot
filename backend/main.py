from __future__ import annotations

"""
main.py — FastAPI application entry point.

Startup:
  1. Lifespan context manager (orchestrator.py) boots DB + workers.
  2. API routers are mounted.
  3. CORS is configured for the Vite dev server (localhost:5173) and
     the production frontend origin.
  4. Static files are served from /static if the frontend is built.

Run locally:
  uvicorn backend.main:app --reload --port 8000

Environment:
  DATABASE_URL, KALSHI_*, POLYMARKET_*, TRADING_MODE, etc. — see .env.example
"""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.routes.activity import router as activity_router
from backend.api.routes.analytics import router as analytics_router
from backend.api.routes.control import router as control_router
from backend.api.routes.markets import router as markets_router
from backend.api.routes.orders import router as orders_router
from backend.api.routes.pnl import router as pnl_router
from backend.api.routes.positions import router as positions_router
from backend.api.routes.signals import router as signals_router
from backend.api.routes.whales import router as whales_router
from backend.api.websocket import router as ws_router
from backend.orchestrator import lifespan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Prediction Bot",
    version="1.0.0",
    description="Kalshi + Polymarket automated trading bot",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────

_ORIGINS = [
    "http://localhost:5173",   # Vite dev
    "http://localhost:3000",   # alternative dev
    os.getenv("FRONTEND_ORIGIN", ""),
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in _ORIGINS if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

_API_PREFIX = "/api/v1"

app.include_router(ws_router)
app.include_router(activity_router, prefix=_API_PREFIX)
app.include_router(analytics_router, prefix=_API_PREFIX)
app.include_router(control_router, prefix=_API_PREFIX)
app.include_router(markets_router, prefix=_API_PREFIX)
app.include_router(positions_router, prefix=_API_PREFIX)
app.include_router(orders_router, prefix=_API_PREFIX)
app.include_router(pnl_router, prefix=_API_PREFIX)
app.include_router(signals_router, prefix=_API_PREFIX)
app.include_router(whales_router, prefix=_API_PREFIX)

# ── Static frontend (production) ──────────────────────────────────────────────

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    logger.info("Serving frontend from %s", _STATIC_DIR)
