"""Article text for statement extraction: `talk-is-cheap ingest article-text`.

Feeds give headlines and short summaries; executives' quotes are in the article body. This
fetches the body of one article per dedup cluster linked to a company (confidence 0.5 or
more), and stores it in `article_texts` for extraction and grounding only. A Google News
article whose publisher URL was found (`news/finnhub.py`) is fetched from that URL.
Archived articles (`news/archives.py`) arrive with their text already stored.

Rules for every request:

- robots.txt of the host is read first and obeyed for the first word of
  ARTICLE_USER_AGENT; it is kept on disk for a day (`ROBOTS_TTL`), so a URL robots.txt
  refuses is skipped without any request. A host whose robots.txt cannot be read is not
  fetched. Redirects are followed one hop at a time, each checked against its own host's
  robots.txt. A Finnhub link is resolved first (one request to finnhub.io) and the
  publisher URL stored on the article (`publisher_url`), so the publisher's own rules,
  pacing and queue apply.
- At most one request per second per host (`MIN_INTERVAL`, or the host's Crawl-delay
  when longer; `--slow`: SLOW_INTERVAL), at most HOSTS_AT_ONCE requests at a time, a
  15-second timeout, and a descriptive User-Agent.
- No retries within a run. 401, 402, 403, 404, 410 and 451, robots.txt refusals,
  paywalls (Seeking Alpha's 403 is its paywall) and pages without article text are
  recorded as `unavailable` for good. Timeouts, connection errors, server errors and
  interstitials (a consent page, or HTTP 202 with an empty body: a bot check) are tried
  again by the next run. A host that answers PAUSE_AFTER interstitials in a row is paused
  for the rest of the run, its other articles left for a later, slower run, while the
  other hosts continue.

Runs are incremental and resumable: each result is stored as soon as it arrives, a
cluster with text is skipped, and a cluster whose best article is unavailable moves on to
its next article.
"""

import asyncio
import hashlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Article, ArticleCompany, ArticleText

MIN_CONFIDENCE = 0.5
MIN_INTERVAL = 1.0  # seconds between two requests to one host
TIMEOUT = 15.0
MAX_REDIRECTS = 5
HOSTS_AT_ONCE = 8
# Extracted text shorter than this is a teaser, a cookie wall or a paywall, not an article.
MIN_TEXT_CHARS = 400
NEVER_RETRY_STATUS = frozenset({401, 402, 403, 404, 410, 451})
PAYWALL_MARKERS = (
    "subscribe to continue reading",
    "subscribe to read",
    "to continue reading, subscribe",
    "this article is for subscribers",
    "already a subscriber",
    "create a free account to continue",
)
# Reasons that a later run tries again; every other unavailable article is final.
RETRYABLE_REASONS = (
    "timeout",
    "connection error",
    "server error",
    "robots.txt unreadable",
    "interstitial",
)
# robots.txt answers are kept on disk this long.
ROBOTS_TTL = timedelta(days=1)
# `--slow`: seconds between two requests to one host, for hosts paused by interstitials.
SLOW_INTERVAL = 10.0
# Interstitials in a row after which a host is paused for the rest of the run.
PAUSE_AFTER = 3
# Hosts whose HTTP 403 is a paywall for subscribers, not a refusal of the request.
PAYWALL_403_HOSTS = frozenset({"seekingalpha.com"})
# Hosts of interstitial consent pages a redirect can lead to.
CONSENT_HOST_PREFIXES = ("consent.", "guce.")
# Links that redirect to the publisher (`news/finnhub.py`).
RESOLVED_HOSTS = frozenset({"finnhub.io"})


def retryable(reason: str | None) -> bool:
    return reason is not None and reason.startswith(RETRYABLE_REASONS)


@dataclass(frozen=True, slots=True)
class Candidate:
    """The article of a cluster to fetch next."""

    article_id: int
    cluster_id: int
    url: str
    publisher: str


@dataclass(frozen=True, slots=True)
class Fetched:
    article_id: int
    status: str  # ok, unavailable, or paused (not stored: its host is paused)
    reason: str | None = None
    http_status: int | None = None
    text: str | None = None
    # The page as fetched, for callers that also read its metadata; never stored.
    html: str | None = None


@dataclass(slots=True)
class PublisherCount:
    fetched: int = 0
    unavailable: int = 0
    skipped: int = 0
    robots: int = 0  # refused by robots.txt, without a request for the page
    paused: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class FetchSummary:
    clusters: int = 0
    by_publisher: dict[str, PublisherCount] = field(
        default_factory=lambda: defaultdict(PublisherCount)
    )
    paused_hosts: set[str] = field(default_factory=set)
    requests: int = 0
    seconds: float = 0.0

    def total(self, name: str) -> int:
        return sum(getattr(c, name) for c in self.by_publisher.values())

    @property
    def success_rate(self) -> float | None:
        """Fetched, of the articles requested: robots.txt refusals (never requested) and
        paused hosts are left out."""
        tried = self.total("fetched") + self.total("unavailable") - self.total("robots")
        return self.total("fetched") / tried if tried else None


