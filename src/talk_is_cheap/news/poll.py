"""One poll of every configured feed: `talk-is-cheap ingest news`.

Feeds are fetched concurrently (at most HOST_CONCURRENCY at a time per host), then
stored one at a time, each in its own transaction. A feed that fails to fetch, parse or
store is recorded as a failure in its health and does not stop the others. Re-running is
safe: articles are stored once per canonical URL, and a feed that answers 304 or sends
the same body as last time costs no parsing or writes.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.news.adapters import TIMEOUT, FeedAdapter, FeedError
from talk_is_cheap.news.linking import CompanyMatcher, Link
from talk_is_cheap.news.records import FetchResult, Validators
from talk_is_cheap.news.store import (
    FeedRef,
    record_failure,
    record_success,
    store_items,
    upsert_feed,
)

HOST_CONCURRENCY = 2


@dataclass(frozen=True, slots=True)
class FeedPollResult:
    key: str
    items: int = 0
    new: int = 0
    duplicates: int = 0
    clustered: int = 0
    spam: int = 0
    not_modified: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def new_client(user_agent: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
    )


async def poll_feeds(
    session_factory: async_sessionmaker[AsyncSession],
    feeds: Sequence[FeedAdapter],
    matcher: CompanyMatcher,
    client: httpx.AsyncClient,
    *,
    on_result: Callable[[FeedPollResult], None] | None = None,
) -> list[FeedPollResult]:
    async with session_factory() as session, session.begin():
        stored = {
            feed.key: await upsert_feed(session, feed.key, feed.type, feed.url, feed.ticker)
            for feed in feeds
        }

    hosts: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(HOST_CONCURRENCY))

    async def fetch(feed: FeedAdapter, validators: Validators) -> FetchResult | str:
        async with hosts[urlsplit(feed.url).netloc]:
            try:
                return await feed.fetch(client, validators)
            except FeedError as exc:
                return str(exc)
            except Exception as exc:  # an adapter bug must not stop the other feeds
                return f"{type(exc).__name__}: {exc}"

    fetched = await asyncio.gather(*(fetch(f, stored[f.key][1]) for f in feeds))

    results = []
    for feed, outcome in zip(feeds, fetched, strict=True):
        feed_id = stored[feed.key][0]
        result = await _store(session_factory, feed, feed_id, outcome, matcher)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


async def _store(
    session_factory: async_sessionmaker[AsyncSession],
    feed: FeedAdapter,
    feed_id: int,
    outcome: FetchResult | str,
    matcher: CompanyMatcher,
) -> FeedPollResult:
    now = datetime.now(UTC)
    if isinstance(outcome, str):
        return await _fail(session_factory, feed, feed_id, now, outcome)
    try:
        async with session_factory() as session, session.begin():
            if outcome.not_modified:
                await record_success(
                    session, feed_id, now, new_articles=0, validators=outcome.validators
                )
                return FeedPollResult(feed.key, not_modified=True)
            link = Link(feed.ticker, *feed.feed_link) if feed.ticker and feed.feed_link else None
            stored = await store_items(
                session, FeedRef(feed_id, feed.key, link), outcome.items, matcher, now
            )
            await record_success(
                session, feed_id, now, new_articles=stored.new, validators=outcome.validators
            )
    except Exception as exc:  # one feed's bad data must not stop the others
        return await _fail(session_factory, feed, feed_id, now, f"{type(exc).__name__}: {exc}")
    return FeedPollResult(
        feed.key,
        items=stored.items,
        new=stored.new,
        duplicates=stored.duplicates,
        clustered=stored.clustered,
        spam=stored.spam,
    )


async def _fail(
    session_factory: async_sessionmaker[AsyncSession],
    feed: FeedAdapter,
    feed_id: int,
    now: datetime,
    error: str,
) -> FeedPollResult:
    async with session_factory() as session, session.begin():
        await record_failure(session, feed_id, now, error)
    return FeedPollResult(feed.key, error=error)
