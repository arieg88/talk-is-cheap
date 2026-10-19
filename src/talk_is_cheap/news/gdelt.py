"""Historical news from the GDELT DOC 2.0 API: `talk-is-cheap ingest news --backfill`.

Each company is searched by its name and aliases, in English, restricted to the
publisher domains configured in feeds.yaml, one week at a time. The domains are split
into groups so that each query stays within MAX_QUERY_CHARS, one query per group. GDELT
returns at most MAX_RECORDS articles per query, so a part that comes back full is split
in half until it fits or reaches MIN_WINDOW. Items go through the same normalization,
deduplication, linking and storage as feed items. GDELT's date is when its crawler saw
the article, not the publish time, so articles are stored with time precision "seen".

Requests are paced at least MIN_INTERVAL apart. GDELT refuses requests it does not want
(HTTP 429, "Please limit requests to one every 5 seconds"); the pace then slows (up to
MAX_INTERVAL) and the request is retried after a pause. Timeouts and connection errors
are retried the same way. A window gets at most ATTEMPTS requests within
MAX_WINDOW_SECONDS. After STOP_AFTER_REFUSALS refusals in a row, GDELT is taken to be
refusing this machine: the backfill stops (GdeltRefused) and finished weeks are kept. A
window is recorded in `backfill_windows` once all of it is fetched and stored, so an
interrupted run continues where it stopped and a re-run of a finished range sends no
requests. Windows that end less than SETTLE_TIME ago are fetched
but not recorded, since GDELT is still adding to them.
"""

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import BackfillWindow
from talk_is_cheap.news.adapters import FeedEntry, FeedError
from talk_is_cheap.news.linking import CompanyMatcher, Link
from talk_is_cheap.news.records import RawItem, TimePrecision
from talk_is_cheap.news.store import (
    FeedRef,
    record_failure,
    record_success,
    store_items,
    upsert_feed,
)
from talk_is_cheap.universe import UniverseCompany

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250
# GDELT answers "Your query was too short or too long" to a 743-character query with 28
# domains; queries of about 100 characters were answered. The exact limit is not
# documented, so the domain list is split to keep each query this short.
MAX_QUERY_CHARS = 250
WINDOW = timedelta(days=7)
MIN_WINDOW = timedelta(hours=3)
MIN_INTERVAL = 5.0  # seconds between requests, GDELT's published limit
MAX_INTERVAL = 60.0
ATTEMPTS = 3
MAX_WINDOW_SECONDS = 180.0
STOP_AFTER_REFUSALS = 3
PAUSES = (30.0, 60.0)  # before the second and third attempts
# GDELT can take tens of seconds to accept a connection and to answer.
TIMEOUT = httpx.Timeout(90.0, connect=30.0)
SETTLE_TIME = timedelta(days=1)
# GDELT searches the full text, ignoring case, so a hit alone is weak evidence.
FEED_LINK = ("feed", 0.3)

_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?%)'\u2019])")
_SPACE_AFTER_OPENING = re.compile(r"([($])\s+")


class GdeltRefused(FeedError):
    """GDELT refused STOP_AFTER_REFUSALS requests in a row."""


class Pacer:
    """Spaces requests at least `interval` seconds apart; slows down when told to."""

    def __init__(self, interval: float = MIN_INTERVAL) -> None:
        self.interval = interval
        self._last = 0.0

    async def wait(self) -> None:
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()

    def slow_down(self) -> None:
        self.interval = min(MAX_INTERVAL, self.interval * 2)