async def plan(
    session: AsyncSession,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    recovered_only: bool = False,
) -> tuple[list[Candidate], dict[str, int], int]:
    """The articles to fetch, one per linked cluster without text: the cluster's best
    article not yet found unavailable for good. An article's link is its publisher URL
    when one was found for it (`news/finnhub.py`), else its own. Best is a direct link
    (Google News links are redirects its robots.txt forbids following), then the
    longest summary, then the earliest. Returns the candidates (newest first, at most
    `limit`), the clusters skipped per publisher (text already stored, or no article
    left to try), and the number of linked clusters."""
    cluster = func.coalesce(Article.cluster_id, Article.id)
    linked = select(ArticleCompany.article_id).where(ArticleCompany.confidence >= MIN_CONFIDENCE)
    if tickers:
        linked = linked.where(ArticleCompany.ticker.in_([t.upper() for t in tickers]))
    clusters = select(cluster.label("cluster")).where(Article.id.in_(linked)).distinct()
    if recovered_only:
        clusters = clusters.where(Article.publisher_url.is_not(None))
    rows = (
        await session.execute(
            select(
                cluster.label("cluster"),
                Article.id,
                func.coalesce(Article.publisher_url, Article.url),
                Article.publisher,
                Article.summary,
                Article.published_at,
                ArticleText.status,
                ArticleText.reason,
            )
            .outerjoin(ArticleText, ArticleText.article_id == Article.id)
            .where(cluster.in_(clusters))
        )
    ).all()
    by_cluster: dict[int, list[_Option]] = defaultdict(list)
    for row in rows:
        by_cluster[int(row[0])].append(_Option(*row[1:]))
    candidates: list[tuple[datetime, Candidate]] = []
    skipped: Counter[str] = Counter()
    for cluster_id, articles in by_cluster.items():
        newest = max(a.published_at for a in articles)
        open_ = [a for a in articles if a.status is None or retryable(a.reason)]
        if any(a.status == "ok" for a in articles) or not open_:
            skipped[articles[0].publisher_name] += 1
            continue
        best = min(
            open_,
            key=lambda a: (_is_google_redirect(a.url), -len(a.summary or ""), a.published_at),
        )
        candidates.append((newest, Candidate(best.id, cluster_id, best.url, best.publisher_name)))
    ordered = [c for _, c in sorted(candidates, key=lambda pair: pair[0], reverse=True)]
    return (ordered[:limit] if limit else ordered), dict(skipped), len(by_cluster)


@dataclass(frozen=True, slots=True)
class _Option:
    """An article of a cluster, and what is stored of its text."""

    id: int
    url: str
    publisher: str | None
    summary: str | None
    published_at: datetime
    status: str | None
    reason: str | None

    @property
    def publisher_name(self) -> str:
        return self.publisher or urlsplit(self.url).hostname or "unknown"


def _is_google_redirect(url: str) -> bool:
    return urlsplit(url).hostname == "news.google.com"


