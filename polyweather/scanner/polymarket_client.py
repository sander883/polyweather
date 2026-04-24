"""Thin read-only client for Polymarket's Gamma API.

Phase 1 only needs: list active weather markets. We do not place orders here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from polyweather.config import get_settings

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

    async def list_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 200,
        offset: int = 0,
        tag: str | None = "weather",
    ) -> list[dict]:
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        if tag:
            params["tag_slug"] = tag
        data = await self._get("/markets", params=params)
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        if not isinstance(data, list):
            log.warning("unexpected markets response type: %s", type(data))
            return []
        return data

    async def search_markets(self, query: str, *, limit: int = 100) -> list[dict]:
        """Fallback when tag filtering misses — free-text search."""
        data = await self._get(
            "/markets", params={"q": query, "limit": limit, "active": "true"}
        )
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        return data if isinstance(data, list) else []
