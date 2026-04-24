"""Parse Polymarket events/markets into normalized weather trading targets.

Weather markets on Polymarket are almost always shipped as **negRisk groups**:
one logical event ("High temperature in NYC on April 25") contains many binary
YES/NO markets, one per bucket. The bucket label lives in ``groupItemTitle``
(e.g. ``"70-74"``, ``"Above 90°F"``), not in the question text. All markets in
a group share the same ``negRiskMarketID``.

This parser accepts either an Event (with nested markets) or a list of flat
markets (we group them by ``negRiskMarketID`` ourselves).

Parsed output:

    ParsedEvent
    ├── condition_id             (negRiskMarketID or event id)
    ├── question                 (event title / first market question)
    ├── city_code                (detected from text)
    ├── settle_time_utc
    └── buckets[ParsedBucket]    one per binary market in the group
        ├── token_id             (YES CLOB token id)
        ├── outcome_label        (groupItemTitle)
        ├── low / high           (parsed from the label)
        └── yes_price            (implied YES probability)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from polyweather.cities import CITIES
from polyweather.scanner.models import Event, Market

log = logging.getLogger(__name__)

_CITY_ALIASES: dict[str, str] = {
    "nyc": "NYC", "new york": "NYC", "new york city": "NYC", "jfk": "NYC",
    "los angeles": "LAX", "la": "LAX", "lax": "LAX",
    "chicago": "ORD", "ord": "ORD", "midway": "ORD",
}

# groupItemTitle patterns, °F assumed unless specified
_RE_RANGE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*[fF]?\s*(?:to|-|–|—)\s*(-?\d+(?:\.\d+)?)\s*°?\s*[fF]?\s*$"
)
_RE_ABOVE = re.compile(
    r"^\s*(?:above|over|≥|>=|>|at least|more than)\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_BELOW = re.compile(
    r"^\s*(?:below|under|≤|<=|<|less than|at most|fewer than)\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
# Celsius bucket (climate anomaly markets often use °C)
_RE_RANGE_C = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*[cC]\s*(?:to|-|–|—)\s*(-?\d+(?:\.\d+)?)\s*°?\s*[cC]"
)


@dataclass
class ParsedBucket:
    token_id: str
    outcome_label: str
    low: float | None
    high: float | None
    yes_price: float | None
    condition_id: str              # per-binary condition id (for future order placement)


@dataclass
class ParsedEvent:
    group_id: str                  # negRiskMarketID or fallback event id
    title: str
    city_code: str
    settle_time_utc: datetime | None
    liquidity_usd: float
    volume_usd: float
    buckets: list[ParsedBucket]
    raw: dict                      # the original event dict (for DB storage)


def detect_city(*texts: str | None) -> str | None:
    blob = " ".join((t or "").lower() for t in texts if t)
    if not blob:
        return None
    # Longer aliases first so "new york" wins over "nyc"; all matches
    # are word-bounded so "la" does not match "Alaska" etc.
    for alias in sorted(_CITY_ALIASES, key=len, reverse=True):
        pattern = rf"\b{re.escape(alias)}\b"
        if re.search(pattern, blob):
            return _CITY_ALIASES[alias]
    for code in CITIES:
        if re.search(rf"\b{code.lower()}\b", blob):
            return code
    return None


def parse_bucket_label(label: str) -> tuple[float | None, float | None]:
    """Parse ``groupItemTitle`` into (low, high) in °F.

    Returns ``(None, None)`` if the label is unrecognized (caller should skip).
    """
    if not label:
        return None, None
    s = label.strip()

    m = _RE_RANGE_C.match(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return lo * 9 / 5 + 32, hi * 9 / 5 + 32

    m = _RE_RANGE.match(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        # inclusive-inclusive on both ends in Polymarket labels; we treat as
        # [low, high+1) for half-open bucket semantics when whole-degree.
        if lo == hi:
            hi = lo + 1.0
        return lo, hi

    m = _RE_ABOVE.match(s)
    if m:
        return float(m.group(1)), None
    m = _RE_BELOW.match(s)
    if m:
        return None, float(m.group(1))
    return None, None


def _parse_settle_time(*candidates: str | None) -> datetime | None:
    for val in candidates:
        if not val:
            continue
        try:
            return (
                datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                .astimezone(timezone.utc)
            )
        except (ValueError, TypeError):
            continue
    return None


def _market_is_usable(m: Market) -> bool:
    if not m.is_tradable:
        return False
    if m.yes_price is None or m.yes_token_id is None:
        return False
    if not m.groupItemTitle:
        return False
    return True


def parse_event(event: Event) -> ParsedEvent | None:
    """Event-first parsing: iterate nested markets, keep usable ones."""
    if not event.is_live:
        return None

    city = detect_city(event.title, event.description, event.slug)
    if not city:
        return None

    raw_markets = event.markets or []
    if not raw_markets:
        return None

    buckets: list[ParsedBucket] = []
    liquidity = 0.0
    volume = 0.0
    group_id: str | None = None
    settle: datetime | None = _parse_settle_time(event.endDate)

    for m in raw_markets:
        if not _market_is_usable(m):
            continue
        lo, hi = parse_bucket_label(m.groupItemTitle or "")
        if lo is None and hi is None:
            continue
        if group_id is None and m.negRiskMarketID:
            group_id = m.negRiskMarketID

        buckets.append(
            ParsedBucket(
                token_id=m.yes_token_id or "",
                outcome_label=m.groupItemTitle or "",
                low=lo,
                high=hi,
                yes_price=m.yes_price,
                condition_id=m.conditionId or "",
            )
        )
        liquidity += float(m.liquidityNum or m.liquidity or 0.0)
        volume += float(m.volumeNum or m.volume or 0.0)
        settle = settle or _parse_settle_time(m.endDateIso, m.endDate)

    if not buckets:
        return None

    return ParsedEvent(
        group_id=group_id or str(event.id or event.slug or ""),
        title=event.title or "",
        city_code=city,
        settle_time_utc=settle,
        liquidity_usd=liquidity,
        volume_usd=volume,
        buckets=buckets,
        raw=event.model_dump(),
    )


def group_flat_markets(markets: list[Market]) -> list[ParsedEvent]:
    """Fallback: group flat markets by ``negRiskMarketID`` when we only have a
    market-level listing.
    """
    by_group: dict[str, list[Market]] = {}
    for m in markets:
        key = m.negRiskMarketID or (str(m.conditionId) if m.conditionId else None)
        if not key:
            continue
        by_group.setdefault(key, []).append(m)

    out: list[ParsedEvent] = []
    for group_id, ms in by_group.items():
        usable = [m for m in ms if _market_is_usable(m)]
        if not usable:
            continue

        title = usable[0].question or ""
        descriptions = " ".join(m.description or "" for m in usable[:3])
        slugs = " ".join(m.slug or "" for m in usable[:3])
        city = detect_city(title, descriptions, slugs)
        if not city:
            continue

        buckets: list[ParsedBucket] = []
        liquidity = 0.0
        volume = 0.0
        settle: datetime | None = None

        for m in usable:
            lo, hi = parse_bucket_label(m.groupItemTitle or "")
            if lo is None and hi is None:
                continue
            buckets.append(
                ParsedBucket(
                    token_id=m.yes_token_id or "",
                    outcome_label=m.groupItemTitle or "",
                    low=lo,
                    high=hi,
                    yes_price=m.yes_price,
                    condition_id=m.conditionId or "",
                )
            )
            liquidity += float(m.liquidityNum or m.liquidity or 0.0)
            volume += float(m.volumeNum or m.volume or 0.0)
            settle = settle or _parse_settle_time(m.endDateIso, m.endDate)

        if not buckets:
            continue

        out.append(
            ParsedEvent(
                group_id=group_id,
                title=title,
                city_code=city,
                settle_time_utc=settle,
                liquidity_usd=liquidity,
                volume_usd=volume,
                buckets=buckets,
                raw={"grouped_markets": [m.model_dump() for m in usable]},
            )
        )
    return out
