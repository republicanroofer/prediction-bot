from __future__ import annotations

"""
Kelly Criterion position sizing for binary prediction markets.

Formula for a YES position at market price m with estimated win probability p:
    b (net odds) = (1 - m) / m
    f* (full Kelly) = (p*b - (1-p)) / b  =  p - (1-p)*m/(1-m)

For a NO trade, pass estimated_prob = P(NO) and market_price = NO price.
Quarter-Kelly (f* × 0.25) is the default; full Kelly is too volatile in
practice because it assumes perfect probability calibration.

Caps applied in order:
  1. category_alloc_pct  (per-category ceiling from CategoryScore)
  2. max_position_pct    (hard per-trade ceiling from settings)
"""

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class KellyResult:
    size_usd: float
    fraction: float      # final applied fraction of portfolio
    full_kelly: float    # uncapped Kelly fraction (for monitoring)
    edge: float          # estimated_prob - market_price
    rationale: str


class KellyCalculator:
    """
    Stateless position sizer. Construct once via from_settings() and reuse.

    kelly_fraction : fraction of full Kelly (0.25 = quarter-Kelly)
    max_position_pct : hard cap per single trade as a fraction of portfolio
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_position_pct: float = 0.03,
    ) -> None:
        if not 0 < kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not 0 < max_position_pct < 1:
            raise ValueError("max_position_pct must be in (0, 1)")
        self._kf = kelly_fraction
        self._max_pct = max_position_pct

    def size(
        self,
        estimated_prob: float,
        market_price: float,
        portfolio_usd: float,
        side: str = "yes",
        category_alloc_pct: Optional[float] = None,
    ) -> KellyResult:
        """
        Compute position size in USD.

        estimated_prob  : our probability estimate for the side we're buying
        market_price    : current mid-price of that side (0.01–0.99)
        portfolio_usd   : available capital
        category_alloc_pct : optional per-category allocation cap (0–1)
        """
        if portfolio_usd <= 0:
            return KellyResult(0.0, 0.0, 0.0, 0.0, "portfolio_usd <= 0")

        p = _clamp(estimated_prob, 0.01, 0.99)
        m = _clamp(market_price, 0.01, 0.99)
        edge = p - m

        if edge <= 0:
            return KellyResult(
                0.0, 0.0, 0.0, edge,
                f"no edge: p={p:.3f} price={m:.3f}",
            )

        fk = _full_kelly(p, m)
        if fk <= 0:
            return KellyResult(0.0, 0.0, fk, edge, f"full_kelly={fk:.4f}")

        fraction = fk * self._kf

        if category_alloc_pct is not None and category_alloc_pct > 0:
            fraction = min(fraction, float(category_alloc_pct))

        fraction = min(fraction, self._max_pct)
        size_usd = round(portfolio_usd * fraction, 2)

        rationale = (
            f"side={side} p={p:.3f} mkt={m:.3f} edge={edge:.3f} "
            f"fullK={fk:.4f} frac={fraction:.4f} ${size_usd:.2f}"
        )
        return KellyResult(size_usd, fraction, fk, edge, rationale)

    def size_from_confidence(
        self,
        confidence: float,
        market_price: float,
        portfolio_usd: float,
        side: str = "yes",
        category_alloc_pct: Optional[float] = None,
    ) -> KellyResult:
        """Treat confidence directly as estimated_prob (signal-level shortcut)."""
        return self.size(
            estimated_prob=confidence,
            market_price=market_price,
            portfolio_usd=portfolio_usd,
            side=side,
            category_alloc_pct=category_alloc_pct,
        )

    def expected_log_growth(
        self, estimated_prob: float, market_price: float
    ) -> float:
        """Expected log-growth at the Kelly-optimal fraction. Positive = +EV."""
        p = _clamp(estimated_prob, 0.01, 0.99)
        m = _clamp(market_price, 0.01, 0.99)
        f = _full_kelly(p, m)
        if f <= 0:
            return float("-inf")
        b = (1 - m) / m
        try:
            return p * math.log(1 + f * b) + (1 - p) * math.log(1 - f)
        except (ValueError, ZeroDivisionError):
            return float("-inf")


def from_settings() -> KellyCalculator:
    from backend.config.settings import get_settings
    cfg = get_settings()
    return KellyCalculator(
        kelly_fraction=cfg.kelly_fraction,
        max_position_pct=cfg.max_position_pct,
    )


# ── Internals ─────────────────────────────────────────────────────────────────

def _full_kelly(p: float, m: float) -> float:
    if m <= 0 or m >= 1:
        return 0.0
    return p - (1 - p) * m / (1 - m)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
