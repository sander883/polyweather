-- polyweather schema v1 (Phase 1)

CREATE TABLE IF NOT EXISTS forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    city_code       TEXT NOT NULL,
    source          TEXT NOT NULL,            -- 'gfs_ensemble', 'ecmwf', 'metar'
    run_time_utc    TEXT NOT NULL,            -- ISO8601 of forecast run
    target_time_utc TEXT NOT NULL,            -- ISO8601 of target (e.g. daily max)
    members_json    TEXT NOT NULL,            -- JSON array of values (degF)
    mean_value      REAL NOT NULL,
    std_value       REAL NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_forecasts_lookup
    ON forecasts(city_code, source, target_time_utc, run_time_utc);

CREATE TABLE IF NOT EXISTS markets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT UNIQUE NOT NULL,
    slug            TEXT,
    question        TEXT NOT NULL,
    city_code       TEXT,
    settle_time_utc TEXT,
    liquidity_usd   REAL DEFAULT 0,
    volume_usd      REAL DEFAULT 0,
    raw_json        TEXT NOT NULL,
    discovered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_markets_city ON markets(city_code);
CREATE INDEX IF NOT EXISTS idx_markets_settle ON markets(settle_time_utc);

CREATE TABLE IF NOT EXISTS market_buckets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    token_id        TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    bucket_low      REAL,                     -- inclusive; null for open-ended
    bucket_high     REAL,                     -- exclusive; null for open-ended
    yes_price       REAL,                     -- implied YES probability
    last_updated    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(market_id, token_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    market_id       INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    bucket_id       INTEGER NOT NULL REFERENCES market_buckets(id) ON DELETE CASCADE,
    forecast_id     INTEGER REFERENCES forecasts(id) ON DELETE SET NULL,
    p_model         REAL NOT NULL,
    p_market        REAL NOT NULL,
    edge            REAL NOT NULL,
    ev              REAL NOT NULL,
    recommended_size_usd REAL NOT NULL,
    kelly_fraction_used REAL NOT NULL,
    reason          TEXT,
    acted           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(generated_at);

CREATE TABLE IF NOT EXISTS paper_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at       TEXT NOT NULL DEFAULT (datetime('now')),
    market_id       INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    bucket_id       INTEGER NOT NULL REFERENCES market_buckets(id) ON DELETE CASCADE,
    signal_id       INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    side            TEXT NOT NULL,            -- 'YES' / 'NO'
    entry_price     REAL NOT NULL,
    size_usd        REAL NOT NULL,
    shares          REAL NOT NULL,            -- size_usd / entry_price for YES
    p_model_at_entry REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / SETTLED / CLOSED_EARLY
    exit_price      REAL,
    pnl_usd         REAL,
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(status);

CREATE TABLE IF NOT EXISTS calibration_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    market_id       INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    bucket_id       INTEGER NOT NULL REFERENCES market_buckets(id) ON DELETE CASCADE,
    p_model         REAL NOT NULL,
    outcome         INTEGER NOT NULL          -- 1 if bucket was the winner, else 0
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    markets_seen    INTEGER DEFAULT 0,
    signals_emitted INTEGER DEFAULT 0,
    error           TEXT
);
