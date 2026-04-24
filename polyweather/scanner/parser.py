"""Parse Polymarket weather market payloads into normalized city + buckets.

Polymarket weather questions come in a few common shapes:

    "Highest temperature in NYC on April 25?"                   (multi-outcome)
    "Will the high in Los Angeles be above 75°F on April 25?"   (binary)
    "NYC high between 70°F and 74°F on April 25?"               (binary range)

This parser is deliberately forgiving: if a market doesn't match a known
pattern, we return ``None`` rather than guessing. Downstream code treats
unparseable markets as skipped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from polyweather.cities import CITIES

log = logging.getLogger(__name__)

# Map common name forms to city codes
_CITY_ALIASES: dict[str, str] = {
    "nyc": "NYC", "new york": "NYC", "new york city": "NYC", "jfk": "NYC",
    "los angeles": "LAX", "la": "LAX", "lax": "LAX",
    "chicago": "ORD", "ord": "ORD",
}

# Patterns we recognize in the question text
_RE_RANGE = re.compile(
    r"between\s+(-?\d+(?:\.\d+)?)\s*°?\s*f?\s*(?:and|to|-)\s+(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_ABOVE = re.compile(
    r"(?:above|over|more than|higher than|≥|>=|>)\s+(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_BELOW = re.compile(
    r"(?:below|under|less than|lower than|≤|<=|<)\s+(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_EXACT_RANGE_OUTCOME = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*f?\s*(?:to|-|–|—)\s*(-?\d+(?:\.\d+)?)\s*°?\s*f?\s*$",
    re.IGNORECASE,
)
_RE_OUTCOME_ABOVE = re.compile(
    r"^\s*(?:above|over|>)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)
_RE_OUTCOME_BELOW = re.compile(
    r"^\s*(?:below|under|<)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)


@dataclass
class ParsedBucket:
    token_id: str
    outcome_label: str
    low: float | None
    high: float | None
    yes_price: float | None


@dataclass
class ParsedMarket:
    condition_id: str
    slug: str | None
    question: str
    city_code: str
    settle_time_utc: datetime | None
    liquidity_usd: float
    volume_usd: float
    buckets: list[ParsedBucket]
    raw: dict


def _detect_city(text: str) -> str | None:
    t = text.lower()
    # Prefer longer aliases first to avoid "la" matching "los angeles"
    for alias in sorted(_CITY_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return _CITY_ALIASES[alias]
    for code in CITIES:
        if re.search(rf"\b{code.lower()}\b", t):
            return code
    return None


def _parse_bucket_from_outcome(outcome: str) -> tuple[float | None, float | None]:
    """Parse a single outcome label like '70-74' or 'Above 80' into (low, high)."""
    m = _RE_EXACT_RANGE_OUTCOME.match(outcome)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi + 1.0 if lo == hi else hi
    m = _RE_OUTCOME_ABOVE.match(outcome)
    if m:
        return float(m.group(1)), None
    m = _RE_OUTCOME_BELOW.match(outcome)
    if m:
        return None, float(m.group(1))
    return None, None


def _parse_bucket_from_question(question: str) -> tuple[float | None, float | None]:
    m = _RE_RANGE.search(question)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi
    m = _RE_ABOVE.search(question)
    if m:
        return float(m.group(1)), None
    m = _RE_BELOW.search(question)
    if m:
        return None, float(m.group(1))
    return None, None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _parse_settle_time(raw: dict) -> datetime | None:
    for key in ("endDate", "end_date_iso", "endDateIso", "end_date", "closedTime"):
        val = raw.get(key)
        if not val:
            continue
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def parse_weather_market(raw: dict) -> ParsedMarket | None:
    question = str(raw.get("question") or raw.get("title") or "").strip()
    if not question:
        return None

    city = _detect_city(question)
    if not city:
        return None

    token_ids = _as_list(raw.get("clobTokenIds"))
    outcomes = _as_list(raw.get("outcomes"))
    prices = _as_list(raw.get("outcomePrices"))

    buckets: list[ParsedBucket] = []

    if len(outcomes) >= 2 and len(token_ids) == len(outcomes):
        # Multi-outcome market — each outcome is a bucket
        # Skip binary Yes/No markets; those need the range parsed from question
        is_yes_no = {str(o).strip().lower() for o in outcomes} == {"yes", "no"}
        if not is_yes_no:
            for i, label in enumerate(outcomes):
                lo, hi = _parse_bucket_from_outcome(str(label))
                price = _float(prices[i], default=None) if i < len(prices) else None
                buckets.append(
                    ParsedBucket(
                        token_id=str(token_ids[i]),
                        outcome_label=str(label),
                        low=lo,
                        high=hi,
                        yes_price=price,
                    )
                )
        else:
            # Binary market on a threshold / range
            lo, hi = _parse_bucket_from_question(question)
            if lo is None and hi is None:
                return None
            # YES token only — we treat this as a single bucket
            yes_idx = next(
                (i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes"),
                0,
            )
            price = _float(prices[yes_idx], default=None) if yes_idx < len(prices) else None
            buckets.append(
                ParsedBucket(
                    token_id=str(token_ids[yes_idx]),
                    outcome_label=f"YES ({question})",
                    low=lo,
                    high=hi,
                    yes_price=price,
                )
            )
    else:
        log.debug("market %r has unusable outcomes shape", question[:60])
        return None

    if not buckets:
        return None

    return ParsedMarket(
        condition_id=str(raw.get("conditionId") or raw.get("condition_id") or raw.get("id") or ""),
        slug=raw.get("slug"),
        question=question,
        city_code=city,
        settle_time_utc=_parse_settle_time(raw),
        liquidity_usd=_float(raw.get("liquidity") or raw.get("liquidityNum")),
        volume_usd=_float(raw.get("volume") or raw.get("volumeNum")),
        buckets=buckets,
        raw=raw,
    )
