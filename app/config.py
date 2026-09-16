from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "multi-country-invoicing"
    app_env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/invoicing"
    redis_url: str = "redis://localhost:6379/0"
    api_key: str = "change-me"

    provider_ar_base_url: str = "http://localhost:8001"
    provider_br_base_url: str = "http://localhost:8002"

    provider_ar_timeout_ms: int = Field(default=1500, ge=100, le=10000)
    provider_br_timeout_ms: int = Field(default=1500, ge=100, le=10000)
    provider_ar_max_retries: int = Field(default=3, ge=1, le=10)
    provider_br_max_retries: int = Field(default=3, ge=1, le=10)

    retry_base_ms: int = Field(default=200, ge=10, le=5000)
    retry_cap_ms: int = Field(default=1500, ge=50, le=10000)

    idempotency_lock_ttl_seconds: int = Field(default=30, ge=5, le=600)
    idempotency_record_ttl_hours: int = Field(default=24, ge=1, le=168)

    pending_reconciliation_minutes: int = Field(default=15, ge=1, le=1440)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
