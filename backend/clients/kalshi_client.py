from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from backend.config.settings import get_settings


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Kalshi API {status_code}: {message}")


class KalshiClient:
    """
    Async Kalshi REST client with RSA-PSS request signing.

    Kalshi's auth spec: sign the string `{timestamp_ms}{METHOD}{path}` using
    RSA-PSS with SHA-256, MGF1, and salt_length=DIGEST_LENGTH. Three headers
    carry the key ID, timestamp, and base64-encoded signature.

    Usage:
        async with KalshiClient.from_settings() as client:
            balance = await client.get_balance()
    """

    _BASE_URL = "https://api.elections.kalshi.com"
    _PSS_PADDING = padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,
    )

    def __init__(
        self,
        api_key_id: str,
        private_key: RSAPrivateKey,
        base_url: str | None = None,
        request_delay_ms: int = 200,
        max_retries: int = 5,
        timeout_s: int = 30,
    ) -> None:
        self._api_key_id = api_key_id
        self._private_key = private_key
        self._base_url = (base_url or self._BASE_URL).rstrip("/")
        self._delay = request_delay_ms / 1000.0
        self._max_retries = max_retries
        self._timeout = httpx.Timeout(timeout_s)
        self._last_request_at: float = 0.0
        self._http: httpx.AsyncClient | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "KalshiClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @classmethod
    def from_settings(cls) -> "KalshiClient":
        cfg = get_settings()
        key_bytes = cfg.kalshi_private_key_path.read_bytes()
        private_key: RSAPrivateKey = serialization.load_pem_private_key(  # type: ignore[assignment]
            key_bytes, password=None
        )
        return cls(
            api_key_id=cfg.kalshi_api_key_id,
            private_key=private_key,
            base_url=cfg.kalshi_base_url,
            request_delay_ms=cfg.kalshi_request_delay_ms,
            max_retries=cfg.kalshi_max_retries,
            timeout_s=cfg.kalshi_timeout_s,
        )

    # ── Auth signing ──────────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        message = (ts_ms + method.upper() + path).encode()
        sig = self._private_key.sign(message, self._PSS_PADDING, hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._delay:
            await asyncio.sleep(self._delay - elapsed)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        if not self._http:
            raise RuntimeError("KalshiClient must be used as an async context manager")

        url = self._base_url + path

        for attempt in range(self._max_retries + 1):
            await self._throttle()
            self._last_request_at = time.monotonic()
            headers = self._auth_headers(method, path)

            try:
                resp = await self._http.request(
                    method, url, headers=headers, params=params, json=json
                )
            except httpx.RequestError as exc:
                if attempt == self._max_retries:
                    raise KalshiAPIError(0, f"Network error: {exc}") from exc
                await asyncio.sleep(2 ** attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    raise KalshiAPIError(resp.status_code, resp.text)
                await asyncio.sleep(2 ** attempt)
                continue

            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text)

            return resp.json()

        raise KalshiAPIError(0, "Max retries exceeded")

    async def _get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict) -> Any:
        return await self._request("POST", path, json=json)

    async def _patch(self, path: str, json: dict) -> Any:
        return await self._request("PATCH", path, json=json)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict:
        return await self._get("/trade-api/v2/portfolio/balance")

    async def get_positions(
        self,
        ticker: str | None = None,
        count_filter: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        p: dict = {"limit": limit}
        if ticker:
            p["ticker"] = ticker
        if count_filter:
            p["count_filter"] = count_filter
        if cursor:
            p["cursor"] = cursor
        return await self._get("/trade-api/v2/portfolio/positions", params=p)

    async def get_all_positions(self) -> list[dict]:
        positions: list[dict] = []
        cursor: str | None = None
        while True:
            data = await self.get_positions(cursor=cursor, limit=200)
            positions.extend(data.get("market_positions", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return positions

    async def get_orders(
        self,
        ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        p: dict = {"limit": limit}
        if ticker:
            p["ticker"] = ticker
        if event_ticker:
            p["event_ticker"] = event_ticker
        if status:
            p["status"] = status
        if cursor:
            p["cursor"] = cursor
        return await self._get("/trade-api/v2/portfolio/orders", params=p)

    async def get_open_orders(self) -> list[dict]:
        orders: list[dict] = []
        cursor: str | None = None
        while True:
            data = await self.get_orders(status="resting", cursor=cursor, limit=200)
            orders.extend(data.get("orders", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return orders

    async def get_order(self, order_id: str) -> dict:
        return await self._get(f"/trade-api/v2/portfolio/orders/{order_id}")

    async def get_fills(
        self,
        ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        p: dict = {"limit": limit}
        if ticker:
            p["ticker"] = ticker
        if cursor:
            p["cursor"] = cursor
        return await self._get("/trade-api/v2/portfolio/fills", params=p)

    async def get_trades(
        self,
        ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        p: dict = {"limit": limit}
        if ticker:
            p["ticker"] = ticker
        if cursor:
            p["cursor"] = cursor
        return await self._get("/trade-api/v2/portfolio/trades", params=p)

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,              # 'yes' or 'no'
        action: str,            # 'buy' or 'sell'
        count: int,             # number of contracts (1 contract = $1 max payout)
        order_type: str = "limit",
        yes_price: int | None = None,   # cents, 1–99
        no_price: int | None = None,
        client_order_id: str | None = None,
        expiration_ts: int | None = None,
    ) -> dict:
        body: dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id
        if expiration_ts:
            body["expiration_ts"] = expiration_ts
        return await self._post("/trade-api/v2/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/trade-api/v2/portfolio/orders/{order_id}")

    async def amend_order(
        self,
        order_id: str,
        count: int | None = None,
        yes_price: int | None = None,
        no_price: int | None = None,
    ) -> dict:
        body: dict = {}
        if count is not None:
            body["count"] = count
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        return await self._patch(f"/trade-api/v2/portfolio/orders/{order_id}", body)

    async def cancel_all_orders(self, ticker: str | None = None) -> dict:
        p = {"ticker": ticker} if ticker else None
        return await self._delete("/trade-api/v2/portfolio/orders")

    # ── Markets ───────────────────────────────────────────────────────────────

    async def get_market(self, ticker: str) -> dict:
        return await self._get(f"/trade-api/v2/markets/{ticker}")

    async def get_markets(
        self,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        p: dict = {"limit": limit}
        if event_ticker:
            p["event_ticker"] = event_ticker
        if status:
            p["status"] = status
        if tickers:
            p["tickers"] = tickers
        if cursor:
            p["cursor"] = cursor
        return await self._get("/trade-api/v2/markets", params=p)

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return await self._get(
            f"/trade-api/v2/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    async def get_market_history(
        self,
        ticker: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        p: dict = {"limit": limit}
        if cursor:
            p["cursor"] = cursor
        return await self._get(f"/trade-api/v2/markets/{ticker}/history", params=p)

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events(
        self,
        status: str = "open",
        with_nested_markets: bool = True,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        p: dict = {
            "status": status,
            "with_nested_markets": str(with_nested_markets).lower(),
            "limit": limit,
        }
        if cursor:
            p["cursor"] = cursor
        return await self._get("/trade-api/v2/events", params=p)

    async def get_all_events(self, status: str = "open", max_pages: int = 25) -> list[dict]:
        events: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self.get_events(status=status, cursor=cursor)
            batch: list[dict] = data.get("events", [])
            events.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return events

    async def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> dict:
        return await self._get(
            f"/trade-api/v2/events/{event_ticker}",
            params={"with_nested_markets": str(with_nested_markets).lower()},
        )

    # ── Series ────────────────────────────────────────────────────────────────

    async def get_series(self, series_ticker: str) -> dict:
        return await self._get(f"/trade-api/v2/series/{series_ticker}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def parse_mid_price(market: dict) -> float | None:
        """Return the YES mid-price (0–1) from a market dict, handling both
        dollar-format (yes_bid_dollars) and cent-format (yes_bid) APIs."""
        try:
            if "yes_bid_dollars" in market and "yes_ask_dollars" in market:
                bid = float(market["yes_bid_dollars"])
                ask = float(market["yes_ask_dollars"])
            elif "yes_bid" in market and "yes_ask" in market:
                bid = float(market["yes_bid"]) / 100
                ask = float(market["yes_ask"]) / 100
            else:
                return None
            return (bid + ask) / 2
        except (TypeError, ValueError):
            return None

    @staticmethod
    def price_to_cents(price: float) -> int:
        """Convert a probability (0.01–0.99) to Kalshi cents (1–99), clamped."""
        return max(1, min(99, round(price * 100)))
