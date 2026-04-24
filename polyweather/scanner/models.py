"""Pydantic models for Polymarket Gamma API responses.

Modeled after the Polymarket/agents reference (agents/utils/objects.py) but
trimmed to fields polyweather actually uses. All fields are Optional because
the Gamma API is generous with what it omits depending on market state.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _coerce_json_list(value: Any) -> list | None:
    """Gamma returns some arrays as JSON-encoded strings. Normalize both shapes."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


class Tag(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    label: str | None = None
    slug: str | None = None


class Market(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | str | None = None
    conditionId: str | None = None
    questionID: str | None = None
    question: str | None = None
    slug: str | None = None
    description: str | None = None

    outcomes: list[str] | None = None
    outcomePrices: list[str] | None = None
    clobTokenIds: list[str] | None = None

    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None
    acceptingOrders: bool | None = None
    enableOrderBook: bool | None = None

    liquidity: float | None = None
    liquidityNum: float | None = None
    liquidityClob: float | None = None
    volume: float | None = None
    volumeNum: float | None = None

    endDate: str | None = None
    endDateIso: str | None = None
    startDate: str | None = None

    # negRisk grouping — the key fields that turn a binary into a bucket
    negRisk: bool | None = None
    negRiskMarketID: str | None = None
    groupItemTitle: str | None = None
    groupItemThreshold: int | str | None = None

    @field_validator("outcomes", "outcomePrices", "clobTokenIds", mode="before")
    @classmethod
    def _json_list(cls, v: Any) -> Any:
        coerced = _coerce_json_list(v)
        return coerced if coerced is not None else v

    @property
    def yes_price(self) -> float | None:
        """YES-side price for a binary market. None if unavailable."""
        if not self.outcomePrices or not self.outcomes:
            return None
        # Match index of the "Yes" outcome
        for i, o in enumerate(self.outcomes):
            if str(o).strip().lower() == "yes" and i < len(self.outcomePrices):
                try:
                    return float(self.outcomePrices[i])
                except (TypeError, ValueError):
                    return None
        # Fallback: first price
        try:
            return float(self.outcomePrices[0])
        except (TypeError, ValueError):
            return None

    @property
    def yes_token_id(self) -> str | None:
        if not self.clobTokenIds or not self.outcomes:
            return None
        for i, o in enumerate(self.outcomes):
            if str(o).strip().lower() == "yes" and i < len(self.clobTokenIds):
                return str(self.clobTokenIds[i])
        return str(self.clobTokenIds[0]) if self.clobTokenIds else None

    @property
    def is_tradable(self) -> bool:
        if self.closed or self.archived:
            return False
        if self.active is False:
            return False
        if self.acceptingOrders is False:
            return False
        return True


class Event(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | int | None = None
    ticker: str | None = None
    slug: str | None = None
    title: str | None = None
    description: str | None = None

    startDate: str | None = None
    endDate: str | None = None

    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None

    liquidity: float | None = None
    volume: float | None = None
    volume24hr: float | None = None

    markets: list[Market] | None = None
    tags: list[Tag] | None = None

    @property
    def tag_slugs(self) -> list[str]:
        return [t.slug for t in (self.tags or []) if t.slug]

    @property
    def is_live(self) -> bool:
        return bool(self.active) and not self.closed and not self.archived
