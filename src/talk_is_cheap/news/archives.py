"""Past articles of interview-heavy publishers from web archives: `talk-is-cheap ingest
archives`.

RSS only reaches back days, so the claims executives made in interviews, at conferences
and in press statements before polling began are read from archived copies of the
publishers' own pages. `archives.yaml` lists each publisher's domain, the URL formats of
its articles and how to read the date from a URL.

1. **Search.** Every Common Crawl crawl since `--since` is queried by domain and path
   prefix (status 200 and HTML, filtered on the server). The Wayback Machine CDX API,
   which also filters URLs on the server (a name and a claim word), is searched for the months
   Common Crawl has no captures for (per publisher; a month of a URL without a date is
   its capture month). Each search is recorded in `archive_searches` once its results
   are stored, so a re-run skips it.
2. **Filter**, free, before any download (`SlugFilter`): the URL must be an article of
   a configured format, its slug must name a company of universe.yaml (ticker, name or
   alias) or one of its executives, contain a claim word, and contain none of that
   company's `exclude_phrases`; URLs already stored as articles are dropped. One
   capture per URL is kept in `archive_captures`: a successful HTML capture, from
   Common Crawl when it has one, else the earliest.
3. **Download.** A Common Crawl capture is one HTTP range request for its WARC record; a
   Wayback capture is the raw page (`id_`). The text is extracted with trafilatura into
   `article_texts`, and the article is stored with its original publisher URL, the
   publication time from the page's metadata (else the URL's date), and its source as
   the feed `commoncrawl:<ticker>` or `wayback:<ticker>`, through the same
   normalization, deduplication and linking as feed items.

The text is kept for extraction and grounding only; answers quote at most 40 words and
link to the publisher's URL, never to the archive copy.
"""

import asyncio
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import ArchiveCapture, ArchiveSearch, Article
from talk_is_cheap.news.archive_index import (
    COMMON_CRAWL,
    WAYBACK,
    ArchiveError,
    Capture,
    Crawl,
    cc_captures,
    cc_pages,
    decode_page,
    page_metadata,
    read_capture,
    wayback_captures,
)
from talk_is_cheap.news.article_text import (
    PAYWALL_MARKERS,
    Fetched,
    HostRules,
    extract_text,
    fetch_article,
    retryable,
)
from talk_is_cheap.news.article_text import Candidate as ArticleCandidate
from talk_is_cheap.news.article_text import store as store_text
from talk_is_cheap.news.http_cache import PoliteFetcher, ServiceUnavailable
from talk_is_cheap.news.linking import CompanyMatcher, Link
from talk_is_cheap.news.normalize import canonical_url, url_hash
from talk_is_cheap.news.records import RawItem, TimePrecision
from talk_is_cheap.news.store import FeedRef, record_success, store_items, upsert_feed
from talk_is_cheap.universe import UniverseCompany

# Where an article's text came from: its live page, or (when the live page is gone and
# robots.txt allows the URL) the archive copy.
LIVE = "the live page"
ARCHIVE_SOURCES = {COMMON_CRAWL: "Common Crawl", WAYBACK: "the Wayback Machine"}
# Live answers after which the archive copy is read: the page is gone or has no text.
# Refusals (robots.txt, 401, 403, 451, paywalls) are never got around with a copy.
LIVE_GONE = ("HTTP 404", "HTTP 410", "no article text")
# The search hit is weak evidence on its own, like a per-company feed: the article is
# linked by its headline and description.
FEED_LINK = ("feed", 0.3)
SOURCE_URLS = {COMMON_CRAWL: "https://index.commoncrawl.org/", WAYBACK: "https://web.archive.org/"}
_PLACEHOLDER = re.compile(r"\{(year|month|m|term)\}")
_WORD_SPLIT = re.compile(r"[-_.]+")


# Configuration (archives.yaml)


class ArchivePattern(BaseModel):
    path: str
    article: str
    date: str | None = None

    @field_validator("article", "date")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    @property
    def has_term(self) -> bool:
        return "{term}" in self.path

    @property
    def has_month(self) -> bool:
        return "{month}" in self.path or "{m}" in self.path


