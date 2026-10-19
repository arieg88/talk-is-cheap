"""Fixtures for tests that need a real Postgres + pgvector database.

They use a database of their own, TEST_DATABASE_URL (default `talkischeap_test` on the
local Docker server), which is dropped, recreated and migrated at the start of every test
session, so no test depends on data left by an earlier run or loaded for development.
`tests/conftest.py` points DATABASE_URL at it for the whole run.

If the server is unreachable, these tests are skipped, unless REQUIRE_DATABASE=1 is set
(as in CI), in which case they fail loudly.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_engine
from tests.conftest import configured_test_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _sync_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _recreate(url: str) -> None:
    """Drop and create the test database, connected to the server's `postgres` database."""
    name = make_url(url).database
    if not name or not name.endswith("_test"):
        raise ValueError(
            f"TEST_DATABASE_URL must name a database ending in '_test' (got {name!r}); "
            "the tests drop and recreate it"
        )
    server = make_url(url).set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(_sync_dsn(server), connect_timeout=3, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def database_url() -> str:
    url = configured_test_database_url()
    assert get_settings().database_url == url  # set by tests/conftest.py
    try:
        _recreate(url)
    except psycopg.OperationalError as exc:
        if os.environ.get("REQUIRE_DATABASE") == "1":
            pytest.fail(f"database required but unreachable: {exc}")
        pytest.skip(f"database server unreachable at TEST_DATABASE_URL: {exc}")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply all migrations to the new test database, once per test session."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    return database_url


@pytest.fixture(scope="session")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(get_settings())
    yield engine
    await engine.dispose()
