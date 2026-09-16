from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AliasChoices,
    AliasGenerator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets are never read directly by domain code."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SIMON_",
        alias_generator=AliasGenerator(
            validation_alias=lambda name: AliasChoices(
                "SIMON_" + name.upper(), "JARVIS_" + name.upper()
            )
        ),
        populate_by_name=True,
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    storage_backend: Literal["memory", "postgres"] = "memory"
    public_origin: str = "http://localhost:8000"
    public_path: str = Field(default="", pattern=r"^(?:/[a-zA-Z0-9_-]+)*$")
    rp_id: str = "localhost"
    dev_login_enabled: bool = False
    dev_login_token: SecretStr | None = None
    session_hours: int = Field(default=8, ge=1, le=24)
    log_level: str = "INFO"
    database_url: SecretStr = SecretStr("postgresql://jarvis:jarvis@localhost:5432/jarvis")
    # Do not let an inherited legacy key override the renamed application's credential.
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="SIMON_OPENAI_API_KEY")
    model_provider: Literal["local", "openai"] = "local"
    openai_model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=100)
    model_max_output_tokens: int = Field(default=2048, ge=128, le=4096)
    model_input_token_limit: int = Field(default=20000, ge=1000, le=20000)
    deep_model: Literal["gpt-5.4-mini", "gpt-5.4"] = "gpt-5.4"
    auto_deep_enabled: bool = True
    web_search_enabled: bool = True
    google_client_id: str = ""
    google_client_secret: SecretStr | None = None
    google_token_key: SecretStr | None = None
    home_devices_file: Path | None = None
    home_household_id: UUID | None = None
    home_auto_discovery: bool = True
    lifx_token: SecretStr | None = None
    tuya_client_id: str = ""
    tuya_client_secret: SecretStr | None = None
    tuya_region: Literal["us", "us-east", "eu", "eu-west", "cn", "in"] = "us"
    shelly_password: SecretStr | None = None
    shelly_lan_discovery: bool = True
    power_monitoring_enabled: bool = True
    power_poll_seconds: int = Field(default=60, ge=30, le=900)
    power_retention_days: int = Field(default=90, ge=1, le=365)
    voice_enabled: bool = True
    voice_model: Literal["gpt-live-1"] = "gpt-live-1"
    voice_name: Literal["marin", "cedar", "meridian", "vesper"] = "cedar"
    voice_max_seconds: int = Field(default=900, ge=60, le=1800)

    @field_validator("tuya_region", mode="before")
    @classmethod
    def normalize_tuya_region(cls, value: object) -> object:
        return "us" if value == "us-west" else value

    @model_validator(mode="after")
    def validate_google(self) -> "Settings":
        if self.google_token_key:
            from cryptography.fernet import Fernet

            try:
                Fernet(self.google_token_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("SIMON_GOOGLE_TOKEN_KEY must be a generated Fernet key") from None
        return self

    @model_validator(mode="after")
    def validate_model(self) -> "Settings":
        if self.model_provider == "openai" and (
            not self.openai_api_key
            or not self.openai_api_key.get_secret_value().strip()
            or self.openai_api_key.get_secret_value().startswith("your-")
        ):
            raise ValueError("OpenAI mode requires SIMON_OPENAI_API_KEY")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")

    @model_validator(mode="after")
    def validate_identity(self) -> "Settings":
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
        ):
            raise ValueError("public_origin must be an exact HTTP(S) origin without a path")
        if origin.hostname != self.rp_id:
            raise ValueError("rp_id must exactly match the public_origin hostname")
        if origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("HTTP identity is restricted to loopback development")
        if self.dev_login_enabled and origin.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("development login is restricted to loopback origins")
        if self.dev_login_enabled and (
            self.dev_login_token is None or len(self.dev_login_token.get_secret_value()) < 32
        ):
            raise ValueError("development login requires a token of at least 32 characters")
        if self.environment == "production" and (
            self.dev_login_enabled or not self.secure_cookies or self.storage_backend != "postgres"
        ):
            raise ValueError(
                "production requires HTTPS, PostgreSQL, and development login disabled"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
