"""Alembic environment: runs migrations over the app's async engine."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from talk_is_cheap.config import get_settings
from talk_is_cheap.db import Base, create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Each embedding model's HNSW index is created at run time (retrieval/store.py), not
# declared on the model, so autogenerate leaves them alone.
VECTOR_INDEX_PREFIX = "ix_embeddings_hnsw_"

# The agent's LangGraph checkpointer tables: created by migration 0009 with the
# library's own schema, read and written only by langgraph-checkpoint-postgres, and not
# declared as models, so autogenerate leaves them and their indexes alone.
CHECKPOINT_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    if type_ == "table" and name in CHECKPOINT_TABLES:
        return False
    if type_ in ("index", "column", "unique_constraint", "foreign_key_constraint"):
        table = getattr(obj, "table", None)
        if getattr(table, "name", None) in CHECKPOINT_TABLES:
            return False
    return not (type_ == "index" and (name or "").startswith(VECTOR_INDEX_PREFIX))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(get_settings())
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
