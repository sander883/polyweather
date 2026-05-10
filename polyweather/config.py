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

    # Phase 2A.4 (Day-11): Phase 2A.3 emitted 0 signals across 48h because
    # min_ev=0.30 + σ=4 + min_bucket_width=2 left an empty intersection
    # — Polymarket weather buckets are mostly 1-2°F wide and rarely priced
    # below 30¢ at the wider end. Loosen all three to let some signals
    # through without going back to Phase 2A.2's neighbour-bucket disaster.
    min_ev: float = 0.20
    min_liquidity: float = 2000.0
    paper_bankroll: float = 500.0
    kelly_fraction: float = 0.25
    max_pct_per_trade: float = 0.03
    max_pct_per_market: float = 0.08
    max_pct_per_city_day: float = 0.15

    min_time_to_settle_hours: float = 2.0
    max_time_to_settle_hours: float = 72.0

    # Forecast-error std (°F / °C) — mid-range between alteregoeth's
    # 2.0/1.2 (too tight, made us overconfident) and Phase 2A.3's 4.0/2.4
    # (too loose, killed all signals).
    sigma_default_f: float = 3.0
    sigma_default_c: float = 1.8

    # Skip buckets narrower than this (°F). 1°F buckets are common near
    # settlement and tradable when the forecast is well-centred.
    min_bucket_width_f: float = 1.0

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
