"""Bucket probabilities from a point forecast.

Switched from Laplace-smoothed ensemble (Day-1) to alteregoeth/weatherbot's
proven approach (Day-5 calibration showed Laplace was fabricating phantom
edges in tail buckets):

  - For closed buckets [low, high): probability is 1.0 if the forecast point
    falls inside, 0.0 otherwise.
  - For open-ended (tail) buckets — "<X°F" or ">=X°F" — use a normal CDF
    around the forecast point with sigma = expected forecast error.

Sigma defaults are conservative (2°F US / 1.2°C). Self-calibration (Phase 2C)
will replace these with empirically-learned per-(city, source) MAEs.
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

    Closed bucket [low, high) → 1.0 if forecast inside, else 0.0.
    Open-ended bucket (low or high is None) → normal CDF using sigma.
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

    # Closed bucket: deterministic match on the forecast point.
    return 1.0 if bucket.contains(forecast) else 0.0


def bucket_probabilities(
    forecast: float,
    buckets: list[Bucket],
    *,
    sigma: float,
) -> dict[str, float]:
    """Per-bucket probabilities for a single point forecast.

    Closed buckets get 1.0/0.0; tail buckets use sigma. The result is NOT
    forced to sum to 1.0 because tail buckets at both ends of a finite-bucket
    market are a small leakage we tolerate (the EV calculation is per-bucket
    anyway).
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
