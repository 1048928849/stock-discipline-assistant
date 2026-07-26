from functools import lru_cache
from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "股票纪律助手"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "sqlite:///./stock_assistant.db"
    llm_provider: str = "openai_compatible"
    llm_base_url: str = ""
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = ""
    llm_enabled: bool = True
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 1
    llm_max_input_chars: int = 40000
    llm_daily_limit: int = 20
    llm_cache_hours: int = 24
    provider_timeout_seconds: float = 20
    provider_max_retries: int = 2
    market_data_fresh_hours: int = 36
    research_profile_fresh_hours: int = 168
    research_financial_fresh_hours: int = 168
    research_valuation_fresh_hours: int = 36
    research_announcement_fresh_hours: int = 24
    tushare_enabled: bool = False
    tushare_token: str = Field(default="", repr=False)
    tushare_priority: int = 20
    professional_market_api_enabled: bool = False
    professional_market_api_priority: int = 10
    professional_market_api_url: str = ""
    professional_market_api_key: str = Field(default="", repr=False)
    news_api_enabled: bool = False
    news_api_priority: int = 30
    news_api_url: str = ""
    news_api_key: str = Field(default="", repr=False)
    x_cookie: str = Field(default="", repr=False)
    scheduler_enabled: bool = True
    x_sync_minutes: int = 15
    watchlist_monitor_enabled: bool = False
    watchlist_monitor_interval_seconds: int = Field(default=300, ge=1, le=86400)
    watchlist_monitor_batch_size: int = Field(default=100, ge=1, le=500)
    watchlist_monitor_lease_seconds: int = Field(default=240, ge=1, le=86400)
    watchlist_plan_max_age_seconds: int = Field(
        default=86400,
        ge=60,
        le=31_536_000,
    )
    watchlist_near_entry_distance_pct: Decimal = Field(
        default=Decimal("2"), ge=0, le=100
    )
    astock_data_enabled: bool = False
    astock_data_timeout_seconds: float = Field(default=10, gt=0, le=120)
    astock_data_max_retries: int = Field(default=2, ge=1, le=5)
    astock_data_min_interval_seconds: float = Field(default=1, ge=0, le=60)
    astock_data_cache_ttl_seconds: int = Field(default=900, ge=0, le=86400)
    candidate_max_age_seconds: int = Field(default=86400, ge=60, le=604800)
    candidate_discovery_enabled: bool = False
    candidate_discovery_hour: int = Field(default=19, ge=0, le=23)
    candidate_discovery_minute: int = Field(default=30, ge=0, le=59)
    candidate_discovery_max_industries: int = Field(default=3, ge=1, le=20)
    candidate_discovery_max_per_industry: int = Field(default=10, ge=1, le=100)
    candidate_discovery_max_candidates: int = Field(default=30, ge=1, le=500)
    candidate_min_history_coverage_ratio: Decimal = Field(
        default=Decimal("0.8"), ge=0, le=1
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
