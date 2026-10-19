"""Store normalized news items: articles once per canonical URL, the feeds that supplied
them, near-duplicate clusters and company links. Shared by polling and backfill."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Article as ArticleRow
from talk_is_cheap.db.models import ArticleCompany, ArticleFeed, Feed
from talk_is_cheap.news.dedup import CLUSTER_WINDOW, Clusterer
from talk_is_cheap.news.linking import CompanyMatcher, Link
from talk_is_cheap.news.normalize import (
    canonical_url,
    clean_summary,
    clean_title,
    domain,
    is_spam,
    parse_timestamp,
    plain_text,
    url_hash,
)
from talk_is_cheap.news.records import Article, RawItem, TimePrecision, Validators

# Articles relinked per statement.
BATCH_SIZE = 1000
# A publish time further ahead than this is a feed error; the fetch time is used instead.
FUTURE_TOLERANCE = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class FeedRef:
    """A feed row's identity and what its items get from the feed alone."""

    id: int
    key: str
    feed_link: Link | None


@dataclass(frozen=True, slots=True)
class StoreResult:
    items: int = 0
    new: int = 0
    duplicates: int = 0  # already stored, or repeated in the feed, under the same URL
    clustered: int = 0  # new, but joined an existing story's cluster
    spam: int = 0  # dropped (normalize.is_spam)


def normalize_item(item: RawItem, fetched_at: datetime) -> Article | None:
    """An item as an article, or None when it has no usable link or title."""
    if not item.link.startswith(("http://", "https://")):
        return None
    title = clean_title(item.title, item.publisher)
    if not title:
        return None
    url = canonical_url(item.link)
    published = parse_timestamp(item.published)
    precision = item.time_precision
    if published is None or published > fetched_at + FUTURE_TOLERANCE:
        published, precision = fetched_at, TimePrecision.FETCHED
    return Article(
        url=url,
        url_hash=url_hash(url),
        title=title,
        summary=clean_summary(item.summary, title),
        publisher=_publisher(item.publisher),
        published_at=published,
        time_precision=precision,
    )


def _publisher(name: str | None) -> str | None:
    """The publisher's name; a URL given as the name (some Google News sources) becomes
    its domain."""
    text = (name or "").strip()
    if text.startswith(("http://", "https://")):
        text = domain(text)
    return text[:255] or None


async def upsert_feed(
    session: AsyncSession, key: str, type_: str, url: str, ticker: str | None
) -> tuple[int, Validators]:
    """The feed's id and stored validators, creating or updating its row."""
    statement = insert(Feed).values(
        key=key, type=type_, url=url, ticker=ticker, consecutive_failures=0
    )
    row = (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[Feed.key],
                set_={"type": type_, "url": url, "ticker": ticker},
            ).returning(Feed.id, Feed.etag, Feed.last_modified, Feed.items_hash, Feed.url)
        )
    ).one()
    return row.id, Validators(row.etag, row.last_modified, row.items_hash)


async def record_success(
    session: AsyncSession,
    feed_id: int,
    now: datetime,
    *,
    new_articles: int,
    validators: Validators | None = None,
) -> None:
    values: dict[str, object] = {
        "last_polled_at": now,
        "last_success_at": now,
        "consecutive_failures": 0,
        "last_error": None,
    }
    if new_articles:
        values["last_new_item_at"] = now
    if validators is not None:
        values |= {
            "etag": validators.etag,
            "last_modified": validators.last_modified,
            "items_hash": validators.items_hash,
        }
    await session.execute(update(Feed).where(Feed.id == feed_id).values(**values))


async def record_failure(session: AsyncSession, feed_id: int, now: datetime, error: str) -> None:
    await session.execute(
        update(Feed)
        .where(Feed.id == feed_id)
        .values(
            last_polled_at=now,
            consecutive_failures=Feed.consecutive_failures + 1,
            last_error=error[:2000],
        )
    )