class ArchivePublisher(BaseModel):
    key: str
    publisher: str
    domain: str
    patterns: list[ArchivePattern] = Field(min_length=1)
    # Search Common Crawl's index for this publisher's URLs (its WARC records are read
    # whenever a capture comes from it). Off where the site's robots.txt keeps CCBot out.
    common_crawl: bool = True
    # Searched and downloaded after every other publisher.
    deferred: bool = False


class ArchivesFile(BaseModel):
    claim_words: list[str] = Field(min_length=1)
    publishers: list[ArchivePublisher] = Field(min_length=1)

    def select(self, keys: Sequence[str] | None) -> list[ArchivePublisher]:
        if not keys:
            return list(self.publishers)
        known = {p.key: p for p in self.publishers}
        unknown = [k for k in keys if k not in known]
        if unknown:
            raise ValueError(f"not in archives file: {', '.join(unknown)}")
        return [known[k] for k in keys]


def load_archives_file(path: Path) -> ArchivesFile:
    with path.open(encoding="utf-8") as handle:
        return ArchivesFile.model_validate(yaml.safe_load(handle) or {})


# The slug filter


def words(text: str) -> list[str]:
    """Lower-case words of a slug or a name: "Jensen-Huang_says" -> jensen, huang, says."""
    return [w for w in _WORD_SPLIT.split(re.sub(r"[^\w.-]+", "-", text.lower())) if w]


@dataclass(frozen=True, slots=True)
class _Term:
    words: tuple[str, ...]
    ticker: str


class SlugFilter:
    """Whether a URL's slug names a company or one of its executives, together with a
    claim word, and none of the company's excluded phrases."""

    def __init__(self, companies: Sequence[UniverseCompany], claim_words: Sequence[str]) -> None:
        self.claim_words = frozenset(w.lower() for w in claim_words)
        self._terms: list[_Term] = []
        self._company_terms: set[str] = set()
        self._excluded: dict[str, list[tuple[str, ...]]] = {}
        for company in companies:
            own = [company.ticker, *company.names]
            self._company_terms.update("-".join(words(n)) for n in own if words(n))
            names = own + [n for executive in company.executives for n in executive.names]
            seen = set()
            for name in names:
                term = tuple(words(name))
                if term and term not in seen:
                    seen.add(term)
                    self._terms.append(_Term(term, company.ticker))
            self._excluded[company.ticker] = [
                tuple(words(p)) for p in company.exclude_phrases if words(p)
            ]

    @property
    def company_terms(self) -> list[str]:
        """Each company's ticker, name and aliases as a slug starts with them
        ("advanced-micro-devices"), for prefix searches."""
        return sorted(self._company_terms)

    def url_regex(self) -> str:
        """A loose server-side filter: some name and some claim word anywhere in the URL,
        in any case. `match` is the exact test."""
        names = sorted({"[-_]".join(map(re.escape, t.words)) for t in self._terms})
        claims = sorted(map(re.escape, self.claim_words))
        return f"(?i)(?=.*(?:{'|'.join(names)}))(?=.*(?:{'|'.join(claims)})).*"

    def match(self, slug: str) -> tuple[list[str], list[str]]:
        """The tickers a slug names and the claim words it holds; both empty unless it
        passes."""
        found = words(slug)
        claims = [w for w in found if w in self.claim_words]
        if not claims:
            return [], []
        present = set(found) | {w[:-1] for w in found if w.endswith("s")}
        tickers = []
        for term in self._terms:
            if term.ticker in tickers or not present.issuperset(term.words):
                continue  # a word of the name is missing: no need to look for the phrase
            if not _contains(found, term.words):
                continue
            if any(_contains(found, phrase) for phrase in self._excluded[term.ticker]):
                continue
            tickers.append(term.ticker)
        return (tickers, claims) if tickers else ([], [])


