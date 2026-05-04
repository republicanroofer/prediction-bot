from __future__ import annotations

"""
Orchestrator — owns all background workers and their lifecycle.

Startup sequence:
  1. Connect to DB
  2. Initialise exchange clients (conditionally based on active_exchange)
  3. Build executor + OrderManager
  4. Start workers: Scanner, WhaleIngester, NewsAnalyzer, PositionTracker
  5. Schedule periodic tasks via APScheduler:
       - WhaleScorer.run_once()       : every 4 hours
       - CategoryScorer.rebuild()     : every 6 hours
       - daily_pnl snapshot           : every hour

Shutdown:
  - asyncio.Event signals all workers to exit their run-loops
  - APScheduler is shut down gracefully
  - Exchange clients are closed
  - DB pool is closed

The orchestrator is instantiated once in main.py and stored in app.state
so API routes can access the DB and other shared objects.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.alerts.telegram import get_alerter
from backend.clients.kalshi_client import KalshiClient
from backend.clients.polymarket_client import GammaClient, PolymarketClobClient
from backend.config.settings import ActiveExchange, TradingMode, get_settings
from backend.db.database import Database
from backend.execution.kalshi_executor import KalshiExecutor
from backend.execution.order_manager import OrderManager
from backend.execution.polymarket_executor import PolymarketExecutor
from backend.paper.paper_engine import PaperEngine
from backend.signals.category_scorer import CategoryScorer
from backend.signals.whale_scorer import WhaleScorer
from backend.workers.news_analyzer import NewsAnalyzerWorker
from backend.workers.position_tracker import PositionTrackerWorker
from backend.workers.scanner import ScannerWorker
from backend.workers.whale_ingester import WhaleIngesterWorker

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self) -> None:
        self.db: Optional[Database] = None
        self.paper_engine: Optional[PaperEngine] = None
        self.order_manager: Optional[OrderManager] = None
        self._stop = asyncio.Event()
        self._scheduler = AsyncIOScheduler()
        self._tasks: list[asyncio.Task] = []

        # Exchange client refs for cleanup
        self._kalshi: Optional[KalshiClient] = None
        self._clob: Optional[PolymarketClobClient] = None

    async def start(self) -> None:
        cfg = get_settings()
        alerter = get_alerter()

        logger.info("Orchestrator: connecting to database")
        self.db = await Database.create()

        # ── Exchange clients ──────────────────────────────────────────────────
        kalshi: Optional[KalshiClient] = None
        gamma: Optional[GammaClient] = None
        clob: Optional[PolymarketClobClient] = None

        use_kalshi = cfg.active_exchange in (ActiveExchange.KALSHI, ActiveExchange.BOTH)
        use_poly = cfg.active_exchange in (ActiveExchange.POLYMARKET, ActiveExchange.BOTH)

        if use_kalshi:
            try:
                kalshi = KalshiClient.from_settings()
                await kalshi.__aenter__()
                self._kalshi = kalshi
                logger.info("Orchestrator: Kalshi client ready")
            except Exception as exc:
                logger.error("Orchestrator: Kalshi client failed: %s", exc)
                kalshi = None

        if use_poly:
            # Gamma is read-only (no auth) — always try to start it
            try:
                gamma = GammaClient.from_settings()
                logger.info("Orchestrator: Polymarket Gamma client ready")
            except Exception as exc:
                logger.error("Orchestrator: Gamma client failed: %s", exc)
                gamma = None

            # CLOB requires wallet key — only needed for live trading
            try:
                clob = PolymarketClobClient.from_settings()
                await clob.__aenter__()
                self._clob = clob
                logger.info("Orchestrator: Polymarket CLOB client ready")
            except Exception as exc:
                logger.warning("Orchestrator: CLOB client unavailable (paper/read-only mode): %s", exc)
                clob = None

        # ── Executors + OrderManager ──────────────────────────────────────────
        self.paper_engine = PaperEngine(self.db)

        kalshi_exec = KalshiExecutor(kalshi) if kalshi else None
        poly_exec = PolymarketExecutor(clob, self.db) if clob else None

        self.order_manager = OrderManager(
            db=self.db,
            kalshi_executor=kalshi_exec,
            poly_executor=poly_exec,
        )

        # ── Workers ───────────────────────────────────────────────────────────
        workers = []

        if kalshi or gamma:
            scanner = ScannerWorker(
                db=self.db,
                kalshi=kalshi,
                gamma=gamma,
                stop_event=self._stop,
            )
            workers.append(scanner.run())

        if use_poly:
            ingester = WhaleIngesterWorker(
                db=self.db,
                stop_event=self._stop,
            )
            workers.append(ingester.run())

        news = NewsAnalyzerWorker(
            db=self.db,
            stop_event=self._stop,
        )
        workers.append(news.run())

        tracker = PositionTrackerWorker(
            db=self.db,
            kalshi=kalshi,
            clob=clob,
            stop_event=self._stop,
        )
        workers.append(tracker.run())

        self._tasks = [asyncio.create_task(w, name=f"worker-{i}") for i, w in enumerate(workers)]

        # ── Scheduled maintenance tasks ───────────────────────────────────────
        whale_scorer = WhaleScorer(self.db)
        cat_scorer = CategoryScorer(self.db)

        self._scheduler.add_job(
            whale_scorer.run_once,
            "interval",
            minutes=30,
            id="whale_scorer",
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            cat_scorer.rebuild,
            "interval",
            hours=6,
            id="category_scorer",
            misfire_grace_time=300,
        )
        self._scheduler.start()

        await alerter.info(
            f"Bot started — mode={cfg.trading_mode.value} "
            f"exchange={cfg.active_exchange.value}"
        )
        logger.info(
            "Orchestrator: %d workers started, scheduler running",
            len(self._tasks),
        )

    async def stop(self) -> None:
        logger.info("Orchestrator: shutting down")
        self._stop.set()

        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self._kalshi:
            try:
                await self._kalshi.__aexit__(None, None, None)
            except Exception:
                pass

        if self._clob:
            try:
                await self._clob.__aexit__(None, None, None)
            except Exception:
                pass

        if self.db:
            await self.db.close()

        logger.info("Orchestrator: shutdown complete")


# ── FastAPI lifespan helper ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):  # type: ignore[type-arg]
    orch = Orchestrator()
    app.state.orchestrator = orch
    app.state.db = None  # will be set by orch.start()

    try:
        await orch.start()
        app.state.db = orch.db
        yield
    finally:
        await orch.stop()