class GdeltBackfill:
    """GDELT DOC 2.0 article search for one configured `backfill` entry."""

    type = "gdelt"  # listed in queries.BACKFILL_TYPES

    def __init__(
        self,
        domains: Sequence[str],
        pacer: Pacer | None = None,
        on_retry: Callable[[str, float], None] | None = None,
    ) -> None:
        self.domains = [d.lower().removeprefix("www.") for d in domains]
        self.pacer = pacer or Pacer()
        # Told the reason and the pause before each retry.
        self.on_retry = on_retry
        self.refusals = 0  # in a row, across windows

    @classmethod
    def from_entry(
        cls, entry: FeedEntry, on_retry: Callable[[str, float], None] | None = None
    ) -> "GdeltBackfill":
        return cls(entry.domains, on_retry=on_retry)

    def queries(self, company: UniverseCompany) -> list[str]:
        """The company's search, once per group of domains that keeps each query within
        MAX_QUERY_CHARS (GDELT rejects long queries)."""
        names = " OR ".join(f'"{name}"' for name in company.names)
        base = f"({names})" if len(company.names) > 1 else names
        if not self.domains:
            return [f"{base} sourcelang:english"]
        queries: list[str] = []
        group: list[str] = []
        for domain_name in self.domains:
            if group and len(_query(base, [*group, domain_name])) > MAX_QUERY_CHARS:
                queries.append(_query(base, group))
                group = []
            group.append(domain_name)
        queries.append(_query(base, group))
        return queries

    async def fetch_window(
        self,
        client: httpx.AsyncClient,
        query: str,
        start: datetime,
        end: datetime,
    ) -> tuple[list[RawItem], bool]:
        """Articles matching `query` that GDELT saw in [start, end), and whether the answer
        hit MAX_RECORDS (the window holds more). FeedError when GDELT cannot answer."""
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(MAX_RECORDS),
            "sort": "dateasc",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        }
        started = time.monotonic()
        last_error = ""
        for attempt in range(ATTEMPTS):
            await self.pacer.wait()
            try:
                response = await client.get(API_URL, params=params, timeout=TIMEOUT)
            except httpx.TransportError as exc:
                # GDELT's servers are often slow to accept connections, or drop them.
                last_error = f"{type(exc).__name__}: {exc}".rstrip(": ")
            except httpx.HTTPError as exc:
                raise FeedError(f"{type(exc).__name__}: {exc}".rstrip(": ")) from exc
            else:
                if response.status_code == 200 and not _is_refusal(response.text):
                    self.refusals = 0
                    items, raw_count = self.parse(response.text)
                    return items, raw_count >= MAX_RECORDS
                if response.status_code not in (200, 429):
                    raise FeedError(f"HTTP {response.status_code}: {response.text[:200]}")
                self.refusals += 1
                last_error = f"refused (HTTP {response.status_code}: {_first_line(response.text)})"
                if self.refusals >= STOP_AFTER_REFUSALS:
                    raise GdeltRefused(f"GDELT {last_error}, {self.refusals} times in a row")
                self.pacer.slow_down()
            if attempt + 1 == ATTEMPTS:
                break
            pause = PAUSES[min(attempt, len(PAUSES) - 1)]
            if time.monotonic() - started + pause > MAX_WINDOW_SECONDS:
                break
            if self.on_retry is not None:
                self.on_retry(last_error, pause)
            await asyncio.sleep(pause)
        raise FeedError(f"{last_error}; gave up on this window")

    def parse(self, body: str) -> tuple[list[RawItem], int]:
        """The wanted articles of an answer, and how many articles it held in all (domain
        and language filtering happen after GDELT's record limit)."""
        if not body.strip():
            return [], 0  # GDELT answers an empty body when nothing matches
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            # Errors in the query come back as plain text with status 200.
            raise FeedError(f"GDELT: {body.strip()[:200]}") from exc
        articles = data.get("articles", [])
        items = []
        for article in articles:
            if not self._wanted(article):
                continue
            items.append(
                RawItem(
                    title=clean_gdelt_title(str(article.get("title", ""))),
                    link=str(article["url"]),
                    published=str(article.get("seendate", "")) or None,
                    publisher=str(article.get("domain", "")) or None,
                    time_precision=TimePrecision.SEEN,
                )
            )
        return items, len(articles)

    def _wanted(self, article: dict[str, Any]) -> bool:
        if not article.get("url") or str(article.get("language", "English")) != "English":
            return False
        host = str(article.get("domain", "")).lower().removeprefix("www.")
        return not self.domains or any(host == d or host.endswith(f".{d}") for d in self.domains)


def _query(base: str, domains: Sequence[str]) -> str:
    sites = " OR ".join(f"domainis:{d}" for d in domains)
    return (
        f"{base} ({sites}) sourcelang:english"
        if len(domains) > 1
        else (f"{base} domainis:{domains[0]} sourcelang:english")
    )


def _is_refusal(body: str) -> bool:
    return body.lstrip().startswith("Please limit requests")


def _first_line(body: str) -> str:
    line = body.strip().split("\n", 1)[0]
    return line[:80] + ("..." if len(line) > 80 else "")


def clean_gdelt_title(title: str) -> str:
    """GDELT's titles have spaces around punctuation ("Buybacks . The Real Prize")."""
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", title.strip())
    return _SPACE_AFTER_OPENING.sub(r"\1", text)