def _contains(found: Sequence[str], term: Sequence[str]) -> bool:
    """Whether `term` occurs as consecutive words; its last word may carry a possessive
    "s" ("nvidias", as slugs drop the apostrophe)."""
    size = len(term)
    for start in range(len(found) - size + 1):
        window = found[start : start + size]
        if list(window[:-1]) == list(term[:-1]) and window[-1] in (term[-1], f"{term[-1]}s"):
            return True
    return False


# URLs


@dataclass(frozen=True, slots=True)
class ArticleUrl:
    url: str  # canonical, on the publisher's own host, without query or fragment
    slug: str
    url_date: date | None


def article_url(publisher: ArchivePublisher, captured: str) -> ArticleUrl | None:
    """The publisher URL of a captured URL when it is an article of one of the
    publisher's formats, else None."""
    parts = urlsplit(captured)
    host = (parts.hostname or "").lower()
    domain = publisher.domain.lower()
    if host != domain and host.removeprefix("www.") != domain.removeprefix("www."):
        return None
    path = parts.path
    for pattern in publisher.patterns:
        if not re.search(pattern.article, path):
            continue
        segments = [s for s in path.split("/") if s]
        slug = re.sub(r"\.html?$", "", segments[-1]) if segments else ""
        return ArticleUrl(
            canonical_url(f"https://{publisher.domain}{path}"),
            slug,
            _url_date(pattern, path),
        )
    return None


def _url_date(pattern: ArchivePattern, path: str) -> date | None:
    if pattern.date is None:
        return None
    match = re.search(pattern.date, path)
    if match is None:
        return None
    groups = match.groupdict()
    try:
        return date(int(groups["year"]), int(groups["month"]), int(groups.get("day") or 1))
    except (KeyError, ValueError):
        return None


# Planning searches


@dataclass(frozen=True, slots=True)
class SearchUnit:
    publisher: ArchivePublisher
    source: str
    period: str  # crawl id, YYYY-MM, or a span of months
    prefix: str  # domain and path
    crawl: str | None = None
    since: date | None = None  # Wayback capture times
    until: date | None = None


def months(since: date, until: date) -> list[date]:
    """The first day of every month from `since` through `until`."""
    found = []
    current = date(since.year, since.month, 1)
    while current <= until:
        found.append(current)
        current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
    return found


def cc_units(publisher: ArchivePublisher, crawls: Sequence[Crawl], since: date) -> list[SearchUnit]:
    """One Common Crawl search per crawl and path prefix: each pattern's path up to its
    first placeholder other than {year}, with {year} expanded to the crawl's year and the
    year before (not before `since`): a crawl also holds articles years old, which year
    prefixes leave out, and an article no earlier crawl captured is rarely first captured
    two years on."""
    units = []
    for crawl in crawls:
        prefixes: list[str] = []
        for pattern in publisher.patterns:
            template = pattern.path[: _first_other_placeholder(pattern.path)]
            if "{year}" in template:
                years = range(max(since.year, crawl.start.year - 1), crawl.end.year + 1)
                prefixes += [template.replace("{year}", str(year)) for year in years]
            else:
                prefixes.append(template)
        units += [
            SearchUnit(
                publisher, COMMON_CRAWL, crawl.id, f"{publisher.domain}{prefix}", crawl=crawl.id
            )
            for prefix in dict.fromkeys(prefixes)
        ]
    return units


def _first_other_placeholder(path: str) -> int:
    for match in _PLACEHOLDER.finditer(path):
        if match.group(1) != "year":
            return match.start()
    return len(path)


