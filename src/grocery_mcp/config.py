from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GROCERY_",
        env_ignore_empty=True,
        extra="ignore",
    )

    state_dir: Path = Path("./state")
    database_url: str | None = None
    encryption_key: SecretStr | None = None
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    debug_http: bool = False
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["grocery-mcp", "localhost", "127.0.0.1"]
    )
    change_set_ttl_seconds: int = 900
    price_change_tolerance_percent: float = 5.0
    retailer_timeout_seconds: float = 20.0
    enable_sixty60_writes: bool = False
    enable_woolworths_writes: bool = False
    sixty60_latitude: float | None = None
    sixty60_longitude: float | None = None
    sixty60_dsl_api_key: SecretStr | None = None
    sixty60_auth_api_key: SecretStr | None = None
    sixty60_profile_api_token: SecretStr | None = None
    sixty60_app_version: str = "iPadOS 2.0.99"
    sixty60_app_build: str = "unknown"
    woolworths_place_id: str | None = None
    woolworths_store_id: str | None = None
    woolworths_email: str | None = None
    woolworths_password: SecretStr | None = None

    @field_validator("change_set_ttl_seconds")
    @classmethod
    def valid_ttl(cls, value: int) -> int:
        if not 60 <= value <= 86400:
            raise ValueError("change-set TTL must be between 60 seconds and 24 hours")
        return value

    @field_validator("price_change_tolerance_percent")
    @classmethod
    def valid_price_tolerance(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("price tolerance must be between 0 and 100 percent")
        return value

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.state_dir / 'grocery.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
