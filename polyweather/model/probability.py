"""Bucket probabilities from a point forecast.

Phase 2A.1 fix (Day-8 review): the alteregoeth/weatherbot pattern of binary
1.0/0.0 for closed buckets was producing 20% win rate in our paper run because
forecasts have ±2-5°F MAE — saying "p=1.0" on a bucket the forecast lands in
ignores that the true outcome could easily fall in a neighbouring bucket.

We now treat the forecast as the mean of a normal distribution with std=sigma
and integrate the normal CDF over EVERY bucket, including closed ones:

    P(X in [low, high)) = Φ((high - forecast) / σ) − Φ((low - forecast) / σ)
    P(X < high)         = Φ((high - forecast) / σ)         (open bottom)
    P(X >= low)         = 1 − Φ((low - forecast) / σ)      (open top)

This produces honest probabilities (typically 0.4-0.8 for the bucket the
forecast hits, vs the previous 1.0) so EV and Kelly downstream reflect actual
uncertainty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Bucket:
    label: str
    low: float | None
    high: float | None

    def contains(self, value: float) -> bool:
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value >= self.high:
            return False
        return True


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(forecast: float, bucket: Bucket, *, sigma: float) -> float:
    """Probability the actual outcome lands in *bucket*, given a point forecast.

    Treats `forecast` as N(forecast, sigma²) and integrates the normal CDF
    over the bucket's interval. Open-ended sides use one-sided CDF.
    """
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")

    # Open-ended on the bottom: bucket is "< high"
    if bucket.low is None and bucket.high is not None:
        return _norm_cdf((bucket.high - forecast) / sigma)

    # Open-ended on the top: bucket is ">= low"
    if bucket.high is None and bucket.low is not None:
        return 1.0 - _norm_cdf((bucket.low - forecast) / sigma)

    # Fully open (rare): degenerate, always 1.0
    if bucket.low is None and bucket.high is None:
        return 1.0

    # Closed bucket [low, high): difference of two CDFs.
    upper = _norm_cdf((bucket.high - forecast) / sigma)
    lower = _norm_cdf((bucket.low - forecast) / sigma)
    return max(0.0, upper - lower)


def bucket_probabilities(
    forecast: float,
    buckets: list[Bucket],
    *,
    sigma: float,
) -> dict[str, float]:
    """Per-bucket probabilities for a single point forecast.

    All buckets are scored via normal CDF. With well-defined buckets (no gaps
    or overlaps) the values approximately sum to 1.0; we don't force-normalise
    because the EV calculation is per-bucket anyway.
    """
    return {b.label: bucket_prob(forecast, b, sigma=sigma) for b in buckets}


def expected_value(p: float, price: float) -> float:
    """Expected return per $1 staked. Matches alteregoeth's calc_ev:

        EV = p * (1/price - 1) - (1 - p)

    Positive EV ⇔ p > price. Use this as the trade filter (e.g. EV >= 0.10
    means a 10% expected return per $ at the quoted price).
    """
    if price <= 0 or price >= 1:
        return 0.0
    return p * (1.0 / price - 1.0) - (1.0 - p)


def summary_stats(samples: list[float]) -> dict[str, float]:
    """Summary stats for raw ensemble samples (still used by /forecast endpoint)."""
    import numpy as np
    arr = np.asarray(samples, dtype=float)
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "std": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
        "min": float(arr.min()),
        "max": float(arr.max()),
        "p10": float(np.percentile(arr, 10)),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
    }
