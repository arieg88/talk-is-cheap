"""Article text for statement extraction: `talk-is-cheap ingest article-text`.

Feeds give headlines and short summaries; executives' quotes are in the article body. This
fetches the body of one article per dedup cluster linked to a company (confidence 0.5 or
more), and stores it in `article_texts` for extraction and grounding only.

Rules for every request:

- robots.txt of the host is read first (once per run) and obeyed for the first word of
  ARTICLE_USER_AGENT; a host whose robots.txt cannot be read is not fetched. Redirects
  are followed one hop at a time, each checked against its own host's robots.txt.
- At most one request per second per host (`MIN_INTERVAL`, or the host's Crawl-delay
  when longer), a 15-second timeout, and a descriptive User-Agent.
- No retries within a run. 401, 402, 403, 404, 410 and 451, robots.txt refusals and pages
  without article text (paywalls, script-rendered pages) are recorded as `unavailable`
  for good; timeouts, connection errors and server errors are recorded as `unavailable`
  and tried again by the next run.

Runs are incremental and resumable: each result is stored as soon as it arrives, a
cluster with text is skipped, and a cluster whose best article is unavailable moves on to
its next article.
"""

import asyncio
import hashlib
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import func, select
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
RETRYABLE_REASONS = ("timeout", "connection error", "server error", "robots.txt unreadable")


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
    status: str  # ok or unavailable
    reason: str | None = None
    http_status: int | None = None
    text: str | None = None


@dataclass(slots=True)
class PublisherCount:
    fetched: int = 0
    unavailable: int = 0
    skipped: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class FetchSummary:
    clusters: int = 0
    by_publisher: dict[str, PublisherCount] = field(
        default_factory=lambda: defaultdict(PublisherCount)
    )
    requests: int = 0
    seconds: float = 0.0

    def total(self, name: str) -> int:
        return sum(getattr(c, name) for c in self.by_publisher.values())


async def plan(
    session: AsyncSession, tickers: Sequence[str] | None = None, limit: int | None = None
) -> tuple[list[Candidate], dict[str, int], int]:
    """The articles to fetch, one per linked cluster without text: the cluster's best
    article not yet found unavailable for good. Best is a direct link (Google News links
    are redirects its robots.txt forbids following), then the longest summary, then the
    earliest. Returns the candidates (newest first, at most `limit`), the clusters skipped
    per publisher (text already stored, or no article left to try), and the number of
    linked clusters."""
    cluster = func.coalesce(Article.cluster_id, Article.id)
    linked = select(ArticleCompany.article_id).where(ArticleCompany.confidence >= MIN_CONFIDENCE)
    if tickers:
        linked = linked.where(ArticleCompany.ticker.in_([t.upper() for t in tickers]))
    clusters = select(cluster.label("cluster")).where(Article.id.in_(linked)).distinct()
    rows = (
        await session.execute(
            select(
                cluster.label("cluster"),
                Article.id,
                Article.url,
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
    ) -> None:
        self._client = client
        self._agent = agent
        self._clock = clock
        self._sleep = sleep
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
        return max(MIN_INTERVAL, float(delay or 0))

    async def allowed(self, url: str) -> str | None:
        """None when robots.txt allows the URL, else why not."""
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host not in self._robots:
            self._robots[host] = await self._read_robots(f"{parts.scheme}://{parts.netloc}")
        robots = self._robots[host]
        if isinstance(robots, str):
            return robots
        if not robots.can_fetch(self._agent, url):
            return "robots.txt disallows"
        return None

    async def _read_robots(self, origin: str) -> RobotFileParser | str:
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
        if response.status_code >= 400:
            parser.parse([])  # no robots.txt: everything is allowed
        else:
            parser.parse(response.text.splitlines())
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
    if status in NEVER_RETRY_STATUS:
        return Fetched(candidate.article_id, "unavailable", f"HTTP {status}", status)
    if status >= 500 or status == 429:
        return Fetched(candidate.article_id, "unavailable", f"server error: HTTP {status}", status)
    if status >= 300:
        return Fetched(candidate.article_id, "unavailable", f"HTTP {status}", status)
    text = await asyncio.to_thread(extract_text, response.text, url)
    if text is None:
        return Fetched(candidate.article_id, "unavailable", "no article text", status)
    if any(marker in text[-600:].lower() for marker in PAYWALL_MARKERS):
        return Fetched(candidate.article_id, "unavailable", "paywall", status)
    return Fetched(candidate.article_id, "ok", None, status, text)


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


async def fetch_texts(
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    agent: str,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    on_result: Callable[[Candidate, Fetched], None] | None = None,
) -> FetchSummary:
    """Fetch the planned articles, hosts in parallel and each host in turn, storing every
    result as it arrives."""
    started = time.monotonic()
    async with sessions() as session:
        candidates, skipped, clusters = await plan(session, tickers, limit)
    summary = FetchSummary(clusters=clusters)
    for publisher, count in skipped.items():
        summary.by_publisher[publisher].skipped += count
    rules = HostRules(client, agent)
    by_host: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_host[urlsplit(candidate.url).netloc.lower()].append(candidate)
    hosts = asyncio.Semaphore(HOSTS_AT_ONCE)

    async def run_host(queue: list[Candidate]) -> None:
        async with hosts:
            for candidate in queue:
                fetched = await fetch_article(rules, candidate)
                await store(sessions, fetched)
                count = summary.by_publisher[candidate.publisher]
                if fetched.status == "ok":
                    count.fetched += 1
                else:
                    count.unavailable += 1
                    count.reasons[_reason_kind(fetched.reason)] += 1
                if on_result is not None:
                    on_result(candidate, fetched)

    await asyncio.gather(*(run_host(queue) for queue in by_host.values()))
    summary.requests = rules.requests
    summary.seconds = time.monotonic() - started
    return summary


def _reason_kind(reason: str | None) -> str:
    """A reason without its details: "robots.txt disallows", "HTTP 403", "timeout"."""
    return (reason or "unknown").split(":")[0].split(" (")[0]