def wayback_units(
    publisher: ArchivePublisher,
    missing: Sequence[date],
    terms: Sequence[str],
) -> list[SearchUnit]:
    """Wayback searches for the months Common Crawl has nothing for: a month's own
    prefix when the URL format has one; else one search per name ({term}), or one of
    the whole path, over the span of those months (a prefix scanned once per month
    would be scanned in full each time)."""
    if not missing:
        return []
    units = []
    first, last = missing[0], month_end(missing[-1])
    span = f"{first:%Y-%m}..{last:%Y-%m}" if (first.year, first.month) != (
        last.year, last.month
    ) else f"{first:%Y-%m}"  # fmt: skip
    for pattern in publisher.patterns:
        if pattern.has_month:
            for month in missing:
                path = (
                    pattern.path.replace("{year}", str(month.year))
                    .replace("{month}", f"{month.month:02d}")
                    .replace("{m}", str(month.month))
                )
                units += [
                    SearchUnit(publisher, WAYBACK, f"{month:%Y-%m}", publisher.domain + p)
                    for p in _expand_terms(path, terms)
                ]
        else:
            units += [
                SearchUnit(publisher, WAYBACK, span, publisher.domain + p, since=first, until=last)
                for p in _expand_terms(pattern.path, terms)
            ]
    return units


def _expand_terms(path: str, terms: Sequence[str]) -> list[str]:
    return [path.replace("{term}", t) for t in terms] if "{term}" in path else [path]


def month_end(month: date) -> date:
    following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return following - timedelta(days=1)


async def searched(session: AsyncSession, publisher: str, source: str) -> set[tuple[str, str]]:
    """The (period, prefix) searches completed for a publisher and source."""
    rows = await session.execute(
        select(ArchiveSearch.period, ArchiveSearch.prefix).where(
            ArchiveSearch.publisher == publisher,
            ArchiveSearch.source == source,
            ArchiveSearch.complete,
        )
    )
    return {(p, x) for p, x in rows}


async def covered_months(session: AsyncSession, publisher: str) -> set[date]:
    """Months Common Crawl holds articles of the publisher for, whether or not they
    passed the slug filter: by URL date, else by capture time."""
    rows = await session.scalars(
        select(ArchiveSearch.months).where(
            ArchiveSearch.publisher == publisher, ArchiveSearch.source == COMMON_CRAWL
        )
    )
    return {date.fromisoformat(f"{m}-01") for found in rows for m in found}


# Running searches


@dataclass(slots=True)
class SearchResult:
    unit: SearchUnit
    requests: int = 0
    captures: int = 0  # rows returned
    articles: int = 0  # article URLs among them
    kept: int = 0  # passed the slug filter and are not stored yet
    already_stored: int = 0
    truncated_pages: int = 0  # Common Crawl pages cut off by the index
    error: str | None = None


async def run_search(
    sessions: async_sessionmaker[AsyncSession],
    fetcher: PoliteFetcher,
    unit: SearchUnit,
    slugs: SlugFilter,
    since: date,
    *,
    on_page: Callable[[SearchUnit, int, int, "SearchResult"], None] | None = None,
) -> SearchResult:
    """Search one unit, storing the captures that pass the filter and the search's
    progress as each answer arrives: a Common Crawl search page by page (resumed at the
    first page not done), a Wayback search in one answer. `on_page` is told after each
    page: (unit, pages done, pages, result so far)."""
    result = SearchResult(unit)
    before = fetcher.requests
    async with sessions() as session, session.begin():
        done = await _start(session, unit)
    try:
        if unit.source == COMMON_CRAWL:
            assert unit.crawl is not None
            pages = await cc_pages(fetcher, unit.crawl, unit.prefix)
            for page in range(done, pages):
                answer = await cc_captures(fetcher, unit.crawl, unit.prefix, page)
                result.truncated_pages += answer.truncated
                await _store_found(sessions, unit, answer.captures, slugs, since, result,
                                   pages=pages, pages_done=page + 1)  # fmt: skip
                if on_page is not None:
                    on_page(unit, page + 1, pages, result)
            await _finish_search(sessions, unit, pages, fetcher.requests - before)
        else:
            found = await wayback_captures(
                fetcher, unit.prefix, slugs.url_regex(), unit.since, unit.until
            )
            await _store_found(sessions, unit, found, slugs, since, result, pages=1, pages_done=1)
            await _finish_search(sessions, unit, 1, fetcher.requests - before)
    except (ServiceUnavailable, ArchiveError) as exc:
        result.error = str(exc)
    result.requests = fetcher.requests - before
    return result


