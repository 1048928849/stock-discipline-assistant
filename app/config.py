from functools import lru_cache

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
    x_cookie: str = Field(default="", repr=False)
    scheduler_enabled: bool = True
    x_sync_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