class HostRules:
    """robots.txt and pacing per host, shared by every request of a run."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        agent: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
        *,
        interval: float = MIN_INTERVAL,
        robots_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._agent = agent
        self._clock = clock
        self._sleep = sleep
        self._min_interval = interval
        self._robots_dir = robots_dir
        self._robots: dict[str, RobotFileParser | str] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.requests = 0

    async def get(self, url: str) -> httpx.Response:
        """One request, after waiting for the host's turn."""
        host = urlsplit(url).netloc.lower()
        async with self._locks[host]:
            interval = self._interval(host)
            last = self._last.get(host)
            if last is not None:
                wait = last + interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)  # type: ignore[misc]
            try:
                self.requests += 1
                return await self._client.get(url, follow_redirects=False, timeout=TIMEOUT)
            finally:
                self._last[host] = self._clock()

    def _interval(self, host: str) -> float:
        robots = self._robots.get(host)
        delay = robots.crawl_delay(self._agent) if isinstance(robots, RobotFileParser) else None
        return max(self._min_interval, float(delay or 0))

    async def allowed(self, url: str) -> str | None:
        """None when robots.txt allows the URL, else why not."""
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host not in self._robots:
            self._robots[host] = self._cached_robots(host) or await self._read_robots(
                f"{parts.scheme}://{parts.netloc}"
            )
        robots = self._robots[host]
        if isinstance(robots, str):
            return robots
        if not robots.can_fetch(self._agent, url):
            return "robots.txt disallows"
        return None

    def _robots_path(self, host: str) -> Path | None:
        return None if self._robots_dir is None else self._robots_dir / f"{host}.json"

    def _cached_robots(self, host: str) -> RobotFileParser | str | None:
        """The robots.txt read within ROBOTS_TTL, from disk."""
        path = self._robots_path(host)
        if path is None or not path.exists():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        if datetime.now(UTC) - datetime.fromisoformat(stored["read_at"]) > ROBOTS_TTL:
            return None
        if stored.get("refusal"):
            return str(stored["refusal"])
        parser = RobotFileParser()
        parser.parse(stored["lines"])
        return parser

    async def _read_robots(self, origin: str) -> RobotFileParser | str:
        host = urlsplit(origin).netloc.lower()
        found = await self._fetch_robots(origin)
        path = self._robots_path(host)
        lines = getattr(found, "_lines", None)
        if path is not None and not (isinstance(found, str) and retryable(found)):
            path.parent.mkdir(parents=True, exist_ok=True)
            stored = {
                "read_at": datetime.now(UTC).isoformat(),
                "refusal": found if isinstance(found, str) else None,
                "lines": lines or [],
            }
            path.write_text(json.dumps(stored), encoding="utf-8")
        return found

    async def _fetch_robots(self, origin: str) -> RobotFileParser | str:
        parser = RobotFileParser()
        try:
            response = await self.get(f"{origin}/robots.txt")
            for _ in range(MAX_REDIRECTS):
                location = response.headers.get("location")
                if not response.is_redirect or not location:
                    break
                response = await self.get(urljoin(str(response.url), location))
        except httpx.HTTPError as exc:
            return f"robots.txt unreadable ({type(exc).__name__})"
        if response.status_code in (401, 403):
            return f"robots.txt disallows (HTTP {response.status_code} for robots.txt)"
        if response.status_code >= 500:
            return f"robots.txt unreadable (HTTP {response.status_code})"
        lines = [] if response.status_code >= 400 else response.text.splitlines()
        parser.parse(lines)  # no robots.txt (4xx): everything is allowed
        parser._lines = lines  # type: ignore[attr-defined]  # kept for the disk cache
        return parser


async def fetch_article(rules: HostRules, candidate: Candidate) -> Fetched:
    """Fetch one article page and extract its text, or say why it is unavailable."""
    url = candidate.url
    for _ in range(MAX_REDIRECTS + 1):
        refused = await rules.allowed(url)
        if refused:
            return Fetched(candidate.article_id, "unavailable", f"{refused}: {_host(url)}")
        try:
            response = await rules.get(url)
        except httpx.TimeoutException:
            return Fetched(candidate.article_id, "unavailable", "timeout")
        except httpx.HTTPError as exc:
            return Fetched(candidate.article_id, "unavailable", f"connection error: {exc}"[:200])
        location = response.headers.get("location")
        if response.is_redirect and location:
            url = urljoin(url, location)
            continue
        break
    else:
        return Fetched(candidate.article_id, "unavailable", "too many redirects")
    status = response.status_code
    host = _host(url).removeprefix("www.")
    if host.startswith(CONSENT_HOST_PREFIXES):
        return Fetched(candidate.article_id, "unavailable", f"interstitial: consent page {host}")
    if status == 202 and len(response.content.strip()) < MIN_TEXT_CHARS:
        return Fetched(
            candidate.article_id, "unavailable", "interstitial: HTTP 202, empty page", status
        )
    if status == 403 and host in PAYWALL_403_HOSTS:
        return Fetched(candidate.article_id, "unavailable", f"paywall: HTTP 403 ({host})", status)
    if status in NEVER_RETRY_STATUS:
        return Fetched(candidate.article_id, "unavailable", f"HTTP {status}", status)
    if status >= 500 or status == 429:
        return Fetched(candidate.article_id, "unavailable", f"server error: HTTP {status}", status)
    if status >= 300:
        return Fetched(candidate.article_id, "unavailable", f"HTTP {status}", status)
    page = response.text
    text = await asyncio.to_thread(extract_text, page, url)
    if text is None:
        return Fetched(candidate.article_id, "unavailable", "no article text", status, html=page)
    if any(marker in text[-600:].lower() for marker in PAYWALL_MARKERS):
        return Fetched(candidate.article_id, "unavailable", "paywall", status)
    return Fetched(candidate.article_id, "ok", None, status, text, html=page)


def extract_text(html: str, url: str) -> str | None:
    """The article body of a page as plain paragraphs, or None when there is no article
    (too short to be one)."""
    import trafilatura

    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        deduplicate=True,
    )
    if not text or len(text) < MIN_TEXT_CHARS:
        return None
    return text.strip()


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