async def _start(session: AsyncSession, unit: SearchUnit) -> int:
    """The search's row, created incomplete if new; returns the pages already done."""
    await session.execute(
        insert(ArchiveSearch)
        .values(
            publisher=unit.publisher.key,
            source=unit.source,
            period=unit.period,
            prefix=unit.prefix,
            requests=0,
            captures=0,
            kept=0,
            months=[],
            pages_done=0,
            complete=False,
        )
        .on_conflict_do_nothing(constraint="uq_archive_searches")
    )
    row = await _row(session, unit)
    return 0 if row.complete else row.pages_done


async def _row(session: AsyncSession, unit: SearchUnit) -> ArchiveSearch:
    found = await session.scalar(
        select(ArchiveSearch).where(
            ArchiveSearch.publisher == unit.publisher.key,
            ArchiveSearch.source == unit.source,
            ArchiveSearch.period == unit.period,
            ArchiveSearch.prefix == unit.prefix,
        )
    )
    assert found is not None
    return found


async def _store_found(
    sessions: async_sessionmaker[AsyncSession],
    unit: SearchUnit,
    found: Sequence[Capture],
    slugs: SlugFilter,
    since: date,
    result: SearchResult,
    *,
    pages: int,
    pages_done: int,
) -> None:
    """Store the captures of one answer that pass the filter, and the search's progress."""
    best: dict[str, tuple[ArticleUrl, Capture, list[str]]] = {}
    found_months: set[str] = set()
    articles = 0
    for capture in found:
        if capture.status != 200 or "html" not in capture.mime:
            continue
        parsed = article_url(unit.publisher, capture.url)
        if parsed is None:
            continue
        articles += 1
        found_months.add(f"{parsed.url_date or capture.timestamp.date():%Y-%m}")
        if parsed.url_date is not None and parsed.url_date < since:
            continue
        tickers, _ = slugs.match(parsed.slug)
        if not tickers:
            continue
        current = best.get(parsed.url)
        if current is None or _better(capture, current[1]):
            best[parsed.url] = (parsed, capture, tickers)
    result.captures += len(found)
    result.articles += articles
    kept = 0
    async with sessions() as session, session.begin():
        hashes = [url_hash(u) for u in best]
        stored = (
            set(await session.scalars(select(Article.url_hash).where(Article.url_hash.in_(hashes))))
            if hashes
            else set()
        )
        for url, (parsed, capture, tickers) in best.items():
            digest = url_hash(url)
            if digest in stored:
                result.already_stored += 1
                continue
            await _upsert_capture(session, unit.publisher.key, digest, parsed, capture, tickers)
            kept += 1
        result.kept += kept
        row = await _row(session, unit)
        row.captures += len(found)
        row.kept += kept
        row.months = sorted(set(row.months) | found_months)
        row.pages = pages
        row.pages_done = pages_done


async def _finish_search(
    sessions: async_sessionmaker[AsyncSession], unit: SearchUnit, pages: int, requests: int
) -> None:
    async with sessions() as session, session.begin():
        row = await _row(session, unit)
        row.pages, row.pages_done, row.complete = pages, pages, True
        row.requests += requests


async def crawl_holds(fetcher: PoliteFetcher, unit: SearchUnit) -> bool:
    """Whether the unit's crawl holds anything of the publisher's domain: one request
    that spares the publisher's other searches of a crawl that holds none."""
    assert unit.crawl is not None
    try:
        return await cc_pages(fetcher, unit.crawl, f"{unit.publisher.domain}/") > 0
    except (ServiceUnavailable, ArchiveError):
        return True  # unknown: search as usual


async def record_empty(sessions: async_sessionmaker[AsyncSession], unit: SearchUnit) -> None:
    """Record a search whose crawl holds nothing of the publisher."""
    async with sessions() as session, session.begin():
        await session.execute(
            insert(ArchiveSearch)
            .values(
                publisher=unit.publisher.key,
                source=unit.source,
                period=unit.period,
                prefix=unit.prefix,
                requests=0,
                captures=0,
                kept=0,
                months=[],
            )
            .on_conflict_do_nothing(constraint="uq_archive_searches")
        )


