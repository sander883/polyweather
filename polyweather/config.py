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

    # Phase 2A — switched threshold from `edge` (additive, p_model − price)
    # to `EV` (multiplicative, p*(1/price-1) − (1-p)). EV scales correctly
    # across price ranges; a 0.20 edge at $0.50 means much less than a 0.20
    # edge at $0.05.
    min_ev: float = 0.10
    min_liquidity: float = 2000.0
    paper_bankroll: float = 500.0
    kelly_fraction: float = 0.25
    max_pct_per_trade: float = 0.03
    max_pct_per_market: float = 0.08
    max_pct_per_city_day: float = 0.15

    min_time_to_settle_hours: float = 2.0
    max_time_to_settle_hours: float = 72.0

    # Phase 2A — alteregoeth/weatherbot sigma defaults: expected forecast
    # error (in °F) for tail-bucket normal CDF. Self-calibration (Phase 2C)
    # replaces these with empirical per-(city, source) MAE.
    sigma_default_f: float = 2.0
    sigma_default_c: float = 1.2

    # Phase 2A — never buy a "favorite". Markets priced > max_price rarely
    # have enough mispricing to clear EV after slippage + fees. Day-5
    # diagnostics showed our normal-priced winners (entry > 50¢) were a
    # rounding error vs the tail-bet variance.
    max_price: float = 0.45
    min_p_market: float = 0.05

    # Lower bound on p_model: with a point forecast + binary bucket, p_model
    # is either 1.0 (forecast in bucket) or 0.0 (out). For tail buckets it
    # falls on the normal CDF. We never want to trade if the forecast says
    # the bucket is highly unlikely.
    p_model_min: float = 0.30

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
