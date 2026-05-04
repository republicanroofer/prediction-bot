from __future__ import annotations

"""
NewsSignal — aggregate pre-computed news_signals rows into a single
actionable trade signal for a given market.

The news_analyzer worker stores individual NewsSignal rows; this module
answers the question "given all recent signals for market X, should we
trade, and in which direction?".

Aggregation logic:
  1. Load all signals for the market from the last LOOKBACK_HOURS.
  2. Discard signals below MIN_RELEVANCE.
  3. Weight each signal by relevance × recency_decay (half-life = 2 h).
  4. Compute weighted sentiment (signed +1/-1).
  5. If the absolute weighted sentiment exceeds SIGNAL_THRESHOLD, emit a
     direction; otherwise return None.
  6. Confidence = min(0.70, BASE_CONF + |weighted_sentiment| * CONF_SCALE).
     Capped at 0.70 because news alone is weaker than a whale signal.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from backend.db.database import Database
from backend.db.models import NewsSignal

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 6
MIN_RELEVANCE = 0.30
SIGNAL_THRESHOLD = 0.20    # minimum weighted sentiment to produce a signal
HALF_LIFE_HOURS = 2.0      # recency decay half-life
BASE_CONF = 0.40
CONF_SCALE = 0.30
MAX_CONF = 0.70


@dataclass(frozen=True)
class NewsTradeSignal:
    direction: str          # 'yes' or 'no'
    confidence: float       # 0.0–0.70
    weighted_sentiment: float
    signal_count: int
    best_headline: str


class NewsSignalAggregator:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_signal(
        self, market_id: UUID, hours: int = LOOKBACK_HOURS
    ) -> Optional[NewsTradeSignal]:
        """
        Return the aggregated news signal for this market, or None if
        news is not strong enough to trade on.
        """
        signals = await self._db.get_recent_signals_for_market(market_id, hours=hours)
        return aggregate(signals)


# ── Pure aggregation function ─────────────────────────────────────────────────

def aggregate(signals: list[NewsSignal]) -> Optional[NewsTradeSignal]:
    """
    Aggregate a list of NewsSignal rows into a single directional signal.
    Returns None if the net signal is below threshold or signals list is empty.
    """
    if not signals:
        return None

    now = datetime.now(timezone.utc)
    total_weight = 0.0
    weighted_sent = 0.0
    best_signal: Optional[NewsSignal] = None
    best_score = -1.0

    for sig in signals:
        relevance = float(sig.relevance_score or 0)
        if relevance < MIN_RELEVANCE:
            continue

        sentiment = float(sig.sentiment_score or 0)
        age_hours = _age_hours(sig.created_at, now)
        decay = math.exp(-math.log(2) * age_hours / HALF_LIFE_HOURS)
        weight = relevance * decay

        weighted_sent += sentiment * weight
        total_weight += weight

        score = relevance * decay
        if score > best_score:
            best_score = score
            best_signal = sig

    if total_weight <= 0:
        return None

    net_sentiment = weighted_sent / total_weight

    if abs(net_sentiment) < SIGNAL_THRESHOLD:
        return None

    direction = "yes" if net_sentiment > 0 else "no"
    confidence = min(MAX_CONF, BASE_CONF + abs(net_sentiment) * CONF_SCALE)

    best_headline = best_signal.headline if best_signal else ""

    return NewsTradeSignal(
        direction=direction,
        confidence=round(confidence, 4),
        weighted_sentiment=round(net_sentiment, 4),
        signal_count=len([s for s in signals if float(s.relevance_score or 0) >= MIN_RELEVANCE]),
        best_headline=best_headline[:200],
    )


def _age_hours(created_at: datetime, now: datetime) -> float:
    ct = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ct).total_seconds() / 3600)
