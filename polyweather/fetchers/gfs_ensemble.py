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
    source: str                              # "gfs_ensemble"
    run_time_utc: datetime
    target_date: date
    target_time_utc: datetime                # end-of-day UTC of target
    daily_max_f_per_member: list[float]      # daily max (°F) per member
    daily_min_f_per_member: list[float]      # daily min (°F) per member

    @property
    def mean(self) -> float:
        return float(np.mean(self.daily_max_f_per_member))

    @property
    def std(self) -> float:
        return float(np.std(self.daily_max_f_per_member, ddof=1))

    @property
    def n_members(self) -> int:
        return len(self.daily_max_f_per_member)

    def samples_for(self, market_type: str) -> list[float]:
        if market_type == "low":
            return self.daily_min_f_per_member
        return self.daily_max_f_per_member


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


def _daily_extrema_per_member(
    times: list[str],
    series: dict[str, list[float | None]],
    target: date,
) -> tuple[list[float], list[float]]:
    """For each member, compute (max, min) over the target UTC day, in °F.

    Returns two parallel lists so index i refers to the same ensemble member.
    Members with all-null hours are skipped in both.
    """
    target_str = target.isoformat()
    idx = [i for i, t in enumerate(times) if t.startswith(target_str)]
    if not idx:
        raise ValueError(f"No hourly rows for target date {target_str}")

    maxes: list[float] = []
    mins: list[float] = []
    for key, values in series.items():
        hours = [values[i] for i in idx if values[i] is not None]
        if not hours:
            log.debug("skipping member %s: all hours null", key)
            continue
        maxes.append(float(_C_TO_F(max(hours))))
        mins.append(float(_C_TO_F(min(hours))))
    return maxes, mins


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

    daily_max, daily_min = _daily_extrema_per_member(times, series, target)
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
        daily_min_f_per_member=daily_min,
    )
    log.info(
        "got %d members; max mean=%.2f°F (std=%.2f); min mean=%.2f°F",
        fc.n_members, fc.mean, fc.std, float(np.mean(daily_min)),
    )
    return fc


def persist_forecast(fc: EnsembleForecast) -> int:
    """Store both the max and min per-member arrays in members_json.

    Format:
        {"max": [...31 floats...], "min": [...31 floats...]}

    Old format (bare list) is treated as max-only by readers.
    """
    payload = {"max": fc.daily_max_f_per_member, "min": fc.daily_min_f_per_member}
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
                json.dumps(payload),
                fc.mean,
                fc.std,
            ),
        )
        return int(cur.lastrowid)


def load_members(members_json: str, metric: str = "max") -> list[float]:
    """Read members_json in either legacy (bare list = max) or new (dict) form."""
    data = json.loads(members_json)
    if isinstance(data, list):
        # Legacy row: only max stored
        if metric == "max":
            return [float(x) for x in data]
        raise ValueError("legacy forecast row has no 'min' array; re-fetch needed")
    if isinstance(data, dict):
        arr = data.get(metric)
        if arr is None:
            raise ValueError(f"forecast row missing metric={metric!r}")
        return [float(x) for x in arr]
    raise ValueError(f"unexpected members_json type: {type(data).__name__}")
