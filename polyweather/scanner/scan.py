"""Scan Polymarket events → score buckets → emit paper-trade signals."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from polyweather.cities import CITIES
from polyweather.config import get_settings
from polyweather.db.connection import get_conn
from polyweather.fetchers.gfs_ensemble import (
    EnsembleForecast,
    fetch_gfs_ensemble,
    load_members,
    persist_forecast,
)
from polyweather.model.probability import Bucket, bucket_probabilities
from polyweather.scanner.parser import ParsedEvent, group_flat_markets, parse_event
from polyweather.scanner.polymarket_client import GammaClient
from polyweather.sizing.kelly import recommended_size

log = logging.getLogger(__name__)


async def _collect_events(client: GammaClient) -> list[ParsedEvent]:
    parsed: list[ParsedEvent] = []

    # Primary: tag-based event discovery.
    for tag in ("weather", "temperature", "climate"):
        try:
            events = await client.list_events(tag_slug=tag, limit=100)
        except Exception as e:  # noqa: BLE001
            log.warning("list_events(tag=%s) failed: %s", tag, e)
            continue
        for ev in events:
            pe = parse_event(ev)
            if pe is not None:
                parsed.append(pe)
        if parsed:
            break   # first tag that returns usable events wins

    # Fallback: free-text market search, grouped by negRiskMarketID
    if not parsed:
        try:
            flat: list = []
            for q in ("temperature", "weather", "Fahrenheit", "Celsius"):
                flat.extend(await client.search_markets(q, limit=100))
            # de-dup by (conditionId, slug)
            seen: set[str] = set()
            uniq = []
            for m in flat:
                key = m.conditionId or m.slug or str(m.id)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(m)
            parsed = group_flat_markets(uniq)
        except Exception as e:  # noqa: BLE001
            log.warning("market fallback failed: %s", e)

    # de-dup by group_id
    seen_groups: set[str] = set()
    dedup: list[ParsedEvent] = []
    for pe in parsed:
        if pe.group_id in seen_groups:
            continue
        seen_groups.add(pe.group_id)
        dedup.append(pe)

    log.info("discovered %d parseable weather events", len(dedup))
    return dedup


async def _forecasts_for_events(
    events: list[ParsedEvent],
) -> dict[tuple[str, str], EnsembleForecast]:
    targets: set[tuple[str, str]] = set()
    for e in events:
        if e.settle_time_utc is None:
            continue
        targets.add((e.city_code, e.settle_time_utc.date().isoformat()))

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


def _upsert_event(pe: ParsedEvent) -> tuple[int, dict[str, int]]:
    """Insert/refresh the logical event row (stored in ``markets`` table) and
    its bucket rows. Returns (market_id, token_id → bucket_id).
    """
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM markets WHERE condition_id = ?", (pe.group_id,)
        )
        row = cur.fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO markets
                    (condition_id, slug, question, city_code, market_type,
                     settle_time_utc, liquidity_usd, volume_usd, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pe.group_id,
                    None,
                    pe.title,
                    pe.city_code,
                    pe.market_type,
                    pe.settle_time_utc.isoformat() if pe.settle_time_utc else None,
                    pe.liquidity_usd,
                    pe.volume_usd,
                    json.dumps(pe.raw, default=str),
                ),
            )
            market_id = int(cur.lastrowid)
        else:
            market_id = int(row["id"])
            conn.execute(
                """
                UPDATE markets
                   SET market_type = ?, liquidity_usd = ?, volume_usd = ?,
                       raw_json = ?, last_seen_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    pe.market_type, pe.liquidity_usd, pe.volume_usd,
                    json.dumps(pe.raw, default=str), market_id,
                ),
            )

        bucket_ids: dict[str, int] = {}
        for b in pe.buckets:
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


def _find_forecast_id(city_code: str, target_date_iso: str) -> int | None:
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
    """One pass: fetch events → forecasts → score → emit signals. Paper only."""
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
        events = await _collect_events(client)
        markets_seen = len(events)
        forecasts = await _forecasts_for_events(events)

        for pe in events:
            if pe.settle_time_utc is None:
                continue
            target_iso = pe.settle_time_utc.date().isoformat()
            fc = forecasts.get((pe.city_code, target_iso))
            if fc is None:
                continue

            now_utc = datetime.now(timezone.utc)
            hours_to_settle = (pe.settle_time_utc - now_utc).total_seconds() / 3600.0
            if hours_to_settle < s.min_time_to_settle_hours:
                continue
            if pe.liquidity_usd < s.min_liquidity:
                continue

            prob_buckets = [
                Bucket(label=b.token_id, low=b.low, high=b.high) for b in pe.buckets
            ]
            samples = fc.samples_for(pe.market_type)
            probs = bucket_probabilities(samples, prob_buckets)

            market_id, bucket_id_map = _upsert_event(pe)
            forecast_id = _find_forecast_id(pe.city_code, target_iso)

            for b in pe.buckets:
                if b.yes_price is None or b.yes_price <= 0 or b.yes_price >= 1:
                    continue
                if b.yes_price < s.min_p_market:
                    # Buckets priced near zero produce huge nominal EV that's
                    # mostly noise — Day-1 calibration showed these tails
                    # don't pay off enough to justify the position.
                    continue
                p_model = probs.get(b.token_id, 0.0)
                if p_model < s.p_model_min or p_model > s.p_model_max:
                    # Day-5 calibration showed actual win rate diverges from
                    # p_model by 25-60% in the 0.30-0.50 and 0.80-0.90 bands.
                    # Trade only where the model is least broken.
                    continue
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
                    pe.city_code, pe.title[:60], b.outcome_label, reason,
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
        "events_seen": markets_seen,
        "signals_emitted": signals_emitted,
        "cities_tracked": sorted(CITIES),
        "error": error,
    }
