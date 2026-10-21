"""Finnhub company news: `talk-is-cheap ingest finnhub`.

Two uses of one API (`finnhub.io/api/v1/company-news`, a free key in FINNHUB_API_KEY;
the free tier is licensed for personal use):

1. **Recovering Google News stories.** A story whose only links are Google News
   redirects cannot be read (Google's robots.txt forbids following them). Each such
   story is matched to a Finnhub item of the same company by headline similarity
   (`dedup.similarity` of `dedup.headline_key`, 0-100) and date (within
   DATE_TOLERANCE). The best match at or above MATCH_THRESHOLD has its Finnhub link
   resolved (one request, the redirect not followed) and the publisher URL is stored on
   the Google News article with the match score, so `ingest article-text` fetches the
   publisher's page under its usual rules.
2. **New stories.** Items that match no stored story and whose headline names a
   company of the universe (Finnhub's per-ticker lists carry many stories about other
   companies, and a company named only in the summary was, in a hand check, mostly a
   market roundup mentioning it) are stored through the usual normalization, deduplication and
   linking, as the feed `finnhub:<ticker>`, under their Finnhub link, which
   `ingest article-text` follows to the publisher.
3. **Choosing what to read.** Most new stories are market coverage. `ingest
   article-text --finnhub` fetches the text only of those whose headline or summary
   names the linked company or one of its executives with an attribution or claim word
   (`text_candidates`, the archives' slug filter applied to the feed text), oldest
   first.

Requests. An answer holds at most MAX_ITEMS items, newest first, so a window that comes
back full is split in half, down to MIN_WINDOW; a day still full is recorded as
truncated. The free tier allows 60 requests a minute (MIN_INTERVAL apart); a 429 is
retried with backoff. How far back the free tier returns news is measured at the start
of each run (`history_depth`). Windows are recorded in `backfill_windows` once stored,
and answers cached, except windows ending within SETTLE_TIME, which are still filling.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    ArticleFeed,
    ArticleText,
    BackfillWindow,
    Feed,
)
from talk_is_cheap.news import article_text
from talk_is_cheap.news.archives import SlugFilter
from talk_is_cheap.news.dedup import headline_key, similarity
from talk_is_cheap.news.http_cache import PoliteFetcher
from talk_is_cheap.news.linking import CompanyMatcher, Link
from talk_is_cheap.news.records import RawItem
from talk_is_cheap.news.store import FeedRef, record_success, store_items, upsert_feed
from talk_is_cheap.universe import UniverseCompany

API_URL = "https://finnhub.io/api/v1/company-news"
SERVICE = "finnhub"
MAX_ITEMS = 250
MIN_INTERVAL = 1.1  # seconds: the free tier allows 60 requests a minute
WINDOW = timedelta(days=7)
MIN_WINDOW = timedelta(days=1)
SETTLE_TIME = timedelta(days=2)
DATE_TOLERANCE = timedelta(days=2)
# Checked by hand on 20 pairs from the development database, four per band (docs/news.md):
# all 16 scoring 80 or more were the same story, 2 of the 4 between 70 and 80 were not.
MATCH_THRESHOLD = 85.0
# A link only from the per-ticker list is weak evidence, like a Yahoo Finance feed.
FEED_LINK = ("feed", 0.3)
# A new item is stored when its headline names a company this surely (a name, alias or
# ticker in the headline; not "ex-Microsoft").
MIN_HEADLINE_CONFIDENCE = 0.8
GOOGLE_HOST = "news.google.com"


class FinnhubError(Exception):
    """Finnhub answered something unusable (a refused key, an error message)."""


@dataclass(frozen=True, slots=True)
class FinnhubItem:
    id: int
    ticker: str  # the symbol it was listed under
    headline: str
    summary: str
    source: str
    url: str  # a finnhub.io redirect to the publisher
    published: datetime


async def fetch_window(
    fetcher: PoliteFetcher, key: str, ticker: str, start: date, end: date, today: date
) -> list[FinnhubItem]:
    """Items listed for `ticker` published from `start` through `end` (whole days)."""
    response = await fetcher.get(
        API_URL,
        params={"symbol": ticker, "from": start.isoformat(), "to": end.isoformat()},
        headers={"X-Finnhub-Token": key},
        cache=end < today - SETTLE_TIME,
    )
    if response.status in (401, 403):
        raise FinnhubError(f"Finnhub refused the key: HTTP {response.status}")
    if response.status != 200:
        raise FinnhubError(f"HTTP {response.status}: {response.text[:200]}")
    data = response.json()
    if not isinstance(data, list):
        raise FinnhubError(str(data)[:200])
    items = []
    for row in data:
        try:
            items.append(
                FinnhubItem(
                    id=int(row["id"]),
                    ticker=ticker,
                    headline=str(row.get("headline") or "").strip(),
                    summary=str(row.get("summary") or "").strip(),
                    source=str(row.get("source") or "").strip(),
                    url=str(row["url"]),
                    published=datetime.fromtimestamp(int(row["datetime"]), tz=UTC),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return [i for i in items if i.headline and i.url.startswith("http")]


async def history_depth(fetcher: PoliteFetcher, key: str, ticker: str, today: date) -> date:
    """The earliest day the free tier returns news for, found by halving: a three-day
    window there holds items, one a day earlier holds none. Not cached: it moves."""

    async def has_news(day: date) -> bool:
        response = await fetcher.get(
            API_URL,
            params={
                "symbol": ticker,
                "from": day.isoformat(),
                "to": (day + timedelta(days=2)).isoformat(),
            },
            headers={"X-Finnhub-Token": key},
            cache=False,
        )
        if response.status != 200:
            raise FinnhubError(f"HTTP {response.status}: {response.text[:200]}")
        data = response.json()
        return isinstance(data, list) and bool(data)

    newer, older = 0, 3 * 365  # days back: news found / none found
    if await has_news(today - timedelta(days=older)):
        return today - timedelta(days=older)
    while older - newer > 1:
        middle = (newer + older) // 2
        if await has_news(today - timedelta(days=middle)):
            newer = middle
        else:
            older = middle
    return today - timedelta(days=newer)


@dataclass(slots=True)
class WindowResult:
    start: date
    end: date
    requests: int = 0
    items: list[FinnhubItem] = field(default_factory=list)
    truncated: bool = False


async def fetch_range(
    fetcher: PoliteFetcher, key: str, ticker: str, start: date, end: date, today: date
) -> WindowResult:
    """Every item listed for `ticker` from `start` through `end`, splitting full
    answers in half down to MIN_WINDOW."""
    result = WindowResult(start, end)
    pending = [(start, end)]
    seen: set[int] = set()
    while pending:
        part_start, part_end = pending.pop(0)
        items = await fetch_window(fetcher, key, ticker, part_start, part_end, today)
        result.requests += 1
        days = (part_end - part_start).days + 1
        if len(items) >= MAX_ITEMS and days > MIN_WINDOW.days:
            middle = part_start + timedelta(days=days // 2 - 1)
            pending[:0] = [(part_start, middle), (middle + timedelta(days=1), part_end)]
            continue
        if len(items) >= MAX_ITEMS:
            result.truncated = True
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                result.items.append(item)
    return result


def windows(start: date, end: date) -> list[tuple[date, date]]:
    """WINDOW-long windows of whole days from `start` through `end`."""
    found = []
    current = start
    while current <= end:
        last = min(current + WINDOW - timedelta(days=1), end)
        found.append((current, last))
        current = last + timedelta(days=1)
    return found


async def fetched_windows(session: AsyncSession, feed_id: int) -> set[date]:
    rows = await session.scalars(
        select(BackfillWindow.window_start).where(BackfillWindow.feed_id == feed_id)
    )
    return {r.date() for r in rows}


# Matching Google News stories


@dataclass(frozen=True, slots=True)
class GoogleStory:
    article_id: int
    cluster_id: int
    ticker: str
    title: str
    publisher: str | None
    published_at: datetime


@dataclass(frozen=True, slots=True)
class Match:
    story: GoogleStory
    item: FinnhubItem
    score: float


async def google_stories(session: AsyncSession, tickers: Sequence[str]) -> list[GoogleStory]:
    """Stories linked to a company (confidence 0.5 or more) whose every article is a
    Google News link without a publisher URL: one article each, the earliest."""
    cluster = func.coalesce(Article.cluster_id, Article.id)
    linked = (
        await session.execute(
            select(ArticleCompany.ticker, cluster)
            .join(Article, Article.id == ArticleCompany.article_id)
            .where(ArticleCompany.confidence >= 0.5, ArticleCompany.ticker.in_(list(tickers)))
            .distinct()
        )
    ).all()
    clusters = {int(c) for _, c in linked}
    rows = (
        await session.execute(
            select(
                cluster,
                Article.id,
                Article.url,
                Article.title,
                Article.publisher,
                Article.published_at,
                Article.publisher_url,
            ).where(cluster.in_(clusters))
        )
    ).all()
    by_cluster: dict[int, list[tuple[int, str, str, str | None, datetime, str | None]]] = {}
    for cluster_id, *rest in rows:
        by_cluster.setdefault(int(cluster_id), []).append(tuple(rest))  # type: ignore[arg-type]
    found = []
    for ticker, cluster_id in linked:
        members = by_cluster.get(int(cluster_id), [])
        if not members or any(
            urlsplit(url).hostname != GOOGLE_HOST or publisher_url
            for _, url, _, _, _, publisher_url in members
        ):
            continue
        article_id, _, title, publisher, published_at, _ = min(members, key=lambda m: m[4])
        found.append(
            GoogleStory(article_id, int(cluster_id), ticker, title, publisher, published_at)
        )
    return found


def match_stories(stories: Sequence[GoogleStory], items: Sequence[FinnhubItem]) -> list[Match]:
    """The best-scoring item of the same company within DATE_TOLERANCE, for every story
    (any score; `MATCH_THRESHOLD` decides which are used)."""
    by_ticker: dict[str, list[tuple[FinnhubItem, str]]] = {}
    for item in items:
        by_ticker.setdefault(item.ticker, []).append((item, headline_key(item.headline)))
    matches = []
    for story in stories:
        key = headline_key(story.title)
        best: tuple[float, float, FinnhubItem] | None = None
        for item, item_key in by_ticker.get(story.ticker, []):
            gap = abs((item.published - story.published_at).total_seconds())
            if gap > DATE_TOLERANCE.total_seconds():
                continue
            score = similarity(key, item_key)
            if best is None or (score, -gap) > (best[0], best[1]):
                best = (score, -gap, item)
        if best is not None:
            matches.append(Match(story, best[2], round(best[0], 2)))
    return matches


async def resolve(fetcher: PoliteFetcher, item: FinnhubItem) -> str | None:
    """The publisher URL a Finnhub link redirects to (the redirect is not followed), or
    None when it leads nowhere usable."""
    response = await fetcher.get(item.url, follow_redirects=False)
    location = response.headers.get("location")
    if response.status not in (301, 302, 303, 307, 308) or not location:
        return None
    host = urlsplit(location).hostname or ""
    if not location.startswith(("http://", "https://")) or host in (GOOGLE_HOST, "finnhub.io"):
        return None
    return location


async def store_publisher_url(
    sessions: async_sessionmaker[AsyncSession], match: Match, url: str
) -> None:
    """Record the publisher URL on the Google News article, and forget the article's
    failed fetch of the Google link, so `ingest article-text` tries the publisher."""
    async with sessions() as session, session.begin():
        await session.execute(
            update(Article)
            .where(Article.id == match.story.article_id)
            .values(
                publisher_url=url,
                publisher_url_score=Decimal(str(match.score)),
                publisher_url_source=SERVICE,
            )
        )
        await session.execute(
            delete(ArticleText).where(
                ArticleText.article_id == match.story.article_id,
                ArticleText.status != "ok",
            )
        )


# Storing new items


def raw_item(item: FinnhubItem) -> RawItem:
    return RawItem(
        title=item.headline,
        link=item.url,
        summary=item.summary or None,
        published=item.published.isoformat(),
        publisher=item.source or None,
    )


def names_a_company(matcher: CompanyMatcher, headline: str) -> bool:
    """Whether a headline names a company of the universe (MIN_HEADLINE_CONFIDENCE)."""
    return any(link.confidence >= MIN_HEADLINE_CONFIDENCE for link in matcher.match(headline))


async def store_window(
    sessions: async_sessionmaker[AsyncSession],
    feed: FeedRef,
    window: WindowResult,
    items: Sequence[FinnhubItem],
    matcher: CompanyMatcher,
    *,
    record: bool,
) -> int:
    """Store a window's new items and record the window. Returns the new articles."""
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        stored = await store_items(session, feed, [raw_item(i) for i in items], matcher, now)
        await record_success(session, feed.id, now, new_articles=stored.new)
        if record:
            await session.execute(
                insert(BackfillWindow)
                .values(
                    feed_id=feed.id,
                    window_start=datetime(
                        window.start.year, window.start.month, window.start.day, tzinfo=UTC
                    ),
                    window_end=datetime(
                        window.end.year, window.end.month, window.end.day, tzinfo=UTC
                    )
                    + timedelta(days=1),
                    requests=window.requests,
                    items=len(window.items),
                    new_articles=stored.new,
                    truncated=window.truncated,
                )
                .on_conflict_do_nothing(constraint="uq_backfill_windows_window")
            )
    return stored.new


