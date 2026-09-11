from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets are never read directly by domain code."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JARVIS_",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    storage_backend: Literal["memory", "postgres"] = "memory"
    public_origin: str = "http://localhost:8000"
    rp_id: str = "localhost"
    dev_login_enabled: bool = False
    dev_login_token: SecretStr | None = None
    session_hours: int = Field(default=8, ge=1, le=24)
    log_level: str = "INFO"
    database_url: SecretStr = SecretStr("postgresql://jarvis:jarvis@localhost:5432/jarvis")
    openai_api_key: SecretStr | None = None
    model_provider: Literal["local", "openai"] = "local"
    openai_model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=100)
    model_max_output_tokens: int = Field(default=2048, ge=128, le=4096)
    model_input_token_limit: int = Field(default=20000, ge=1000, le=20000)

    @model_validator(mode="after")
    def validate_model(self) -> "Settings":
        if self.model_provider == "openai" and (
            not self.openai_api_key
            or not self.openai_api_key.get_secret_value().strip()
            or self.openai_api_key.get_secret_value().startswith("your-")
        ):
            raise ValueError("OpenAI mode requires JARVIS_OPENAI_API_KEY")
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
