from __future__ import annotations

"""
News analyzer — runs every 5 minutes.

For each active market:
  1. Extract keywords from the market title.
  2. Query NewsAPI for recent articles matching those keywords.
  3. Query GDELT for related news events (GDELT provides a tone/sentiment field).
  4. Score relevance (keyword overlap) and sentiment per article.
  5. Determine directional signal (bullish_yes / bullish_no / neutral).
  6. Store in news_signals table.

Rate-limit awareness:
  - NewsAPI free tier: 100 req/day. We process at most MAX_MARKETS_PER_RUN per tick.
  - GDELT: no hard limit but we keep requests minimal.
  - Articles published in the last LOOKBACK_HOURS are considered.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from backend.config.settings import get_settings
from backend.db.database import Database
from backend.db.models import Exchange, Market, NewsSignalInsert

logger = logging.getLogger(__name__)

MAX_MARKETS_PER_RUN = 50
LOOKBACK_HOURS = 24
MIN_RELEVANCE = 0.25
GDELT_MIN_INTERVAL_S = 6

# Simple sentiment word lists for scoring
_POS_WORDS = frozenset({
    "win", "wins", "won", "victory", "yes", "pass", "approve", "approved",
    "likely", "confirmed", "rise", "rises", "rose", "gain", "gains", "up",
    "increase", "increases", "higher", "positive", "bullish", "advance",
    "succeed", "success", "agree", "agreed", "sign", "signed",
})
_NEG_WORDS = frozenset({
    "lose", "loses", "lost", "defeat", "no", "fail", "fails", "failed",
    "unlikely", "denied", "deny", "drop", "drops", "fell", "fall", "down",
    "decrease", "lower", "negative", "bearish", "decline", "oppose", "opposed",
    "reject", "rejected", "veto", "vetoed", "block", "blocked",
})


class NewsAnalyzerWorker:
    def __init__(
        self,
        db: Database,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        self._db = db
        self._stop = stop_event or asyncio.Event()
        self._cfg = get_settings()
        self._last_gdelt_call: float = 0.0

    async def run(self) -> None:
        logger.info(
            "News analyzer started (interval=%ds)", self._cfg.news_scan_interval_s
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(20)) as http:
            self._http = http
            while not self._stop.is_set():
                try:
                    await self._analyze()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("News analyzer error — will retry next tick")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._cfg.news_scan_interval_s,
                    )
                except asyncio.TimeoutError:
                    pass

        logger.info("News analyzer stopped")

    # ── Main analysis ─────────────────────────────────────────────────────────

    async def _analyze(self) -> None:
        markets = await self._db.get_active_markets()
        # Only analyze markets that close in 1–90 days (matches scanner filters)
        markets = [m for m in markets if m.days_to_close is not None and 1 <= m.days_to_close <= 90]
        # Prioritise markets with higher volume (most likely to have news)
        markets.sort(key=lambda m: float(m.volume_24h_usd or 0), reverse=True)
        batch = markets[:MAX_MARKETS_PER_RUN]

        # URL-level dedup: one set shared across all markets this run.
        # Prevents the same article being stored for 6 different markets.
        self._seen_urls: set[str] = await self._db.get_recent_signal_urls(hours=24)

        total_signals = 0
        for market in batch:
            try:
                count = await self._analyze_market(market)
                total_signals += count
                await asyncio.sleep(0.5)
            except Exception:
                logger.exception("News analysis failed for %s", market.external_id)

        if total_signals:
            logger.info("News analyzer: stored %d new signals across %d markets", total_signals, len(batch))

    async def _analyze_market(self, market: Market) -> int:
        keywords = _extract_keywords(market.title)
        if not keywords:
            return 0

        query_str = " ".join(keywords[:5])  # top 5 keywords
        count = 0

        # NewsAPI — always fetch; dedup by URL across markets
        if self._cfg.newsapi_key:
            articles = await self._fetch_newsapi(query_str)
            for article in articles:
                url = article.get("url") or ""
                if url and url in self._seen_urls:
                    continue
                sig = _score_article(
                    headline=article.get("title", ""),
                    description=article.get("description", ""),
                    url=url or None,
                    published_at=_parse_datetime(article.get("publishedAt")),
                    source="newsapi",
                    market_keywords=keywords,
                    raw=article,
                )
                if sig and sig.relevance_score and sig.relevance_score >= MIN_RELEVANCE:
                    sig.market_id = market.id
                    sig.external_market_id = market.external_id
                    stored_id = await self._db.insert_news_signal(sig)
                    if stored_id:
                        count += 1
                        if url:
                            self._seen_urls.add(url)

        # GDELT — same URL-level dedup
        gdelt_articles = await self._fetch_gdelt(query_str)
        for article in gdelt_articles:
            url = article.get("url") or ""
            if url and url in self._seen_urls:
                continue
            sig = _score_gdelt_article(article=article, market_keywords=keywords)
            if sig and sig.relevance_score and sig.relevance_score >= MIN_RELEVANCE:
                sig.market_id = market.id
                sig.external_market_id = market.external_id
                stored_id = await self._db.insert_news_signal(sig)
                if stored_id:
                    count += 1
                    if url:
                        self._seen_urls.add(url)

        return count

    # ── NewsAPI ───────────────────────────────────────────────────────────────

    async def _fetch_newsapi(self, query: str) -> list[dict]:
        from_dt = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 20,
            "from": from_dt,
            "apiKey": self._cfg.newsapi_key,
        }
        for attempt in range(3):
            try:
                resp = await self._http.get(
                    "https://newsapi.org/v2/everything", params=params
                )
                if resp.status_code == 429:
                    logger.debug("NewsAPI 429 — daily quota likely exhausted, skipping")
                    return []
                if resp.status_code == 426:
                    # Upgrade required — free tier endpoint limit
                    logger.warning("NewsAPI: free tier endpoint limit hit")
                    return []
                resp.raise_for_status()
                data = resp.json()
                return data.get("articles", [])
            except httpx.RequestError as exc:
                if attempt == 2:
                    logger.warning("NewsAPI request failed: %s", exc)
                    return []
                await asyncio.sleep(2 ** attempt)
        return []

    # ── GDELT ─────────────────────────────────────────────────────────────────

    async def _fetch_gdelt(self, query: str) -> list[dict]:
        # Enforce minimum interval between GDELT calls across all markets
        elapsed = time.monotonic() - self._last_gdelt_call
        if elapsed < GDELT_MIN_INTERVAL_S:
            await asyncio.sleep(GDELT_MIN_INTERVAL_S - elapsed)
        self._last_gdelt_call = time.monotonic()

        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": "25",
            "format": "json",
            "timespan": f"{LOOKBACK_HOURS}h",
            "sourcelang": "eng",
        }
        for attempt in range(3):
            try:
                resp = await self._http.get(
                    self._cfg.gdelt_base_url, params=params
                )
                if resp.status_code == 429:
                    # Skip this market — 20s interval will guard the next one
                    logger.debug("GDELT 429 — skipping market, interval will recover")
                    return []
                if resp.status_code >= 500:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("articles", [])
                return []
            except (httpx.RequestError, ValueError) as exc:
                if attempt == 2:
                    logger.warning("GDELT request failed: %s", exc)
                    return []
                await asyncio.sleep(5)
        return []


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _score_article(
    headline: str,
    description: str,
    url: Optional[str],
    published_at: Optional[datetime],
    source: str,
    market_keywords: list[str],
    raw: Optional[dict] = None,
) -> Optional[NewsSignalInsert]:
    if not headline:
        return None

    text = f"{headline} {description or ''}".lower()
    relevance = _keyword_overlap(text, market_keywords)
    if relevance < MIN_RELEVANCE:
        return None

    sentiment = _sentiment_score(text)
    direction = _determine_direction(sentiment)

    return NewsSignalInsert(
        source=source,
        headline=headline[:500],
        url=url,
        published_at=published_at,
        sentiment_score=round(sentiment, 4),
        relevance_score=round(relevance, 4),
        direction=direction,
        keywords=market_keywords[:10],
        raw=raw,
    )


def _score_gdelt_article(
    article: dict,
    market_keywords: list[str],
) -> Optional[NewsSignalInsert]:
    headline = article.get("title", "")
    url = article.get("url")
    published_at = _parse_datetime(article.get("seendate"))

    if not headline:
        return None

    text = headline.lower()
    relevance = _keyword_overlap(text, market_keywords)
    if relevance < MIN_RELEVANCE:
        return None

    # GDELT provides a 'tone' field, but artlist mode often omits it.
    # Fall back to word-based sentiment when tone is missing/zero.
    gdelt_tone = float(article.get("tone", 0) or 0)
    if abs(gdelt_tone) > 0.5:
        sentiment = max(-1.0, min(1.0, gdelt_tone / 25.0))
    else:
        sentiment = _sentiment_score(text)

    # Skip headlines with zero sentiment — game recaps and schedule
    # articles with no directional words add noise without signal.
    if sentiment == 0.0:
        return None
    direction = _determine_direction(sentiment)

    return NewsSignalInsert(
        source="gdelt",
        headline=headline[:500],
        url=url,
        published_at=published_at,
        sentiment_score=round(sentiment, 4),
        relevance_score=round(relevance, 4),
        direction=direction,
        keywords=market_keywords[:10],
        raw={"domain": article.get("domain"), "sourcecountry": article.get("sourcecountry")},
    )


def _keyword_overlap(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    hits = sum(1 for kw in keywords if kw.lower() in text)
    return hits / len(keywords)


def _sentiment_score(text: str) -> float:
    words = re.findall(r"\b\w+\b", text.lower())
    if not words:
        return 0.0
    pos = sum(1 for w in words if w in _POS_WORDS)
    neg = sum(1 for w in words if w in _NEG_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def _determine_direction(sentiment: float) -> str:
    if sentiment >= 0.20:
        return "bullish_yes"
    if sentiment <= -0.20:
        return "bullish_no"
    return "neutral"


def _extract_keywords(title: str) -> list[str]:
    """
    Extract meaningful keywords from a market title.
    Strips stop words and short tokens; returns up to 8 keywords.
    """
    stop_words = {
        "will", "the", "a", "an", "in", "on", "at", "by", "for", "of",
        "to", "be", "is", "are", "was", "were", "or", "and", "but",
        "if", "then", "this", "that", "it", "its", "with", "from",
        "have", "has", "had", "do", "does", "did", "not", "no", "any",
        "who", "what", "when", "where", "how", "which", "more", "than",
        "before", "after", "during", "between", "least", "most",
    }
    tokens = re.findall(r"\b[a-zA-Z][a-zA-Z0-9\-\']+\b", title)
    keywords = [
        t for t in tokens
        if t.lower() not in stop_words and len(t) >= 3
    ]
    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        lw = kw.lower()
        if lw not in seen:
            seen.add(lw)
            unique.append(kw)
    return unique[:8]


def _parse_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    # GDELT format: "20250504T120000Z"
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None