async def store(sessions: async_sessionmaker[AsyncSession], fetched: Fetched) -> None:
    values = {
        "article_id": fetched.article_id,
        "status": fetched.status,
        "reason": fetched.reason,
        "http_status": fetched.http_status,
        "text": fetched.text,
        "content_hash": hashlib.sha256(fetched.text.encode()).hexdigest() if fetched.text else None,
        "fetched_at": func.now(),
    }
    statement = insert(ArticleText).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[ArticleText.article_id],
        set_={k: statement.excluded[k] for k in values if k != "article_id"},
    )
    async with sessions() as session, session.begin():
        await session.execute(statement)


async def resolve_link(rules: HostRules, url: str) -> str | None:
    """Where a redirecting link (a Finnhub link) leads, by one request that does not
    follow it; None when it leads nowhere."""
    if await rules.allowed(url):
        return None
    try:
        response = await rules.get(url)
    except httpx.HTTPError:
        return None
    location = str(response.headers.get("location") or "")
    if not response.is_redirect or not location:
        return None
    return urljoin(url, location)


async def store_publisher_url(
    sessions: async_sessionmaker[AsyncSession], article_id: int, url: str
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            update(Article)
            .where(Article.id == article_id)
            .values(publisher_url=url, publisher_url_source="finnhub")
        )


async def fetch_texts(
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    agent: str,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    recovered_only: bool = False,
    selected: Sequence[Candidate] | None = None,
    interval: float = MIN_INTERVAL,
    robots_dir: Path | None = None,
    on_result: Callable[[Candidate, Fetched], None] | None = None,
) -> FetchSummary:
    """Fetch the planned articles (or the `selected` ones, in their order): one queue per
    host, hosts in parallel, at most HOSTS_AT_ONCE requests at a time, every result
    stored as it arrives. Finnhub links are resolved first (their queue is finnhub.io)
    and each article then joins its publisher's queue. A host that answers PAUSE_AFTER
    interstitials in a row is paused: its remaining articles are left for a later run."""
    started = time.monotonic()
    skipped: dict[str, int] = {}
    if selected is not None:
        candidates, clusters = list(selected), len(selected)
    else:
        async with sessions() as session:
            candidates, skipped, clusters = await plan(session, tickers, limit, recovered_only)
    summary = FetchSummary(clusters=clusters)
    for publisher, count in skipped.items():
        summary.by_publisher[publisher].skipped += count
    rules = HostRules(client, agent, interval=interval, robots_dir=robots_dir)
    gate = asyncio.Semaphore(HOSTS_AT_ONCE)
    queues: dict[str, asyncio.Queue[Candidate | None]] = {}
    workers: list[asyncio.Task[None]] = []
    strikes: Counter[str] = Counter()

    def dispatch(candidate: Candidate) -> None:
        host = _host(candidate.url).removeprefix("www.")
        if host not in queues:
            queues[host] = asyncio.Queue()
            workers.append(asyncio.create_task(work(host)))
        queues[host].put_nowait(candidate)

    async def record(candidate: Candidate, fetched: Fetched, host: str) -> None:
        count = summary.by_publisher[candidate.publisher]
        if fetched.status == "paused":
            count.paused += 1
        else:
            await store(sessions, fetched)
            if fetched.status == "ok":
                count.fetched += 1
            else:
                count.unavailable += 1
                count.reasons[_reason_kind(fetched.reason)] += 1
                if (fetched.reason or "").startswith("robots.txt"):
                    count.robots += 1
            if (fetched.reason or "").startswith("interstitial"):
                strikes[host] += 1
                if strikes[host] >= PAUSE_AFTER:
                    summary.paused_hosts.add(host)
            else:
                strikes[host] = 0
        if on_result is not None:
            on_result(candidate, fetched)

    async def work(host: str) -> None:
        queue = queues[host]
        while (candidate := await queue.get()) is not None:
            if host in summary.paused_hosts:
                await record(candidate, Fetched(candidate.article_id, "paused", host), host)
                continue
            async with gate:
                fetched = await fetch_article(rules, candidate)
            await record(candidate, fetched, host)

    async def resolve(links: list[Candidate]) -> None:
        for candidate in links:
            async with gate:
                url = await resolve_link(rules, candidate.url)
            if url is not None:
                await store_publisher_url(sessions, candidate.article_id, url)
                candidate = Candidate(
                    candidate.article_id, candidate.cluster_id, url, candidate.publisher
                )
            dispatch(candidate)

    links = [c for c in candidates if _host(c.url) in RESOLVED_HOSTS]
    for candidate in candidates:
        if _host(candidate.url) not in RESOLVED_HOSTS:
            dispatch(candidate)
    await resolve(links)
    for queue in queues.values():
        queue.put_nowait(None)
    await asyncio.gather(*workers)
    summary.requests = rules.requests
    summary.seconds = time.monotonic() - started
    return summary


def _reason_kind(reason: str | None) -> str:
    """A reason without its details: "robots.txt disallows", "HTTP 403", "timeout"."""
    return (reason or "unknown").split(":")[0].split(" (")[0]
