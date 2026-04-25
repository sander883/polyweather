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

    edge_threshold: float = 0.08
    min_liquidity: float = 2000.0
    paper_bankroll: float = 500.0
    kelly_fraction: float = 0.25
    max_pct_per_trade: float = 0.03
    max_pct_per_market: float = 0.08
    max_pct_per_city_day: float = 0.15

    min_time_to_settle_hours: float = 1.0
    ensemble_members: int = 31
    laplace_alpha: float = 0.5

    # Day-1 calibration learnings (see commit log):
    # - tail buckets priced near zero are noise, not alpha → skip
    # - very-high model confidence is suspicious until we have enough
    #   calibration data, so halve the Kelly fraction in that regime.
    min_p_market: float = 0.01
    overconfidence_threshold: float = 0.85
    overconfidence_kelly_multiplier: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