def _better(new: Capture, old: Capture) -> bool:
    """Common Crawl before Wayback (a range request instead of a page), then the
    earliest capture."""
    return (new.source != COMMON_CRAWL, new.timestamp) < (old.source != COMMON_CRAWL, old.timestamp)


async def _upsert_capture(
    session: AsyncSession,
    publisher: str,
    digest: str,
    parsed: ArticleUrl,
    capture: Capture,
    tickers: Sequence[str],
) -> None:
    values = {
        "url_hash": digest,
        "url": parsed.url,
        "publisher": publisher,
        "source": capture.source,
        "captured_url": capture.url,
        "captured_at": capture.timestamp,
        "warc_filename": capture.filename,
        "warc_offset": capture.offset,
        "warc_length": capture.length,
        "url_date": parsed.url_date,
        "tickers": list(tickers),
    }
    statement = insert(ArchiveCapture).values(**values)
    # A pending capture is replaced by a better one; a downloaded one is kept.
    better = or_(
        (statement.excluded.source == COMMON_CRAWL) & (ArchiveCapture.source != COMMON_CRAWL),
        (statement.excluded.source == ArchiveCapture.source)
        & (statement.excluded.captured_at < ArchiveCapture.captured_at),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ArchiveCapture.url_hash],
            set_={k: statement.excluded[k] for k in values if k not in ("url_hash", "publisher")},
            where=(ArchiveCapture.status == "pending") & better,
        )
    )


# Counts before downloading


@dataclass(frozen=True, slots=True)
class CaptureCounts:
    by_publisher: dict[str, Counter[int]]
    by_company: dict[str, Counter[int]]
    by_status: Counter[str]


async def capture_counts(
    session: AsyncSession,
    publishers: Sequence[str],
    tickers: Sequence[str] | None,
    since: date,
) -> CaptureCounts:
    """URLs found per publisher and per company, by year (the URL's date, else the
    capture's), and per status."""
    rows = await session.execute(
        select(
            ArchiveCapture.publisher,
            ArchiveCapture.tickers,
            ArchiveCapture.url_date,
            ArchiveCapture.captured_at,
            ArchiveCapture.status,
        ).where(ArchiveCapture.publisher.in_(list(publishers)))
    )
    by_publisher: dict[str, Counter[int]] = defaultdict(Counter)
    by_company: dict[str, Counter[int]] = defaultdict(Counter)
    statuses: Counter[str] = Counter()
    wanted = set(tickers) if tickers else None
    for publisher, found, url_date, captured_at, status in rows:
        day = url_date or captured_at.date()
        if day < since:
            continue
        named = [t for t in found if wanted is None or t in wanted]
        if not named:
            continue
        by_publisher[publisher][day.year] += 1
        for ticker in named:
            by_company[ticker][day.year] += 1
        statuses[status] += 1
    return CaptureCounts(dict(by_publisher), dict(by_company), statuses)


# Downloading


@dataclass(frozen=True, slots=True)
class Download:
    capture_id: int
    publisher: str
    status: str  # stored, unavailable, skipped
    reason: str | None = None
    title: str | None = None
    url: str | None = None
    new_article: bool = False
    text_chars: int = 0
    text_source: str | None = None  # LIVE, or the archive the text came from


@dataclass(slots=True)
class DownloadSummary:
    by_publisher: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    requests: int = 0
    seconds: float = 0.0


