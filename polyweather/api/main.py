"""FastAPI entrypoint.

Run: uvicorn polyweather.api.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from polyweather import __version__
from polyweather.cities import CITIES
from polyweather.config import get_settings
from polyweather.db.connection import get_conn
from polyweather.db.init import init_db
from polyweather.fetchers.forecast import (
    fetch_point_forecast,
    load_point_forecast,
    persist_point_forecast,
)
from polyweather.logging_setup import setup_logging
from polyweather.model.calibration import reliability_bins
from polyweather.scanner.scan import scan_once
from polyweather.scheduler import (
    scheduler_status,
    start_scheduler,
    stop_scheduler,
)
from polyweather.trading.paper import (
    bankroll_summary,
    execute_pending_signals,
    list_positions,
)
from polyweather.trading.resolver import settle_open_positions


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    setup_logging()
    init_db()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="polyweather", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "cities": sorted(CITIES),
        "settings": {
            "min_ev": get_settings().min_ev,
            "max_price": get_settings().max_price,
            "min_liquidity": get_settings().min_liquidity,
            "paper_bankroll": get_settings().paper_bankroll,
            "kelly_fraction": get_settings().kelly_fraction,
        },
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/cities")
def cities() -> list[dict]:
    return [
        {
            "code": c.code, "name": c.name, "station": c.station,
            "lat": c.latitude, "lon": c.longitude, "tz": c.timezone,
        }
        for c in CITIES.values()
    ]


@app.get("/forecast/{city}")
async def forecast(
    city: str,
    target: str | None = Query(None, description="YYYY-MM-DD (UTC), default=today"),
    fresh: bool = Query(False, description="If true, refetch from Open-Meteo"),
) -> dict:
    code = city.upper()
    if code not in CITIES:
        raise HTTPException(404, f"Unknown city: {city}")

    target_date = (
        date.fromisoformat(target) if target else datetime.now(timezone.utc).date()
    )

    if fresh:
        fc = await fetch_point_forecast(code, target_date)
        persist_point_forecast(fc)
        return {
            "city": code,
            "target_date": target_date.isoformat(),
            "source": "multi_source",
            "ecmwf": {"max": fc.ecmwf_max, "min": fc.ecmwf_min},
            "hrrr": {"max": fc.hrrr_max, "min": fc.hrrr_min},
        }

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, run_time_utc, target_time_utc, members_json,
                   mean_value, std_value, fetched_at
              FROM forecasts
             WHERE city_code = ?
               AND source IN ('multi_source', 'gfs_ensemble')
               AND substr(target_time_utc, 1, 10) = ?
          ORDER BY fetched_at DESC LIMIT 1
            """,
            (code, target_date.isoformat()),
        ).fetchone()
    if row is None:
        raise HTTPException(
            404,
            f"No cached forecast for {code} on {target_date}. Retry with fresh=true.",
        )
    return {
        "city": code,
        "target_date": target_date.isoformat(),
        "source": "multi_source",
        "fetched_at": row["fetched_at"],
        "data": load_point_forecast(row["members_json"]),
    }


@app.get("/markets")
def markets(city: str | None = None, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        if city:
            rows = conn.execute(
                """
                SELECT id, condition_id, question, city_code, settle_time_utc,
                       liquidity_usd, volume_usd, last_seen_at
                  FROM markets WHERE city_code = ?
                 ORDER BY last_seen_at DESC LIMIT ?
                """,
                (city.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, condition_id, question, city_code, settle_time_utc,
                       liquidity_usd, volume_usd, last_seen_at
                  FROM markets ORDER BY last_seen_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/signals")
def signals(limit: int = 50, only_unacted: bool = False) -> list[dict]:
    with get_conn() as conn:
        sql = """
            SELECT s.*, m.question, m.city_code, mb.outcome_label
              FROM signals s
              JOIN markets m ON m.id = s.market_id
              JOIN market_buckets mb ON mb.id = s.bucket_id
        """
        if only_unacted:
            sql += " WHERE s.acted = 0"
        sql += " ORDER BY s.id DESC LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/positions")
def positions(status: str | None = "OPEN") -> list[dict]:
    return list_positions(status=status)


@app.get("/bankroll")
def bankroll() -> dict:
    return bankroll_summary()


@app.get("/calibration")
def calibration(
    n_bins: int = 10,
    include_tails: bool = Query(
        False, description="Include tail bets (entry price below min_p_market)",
    ),
) -> list[dict]:
    floor = None if include_tails else get_settings().min_p_market
    return [
        {
            "lower": b.lower, "upper": b.upper, "n": b.n,
            "mean_pred": b.mean_pred, "fraction_positive": b.fraction_positive,
        }
        for b in reliability_bins(n_bins=n_bins, min_p_market=floor)
    ]


@app.post("/scan")
async def trigger_scan(execute: bool = Query(False, description="Also open paper positions")) -> dict:
    result = await scan_once()
    if execute:
        opened = execute_pending_signals()
        result["paper_positions_opened"] = opened
    return result


@app.post("/settle")
async def trigger_settle(
    only_past_settle: bool = Query(
        True, description="Skip positions whose settle_time is still in the future",
    ),
) -> dict:
    """Resolve open paper positions by re-reading final Polymarket prices.

    For each open position whose market has resolved, close it and record
    a calibration row. Safe to call repeatedly.
    """
    return await settle_open_positions(only_past_settle=only_past_settle)


@app.get("/scheduler")
def scheduler() -> dict:
    return scheduler_status()


@app.get("/scans")
def scans(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
