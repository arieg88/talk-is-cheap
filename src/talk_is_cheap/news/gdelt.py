"""Historical news from the GDELT DOC 2.0 API: `talk-is-cheap ingest news --backfill`.

Two kinds of window: weeks from a start date (`--since`), or the days around each stored
earnings release (`--around-earnings`, recommended: it fetches the news that matters for
a fraction of the requests). Windows already fetched are skipped, and earnings windows
end where RSS polling began.

Queries. GDELT returns at most MAX_RECORDS articles per query and rejects long queries,
so the publisher domains in feeds.yaml are sent in batches of at most MAX_QUERY_CHARS.
A company flagged `gdelt_filter_publishers` in universe.yaml (names that are common
words, or heavily covered) is always searched with the domain batches. Any other company
is searched by name alone first, with publishers filtered locally; when that answer is
full, its articles are kept and the window is searched again with the domain batches.
Only a domain-batch answer that is still full is split in half, down to MIN_WINDOW; a
part still full there is recorded as truncated. Items go through the same normalization,
deduplication, linking and storage as feed items. GDELT's date is when its crawler saw
the article, not the publish time, so articles are stored with time precision "seen".

Requests. As of 2026-10-19 the API is overloaded: every answer, a refusal (HTTP 429,
"Please limit requests to one every 5 seconds") included, takes about 10 seconds, and it
refuses most requests intermittently from every network tested, even a first request. A
refusal means "busy, retry": the same request is answered within a few tries. So after a
refusal, a timeout, a dropped connection or a server error the request is retried
RETRY_PAUSE after the answer, never sooner than MIN_INTERVAL after the previous request.
After MAX_FAILURES failures in a row the window is skipped (GdeltGaveUp) and left for
the next run; the backfill moves on. Every request uses a new connection: GDELT closes
connections without saying so, and reusing one failed at once.

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
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import BackfillWindow, Company, Document, Feed, Filing
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
# GDELT answered "Your query was too short or too long" to a 743-character query naming
# 28 domains; queries of about 100 characters were answered. The limit is not
# documented, so domain batches keep each query this short.
MAX_QUERY_CHARS = 250
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
# Measured 2026-10-19 over nine windows: 41 answered requests took 59.7 minutes with
# refusals and pauses. Used for the estimate printed before a backfill starts.
SECONDS_PER_ANSWER = 87.0
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
        return f"{_names(company)} sourcelang:english"

    def domain_queries(self, company: UniverseCompany) -> list[str]:
        """The company's names restricted to the configured publishers, in batches that
        keep each query within MAX_QUERY_CHARS. Empty when no publishers are configured."""
        base = _names(company)
        queries: list[str] = []
        batch: list[str] = []
        for domain_name in self.domains:
            if batch and len(_domain_query(base, [*batch, domain_name])) > MAX_QUERY_CHARS:
                queries.append(_domain_query(base, batch))
                batch = []
            batch.append(domain_name)
        if batch:
            queries.append(_domain_query(base, batch))
        return queries

    def first_queries(self, company: UniverseCompany) -> list[tuple[str, bool]]:
        """The queries a window starts with, each with whether it is domain-filtered."""
        filtered = self.domain_queries(company)
        if company.gdelt_filter_publishers and filtered:
            return [(q, True) for q in filtered]
        return [(self.query(company), False)]

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
                # A new connection each time: GDELT closes idle ones without saying so,
                # and the next request on a reused one failed at once.
                response = await client.get(
                    API_URL, params=params, headers={"Connection": "close"}, timeout=TIMEOUT
                )
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


def _names(company: UniverseCompany) -> str:
    names = " OR ".join(f'"{name}"' for name in company.names)
    return f"({names})" if len(company.names) > 1 else names


def _domain_query(base: str, domains: Sequence[str]) -> str:
    if len(domains) == 1:
        return f"{base} domainis:{domains[0]} sourcelang:english"
    sites = " OR ".join(f"domainis:{d}" for d in domains)
    return f"{base} ({sites}) sourcelang:english"


def _is_refusal(body: str) -> bool:
    return body.lstrip().startswith("Please limit requests")


def _first_line(body: str) -> str:
    line = body.strip().split("\n", 1)[0]
    return line[:80] + ("..." if len(line) > 80 else "")


def clean_gdelt_title(title: str) -> str:
    """GDELT's titles have spaces around punctuation ("Buybacks . The Real Prize")."""
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", title.strip())
    return _SPACE_AFTER_OPENING.sub(r"\1", text)


