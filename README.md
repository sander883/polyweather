# polyweather

Polymarket weather market trading bot. Paper-mode only in Phase 1.

## Stack

- Python 3.11+ / FastAPI / SQLite
- Forecast: GFS 31-member ensemble via [Open-Meteo](https://open-meteo.com/)
- Target venue: Polymarket (Gamma API + CLOB)

## Phase 1 scope

- GFS ensemble fetcher
- Bucket-probability engine (baseline, GFS only)
- Polymarket scanner (read-only, weather / temperature markets)
- Paper trading engine with Kelly-¼ sizing and hard caps
- FastAPI with status / signals / positions / calibration endpoints

Out of scope for Phase 1: ECMWF bias correction, METAR anchor, HRRR, live execution.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m polyweather.db.init
uvicorn polyweather.api.main:app --reload
```

Then:

- `GET /health` — liveness
- `GET /forecast/{city}` — latest ensemble probability per bucket
- `GET /signals` — current paper-trading signals
- `GET /positions` — open paper positions
- `POST /scan` — trigger an ad-hoc scan

## Layout

```
polyweather/
  config.py            settings via pydantic-settings
  cities.py            tracked cities (NYC / LAX / ORD)
  db/                  SQLite schema + helpers
  fetchers/            Open-Meteo GFS ensemble
  model/               probability engine + calibration
  scanner/             Polymarket Gamma client + edge detector
  sizing/              Kelly fractional + hard caps
  trading/             paper trading ledger
  api/                 FastAPI app
```
