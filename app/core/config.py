"""Application settings, loaded from the environment only.

Every value here has a safe development default so the app boots on a clean
checkout, but each one can be overridden in production. Secrets are never
hardcoded: SECRET_KEY has no usable default and the app refuses to start with
the placeholder when APP_ENV=production.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRET = "change-me-in-production-this-is-not-a-real-key"


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Barq"
    app_env: Literal["development", "production", "test"] = "development"
    debug: bool = False

    # --- persistence -----------------------------------------------------
    # Falls back to a local SQLite file when DATABASE_URL is unset, so the app
    # runs with zero external services. docker-compose injects Postgres.
    database_url: str = "sqlite:///./barq.db"
    db_echo: bool = False

    # --- auth ------------------------------------------------------------
    secret_key: str = _PLACEHOLDER_SECRET
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 12
    bcrypt_rounds: int = 12

    # --- security --------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    auth_rate_limit_requests: int = 10
    auth_rate_limit_window_seconds: int = 60

    # --- grid domain -----------------------------------------------------
    nominal_frequency_hz: float = 50.0
    # Aggregate primary-response stiffness, MW per Hz of deviation. Pakistan's
    # system is roughly 25 GW with a 4-5% droop, giving ~500-600 MW/Hz.
    grid_stiffness_mw_per_hz: float = 550.0
    dispatch_interval_minutes: int = 15
    reserve_margin_ratio: float = 0.08
    # Two standard deviations of the forecasting error, as a fraction of load.
    forecast_sigma_ratio: float = 0.03

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, value: str) -> str:
        # Heroku/Render style URLs arrive as postgres:// which SQLAlchemy 2
        # rejects. Rewrite rather than crash on a perfectly valid URL.
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def secret_key_is_placeholder(self) -> bool:
        return self.secret_key == _PLACEHOLDER_SECRET

    def validate_for_startup(self) -> None:
        """Refuse to boot in production with a placeholder secret."""
        if self.is_production and self.secret_key_is_placeholder:
            raise RuntimeError(
                "SECRET_KEY is still the development placeholder. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
