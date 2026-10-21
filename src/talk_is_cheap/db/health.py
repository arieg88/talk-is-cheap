"""Database readiness checks."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    server_version: str
    vector_extension_version: str | None
    migration_revision: str | None

    @property
    def ready(self) -> bool:
        """True when pgvector is enabled and at least one migration has been applied."""
        return self.vector_extension_version is not None and self.migration_revision is not None


async def check_database(engine: AsyncEngine) -> DatabaseStatus:
    """Connect and report server version, pgvector version and the applied migration."""
    async with engine.connect() as conn:
        server_version = (await conn.execute(text("SHOW server_version"))).scalar_one()
        vector_version = (
            await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        ).scalar_one_or_none()
        has_version_table = (
            await conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        ).scalar_one()
        revision = None
        if has_version_table:
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()

    return DatabaseStatus(
        server_version=str(server_version),
        vector_extension_version=vector_version,
        migration_revision=revision,
    )


def migration_head(alembic_ini: Path) -> str | None:
    """The newest migration in the project's Alembic scripts, or None when they are not
    there."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not alembic_ini.exists():
        return None
    return ScriptDirectory.from_config(Config(str(alembic_ini))).get_current_head()
