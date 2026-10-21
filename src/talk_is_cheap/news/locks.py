"""Keep news writers from overlapping, with Postgres advisory locks.

A scheduled poll that starts while the previous one is still running should exit, not
interleave with it. Locks are held by a database session, so they work whichever host or
scheduler starts the command, and Postgres releases them if the process dies.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

# Arbitrary, fixed keys ("tic" news), one per kind of writer.
POLL_LOCK = 7_469_630_001
BACKFILL_LOCK = 7_469_630_002
ARCHIVES_LOCK = 7_469_630_003
FINNHUB_LOCK = 7_469_630_004


@asynccontextmanager
async def advisory_locks(engine: AsyncEngine, *keys: int) -> AsyncIterator[bool]:
    """Hold all `keys` for the block; yields False, holding none, if any is taken."""
    async with engine.connect() as connection:
        held: list[int] = []
        try:
            for key in keys:
                if not await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ):
                    break
                held.append(key)
            if len(held) < len(keys):
                await _unlock(connection, held)
                held = []
            yield len(held) == len(keys)
        finally:
            await _unlock(connection, held)


async def _unlock(connection: AsyncConnection, keys: list[int]) -> None:
    for key in keys:
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
