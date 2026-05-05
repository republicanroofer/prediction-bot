from __future__ import annotations

import asyncio
from typing import Any

import httpx

from backend.config.settings import get_settings

# py_clob_client is synchronous; all calls are wrapped in run_in_executor.
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        BookParams,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        TradeParams,
    )
    from py_clob_client.constants import POLYGON
    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    POLYGON = 137


# Polymarket's own liquidity / fee wallets — excluded from whale signal generation
PLATFORM_WALLETS: frozenset[str] = frozenset({
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
})


class PolymarketAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Polymarket API {status_code}: {message}")


# ── GammaClient ───────────────────────────────────────────────────────────────

class GammaClient:
    """
    Async REST client for the Polymarket Gamma API (read-only market metadata).
    No authentication required. All markets, events, and token mappings come
    from here before any CLOB interaction.
    """

    def __init__(self, base_url: str, timeout_s: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s)
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GammaClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @classmethod
    def from_settings(cls) -> "GammaClient":
        cfg = get_settings()
        return cls(base_url=cfg.polymarket_gamma_url)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self._http:
            raise RuntimeError("GammaClient must be used as an async context manager")
        for attempt in range(5):
            try:
                resp = await self._http.get(self._base_url + path, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.RequestError as exc:
                if attempt == 4:
                    raise PolymarketAPIError(0, str(exc)) from exc
                await asyncio.sleep(2 ** attempt)
        raise PolymarketAPIError(0, "Max retries exceeded")

    # ── Markets ───────────────────────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 500,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        order_by: str | None = None,
        ascending: bool = False,
    ) -> list[dict]:
        params: dict = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "archived": str(archived).lower(),
        }
        if order_by:
            params["order"] = order_by
            params["ascending"] = str(ascending).lower()
        return await self._get("/markets", params=params)

    async def get_all_markets(
        self,
        active_only: bool = True,
        order_by: str | None = None,
        ascending: bool = False,
        max_total: int | None = None,
    ) -> list[dict]:
        markets: list[dict] = []
        offset = 0
        limit = 500
        while True:
            remaining = (max_total - len(markets)) if max_total else limit
            batch_size = min(limit, remaining) if max_total else limit
            batch = await self.get_markets(
                limit=batch_size,
                offset=offset,
                active=active_only,
                order_by=order_by,
                ascending=ascending,
            )
            if not batch:
                break
            markets.extend(batch)
            if max_total and len(markets) >= max_total:
                break
            if len(batch) < batch_size:
                break
            offset += batch_size
        return markets

    async def get_market(self, condition_id: str) -> dict:
        return await self._get(f"/markets/{condition_id}")

    async def get_clob_tradable_markets(self, max_total: int = 2000) -> list[dict]:
        """
        Return CLOB-tradable markets sorted by 24h volume descending, capped at
        max_total. Fetching all 50k+ markets is wasteful — the top 2,000 by volume
        covers every market worth trading.
        """
        markets = await self.get_all_markets(
            active_only=True,
            order_by="volume24hr",
            ascending=False,
            max_total=max_total,
        )
        return [
            m for m in markets
            if m.get("clobTokenIds") and m.get("active") and not m.get("closed")
        ]

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events(
        self,
        limit: int = 500,
        offset: int = 0,
        active: bool = True,
    ) -> list[dict]:
        return await self._get("/events", params={
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
        })

    async def get_all_events(self) -> list[dict]:
        events: list[dict] = []
        offset = 0
        limit = 500
        while True:
            batch = await self.get_events(limit=limit, offset=offset)
            if not batch:
                break
            events.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return events

    async def get_event(self, slug: str) -> dict:
        return await self._get(f"/events/{slug}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def parse_token_ids(market: dict) -> tuple[str, str]:
        """
        Extract YES and NO token IDs from a Gamma market dict.
        clobTokenIds is a JSON-stringified list: '["yes_token_id", "no_token_id"]'
        """
        import json
        raw = market.get("clobTokenIds", "[]")
        if isinstance(raw, str):
            ids = json.loads(raw)
        else:
            ids = raw
        yes = ids[0] if len(ids) > 0 else ""
        no = ids[1] if len(ids) > 1 else ""
        return yes, no

    @staticmethod
    def parse_outcome_prices(market: dict) -> tuple[float, float]:
        """
        Return (yes_price, no_price) from outcomePrices.
        outcomePrices is a JSON-stringified list of string floats.
        """
        import json
        raw = market.get("outcomePrices", "[0.5, 0.5]")
        if isinstance(raw, str):
            prices = json.loads(raw)
        else:
            prices = raw
        try:
            yes = float(prices[0]) if len(prices) > 0 else 0.5
            no = float(prices[1]) if len(prices) > 1 else 1.0 - yes
        except (ValueError, TypeError):
            yes, no = 0.5, 0.5
        return yes, no


# ── PolymarketClobClient ──────────────────────────────────────────────────────

class PolymarketClobClient:
    """
    Async wrapper around py_clob_client for Polymarket CLOB order execution.

    Authentication is wallet-based: the CLOB derives per-session API credentials
    from an EIP-712 signature of the wallet's private key — no separate API key
    or secret is needed.

    py_clob_client is synchronous. Every call is dispatched to the default
    thread-pool executor via asyncio.get_event_loop().run_in_executor().

    Usage:
        async with PolymarketClobClient.from_settings() as clob:
            balance = await clob.get_balance()
            order = await clob.place_limit_order(token_id, price=0.72, size=50, side="BUY")
    """

    def __init__(
        self,
        private_key: str,
        clob_url: str,
        chain_id: int = POLYGON,
    ) -> None:
        if not _CLOB_AVAILABLE:
            raise RuntimeError(
                "py_clob_client is not installed. Run: pip install py_clob_client"
            )
        self._private_key = private_key
        self._clob_url = clob_url
        self._chain_id = chain_id
        self._client: "ClobClient | None" = None

    async def __aenter__(self) -> "PolymarketClobClient":
        loop = asyncio.get_event_loop()
        self._client = await loop.run_in_executor(None, self._init_client)
        await self._init_approvals()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._client = None

    @classmethod
    def from_settings(cls) -> "PolymarketClobClient":
        cfg = get_settings()
        return cls(
            private_key=cfg.polygon_wallet_private_key,
            clob_url=cfg.polymarket_clob_url,
            chain_id=cfg.polymarket_chain_id,
        )

    def _init_client(self) -> "ClobClient":
        client = ClobClient(
            host=self._clob_url,
            key=self._private_key,
            chain_id=self._chain_id,
        )
        creds: ApiCreds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return client

    def _client_or_raise(self) -> "ClobClient":
        if not self._client:
            raise RuntimeError(
                "PolymarketClobClient must be used as an async context manager"
            )
        return self._client

    async def _run(self, fn, *args, **kwargs) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def _init_approvals(self) -> None:
        client = self._client_or_raise()
        try:
            await self._run(client.set_allowance, "usdc")
        except Exception:
            # Already approved or no USDC balance yet — safe to ignore on startup.
            pass

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict:
        client = self._client_or_raise()
        return await self._run(client.get_balance)

    async def get_api_keys(self) -> list[dict]:
        client = self._client_or_raise()
        return await self._run(client.get_api_keys)

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_market(self, condition_id: str) -> dict:
        client = self._client_or_raise()
        return await self._run(client.get_market, condition_id)

    async def get_markets(self, next_cursor: str = "LTE=") -> dict:
        client = self._client_or_raise()
        return await self._run(client.get_markets, next_cursor)

    async def get_orderbook(self, token_id: str) -> dict:
        client = self._client_or_raise()
        params = BookParams(token_id=token_id)
        return await self._run(client.get_order_book, params)

    async def get_orderbook_mid(self, token_id: str) -> float:
        """Return best-bid/ask midpoint (0.0–1.0)."""
        book = await self.get_orderbook(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        return (best_bid + best_ask) / 2

    async def get_best_ask(self, token_id: str) -> float:
        book = await self.get_orderbook(token_id)
        asks = book.get("asks", [])
        return float(asks[0]["price"]) if asks else 1.0

    async def get_best_bid(self, token_id: str) -> float:
        book = await self.get_orderbook(token_id)
        bids = book.get("bids", [])
        return float(bids[0]["price"]) if bids else 0.0

    async def get_tick_size(self, token_id: str) -> float:
        market = await self.get_market(token_id)
        return float(market.get("minimum_tick_size", 0.01))

    # ── Open orders ───────────────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict:
        client = self._client_or_raise()
        return await self._run(client.get_order, order_id)

    async def get_open_orders(self, market: str | None = None) -> list[dict]:
        client = self._client_or_raise()
        if market:
            return await self._run(client.get_orders, {"market": market})
        return await self._run(client.get_orders)

    async def get_trades(
        self,
        maker_address: str | None = None,
        market: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        client = self._client_or_raise()
        p = TradeParams(
            maker_address=maker_address or "",
            market=market or "",
        )
        return await self._run(client.get_trades, p)

    # ── Order execution ───────────────────────────────────────────────────────

    async def place_limit_order(
        self,
        token_id: str,
        price: float,     # probability 0.01–0.99
        size: float,      # USDC amount
        side: str,        # 'BUY' or 'SELL'
    ) -> dict:
        """
        Place a GTC limit order. price is the probability (e.g. 0.72 = 72¢).
        size is the USDC notional. Returns the CLOB response dict.
        """
        client = self._client_or_raise()

        args = OrderArgs(
            token_id=token_id,
            price=round(price, 2),
            size=round(size, 2),
            side=side.upper(),
        )
        options = PartialCreateOrderOptions(tick_size=0.01, neg_risk=False)

        signed_order = await self._run(client.create_order, args, options)
        return await self._run(client.post_order, signed_order, OrderType.GTC)

    async def place_market_order(
        self,
        token_id: str,
        amount: float,    # USDC amount to spend
        side: str,        # 'BUY' or 'SELL'
    ) -> dict:
        """Fill-or-kill market order. Useful for urgent mirroring."""
        client = self._client_or_raise()

        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(amount, 2),
        )
        signed_order = await self._run(
            client.create_market_order, args
        )
        return await self._run(client.post_order, signed_order, OrderType.FOK)

    async def cancel_order(self, order_id: str) -> dict:
        client = self._client_or_raise()
        return await self._run(client.cancel, order_id)

    async def cancel_all_orders(self) -> dict:
        client = self._client_or_raise()
        return await self._run(client.cancel_all)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def is_platform_wallet(address: str) -> bool:
        return address.lower() in PLATFORM_WALLETS

    @staticmethod
    def normalize_price(raw_amount: int, usdc_side: bool = True) -> float:
        """Convert raw Goldsky integer amounts (6 decimals) to float."""
        return raw_amount / 1_000_000


# ── Convenience factory ───────────────────────────────────────────────────────

async def build_polymarket_clients() -> tuple[GammaClient, PolymarketClobClient]:
    """
    Enter both clients and return them. Caller is responsible for calling
    __aexit__ on each, or using them individually as context managers.
    """
    cfg = get_settings()
    gamma = GammaClient(base_url=cfg.polymarket_gamma_url)
    clob = PolymarketClobClient(
        private_key=cfg.polygon_wallet_private_key,
        clob_url=cfg.polymarket_clob_url,
        chain_id=cfg.polymarket_chain_id,
    )
    return gamma, clob
