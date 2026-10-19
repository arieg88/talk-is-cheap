"""Historical news from the GDELT DOC 2.0 API: `talk-is-cheap ingest news --backfill`.

Each company is searched by its name and aliases in English, one query per week, without
a publisher filter (GDELT rejects long queries). Publishers are filtered locally against
the domains configured in feeds.yaml. GDELT returns at most MAX_RECORDS articles per
query, and local filtering happens after that limit, so a part that comes back full is
split in half and both halves are queried, down to MIN_WINDOW; a part still full there is
recorded as truncated. Items go through the same normalization, deduplication, linking
and storage as feed items. GDELT's date is when its crawler saw the article, not the
publish time, so articles are stored with time precision "seen".

As of 2026-10-19 the API is overloaded: every answer, a refusal (HTTP 429, "Please limit
requests to one every 5 seconds") included, takes about 10 seconds, and refuses most
requests intermittently from every network tested, even a first request. A refusal
means "busy, retry": the same request is answered within a few tries. So after a
refusal, a timeout, a dropped connection or a server error the request is retried
RETRY_PAUSE after the answer, never sooner than MIN_INTERVAL after the previous request.
After MAX_FAILURES failures in a row the window is skipped (GdeltGaveUp) and left for
the next run; the backfill moves on.

Each answered part is stored at once, and a window is recorded in `backfill_windows`
once all of it is stored, so stopping at any point (Ctrl+C, a killed process) loses at
most the window in progress, and running the same command again resumes there. Windows
that end less than SETTLE_TIME ago are fetched but not recorded, since GDELT is still
adding to them.
"""

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
WINDOW = timedelta(days=7)
# GDELT's dates have 15-minute resolution; a part this short that is still full is kept
# as truncated.
MIN_WINDOW = timedelta(minutes=30)
MIN_INTERVAL = 5.0  # seconds between request starts, GDELT's published limit
RETRY_PAUSE = 10.0  # seconds after a refused or failed answer before trying again
MAX_FAILURES = 30  # in a row before a window is skipped (about 10 minutes)
# Answers take 10-15 seconds while GDELT is busy, and sometimes much longer.
TIMEOUT = httpx.Timeout(90.0, connect=30.0)
SETTLE_TIME = timedelta(days=1)
# GDELT searches the full text, ignoring case, so a hit alone is weak evidence.
FEED_LINK = ("feed", 0.3)

_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?%)'\u2019])")
_SPACE_AFTER_OPENING = re.compile(r"([($])\s+")


class GdeltGaveUp(FeedError):
    """A window failed MAX_FAILURES times in a row; it is left for the next run."""


class Pacer:
    """Spaces requests at least `interval` seconds apart."""

    def __init__(self, interval: float = MIN_INTERVAL) -> None:
        self.interval = interval
        self._last = 0.0

    async def wait(self) -> None:
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()


