"""Async engine and session factories."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from talk_is_cheap.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Create an async engine with connection health checks enabled."""
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory whose objects stay usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