async def pending(
    session: AsyncSession,
    publishers: Sequence[str],
    tickers: Sequence[str] | None,
    since: date,
    limit: int | None,
) -> list[ArchiveCapture]:
    """Captures still to download, oldest first."""
    when = func.coalesce(ArchiveCapture.url_date, func.date(ArchiveCapture.captured_at))
    statement = (
        select(ArchiveCapture)
        .where(
            ArchiveCapture.status == "pending",
            ArchiveCapture.publisher.in_(list(publishers)),
            when >= since,
        )
        .order_by(when, ArchiveCapture.id)
    )
    rows = list((await session.scalars(statement)).all())
    if tickers:
        wanted = set(tickers)
        rows = [r for r in rows if wanted.intersection(r.tickers)]
    return rows[:limit] if limit else rows


async def download(
    sessions: async_sessionmaker[AsyncSession],
    rules: HostRules,
    archives: dict[str, PoliteFetcher],
    captures: Sequence[ArchiveCapture],
    publishers: dict[str, ArchivePublisher],
    matcher: CompanyMatcher,
    since: date,
    *,
    on_result: Callable[[Download], None] | None = None,
) -> DownloadSummary:
    """Fetch each found article from its live publisher URL, under the article-text
    rules (`rules`: robots.txt, one request a second per host), every publisher's
    domain in parallel. The archive copy (`archives`, by the capture's source) is read
    only when the live page is gone (LIVE_GONE) and robots.txt allows the URL."""
    summary = DownloadSummary()
    started = asyncio.get_running_loop().time()
    by_domain: dict[str, list[ArchiveCapture]] = defaultdict(list)
    for capture in captures:
        by_domain[publishers[capture.publisher].domain].append(capture)
    feeds: dict[tuple[str, str], int] = {}
    lock = asyncio.Lock()

    async def feed_id(source: str, ticker: str) -> int:
        async with lock:
            if (source, ticker) not in feeds:
                async with sessions() as session, session.begin():
                    feeds[(source, ticker)], _ = await upsert_feed(
                        session, f"{source}:{ticker}", source, SOURCE_URLS[source], ticker
                    )
            return feeds[(source, ticker)]

    async def run(queue: Sequence[ArchiveCapture]) -> None:
        for capture in queue:
            result = await _download_one(
                sessions, rules, archives, capture, publishers[capture.publisher], matcher,
                since, feed_id,
            )  # fmt: skip
            summary.by_publisher[capture.publisher][result.status] += 1
            if result.text_source:
                summary.by_publisher[capture.publisher][f"text: {result.text_source}"] += 1
            if on_result is not None:
                on_result(result)

    await asyncio.gather(*(run(queue) for queue in by_domain.values()))
    summary.requests = rules.requests + sum(f.requests for f in archives.values())
    summary.seconds = asyncio.get_running_loop().time() - started
    return summary


