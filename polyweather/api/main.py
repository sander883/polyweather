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
from polyweather.fetchers.gfs_ensemble import fetch_gfs_ensemble, persist_forecast
from polyweather.logging_setup import setup_logging
from polyweather.model.calibration import reliability_bins
from polyweather.model.probability import summary_stats
from polyweather.scanner.scan import scan_once
from polyweather.trading.paper import (
    bankroll_summary,
    execute_pending_signals,
    list_positions,
)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    setup_logging()
    init_db()
    yield


app = FastAPI(title="polyweather", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "cities": sorted(CITIES),
        "settings": {
            "edge_threshold": get_settings().edge_threshold,
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
        fc = await fetch_gfs_ensemble(code, target_date)
        persist_forecast(fc)
        return {
            "city": code,
            "target_date": target_date.isoformat(),
            "source": "gfs_ensemble",
            "members": fc.daily_max_f_per_member,
            "stats": summary_stats(fc.daily_max_f_per_member),
        }

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, run_time_utc, target_time_utc, members_json,
                   mean_value, std_value, fetched_at
              FROM forecasts
             WHERE city_code = ? AND source = 'gfs_ensemble'
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
    import json as _json
    members = _json.loads(row["members_json"])
    return {
        "city": code,
        "target_date": target_date.isoformat(),
        "source": "gfs_ensemble",
        "fetched_at": row["fetched_at"],
        "members": members,
        "stats": summary_stats(members),
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
def calibration(n_bins: int = 10) -> list[dict]:
    return [
        {
            "lower": b.lower, "upper": b.upper, "n": b.n,
            "mean_pred": b.mean_pred, "fraction_positive": b.fraction_positive,
        }
        for b in reliability_bins(n_bins=n_bins)
    ]


@app.post("/scan")
async def trigger_scan(execute: bool = Query(False, description="Also open paper positions")) -> dict:
    result = await scan_once()
    if execute:
        opened = execute_pending_signals()
        result["paper_positions_opened"] = opened
    return result


@app.get("/scans")
def scans(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