def weekly_windows(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """Consecutive WINDOW-long windows from `since`, the last one cut at `until`."""
    result = []
    start = since
    while start < until:
        result.append((start, min(start + WINDOW, until)))
        start += WINDOW
    return result


async def earnings_windows(
    session: AsyncSession,
    companies: Sequence[UniverseCompany],
    *,
    days_before: int,
    days_after: int,
) -> list[tuple[UniverseCompany, datetime, datetime]]:
    """A window around each stored earnings press release (deliveries reports are not
    releases), newest first: from `days_before` days before the filing date through
    `days_after` days after it, in whole UTC days."""
    by_ticker = {c.ticker: c for c in companies}
    rows = await session.execute(
        select(Company.ticker, Filing.filing_date)
        .join(Filing, Filing.cik == Company.cik)
        .join(Document, Document.accession_no == Filing.accession_no)
        .where(Company.ticker.in_(list(by_ticker)), Document.doc_type == "press_release")
        .distinct()
    )
    found = []
    for ticker, filed in rows:
        start = _midnight(filed - timedelta(days=days_before))
        end = _midnight(filed + timedelta(days=days_after + 1))
        found.append((by_ticker[ticker], start, end))
    return sorted(found, key=lambda w: (-w[1].timestamp(), w[0].ticker))


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def polling_started(session: AsyncSession) -> datetime | None:
    """When RSS polling first succeeded: news after it comes from the feeds."""
    return await session.scalar(
        select(func.min(Feed.created_at)).where(
            Feed.type != GdeltBackfill.type, Feed.last_success_at.is_not(None)
        )
    )


@dataclass(frozen=True, slots=True)
class PlannedWindow:
    company: UniverseCompany
    feed_id: int
    start: datetime
    end: datetime


async def plan_backfill(
    session_factory: async_sessionmaker[AsyncSession],
    source: "GdeltBackfill",
    windows: Sequence[tuple[UniverseCompany, datetime, datetime]],
    *,
    not_after: datetime | None = None,
) -> list[PlannedWindow]:
    """The windows still to fetch, in the given order: a window is cut at `not_after`
    (dropped if it starts there or later), and dropped when windows already recorded
    for its company cover it."""
    planned = []
    async with session_factory() as session, session.begin():
        feeds: dict[str, tuple[int, list[tuple[datetime, datetime]]]] = {}
        for company, start, end in windows:
            if company.ticker not in feeds:
                feed_id, _ = await upsert_feed(
                    session,
                    f"{source.type}:{company.ticker}",
                    source.type,
                    API_URL,
                    company.ticker,
                )
                done = await session.execute(
                    select(BackfillWindow.window_start, BackfillWindow.window_end).where(
                        BackfillWindow.feed_id == feed_id
                    )
                )
                feeds[company.ticker] = (feed_id, sorted((s, e) for s, e in done))
            feed_id, done_windows = feeds[company.ticker]
            if not_after is not None:
                if start >= not_after:
                    continue
                end = min(end, not_after)
            if not _covered(done_windows, start, end):
                planned.append(PlannedWindow(company, feed_id, start, end))
    return planned


def estimate(source: "GdeltBackfill", planned: Sequence[PlannedWindow]) -> tuple[int, timedelta]:
    """The fewest requests the planned windows need, and how long that takes at the
    measured SECONDS_PER_ANSWER. Busy windows need more."""
    requests = sum(len(source.first_queries(w.company)) for w in planned)
    return requests, timedelta(seconds=round(requests * SECONDS_PER_ANSWER))


def _covered(done: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    reached = start
    for done_start, done_end in done:  # sorted by start
        if done_start > reached:
            break
        reached = max(reached, done_end)
        if reached >= end:
            return True
    return reached >= end


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
    windows_done: int  # of this company, in this run
    windows_total: int  # planned for this company
    new_articles: int
    requests: int
    remaining: timedelta | None  # estimate for all planned windows
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


@dataclass(slots=True)
class _CompanyRun:
    total: int
    done: int = 0
    new: int = 0
    requests: int = 0
    skipped: int = 0
    error: str | None = None


async def backfill(
    session_factory: async_sessionmaker[AsyncSession],
    source: "GdeltBackfill",
    planned: Sequence[PlannedWindow],
    matcher: CompanyMatcher,
    client: httpx.AsyncClient,
    *,
    on_progress: Callable[[BackfillProgress], None] | None = None,
) -> list[BackfillProgress]:
    """Fetch the planned windows in order, reporting progress after every window, and
    return one final result per company.

    A window that fails MAX_FAILURES times in a row is skipped and left for the next run.
    A company whose window fails with another error gets no further windows. Finished
    windows are always kept.
    """
    runs: dict[str, _CompanyRun] = {}
    for window in planned:
        runs.setdefault(window.company.ticker, _CompanyRun(0)).total += 1
    remaining = len(planned)
    started = time.monotonic()
    fetched = 0

    def progress(ticker: str) -> BackfillProgress:
        run = runs[ticker]
        pace = (time.monotonic() - started) / fetched if fetched else None
        return BackfillProgress(
            ticker,
            run.done,
            run.total,
            run.new,
            run.requests,
            None if pace is None else timedelta(seconds=round(pace * max(remaining, 0))),
            fetched=fetched,
            elapsed=timedelta(seconds=round(time.monotonic() - started)),
            error=run.error,
            skipped=run.skipped,
        )

    for window in planned:
        company = window.company
        run = runs[company.ticker]
        remaining -= 1
        if run.error is not None:
            continue
        feed = FeedRef(
            window.feed_id, f"{source.type}:{company.ticker}", Link(company.ticker, *FEED_LINK)
        )
        try:
            result = await _fetch_window(
                session_factory, source, client, company, feed, matcher, window.start, window.end
            )
        except FeedError as exc:
            if isinstance(exc, GdeltGaveUp):
                run.skipped += 1
            else:
                run.error = str(exc)
            async with session_factory() as session, session.begin():
                await record_failure(session, window.feed_id, datetime.now(UTC), str(exc))
        else:
            fetched += 1
            run.done += 1
            run.new += result.new
            run.requests += result.requests
        if on_progress is not None:
            on_progress(progress(company.ticker))

    return [replace(progress(ticker), finished=True) for ticker in runs]


async def _fetch_window(
    session_factory: async_sessionmaker[AsyncSession],
    source: "GdeltBackfill",
    client: httpx.AsyncClient,
    company: UniverseCompany,
    feed: FeedRef,
    matcher: CompanyMatcher,
    start: datetime,
    end: datetime,
) -> _WindowResult:
    """Fetch and store one window (see the module docstring for the order of queries).

    GdeltGaveUp when one request fails MAX_FAILURES times in a row.
    """
    result = _WindowResult()
    filtered_queries = source.domain_queries(company)
    pending = [(query, filtered, start, end) for query, filtered in source.first_queries(company)]
    while pending:
        query, filtered, part_start, part_end = pending.pop(0)
        items, full = await source.fetch_window(client, query, part_start, part_end, company.ticker)
        result.requests += 1
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            stored = await store_items(session, feed, items, matcher, now)
            await record_success(session, feed.id, now, new_articles=stored.new)
        result.items += stored.items
        result.new += stored.new
        if not full:
            continue
        if not filtered and filtered_queries:
            # Mostly other publishers: search the same window within the configured ones.
            pending[:0] = [(q, True, part_start, part_end) for q in filtered_queries]
        elif part_end - part_start > MIN_WINDOW:
            middle = part_start + (part_end - part_start) / 2
            pending[:0] = [
                (query, filtered, part_start, middle),
                (query, filtered, middle, part_end),
            ]
        else:
            result.truncated = True

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
