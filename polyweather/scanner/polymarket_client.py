"""Read-only Gamma API client for Polymarket.

Modeled on Polymarket/agents' GammaMarketClient, trimmed to what polyweather
needs. Phase 1 does not place orders — execution is a Phase 3 concern via
py-clob-client. We fetch events (with nested markets) because that's how
negRisk bucket groups are shipped.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import ValidationError

from polyweather.config import get_settings
from polyweather.scanner.models import Event, Market

log = logging.getLogger(__name__)


class GammaClient:
    def __init__(self, base_url: str | None = None, timeout: float = 20.0) -> None:
        self.base_url = (base_url or get_settings().polymarket_gamma_base).rstrip("/")
        self._timeout = httpx.Timeout(timeout, connect=10.0)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _as_list(payload: Any) -> list[dict]:
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        return payload if isinstance(payload, list) else []

    async def list_events(
        self,
        *,
        tag_slug: str | None = "weather",
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Event]:
        """List events, optionally filtered by tag slug. Events carry nested markets."""
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "archived": str(archived).lower(),
            "limit": limit,
            "offset": offset,
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        raw = self._as_list(await self._get("/events", params=params))
        out: list[Event] = []
        for item in raw:
            try:
                out.append(Event.model_validate(item))
            except ValidationError as e:
                log.debug("event validation failed (id=%s): %s", item.get("id"), e)
        return out

    async def list_markets(
        self,
        *,
        tag_slug: str | None = "weather",
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Market]:
        """Flat market listing. Useful as a fallback when event-based discovery misses."""
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "archived": str(archived).lower(),
            "limit": limit,
            "offset": offset,
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        raw = self._as_list(await self._get("/markets", params=params))
        out: list[Market] = []
        for item in raw:
            try:
                out.append(Market.model_validate(item))
            except ValidationError as e:
                log.debug("market validation failed (id=%s): %s", item.get("id"), e)
        return out

    async def search_markets(self, query: str, *, limit: int = 100) -> list[Market]:
        raw = self._as_list(
            await self._get(
                "/markets", params={"q": query, "limit": limit, "active": "true"}
            )
        )
        out: list[Market] = []
        for item in raw:
            try:
                out.append(Market.model_validate(item))
            except ValidationError:
                continue
        return out
