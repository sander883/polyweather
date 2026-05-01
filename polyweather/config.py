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

    edge_threshold: float = 0.20
    min_liquidity: float = 2000.0
    paper_bankroll: float = 500.0
    kelly_fraction: float = 0.25
    max_pct_per_trade: float = 0.03
    max_pct_per_market: float = 0.08
    max_pct_per_city_day: float = 0.15

    min_time_to_settle_hours: float = 1.0
    ensemble_members: int = 31
    laplace_alpha: float = 0.1

    # Day-5 calibration learnings (n=56 settled positions):
    # - 0.5 Laplace smoothing was inflating probabilities on unsampled tails,
    #   creating fake edges → dropped to 0.1 so probability concentrates where
    #   the ensemble actually points.
    # - The 0.08 edge threshold let through noise; win rate by edge band was
    #   essentially flat. Raised to 0.20 to require substantial disagreement.
    # - Buckets priced 1¢-10¢ produced winners only by variance; the actual
    #   alpha lived in the 30-70¢ price range where model+market roughly
    #   agreed. Floor raised to 0.10.
    # - Calibration showed model is severely overconfident in 0.30-0.50 and
    #   0.80-0.90 p_model bands (gap of −28% and −60% respectively). Trade
    #   only the regions where calibration is least broken.
    min_p_market: float = 0.10
    p_model_min: float = 0.30
    p_model_max: float = 0.80
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
