"""Conversation memory: LangGraph's Postgres checkpointer, keyed by thread id.

Its tables are created by migration 0009 (`make migrate`), not by the library's
`setup()`, so tests and every database get them from Alembic. `open_checkpointer`
checks that the migrations recorded match the installed library, so an upgraded library
that needs a new table change says so instead of failing mid-conversation.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import MIGRATIONS
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine


class CheckpointerSchemaError(RuntimeError):
    pass


def psycopg_url(database_url: str) -> str:
    """A SQLAlchemy `postgresql+psycopg://` URL as a plain libpq one."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


@asynccontextmanager
async def open_checkpointer(
    engine: AsyncEngine, *, pool_size: int | None = None
) -> AsyncIterator[AsyncPostgresSaver]:
    """The checkpointer on one connection, or on a pool of up to `pool_size` connections
    for concurrent runs (the HTTP API)."""
    await check_checkpointer_schema(engine)
    url = psycopg_url(engine.url.render_as_string(hide_password=False))
    if pool_size is None:
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            yield saver
        return
    # The connection settings from_conn_string uses.
    options = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
    async with AsyncConnectionPool(
        url,
        min_size=1,
        max_size=pool_size,
        kwargs=options,
        connection_class=AsyncConnection[DictRow],
        open=False,
    ) as pool:
        yield AsyncPostgresSaver(pool)


async def check_checkpointer_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        exists = await connection.scalar(text("SELECT to_regclass('checkpoint_migrations')"))
        if exists is None:
            raise CheckpointerSchemaError(
                "the agent's checkpoint tables are missing; run `make migrate`"
            )
        applied = await connection.scalar(text("SELECT max(v) FROM checkpoint_migrations"))
    if applied != len(MIGRATIONS) - 1:
        raise CheckpointerSchemaError(
            f"checkpoint tables are at version {applied}, langgraph-checkpoint-postgres "
            f"expects {len(MIGRATIONS) - 1}: add a migration with its new statements"
        )
