"""Application settings, read from environment variables and an optional .env file."""

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

REQUIRED_DRIVER_PREFIX = "postgresql+psycopg://"


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Typed configuration. Every field maps to an upper-case environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    log_level: LogLevel = "INFO"

    database_url: str = "postgresql+psycopg://talkischeap:talkischeap@localhost:5432/talkischeap"
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1, le=50)

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        if not value.startswith(REQUIRED_DRIVER_PREFIX):
            raise ValueError(
                f"DATABASE_URL must start with '{REQUIRED_DRIVER_PREFIX}' "
                "(SQLAlchemy with the psycopg 3 driver)"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
