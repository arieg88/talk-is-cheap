"""Read news back: a company's stories, the window after an earnings release, feed
health."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    ArticleFeed,
    BackfillWindow,
    Document,
    Feed,
    Filing,
)
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.financials import find_company

# Links below this confidence (a per-company feed alone) are not shown by default.
DEFAULT_MIN_CONFIDENCE = Decimal("0.5")
EARNINGS_WINDOW = timedelta(hours=48)
# A feed with no new item for this long is stale.
STALE_AFTER = timedelta(hours=72)
# Where an article's text was read, for citing it: the publisher URL found for a Google
# News story (`finnhub.py`), which may be another outlet's version of the story, and that
# site; else the article's own link and publisher.
ARTICLE_LINK = func.coalesce(Article.publisher_url, Article.url)
ARTICLE_PUBLISHER = case(
    (
        Article.publisher_url.is_not(None),
        func.substring(Article.publisher_url, r"^https?://(?:www\.)?([^/:]+)"),
    ),
    else_=Article.publisher,
)
# Feed types that are backfill sources, not polled feeds (news/gdelt.py, news/archives.py,
# news/finnhub.py).
BACKFILL_TYPES = frozenset({"gdelt", "commoncrawl", "wayback", "finnhub"})


@dataclass(frozen=True, slots=True)
class StoryLine:
    """One cluster of near-duplicate articles about a company: its first article, and the
    publishers of the others."""

    published_at: datetime
    time_precision: str
    publisher: str | None
    title: str
    url: str
    confidence: Decimal
    others: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BackfillSource:
    key: str
    windows: int  # windows fully fetched
    first_window: datetime | None
    last_window: datetime | None  # end of the latest fetched week
    articles: int
    truncated: int  # weeks still over GDELT's record limit at the smallest split
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class FeedHealth:
    key: str
    type: str
    status: str
    last_success_at: datetime | None
    last_new_item_at: datetime | None
    consecutive_failures: int
    articles: int
    last_error: str | None


def feed_status(
    consecutive_failures: int,
    last_success_at: datetime | None,
    last_new_item_at: datetime | None,
    created_at: datetime,
    now: datetime,
) -> str:
    """failing (the last poll failed), new (never fetched), stale (no new item for
    STALE_AFTER) or ok."""
    if consecutive_failures:
        return "failing"
    if last_success_at is None:
        return "new"
    if now - (last_new_item_at or created_at) > STALE_AFTER:
        return "stale"
    return "ok"


async def company_news(
    session: AsyncSession,
    ticker: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    min_confidence: Decimal = DEFAULT_MIN_CONFIDENCE,
    limit: int | None = None,
) -> list[StoryLine]:
    """A company's stories, newest first, one per cluster."""
    statement = (
        select(Article, ArticleCompany.confidence)
        .join(ArticleCompany, ArticleCompany.article_id == Article.id)
        .where(
            ArticleCompany.ticker == ticker.upper(),
            ArticleCompany.confidence >= min_confidence,
        )
        .order_by(Article.published_at, Article.id)
    )
    if since is not None:
        statement = statement.where(Article.published_at >= since)
    if until is not None:
        statement = statement.where(Article.published_at < until)

    clusters: dict[int, list[tuple[Article, Decimal]]] = {}
    for article, confidence in await session.execute(statement):
        clusters.setdefault(article.cluster_id or article.id, []).append((article, confidence))

    stories = []
    for members in clusters.values():
        first = members[0][0]
        stories.append(
            StoryLine(
                published_at=first.published_at,
                time_precision=first.time_precision,
                publisher=first.publisher,
                title=first.title,
                url=first.url,
                confidence=max(c for _, c in members),
                others=list(dict.fromkeys(a.publisher or "?" for a, _ in members[1:])),
            )
        )
    stories.sort(key=lambda s: s.published_at, reverse=True)
    return stories[:limit] if limit else stories


async def earnings_window(
    session: AsyncSession, ticker: str, quarter: FiscalQuarter
) -> tuple[datetime, datetime] | None:
    """The EARNINGS_WINDOW after the quarter's earnings release was filed (its SEC
    acceptance time, or the start of its filing date when that is unknown)."""
    company = await find_company(session, ticker)
    if company is None:
        return None
    row = (
        await session.execute(
            select(Filing.acceptance_datetime, Filing.filing_date)
            .join(Document, Document.accession_no == Filing.accession_no)
            .where(
                Document.cik == company.cik,
                Document.doc_type == "press_release",
                Document.fiscal_year == quarter.fiscal_year,
                Document.fiscal_quarter == quarter.fiscal_quarter,
            )
            .order_by(Filing.filing_date)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    accepted, filed = row
    start = accepted or datetime.combine(filed, time(), tzinfo=UTC)
    return start, start + EARNINGS_WINDOW


async def backfill_progress(session: AsyncSession) -> list[BackfillSource]:
    """Each backfill source: the windows fetched, the range they cover, and its health."""
    windows = (
        select(
            BackfillWindow.feed_id,
            func.count().label("windows"),
            func.min(BackfillWindow.window_start).label("first"),
            func.max(BackfillWindow.window_end).label("last"),
            func.count().filter(BackfillWindow.truncated).label("truncated"),
        )
        .group_by(BackfillWindow.feed_id)
        .subquery()
    )
    articles = (
        select(ArticleFeed.feed_id, func.count().label("articles"))
        .group_by(ArticleFeed.feed_id)
        .subquery()
    )
    rows = await session.execute(
        select(
            Feed,
            func.coalesce(windows.c.windows, 0),
            windows.c.first,
            windows.c.last,
            func.coalesce(windows.c.truncated, 0),
            func.coalesce(articles.c.articles, 0),
        )
        .outerjoin(windows, windows.c.feed_id == Feed.id)
        .outerjoin(articles, articles.c.feed_id == Feed.id)
        .where(Feed.type.in_(BACKFILL_TYPES))
        .order_by(Feed.key)
    )
    return [
        BackfillSource(
            key=feed.key,
            windows=int(count),
            first_window=first,
            last_window=last,
            articles=int(stored),
            truncated=int(truncated),
            last_success_at=feed.last_success_at,
            consecutive_failures=feed.consecutive_failures,
            last_error=feed.last_error,
        )
        for feed, count, first, last, truncated, stored in rows
    ]


async def feed_health(session: AsyncSession, now: datetime) -> list[FeedHealth]:
    """Polled feeds only; backfill sources are in `backfill_progress`."""
    articles = (
        select(ArticleFeed.feed_id, func.count().label("articles"))
        .group_by(ArticleFeed.feed_id)
        .subquery()
    )
    rows = await session.execute(
        select(Feed, func.coalesce(articles.c.articles, 0))
        .outerjoin(articles, articles.c.feed_id == Feed.id)
        .where(Feed.type.not_in(BACKFILL_TYPES))
        .order_by(Feed.type, Feed.key)
    )
    return [
        FeedHealth(
            key=feed.key,
            type=feed.type,
            status=feed_status(
                feed.consecutive_failures,
                feed.last_success_at,
                feed.last_new_item_at,
                feed.created_at,
                now,
            ),
            last_success_at=feed.last_success_at,
            last_new_item_at=feed.last_new_item_at,
            consecutive_failures=feed.consecutive_failures,
            articles=int(count),
            last_error=feed.last_error,
        )
        for feed, count in rows
    ]
