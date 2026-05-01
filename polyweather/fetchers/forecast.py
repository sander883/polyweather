"""Multi-source point forecasts: ECMWF (global) + HRRR/GFS-seamless (US).

Adopts the alteregoeth/weatherbot approach proven in production:
  - ECMWF IFS-0.25° with Open-Meteo's `bias_correction=true` (climatology-
    corrected, beats raw model output by ~0.5-1°F)
  - HRRR/GFS-seamless for US near-term (D+0/D+1) — higher resolution beats
    ECMWF inside 48h
  - Pick the "best" source per (city, horizon): HRRR for US D+0/D+1, else ECMWF

Returns daily extrema as scalar °F values (point estimates), not ensembles.
The scanner pairs these with a per-source sigma to score tail buckets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from polyweather.cities import City, get_city
from polyweather.config import get_settings
from polyweather.db.connection import get_conn

log = logging.getLogger(__name__)

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


@dataclass
class PointForecast:
    """Per-source point forecast for a single (city, target_date) pair."""

    city_code: str
    target_date: date
    fetched_at: datetime

    # Per-source daily extrema in °F. None if the source didn't return data.
    ecmwf_max: float | None = None
    ecmwf_min: float | None = None
    hrrr_max: float | None = None
    hrrr_min: float | None = None

    def best(self, market_type: str, hours_to_settle: float) -> tuple[float, str] | None:
        """Pick the best forecast for the market type. Returns (value, source) or None.

        HRRR wins for US-region cities inside ~48h (higher resolution, more
        skilled at near-term). ECMWF wins everywhere else.
        """
        attr = "max" if market_type == "high" else "min"

        if hours_to_settle <= 48 and getattr(self, f"hrrr_{attr}") is not None:
            return getattr(self, f"hrrr_{attr}"), "hrrr"
        if getattr(self, f"ecmwf_{attr}") is not None:
            return getattr(self, f"ecmwf_{attr}"), "ecmwf"
        # Last resort: HRRR even past 48h if ECMWF missing
        if getattr(self, f"hrrr_{attr}") is not None:
            return getattr(self, f"hrrr_{attr}"), "hrrr"
        return None


async def _http_json(url: str, params: dict) -> dict:
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _fetch_open_meteo_daily(
    city: City,
    target: date,
    *,
    model: str,
    bias_correction: bool,
) -> tuple[float | None, float | None]:
    """Pull (max_F, min_F) for the target date from a single Open-Meteo model."""
    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": city.timezone,
        "forecast_days": 7,
        "models": model,
    }
    if bias_correction:
        params["bias_correction"] = "true"

    try:
        data = await _http_json(OPEN_METEO_FORECAST, params)
    except Exception as e:  # noqa: BLE001
        log.warning("open-meteo %s fetch failed for %s: %s", model, city.code, e)
        return None, None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    maxes = daily.get("temperature_2m_max") or []
    mins = daily.get("temperature_2m_min") or []
    target_iso = target.isoformat()

    for i, t in enumerate(times):
        if t == target_iso:
            tmax = maxes[i] if i < len(maxes) else None
            tmin = mins[i] if i < len(mins) else None
            return (
                float(tmax) if tmax is not None else None,
                float(tmin) if tmin is not None else None,
            )
    log.debug("open-meteo %s: target %s not in returned dates %s",
              model, target_iso, times)
    return None, None


async def fetch_point_forecast(city_code: str, target: date) -> PointForecast:
    """Fetch ECMWF (always) and HRRR (US only) for a single (city, date).

    Both calls run, and the scanner picks the right source per market based on
    the time-to-settlement.
    """
    city = get_city(city_code)
    fc = PointForecast(
        city_code=city.code,
        target_date=target,
        fetched_at=datetime.now(timezone.utc).replace(microsecond=0),
    )

    # ECMWF with climatological bias correction (Open-Meteo applies a per-grid
    # offset learned from station obs — free, no key needed).
    fc.ecmwf_max, fc.ecmwf_min = await _fetch_open_meteo_daily(
        city, target, model="ecmwf_ifs025", bias_correction=True,
    )

    # HRRR (3km, US-only, D+0/D+1) — Open-Meteo's gfs_seamless blends HRRR
    # near-term with GFS beyond, which is what alteregoeth's bot does.
    if city.region == "us":
        fc.hrrr_max, fc.hrrr_min = await _fetch_open_meteo_daily(
            city, target, model="gfs_seamless", bias_correction=False,
        )

    log.info(
        "point forecast %s %s: ecmwf=%s/%s hrrr=%s/%s",
        city_code, target,
        _fmt(fc.ecmwf_max), _fmt(fc.ecmwf_min),
        _fmt(fc.hrrr_max), _fmt(fc.hrrr_min),
    )
    return fc


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def persist_point_forecast(fc: PointForecast) -> int:
    """Store the multi-source snapshot in the existing forecasts table.

    We re-use the schema by serialising both sources into members_json:
        {"ecmwf": {"max": 72.1, "min": 58.4},
         "hrrr":  {"max": 73.0, "min": 57.8}}

    `mean_value` / `std_value` get the ECMWF max as a representative scalar.
    """
    payload = {
        "ecmwf": {"max": fc.ecmwf_max, "min": fc.ecmwf_min},
        "hrrr": {"max": fc.hrrr_max, "min": fc.hrrr_min},
    }
    target_eod = datetime.combine(fc.target_date, datetime.max.time()).replace(
        tzinfo=timezone.utc, microsecond=0
    )
    representative = fc.ecmwf_max if fc.ecmwf_max is not None else fc.hrrr_max
    # std proxy: gap between ECMWF and HRRR when both present (model
    # disagreement is a useful uncertainty signal); 0.0 otherwise.
    if fc.ecmwf_max is not None and fc.hrrr_max is not None:
        std_proxy = abs(fc.ecmwf_max - fc.hrrr_max)
    else:
        std_proxy = 0.0
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
                "multi_source",
                fc.fetched_at.isoformat(),
                target_eod.isoformat(),
                json.dumps(payload),
                float(representative) if representative is not None else 0.0,
                std_proxy,
            ),
        )
        return int(cur.lastrowid)


def load_point_forecast(members_json: str) -> dict:
    """Read a multi_source row back into the {ecmwf: {max, min}, hrrr: ...} dict."""
    return json.loads(members_json)
