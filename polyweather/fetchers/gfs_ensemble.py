"""Fetch GFS ensemble forecasts (31 members) from Open-Meteo.

The Open-Meteo ensemble endpoint returns, for each requested variable, one
column per member named e.g. `temperature_2m_member01` through
`temperature_2m_member30`, plus a control run `temperature_2m` (treated as
member 00). We aggregate hourly samples into a daily max per member so we can
map the resulting distribution to Polymarket temperature buckets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx
import numpy as np

from polyweather.cities import City, get_city
from polyweather.config import get_settings
from polyweather.db.connection import get_conn

log = logging.getLogger(__name__)

MEMBER_COUNT_MAX = 31          # control + 30 perturbations
_C_TO_F = lambda c: c * 9 / 5 + 32  # noqa: E731


@dataclass
class EnsembleForecast:
    city_code: str
    source: str                          # "gfs_ensemble"
    run_time_utc: datetime
    target_date: date
    target_time_utc: datetime            # end-of-day UTC of target (for lookup)
    daily_max_f_per_member: list[float]  # daily max in degrees F, one per member

    @property
    def mean(self) -> float:
        return float(np.mean(self.daily_max_f_per_member))

    @property
    def std(self) -> float:
        return float(np.std(self.daily_max_f_per_member, ddof=1))

    @property
    def n_members(self) -> int:
        return len(self.daily_max_f_per_member)


async def _http_get_json(url: str, params: dict) -> dict:
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _extract_member_series(hourly: dict) -> dict[str, list[float | None]]:
    """Pick every temperature_2m* column from the hourly response."""
    series: dict[str, list[float | None]] = {}
    for key, values in hourly.items():
        if key == "time" or not key.startswith("temperature_2m"):
            continue
        series[key] = values
    return series


def _daily_max_per_member(
    times: list[str],
    series: dict[str, list[float | None]],
    target: date,
) -> list[float]:
    """For each member, compute the max temp during the target UTC day, in °F."""
    target_str = target.isoformat()
    idx = [i for i, t in enumerate(times) if t.startswith(target_str)]
    if not idx:
        raise ValueError(f"No hourly rows for target date {target_str}")

    out: list[float] = []
    for key, values in series.items():
        hours = [values[i] for i in idx if values[i] is not None]
        if not hours:
            log.debug("skipping member %s: all hours null", key)
            continue
        out.append(float(_C_TO_F(max(hours))))
    return out


async def fetch_gfs_ensemble(city_code: str, target: date) -> EnsembleForecast:
    settings = get_settings()
    city: City = get_city(city_code)

    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "hourly": "temperature_2m",
        "models": "gfs_seamless",
        "temperature_unit": "celsius",
        "timezone": "UTC",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }
    log.info("fetching GFS ensemble: city=%s target=%s", city_code, target)
    payload = await _http_get_json(settings.open_meteo_base, params)

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    series = _extract_member_series(hourly)
    if not series:
        raise RuntimeError(
            "Open-Meteo returned no temperature_2m members "
            f"(keys={list(hourly.keys())})"
        )

    daily_max = _daily_max_per_member(times, series, target)
    if len(daily_max) < 10:
        raise RuntimeError(
            f"Too few ensemble members returned ({len(daily_max)}); expected ~{MEMBER_COUNT_MAX}"
        )

    now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)
    target_eod = datetime.combine(target, datetime.max.time()).replace(
        tzinfo=timezone.utc, microsecond=0
    )

    fc = EnsembleForecast(
        city_code=city.code,
        source="gfs_ensemble",
        run_time_utc=now_utc,
        target_date=target,
        target_time_utc=target_eod,
        daily_max_f_per_member=daily_max,
    )
    log.info(
        "got %d members; mean=%.2f°F std=%.2f°F",
        fc.n_members, fc.mean, fc.std,
    )
    return fc


def persist_forecast(fc: EnsembleForecast) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO forecasts
                (city_code, source, run_time_utc, target_time_utc,
                 members_json, mean_value, std_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fc.city_code,
                fc.source,
                fc.run_time_utc.isoformat(),
                fc.target_time_utc.isoformat(),
                json.dumps(fc.daily_max_f_per_member),
                fc.mean,
                fc.std,
            ),
        )
        return int(cur.lastrowid)