async def _download_one(
    sessions: async_sessionmaker[AsyncSession],
    rules: HostRules,
    archives: dict[str, PoliteFetcher],
    capture: ArchiveCapture,
    publisher: ArchivePublisher,
    matcher: CompanyMatcher,
    since: date,
    feed_id: Callable[[str, str], Awaitable[int]],
) -> Download:
    live = await fetch_article(rules, ArticleCandidate(0, 0, capture.url, publisher.publisher))
    html, text, source, note = live.html, live.text, LIVE, "live page"
    if live.status != "ok":
        reason = live.reason or ""
        if retryable(reason):
            # Left pending: a later run tries the live page again.
            return Download(capture.id, publisher.key, "pending", reason[:200], url=capture.url)
        if not reason.startswith(LIVE_GONE) or await rules.allowed(capture.url) is not None:
            # A refusal (robots.txt, 403, a paywall) is never got around with a copy.
            return await _finish(sessions, capture, "unavailable", f"live page: {reason}")
        record = Capture(
            source=capture.source, url=capture.captured_url, timestamp=capture.captured_at,
            status=200, mime="text/html", filename=capture.warc_filename,
            offset=capture.warc_offset, length=capture.warc_length,
        )  # fmt: skip
        try:
            page = await read_capture(archives[capture.source], record)
        except ServiceUnavailable as exc:
            return Download(capture.id, publisher.key, "pending", str(exc)[:200], url=capture.url)
        except ArchiveError as exc:
            return await _finish(sessions, capture, "unavailable", f"live page: {reason}; {exc}")
        note = f"live page: {reason}; archive copy"
        if page.status == 200:
            copy = decode_page(page)
            copied = await asyncio.to_thread(extract_text, copy, capture.url)
            if copied is not None or html is None:
                html, text = copy, copied
                source = ARCHIVE_SOURCES[capture.source]
    if html is None:
        return await _finish(sessions, capture, "unavailable", f"{note}: no page")
    meta = page_metadata(html)
    published, precision = _publication(meta.published, meta.date_only, capture)
    if published.date() < since:
        return await _finish(sessions, capture, "skipped", f"published {published:%Y-%m-%d}")
    title = meta.title or " ".join(words(capture.url.rstrip("/").rsplit("/", 1)[-1]))
    item = RawItem(
        title=title,
        link=capture.url,
        summary=meta.description,
        published=published.isoformat(),
        publisher=publisher.publisher,
        time_precision=precision,
    )
    now = datetime.now(UTC)
    new_article = False
    for ticker in capture.tickers:
        identifier = await feed_id(capture.source, ticker)
        feed = FeedRef(identifier, f"{capture.source}:{ticker}", Link(ticker, *FEED_LINK))
        async with sessions() as session, session.begin():
            stored = await store_items(session, feed, [item], matcher, now)
            await record_success(session, identifier, now, new_articles=stored.new)
            new_article = new_article or stored.new > 0
    async with sessions() as session:
        article_id = await session.scalar(
            select(Article.id).where(Article.url_hash == url_hash(canonical_url(capture.url)))
        )
    if article_id is None:  # not a usable item (no title)
        return await _finish(sessions, capture, "unavailable", "no title")
    if text is None:
        fetched = Fetched(article_id, "unavailable", "no article text", 200)
    elif any(marker in text[-600:].lower() for marker in PAYWALL_MARKERS):
        fetched = Fetched(article_id, "unavailable", "paywall", 200)
    elif source == LIVE:
        fetched = Fetched(article_id, "ok", None, 200, text)
    else:
        stamp = f"{capture.captured_at:%Y-%m-%d %H:%M}"
        fetched = Fetched(article_id, "ok", f"text from {source} capture of {stamp}", 200, text)
    # Text already stored for the article is never overwritten by a failure.
    if fetched.status == "ok" or new_article:
        await store_text(sessions, fetched)
    status = "stored" if fetched.status == "ok" else "unavailable"
    await _finish(sessions, capture, status, f"{note}; text: {source if text else 'none'}",
                  article_id)  # fmt: skip
    return Download(
        capture.id, publisher.key, status, fetched.reason, title, capture.url, new_article,
        len(fetched.text or ""), source if fetched.status == "ok" else None,
    )  # fmt: skip


def _publication(
    published: datetime | None, date_only: bool, capture: ArchiveCapture
) -> tuple[datetime, TimePrecision]:
    """The page's own publication time, unless it is later than the capture (a
    modification time); else the URL's date; else the capture time."""
    latest = capture.captured_at + timedelta(days=1)
    if published is not None and published <= latest:
        return published, TimePrecision.DATE if date_only else TimePrecision.PUBLISHED
    if capture.url_date is not None:
        day = capture.url_date
        return datetime(day.year, day.month, day.day, tzinfo=UTC), TimePrecision.DATE
    return capture.captured_at, TimePrecision.SEEN


async def _finish(
    sessions: async_sessionmaker[AsyncSession],
    capture: ArchiveCapture,
    status: str,
    reason: str | None,
    article_id: int | None = None,
) -> Download:
    async with sessions() as session, session.begin():
        await session.execute(
            update(ArchiveCapture)
            .where(ArchiveCapture.id == capture.id)
            .values(status=status, reason=reason, article_id=article_id, fetched_at=func.now())
        )
    return Download(capture.id, capture.publisher, status, reason, url=capture.url)


def years_of(counts: Iterable[Counter[int]]) -> list[int]:
    return sorted({year for counter in counts for year in counter})