def windows(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """Consecutive WINDOW-long windows from `since`, the last one cut at `until`."""
    result = []
    start = since
    while start < until:
        result.append((start, min(start + WINDOW, until)))
        start += WINDOW
    return result


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    ticker: str
    windows_done: int
    windows_total: int
    new_articles: int
    requests: int
    remaining: timedelta | None  # estimate for all selected companies
    finished: bool = False
    error: str | None = None
    refused: bool = False  # GDELT refused this machine; the backfill stopped here


@dataclass(slots=True)
class _WindowResult:
    requests: int = 0
    items: int = 0
    new: int = 0
    truncated: bool = False


async def backfill(
    session_factory: async_sessionmaker[AsyncSession],
    source: GdeltBackfill,
    companies: Sequence[UniverseCompany],
    matcher: CompanyMatcher,
    client: httpx.AsyncClient,
    *,
    since: datetime,
    until: datetime,
    on_progress: Callable[[BackfillProgress], None] | None = None,
    progress_every: timedelta = timedelta(minutes=1),
) -> list[BackfillProgress]:
    """Backfill each company over [since, until). A company whose window fails stops
    there and the next one starts; when GDELT refuses this machine the whole backfill
    stops. Finished windows are always kept."""
    plans = []
    async with session_factory() as session, session.begin():
        for company in companies:
            feed_id, _ = await upsert_feed(
                session, f"{source.type}:{company.ticker}", source.type, API_URL, company.ticker
            )
            done = set(
                (
                    await session.execute(
                        select(BackfillWindow.window_start).where(BackfillWindow.feed_id == feed_id)
                    )
                ).scalars()
            )
            to_fetch = [w for w in windows(since, until) if w[0] not in done]
            plans.append((company, feed_id, to_fetch, len(windows(since, until))))

    remaining_windows = sum(len(to_fetch) for _, _, to_fetch, _ in plans)
    started = time.monotonic()
    fetched_windows = 0
    results = []
    for company, feed_id, to_fetch, total in plans:
        feed = FeedRef(feed_id, f"{source.type}:{company.ticker}", Link(company.ticker, *FEED_LINK))
        done_count, new, requests = total - len(to_fetch), 0, 0
        last_report = time.monotonic()
        error = None
        refused = False
        for index, (start, end) in enumerate(to_fetch):
            try:
                window = await _fetch_window(
                    session_factory, source, client, company, feed, matcher, start, end
                )
            except FeedError as exc:
                error = str(exc)
                refused = isinstance(exc, GdeltRefused)
                async with session_factory() as session, session.begin():
                    await record_failure(session, feed_id, datetime.now(UTC), error)
                remaining_windows -= len(to_fetch) - index
                break
            done_count += 1
            fetched_windows += 1
            remaining_windows -= 1
            new += window.new
            requests += window.requests
            due = time.monotonic() - last_report >= progress_every.total_seconds()
            if on_progress is not None and due:
                last_report = time.monotonic()
                on_progress(
                    BackfillProgress(
                        company.ticker,
                        done_count,
                        total,
                        new,
                        requests,
                        _estimate(started, fetched_windows, remaining_windows),
                    )
                )
        progress = BackfillProgress(
            company.ticker,
            done_count,
            total,
            new,
            requests,
            _estimate(started, fetched_windows, remaining_windows),
            finished=True,
            error=error,
            refused=refused,
        )
        results.append(progress)
        if on_progress is not None:
            on_progress(progress)
        if refused:
            break  # the other companies would be refused too
    return results


async def _fetch_window(
    session_factory: async_sessionmaker[AsyncSession],
    source: GdeltBackfill,
    client: httpx.AsyncClient,
    company: UniverseCompany,
    feed: FeedRef,
    matcher: CompanyMatcher,
    start: datetime,
    end: datetime,
) -> _WindowResult:
    """Fetch and store one window for each query, splitting a part while GDELT's answer is
    full."""
    result = _WindowResult()
    pending = [(query, start, end) for query in source.queries(company)]
    while pending:
        query, part_start, part_end = pending.pop(0)
        items, full = await source.fetch_window(client, query, part_start, part_end)
        result.requests += 1
        if full and part_end - part_start > MIN_WINDOW:
            middle = part_start + (part_end - part_start) / 2
            pending[:0] = [(query, part_start, middle), (query, middle, part_end)]
            continue
        result.truncated |= full
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            stored = await store_items(session, feed, items, matcher, now)
            await record_success(session, feed.id, now, new_articles=stored.new)
        result.items += stored.items
        result.new += stored.new

    if end <= datetime.now(UTC) - SETTLE_TIME:
        async with session_factory() as session, session.begin():
            await session.execute(
                insert(BackfillWindow)
                .values(
                    feed_id=feed.id,
                    window_start=start,
                    window_end=end,
                    requests=result.requests,
                    items=result.items,
                    new_articles=result.new,
                    truncated=result.truncated,
                )
                .on_conflict_do_nothing(constraint="uq_backfill_windows_window")
            )
    return result


def _estimate(started: float, fetched: int, remaining: int) -> timedelta | None:
    if not fetched:
        return None
    return timedelta(seconds=round((time.monotonic() - started) / fetched * max(remaining, 0)))
