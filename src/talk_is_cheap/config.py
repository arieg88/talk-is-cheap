"""Application settings, read from environment variables and an optional .env file."""

import re
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

REQUIRED_DRIVER_PREFIX = "postgresql+psycopg://"

# The SEC asks automated clients to identify themselves as "Name contact@example.com".
_IDENTITY_PATTERN = re.compile(r"^\S.*\s+\S+@\S+\.\S+$")


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

    universe_file: Path = Path("universe.yaml")

    # Sent to the SEC with every request, as its fair-access policy requires.
    edgar_identity: str | None = None
    # Where edgartools caches SEC responses.
    edgar_data_dir: Path = Path(".cache/edgar")

    # News feeds and backfill sources.
    feeds_file: Path = Path("feeds.yaml")
    # Sent with every news request. Feed publishers see it; keep it descriptive.
    news_user_agent: str = "talk-is-cheap/0.1 (news headline collector; metadata only)"
    # Sent when fetching article pages for statement extraction (`ingest article-text`).
    # Its first word is the robots.txt user agent the fetcher obeys.
    article_user_agent: str = (
        "talk-is-cheap/0.1 (research: reads executive quotes in company news, "
        "follows robots.txt, at most one request per second per site)"
    )

    # Local models for search (fastembed). Changing the embedding model re-embeds
    # everything on the next `talk-is-cheap index`.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    # Where the models are downloaded, once.
    model_cache_dir: Path = Path(".cache/fastembed")

    # Chat model for guidance extraction, through LangChain. Haiku 5.5 is the default for
    # cost; the benchmark comparison is in docs/guidance.md.
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-5-5"
    # Anthropic reasoning effort (low, medium, high, xhigh, max); empty for the model's
    # default. Part of the model's name in the cache and in stored rows.
    llm_effort: str | None = "low"
    llm_api_key: SecretStr | None = None
    # The key under the Anthropic SDK's own name, used when LLM_API_KEY is empty.
    anthropic_api_key: SecretStr | None = None
    # A run stops before a call that could take its spending over this many US dollars.
    llm_cost_cap: Decimal = Field(default=Decimal("10.00"), ge=0)
    # Hard cap on all LLM spending recorded in the ledger (`talk-is-cheap spend`): a call
    # that could take the total over it is refused.
    llm_budget_usd: Decimal = Field(default=Decimal("2.50"), ge=0)
    # Prices in US dollars per million tokens, for models without a list price in
    # guidance/llm.py. Both or neither.
    llm_input_price: Decimal | None = None
    llm_output_price: Decimal | None = None
    # Every model response, so re-runs cost nothing.
    llm_cache_dir: Path = Path(".cache/llm")

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        if not value.startswith(REQUIRED_DRIVER_PREFIX):
            raise ValueError(
                f"DATABASE_URL must start with '{REQUIRED_DRIVER_PREFIX}' "
                "(SQLAlchemy with the psycopg 3 driver)"
            )
        return value

    @field_validator(
        "llm_api_key", "llm_effort", "llm_input_price", "llm_output_price", mode="before"
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _anthropic_key_as_llm_key(self) -> "Settings":
        if self.llm_api_key is None and self.llm_provider == "anthropic":
            self.llm_api_key = self.anthropic_api_key
        return self

    @field_validator("edgar_identity")
    @classmethod
    def _require_sec_contact(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None  # unset; only commands that call the SEC require it
        if not _IDENTITY_PATTERN.match(value.strip()):
            raise ValueError(
                "EDGAR_IDENTITY must be a name followed by a contact email, "
                "e.g. 'Jane Doe jane@example.com'"
            )
        return value.strip()


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