async def store_items(
    session: AsyncSession,
    feed: FeedRef,
    items: Sequence[RawItem],
    matcher: CompanyMatcher,
    fetched_at: datetime,
) -> StoreResult:
    """Store one feed's items. Articles already stored gain this feed and its links; spam
    is dropped and counted."""
    articles: dict[str, Article] = {}
    usable = spam = 0
    for item in items:
        if is_spam(plain_text(item.title)):
            spam += 1
            continue
        article = normalize_item(item, fetched_at)
        if article is not None:
            usable += 1
            articles.setdefault(article.url_hash, article)
    if not articles:
        return StoreResult(items=len(items), spam=spam)

    rows = await session.execute(
        select(ArticleRow.url_hash, ArticleRow.id).where(ArticleRow.url_hash.in_(list(articles)))
    )
    existing = {digest: article_id for digest, article_id in rows}
    fresh = sorted(
        (a for h, a in articles.items() if h not in existing), key=lambda a: a.published_at
    )
    clusterer = await _load_clusterer(session, fresh, matcher)

    ids: dict[str, int] = dict(existing)
    clustered = new = 0
    own_cluster = []
    for article in fresh:
        companies = named_companies(matcher, article.title)
        cluster_id = clusterer.find(article.title, article.published_at, companies)
        article_id = await session.scalar(
            insert(ArticleRow)
            .values(
                url_hash=article.url_hash,
                url=article.url,
                title=article.title,
                summary=article.summary,
                publisher=article.publisher,
                published_at=article.published_at,
                time_precision=article.time_precision.value,
                cluster_id=cluster_id,
            )
            # A poll or backfill running at the same time may have stored it first.
            .on_conflict_do_nothing(index_elements=[ArticleRow.url_hash])
            .returning(ArticleRow.id)
        )
        if article_id is None:
            stored_id = await session.scalar(
                select(ArticleRow.id).where(ArticleRow.url_hash == article.url_hash)
            )
            assert stored_id is not None
            ids[article.url_hash] = stored_id
            continue
        new += 1
        ids[article.url_hash] = article_id
        if cluster_id is None:
            own_cluster.append(article_id)
            cluster_id = article_id
        else:
            clustered += 1
        clusterer.add(cluster_id, article.title, article.published_at, companies)
    if own_cluster:
        await session.execute(
            update(ArticleRow)
            .where(ArticleRow.id.in_(own_cluster))
            .values(cluster_id=ArticleRow.id)
        )

    await session.execute(
        insert(ArticleFeed)
        .values([{"article_id": i, "feed_id": feed.id} for i in ids.values()])
        .on_conflict_do_nothing()
    )
    await _upsert_links(session, articles, ids, feed, matcher)
    return StoreResult(
        items=len(items),
        new=new,
        duplicates=usable - new,
        clustered=clustered,
        spam=spam,
    )


def named_companies(matcher: CompanyMatcher, title: str) -> frozenset[str]:
    """The companies a headline names, which near-duplicates must share."""
    return frozenset(link.ticker for link in matcher.match(title))


async def _load_clusterer(
    session: AsyncSession, fresh: Sequence[Article], matcher: CompanyMatcher
) -> Clusterer:
    clusterer = Clusterer()
    if not fresh:
        return clusterer
    start = fresh[0].published_at - CLUSTER_WINDOW
    end = fresh[-1].published_at + CLUSTER_WINDOW
    rows = await session.execute(
        select(ArticleRow.cluster_id, ArticleRow.id, ArticleRow.title, ArticleRow.published_at)
        .where(ArticleRow.published_at.between(start, end))
        .order_by(ArticleRow.id)
    )
    for cluster_id, article_id, title, published_at in rows:
        clusterer.add(
            cluster_id or article_id, title, published_at, named_companies(matcher, title)
        )
    return clusterer


async def _upsert_links(
    session: AsyncSession,
    articles: dict[str, Article],
    ids: dict[str, int],
    feed: FeedRef,
    matcher: CompanyMatcher,
) -> None:
    """Upsert each article's links; a link only ever gains confidence."""
    rows = []
    feed_links = [feed.feed_link] if feed.feed_link else []
    for digest, article in articles.items():
        for link in matcher.link(article.title, article.summary, article.publisher, feed_links):
            rows.append(
                {
                    "article_id": ids[digest],
                    "ticker": link.ticker,
                    "method": link.method,
                    "confidence": Decimal(str(link.confidence)),
                }
            )
    if not rows:
        return
    statement = insert(ArticleCompany).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ArticleCompany.article_id, ArticleCompany.ticker],
            set_={"method": statement.excluded.method, "confidence": statement.excluded.confidence},
            where=statement.excluded.confidence > ArticleCompany.confidence,
        )
    )


