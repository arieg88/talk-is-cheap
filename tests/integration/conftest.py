"""Fixtures for tests that need a real Postgres + pgvector database.

The database comes from DATABASE_URL. If it is unreachable, these tests are skipped,
unless REQUIRE_DATABASE=1 is set (as in CI), in which case they fail loudly.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _sync_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = get_settings().database_url
    try:
        psycopg.connect(_sync_dsn(url), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail(f"database required but unreachable: {exc}")
        pytest.skip(f"database unreachable at DATABASE_URL: {exc}")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply all migrations once per test session."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    return database_url


@pytest.fixture(scope="session")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(get_settings())
    yield engine
    await engine.dispose()
