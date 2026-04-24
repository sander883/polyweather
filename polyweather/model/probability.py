"""Convert an ensemble sample into per-bucket probabilities.

A ``Bucket`` is a half-open interval ``[low, high)`` in °F. Either bound may be
``None`` for open-ended buckets (e.g. ``"< 50°F"`` or ``">= 90°F"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from polyweather.config import get_settings


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


def _count_in_bucket(samples: Iterable[float], bucket: Bucket) -> int:
    return sum(1 for s in samples if bucket.contains(s))


def bucket_probabilities(
    samples: list[float],
    buckets: list[Bucket],
    *,
    alpha: float | None = None,
) -> dict[str, float]:
    """Return label → probability, Laplace-smoothed, summing to 1.0.

    Smoothing prevents P=0 on unsampled tails — important when 0/31 members
    doesn't mean "impossible", just "not yet sampled".
    """
    if not samples:
        raise ValueError("samples must be non-empty")
    if not buckets:
        raise ValueError("buckets must be non-empty")

    a = get_settings().laplace_alpha if alpha is None else alpha
    n = len(samples)
    k = len(buckets)

    denom = n + a * k
    probs: dict[str, float] = {}
    for b in buckets:
        c = _count_in_bucket(samples, b)
        probs[b.label] = (c + a) / denom

    # Numerical safety: renormalize (accounts for samples outside all buckets)
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs


def summary_stats(samples: list[float]) -> dict[str, float]:
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
