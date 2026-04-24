"""Scan Polymarket weather markets, score against GFS ensemble, emit signals."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from polyweather.cities import CITIES
from polyweather.config import get_settings
from polyweather.db.connection import get_conn
from polyweather.fetchers.gfs_ensemble import (
    EnsembleForecast,
    fetch_gfs_ensemble,
    persist_forecast,
)
from polyweather.model.probability import Bucket, bucket_probabilities
from polyweather.scanner.parser import ParsedMarket, parse_weather_market
from polyweather.scanner.polymarket_client import GammaClient
from polyweather.sizing.kelly import recommended_size

log = logging.getLogger(__name__)


async def _collect_markets(client: GammaClient) -> list[ParsedMarket]:
    raw_markets: list[dict] = []
    try:
        raw_markets = await client.list_markets(tag="weather", limit=200)
    except Exception as e:  # noqa: BLE001
        log.warning("tag-based fetch failed: %s; falling back to search", e)

    if not raw_markets:
        for q in ("weather", "temperature", "high temperature"):
            try:
                raw_markets.extend(await client.search_markets(q, limit=100))
            except Exception as e:  # noqa: BLE001
                log.warning("search(%r) failed: %s", q, e)

    # De-duplicate by conditionId
    seen: set[str] = set()
    parsed: list[ParsedMarket] = []
    for raw in raw_markets:
        cid = str(raw.get("conditionId") or raw.get("id") or "")
        if cid in seen:
            continue
        seen.add(cid)
        pm = parse_weather_market(raw)
        if pm is not None:
            parsed.append(pm)
    log.info("parsed %d weather markets from %d raw", len(parsed), len(raw_markets))
    return parsed


async def _forecasts_for_markets(
    markets: list[ParsedMarket],
) -> dict[tuple[str, str], EnsembleForecast]:
    """Fetch one ensemble forecast per (city, target_date) we need."""
    targets: set[tuple[str, str]] = set()
    for m in markets:
        if m.settle_time_utc is None:
            continue
        targets.add((m.city_code, m.settle_time_utc.date().isoformat()))

    out: dict[tuple[str, str], EnsembleForecast] = {}

    async def _one(city_code: str, date_iso: str) -> None:
        try:
            target = datetime.fromisoformat(date_iso).date()
            fc = await fetch_gfs_ensemble(city_code, target)
            out[(city_code, date_iso)] = fc
            persist_forecast(fc)
        except Exception as e:  # noqa: BLE001
            log.warning("forecast fetch failed city=%s date=%s: %s", city_code, date_iso, e)

    await asyncio.gather(*(_one(c, d) for c, d in targets))
    return out


def _upsert_market(pm: ParsedMarket) -> tuple[int, dict[str, int]]:
    """Insert/refresh market + buckets. Returns (market_id, token_id -> bucket_id)."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM markets WHERE condition_id = ?", (pm.condition_id,)
        )
        row = cur.fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO markets
                    (condition_id, slug, question, city_code,
                     settle_time_utc, liquidity_usd, volume_usd, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pm.condition_id,
                    pm.slug,
                    pm.question,
                    pm.city_code,
                    pm.settle_time_utc.isoformat() if pm.settle_time_utc else None,
                    pm.liquidity_usd,
                    pm.volume_usd,
                    json.dumps(pm.raw),
                ),
            )
            market_id = int(cur.lastrowid)
        else:
            market_id = int(row["id"])
            conn.execute(
                """
                UPDATE markets
                   SET liquidity_usd = ?, volume_usd = ?, raw_json = ?,
                       last_seen_at = datetime('now')
                 WHERE id = ?
                """,
                (pm.liquidity_usd, pm.volume_usd, json.dumps(pm.raw), market_id),
            )

        bucket_ids: dict[str, int] = {}
        for b in pm.buckets:
            conn.execute(
                """
                INSERT INTO market_buckets
                    (market_id, token_id, outcome_label, bucket_low, bucket_high,
                     yes_price, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(market_id, token_id) DO UPDATE SET
                    yes_price = excluded.yes_price,
                    last_updated = excluded.last_updated
                """,
                (market_id, b.token_id, b.outcome_label, b.low, b.high, b.yes_price),
            )
            bid = conn.execute(
                "SELECT id FROM market_buckets WHERE market_id = ? AND token_id = ?",
                (market_id, b.token_id),
            ).fetchone()["id"]
            bucket_ids[b.token_id] = int(bid)

    return market_id, bucket_ids


