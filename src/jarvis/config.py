from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets are never read directly by domain code."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JARVIS_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: SecretStr = SecretStr("postgresql://jarvis:jarvis@localhost:5432/jarvis")
    openai_api_key: SecretStr | None = None
    allow_origins: tuple[str, ...] = ("http://localhost:3000",)


@lru_cache
def get_settings() -> Settings:
    return Settings()