@dataclass(frozen=True, slots=True)
class ReprocessResult:
    articles: int
    spam_removed: int
    titles_changed: int
    clusters_before: int
    clusters_after: int
    links: int


async def reprocess(
    session: AsyncSession,
    matcher: CompanyMatcher,
    feed_links: dict[str, tuple[str, float]],
    article_ids: Sequence[int] | None = None,
) -> ReprocessResult:
    """Apply the current rules to stored articles (all, or `article_ids`): delete spam,
    clean titles again, rebuild the near-duplicate clusters in publish order, and replace
    every link with one computed from the text, publisher and supplying feeds
    (`feed_links` maps a feed type to its feed-only link)."""
    statement = select(
        ArticleRow.id,
        ArticleRow.title,
        ArticleRow.summary,
        ArticleRow.publisher,
        ArticleRow.published_at,
        ArticleRow.cluster_id,
    ).order_by(ArticleRow.published_at, ArticleRow.id)
    if article_ids is not None:
        statement = statement.where(ArticleRow.id.in_(article_ids))
    rows = list(await session.execute(statement))

    spam = [row.id for row in rows if is_spam(row.title)]
    for start in range(0, len(spam), BATCH_SIZE):
        await session.execute(
            delete(ArticleRow).where(ArticleRow.id.in_(spam[start : start + BATCH_SIZE]))
        )
    spam_ids = set(spam)
    kept = [row for row in rows if row.id not in spam_ids]

    clusterer = Clusterer()
    changes = []
    clusters = set()
    titles_changed = 0
    for row in kept:
        title = clean_title(row.title, row.publisher)
        companies = named_companies(matcher, title)
        cluster_id = clusterer.find(title, row.published_at, companies) or row.id
        clusterer.add(cluster_id, title, row.published_at, companies)
        clusters.add(cluster_id)
        if title != row.title or cluster_id != row.cluster_id:
            changes.append({"id": row.id, "title": title, "cluster_id": cluster_id})
            titles_changed += title != row.title
    for start in range(0, len(changes), BATCH_SIZE):
        await session.execute(update(ArticleRow), changes[start : start + BATCH_SIZE])

    links = await relink(session, matcher, feed_links, [row.id for row in kept])
    return ReprocessResult(
        articles=len(kept),
        spam_removed=len(spam),
        titles_changed=titles_changed,
        clusters_before=len({row.cluster_id or row.id for row in kept}),
        clusters_after=len(clusters),
        links=links,
    )


async def relink(
    session: AsyncSession,
    matcher: CompanyMatcher,
    feed_links: dict[str, tuple[str, float]],
    article_ids: Sequence[int],
) -> int:
    """Replace the links of `article_ids` with ones computed from their text, publisher
    and the feeds that supplied them. Returns the number of links written."""
    written = 0
    for start in range(0, len(article_ids), BATCH_SIZE):
        batch = article_ids[start : start + BATCH_SIZE]
        supplied: dict[int, list[Link]] = {}
        rows = await session.execute(
            select(ArticleFeed.article_id, Feed.type, Feed.ticker)
            .join(Feed, Feed.id == ArticleFeed.feed_id)
            .where(ArticleFeed.article_id.in_(batch))
        )
        for article_id, feed_type, ticker in rows:
            if ticker and feed_type in feed_links:
                supplied.setdefault(article_id, []).append(Link(ticker, *feed_links[feed_type]))
        await session.execute(delete(ArticleCompany).where(ArticleCompany.article_id.in_(batch)))
        articles = await session.execute(
            select(ArticleRow.id, ArticleRow.title, ArticleRow.summary, ArticleRow.publisher).where(
                ArticleRow.id.in_(batch)
            )
        )
        links = [
            {
                "article_id": article_id,
                "ticker": link.ticker,
                "method": link.method,
                "confidence": Decimal(str(link.confidence)),
            }
            for article_id, title, summary, publisher in articles
            for link in matcher.link(title, summary, publisher, supplied.get(article_id, []))
        ]
        if links:
            await session.execute(insert(ArticleCompany).values(links))
            written += len(links)
    return written