class GdeltBackfill:
    """GDELT DOC 2.0 article search for one configured `backfill` entry."""

    type = "gdelt"  # listed in queries.BACKFILL_TYPES

    def __init__(
        self,
        domains: Sequence[str],
        pacer: Pacer | None = None,
        on_attempt: Callable[["Attempt"], None] | None = None,
    ) -> None:
        self.domains = [d.lower().removeprefix("www.") for d in domains]
        self.pacer = pacer or Pacer()
        # Told about every request: what was asked, how it went, how long it took.
        self.on_attempt = on_attempt

    @classmethod
    def from_entry(
        cls, entry: FeedEntry, on_attempt: Callable[["Attempt"], None] | None = None
    ) -> "GdeltBackfill":
        return cls(entry.domains, on_attempt=on_attempt)

    def query(self, company: UniverseCompany) -> str:
        """The company's names, in English; publishers are filtered after fetching."""
        names = " OR ".join(f'"{name}"' for name in company.names)
        base = f"({names})" if len(company.names) > 1 else names
        return f"{base} sourcelang:english"

    async def fetch_window(
        self,
        client: httpx.AsyncClient,
        query: str,
        start: datetime,
        end: datetime,
        label: str = "",
    ) -> tuple[list[RawItem], bool]:
        """Articles matching `query` that GDELT saw in [start, end), and whether the answer
        hit MAX_RECORDS (the part holds more).

        Refusals, timeouts, dropped connections and server errors are retried RETRY_PAUSE
        after the answer; after MAX_FAILURES in a row, GdeltGaveUp. Other errors raise
        FeedError at once. `label` (the ticker) is passed on to `on_attempt`.
        """
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(MAX_RECORDS),
            "sort": "dateasc",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        }
        for attempt in range(1, MAX_FAILURES + 1):
            await self.pacer.wait()
            sent = time.monotonic()
            try:
                response = await client.get(API_URL, params=params, timeout=TIMEOUT)
            except httpx.TransportError as exc:
                # GDELT's servers are often slow to accept connections, or drop them.
                outcome = f"{type(exc).__name__}: {exc}".rstrip(": ")
            except httpx.HTTPError as exc:
                raise FeedError(f"{type(exc).__name__}: {exc}".rstrip(": ")) from exc
            else:
                if response.status_code == 200 and not _is_refusal(response.text):
                    items, raw_count = self.parse(response.text)
                    self._report(label, start, end, attempt, f"{raw_count} articles", sent)
                    return items, raw_count >= MAX_RECORDS
                if response.status_code not in (200, 429) and response.status_code < 500:
                    raise FeedError(f"HTTP {response.status_code}: {response.text[:200]}")
                outcome = f"refused (HTTP {response.status_code})"
            self._report(label, start, end, attempt, outcome, sent)
            if attempt < MAX_FAILURES:
                await asyncio.sleep(RETRY_PAUSE)
        raise GdeltGaveUp(
            f"the GDELT API kept refusing requests: {MAX_FAILURES} failed attempts in a row "
            f"for {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} (last: {outcome})"
        )

    def _report(
        self, label: str, start: datetime, end: datetime, attempt: int, outcome: str, sent: float
    ) -> None:
        if self.on_attempt is not None:
            self.on_attempt(Attempt(label, start, end, attempt, outcome, time.monotonic() - sent))

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
class Attempt:
    """One request to GDELT."""

    label: str
    start: datetime
    end: datetime
    number: int  # 1 for the first try of this part
    outcome: str  # "250 articles", "refused (HTTP 429)", "ReadTimeout: ..."
    seconds: float  # from sending the request to its answer or failure


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    ticker: str
    windows_done: int
    windows_total: int
    new_articles: int
    requests: int
    remaining: timedelta | None  # estimate for all selected companies
    fetched: int = 0  # windows fetched in this run, all companies
    elapsed: timedelta = timedelta()  # since this run started
    finished: bool = False
    error: str | None = None
    skipped: int = 0  # windows of this company left for the next run (GdeltGaveUp)


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
) -> list[BackfillProgress]:
    """Backfill each company over [since, until), reporting progress after every window.

    A window that fails MAX_FAILURES times in a row is skipped and left for the next run.
    A company whose window fails with another error stops there and the next one starts.
    Finished windows are always kept.
    """
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

    def progress(ticker: str, done: int, total: int, new: int, requests: int) -> BackfillProgress:
        return BackfillProgress(
            ticker,
            done,
            total,
            new,
            requests,
            _estimate(started, fetched_windows, remaining_windows),
            fetched=fetched_windows,
            elapsed=timedelta(seconds=round(time.monotonic() - started)),
        )

    for company, feed_id, to_fetch, total in plans:
        feed = FeedRef(feed_id, f"{source.type}:{company.ticker}", Link(company.ticker, *FEED_LINK))
        done_count, new, requests = total - len(to_fetch), 0, 0
        error = None
        skipped = 0
        for index, (start, end) in enumerate(to_fetch):
            try:
                window = await _fetch_window(
                    session_factory, source, client, company, feed, matcher, start, end
                )
            except GdeltGaveUp as exc:
                skipped += 1
                remaining_windows -= 1
                async with session_factory() as session, session.begin():
                    await record_failure(session, feed_id, datetime.now(UTC), str(exc))
                continue
            except FeedError as exc:
                error = str(exc)
                async with session_factory() as session, session.begin():
                    await record_failure(session, feed_id, datetime.now(UTC), error)
                remaining_windows -= len(to_fetch) - index
                break
            done_count += 1
            fetched_windows += 1
            remaining_windows -= 1
            new += window.new
            requests += window.requests
            if on_progress is not None and index + 1 < len(to_fetch):
                on_progress(progress(company.ticker, done_count, total, new, requests))
        result = replace(
            progress(company.ticker, done_count, total, new, requests),
            finished=True,
            error=error,
            skipped=skipped,
        )
        results.append(result)
        if on_progress is not None:
            on_progress(result)
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
    """Fetch and store one window, splitting a part in half while GDELT's answer is full.

    GdeltGaveUp when one part fails MAX_FAILURES times in a row.
    """
    result = _WindowResult()
    query = source.query(company)
    pending = [(start, end)]
    while pending:
        part_start, part_end = pending.pop(0)
        items, full = await source.fetch_window(client, query, part_start, part_end, company.ticker)
        result.requests += 1
        if full and part_end - part_start > MIN_WINDOW:
            middle = part_start + (part_end - part_start) / 2
            pending[:0] = [(part_start, middle), (middle, part_end)]
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
