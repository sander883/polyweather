from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="POLYWEATHER_",
        extra="ignore",
    )

    db_path: str = "./data/polyweather.db"
    log_level: str = "INFO"

    open_meteo_base: str = Field(
        default="https://ensemble-api.open-meteo.com/v1/ensemble",
        validation_alias="OPEN_METEO_BASE",
    )
    polymarket_gamma_base: str = Field(
        default="https://gamma-api.polymarket.com",
        validation_alias="POLYMARKET_GAMMA_BASE",
    )
    polymarket_clob_base: str = Field(
        default="https://clob.polymarket.com",
        validation_alias="POLYMARKET_CLOB_BASE",
    )

    # Phase 2A.3 — Day-9 review (0/23 wins on Phase 2A.2):
    #   - probabilistic neighbour-bucket trading produced systematic losses
    #     because real forecast MAE (3-5°F at D+1 to D+3) is much wider
    #     than the σ=2°F default; we overestimated p on neighbours by ~2x.
    #   - many Polymarket weather markets use 1°F-wide buckets, smaller
    #     than the typical forecast error → bucket mis-assignment is the
    #     dominant failure mode.
    # Fix: only trade the bucket the forecast actually hits, skip narrow
    # buckets, raise sigma to a more honest value, raise min_ev so any
    # single trade has substantial buffer to survive bucket mis-assignment.
    min_ev: float = 0.30
    min_liquidity: float = 2000.0
    paper_bankroll: float = 500.0
    kelly_fraction: float = 0.25
    max_pct_per_trade: float = 0.03
    max_pct_per_market: float = 0.08
    max_pct_per_city_day: float = 0.15

    min_time_to_settle_hours: float = 2.0
    max_time_to_settle_hours: float = 72.0

    # Forecast-error std defaults (°F / °C) for the normal-CDF bucket model.
    # Bumped from 2.0/1.2 → 4.0/2.4 to match observed ECMWF/HRRR MAE at
    # 24-72h horizons. Self-calibration (Phase 2C) replaces these with
    # empirical per-(city, source) MAE.
    sigma_default_f: float = 4.0
    sigma_default_c: float = 2.4

    # Skip buckets narrower than this (in °F) — forecast error makes them
    # un-tradeable. 1°F buckets near the resolution day are common but
    # smaller than HRRR/ECMWF MAE so we bucket-miss most of the time.
    min_bucket_width_f: float = 2.0

    # Never buy a "favorite". Markets priced > max_price rarely have enough
    # mispricing to clear EV after slippage + fees.
    max_price: float = 0.45
    min_p_market: float = 0.05

    overconfidence_threshold: float = 0.85
    overconfidence_kelly_multiplier: float = 0.5

    # Realism penalties applied to paper PnL so the running total tracks
    # something closer to live trading conditions:
    #  - simulated_slippage_pct widens the effective entry price (we "pay"
    #    more than the displayed yes_price; weather buckets routinely show
    #    bid/ask spreads of 5-15%).
    #  - simulated_fee_pct deducts taker fees from PnL at close time
    #    (Polymarket weather markets currently quote 5% taker).
    simulated_slippage_pct: float = 0.05
    simulated_fee_pct: float = 0.05

    # Background scheduler — runs scan + settle automatically when uvicorn
    # is up. Set scheduler_enabled=false to disable (manual /scan + /settle
    # still work).
    scheduler_enabled: bool = True
    scheduler_scan_minutes: int = 30
    scheduler_settle_minutes: int = 30
    scheduler_run_on_startup: bool = True
    scheduler_auto_execute: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
