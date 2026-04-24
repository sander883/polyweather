# polyweather

Polymarket weather market trading bot. Paper-mode only in Phase 1.

## Stack

- Python 3.11+ / FastAPI / SQLite
- Forecast: GFS 31-member ensemble via [Open-Meteo](https://open-meteo.com/)
- Target venue: Polymarket (Gamma API via httpx; `py-clob-client` reserved for Phase 3)

## How Polymarket weather markets actually look

Polymarket ships weather buckets as **negRisk groups**: one logical event
(e.g. *Highest temperature in NYC on April 25*) contains many binary YES/NO
markets, one per bucket. All share the same `negRiskMarketID`, and the bucket
label lives in `groupItemTitle` (e.g. `"70-74"`, `"Above 80"`, `"1.24-1.30"`).

Scanner discovery path:
1. `/events?tag_slug=weather` (primary)
2. Fallback tags: `temperature`, `climate`
3. Last-resort free-text search on `/markets`, grouped by `negRiskMarketID`

## Phase 1 scope

- GFS ensemble fetcher
- Bucket-probability engine (baseline, GFS only, Laplace smoothing)
- Event-based Polymarket scanner (negRisk-aware, read-only)
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