async def finnhub_feed(session: AsyncSession, ticker: str) -> FeedRef:
    feed_id, _ = await upsert_feed(session, f"{SERVICE}:{ticker}", SERVICE, API_URL, ticker)
    return FeedRef(feed_id, f"{SERVICE}:{ticker}", Link(ticker, *FEED_LINK))


def matched_ids(matches: Sequence[Match]) -> set[int]:
    """Finnhub items at or above the threshold: stories already stored."""
    return {m.item.id for m in matches if m.score >= MATCH_THRESHOLD}


ProgressCallback = Callable[[str, WindowResult, int], None]


# Choosing which new stories to fetch the text of


@dataclass(slots=True)
class TextSelection:
    """Finnhub stories without text, those that pass the pre-filter (per company), and
    the articles to fetch for them, oldest story first."""

    stories: Counter[str] = field(default_factory=Counter)
    passed: Counter[str] = field(default_factory=Counter)
    candidates: list[article_text.Candidate] = field(default_factory=list)


async def text_candidates(
    session: AsyncSession,
    companies: Sequence[UniverseCompany],
    claim_words: Sequence[str],
    limit: int | None = None,
) -> TextSelection:
    """Stories found by Finnhub whose text is worth fetching: the headline or summary of
    one of its articles names the linked company or one of its executives, together
    with an attribution or claim word, and none of the company's excluded phrases (the
    slug filter of `archives.py`, applied to the feed text). Oldest first."""
    tickers = [c.ticker for c in companies]
    planned, _, _ = await article_text.plan(session, tickers)
    by_cluster = {c.cluster_id: c for c in planned}
    cluster = func.coalesce(Article.cluster_id, Article.id)
    finnhub_clusters = set(
        await session.scalars(
            select(cluster)
            .join(ArticleFeed, ArticleFeed.article_id == Article.id)
            .join(Feed, Feed.id == ArticleFeed.feed_id)
            .where(Feed.type == SERVICE, cluster.in_(list(by_cluster)))
            .distinct()
        )
    )
    texts: dict[int, list[str]] = defaultdict(list)
    first: dict[int, datetime] = {}
    rows = await session.execute(
        select(cluster, Article.title, Article.summary, Article.published_at).where(
            cluster.in_(list(finnhub_clusters))
        )
    )
    for cluster_id, title, summary, published_at in rows:
        texts[cluster_id].append(f"{title}\n{summary or ''}")
        first[cluster_id] = min(first.get(cluster_id, published_at), published_at)
    links = await session.execute(
        select(cluster, ArticleCompany.ticker)
        .join(ArticleCompany, ArticleCompany.article_id == Article.id)
        .where(
            cluster.in_(list(finnhub_clusters)),
            ArticleCompany.confidence >= 0.5,
            ArticleCompany.ticker.in_(tickers),
        )
        .distinct()
    )
    names = SlugFilter(companies, claim_words)
    selection = TextSelection()
    chosen: set[int] = set()
    for cluster_id, ticker in links:
        selection.stories[ticker] += 1
        if any(ticker in names.match(text)[0] for text in texts[cluster_id]):
            selection.passed[ticker] += 1
            chosen.add(cluster_id)
    ordered = sorted(chosen, key=lambda c: (first[c], c))
    selection.candidates = [by_cluster[c] for c in ordered[:limit] if c in by_cluster]
    return selection
