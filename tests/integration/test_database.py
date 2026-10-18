from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from talk_is_cheap.db import check_database

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def test_database_is_at_the_latest_migration(engine: AsyncEngine) -> None:
    head = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_current_head()
    status = await check_database(engine)
    assert status.ready
    assert status.server_version.startswith("16")
    assert status.migration_revision == head


async def test_vector_similarity_search_works(engine: AsyncEngine) -> None:
    """pgvector is usable end to end: store vectors, then order by cosine distance."""
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE probe (label text, embedding vector(3)) ON COMMIT DROP")
        )
        await conn.execute(
            text(
                "INSERT INTO probe VALUES "
                "('same', '[1, 0, 0]'), ('close', '[0.9, 0.1, 0]'), ('far', '[0, 0, 1]')"
            )
        )
        labels = (
            (
                await conn.execute(
                    text("SELECT label FROM probe ORDER BY embedding <=> '[1, 0, 0]' LIMIT 2")
                )
            )
            .scalars()
            .all()
        )
    assert labels == ["same", "close"]