def _find_forecast_id(
    city_code: str, target_date_iso: str
) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id FROM forecasts
             WHERE city_code = ?
               AND source = 'gfs_ensemble'
               AND substr(target_time_utc, 1, 10) = ?
          ORDER BY fetched_at DESC LIMIT 1
            """,
            (city_code, target_date_iso),
        ).fetchone()
    return int(row["id"]) if row else None


def _insert_signal(
    *,
    market_id: int,
    bucket_id: int,
    forecast_id: int | None,
    p_model: float,
    p_market: float,
    edge: float,
    ev: float,
    size_usd: float,
    kelly_used: float,
    reason: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (market_id, bucket_id, forecast_id,
                 p_model, p_market, edge, ev,
                 recommended_size_usd, kelly_fraction_used, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id, bucket_id, forecast_id,
                p_model, p_market, edge, ev,
                size_usd, kelly_used, reason,
            ),
        )
        return int(cur.lastrowid)


async def scan_once() -> dict:
    """One pass: fetch markets → forecasts → score → emit signals. Paper only."""
    s = get_settings()
    started = datetime.now(timezone.utc)
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO scan_runs DEFAULT VALUES")
        run_id = int(cur.lastrowid)

    markets_seen = 0
    signals_emitted = 0
    error: str | None = None

    try:
        client = GammaClient()
        markets = await _collect_markets(client)
        markets_seen = len(markets)
        forecasts = await _forecasts_for_markets(markets)

        for pm in markets:
            if pm.settle_time_utc is None:
                continue
            target_iso = pm.settle_time_utc.date().isoformat()
            fc = forecasts.get((pm.city_code, target_iso))
            if fc is None:
                continue

            now_utc = datetime.now(timezone.utc)
            hours_to_settle = (pm.settle_time_utc - now_utc).total_seconds() / 3600.0
            if hours_to_settle < s.min_time_to_settle_hours:
                continue
            if pm.liquidity_usd < s.min_liquidity:
                continue

            buckets = [
                Bucket(label=b.token_id, low=b.low, high=b.high) for b in pm.buckets
            ]
            probs = bucket_probabilities(fc.daily_max_f_per_member, buckets)

            market_id, bucket_id_map = _upsert_market(pm)
            forecast_id = _find_forecast_id(pm.city_code, target_iso)

            for b in pm.buckets:
                if b.yes_price is None or b.yes_price <= 0 or b.yes_price >= 1:
                    continue
                p_model = probs.get(b.token_id, 0.0)
                edge = p_model - b.yes_price
                if edge < s.edge_threshold:
                    continue
                ev = (p_model / b.yes_price) - 1.0
                if ev <= 0:
                    continue

                sz = recommended_size(
                    p_model=p_model,
                    price=b.yes_price,
                    bankroll=s.paper_bankroll,
                )
                if sz.size_usd <= 0:
                    continue

                reason = (
                    f"edge={edge:.3f} ev={ev:.3f} "
                    f"p_model={p_model:.3f} p_market={b.yes_price:.3f} "
                    f"kelly_full={sz.kelly_full:.3f} cap={sz.cap_hit}"
                )
                _insert_signal(
                    market_id=market_id,
                    bucket_id=bucket_id_map[b.token_id],
                    forecast_id=forecast_id,
                    p_model=p_model,
                    p_market=b.yes_price,
                    edge=edge,
                    ev=ev,
                    size_usd=sz.size_usd,
                    kelly_used=sz.kelly_used,
                    reason=reason,
                )
                signals_emitted += 1
                log.info(
                    "signal: %s [%s] %s → %s",
                    pm.city_code, pm.question[:60], b.outcome_label, reason,
                )
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        log.exception("scan failed")
    finally:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE scan_runs
                   SET finished_at = datetime('now'),
                       markets_seen = ?, signals_emitted = ?, error = ?
                 WHERE id = ?
                """,
                (markets_seen, signals_emitted, error, run_id),
            )

    return {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "markets_seen": markets_seen,
        "signals_emitted": signals_emitted,
        "cities_tracked": sorted(CITIES),
        "error": error,
    }
