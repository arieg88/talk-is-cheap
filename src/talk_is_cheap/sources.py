"""What each news source contributes: `talk-is-cheap sources [--report]`.

A source is where articles come from: RSS (polled feeds: Yahoo Finance, Google News,
market-wide feeds, newsrooms), GDELT, Common Crawl, the Wayback Machine and Finnhub, by
the feeds that supplied each article (`article_feeds`). A Google News article whose
publisher URL was found on Finnhub counts as found by both RSS and Finnhub.

Without `--report`: articles and stories (dedup clusters) per source, shared or not. With
`--report`: only what no other source also found, over the stories whose articles all
come from one source: articles linked to a company (confidence 0.5 or more), stories
with text, management claims, and due claims (one per group of identical promises,
due by the resolver's own rule, `promises/resolve.py: plan_resolution`). Read-only.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    ArticleFeed,
    ArticleText,
    Feed,
    Statement,
)
from talk_is_cheap.edgar.fiscal import FiscalCalendar

SOURCES = ("RSS", "GDELT", "Common Crawl", "Wayback", "Finnhub")
FEED_SOURCES = {
    "yahoo_finance": "RSS",
    "google_news": "RSS",
    "rss": "RSS",
    "press_releases": "RSS",
    "gdelt": "GDELT",
    "commoncrawl": "Common Crawl",
    "wayback": "Wayback",
    "finnhub": "Finnhub",
}
MIN_CONFIDENCE = 0.5


@dataclass(slots=True)
class SourceCounts:
    articles: Counter[str] = field(default_factory=Counter)  # all, shared or not
    stories: Counter[str] = field(default_factory=Counter)
    # Only what one source found:
    only_stories: Counter[str] = field(default_factory=Counter)
    shared_stories: int = 0
    linked: Counter[str] = field(default_factory=Counter)
    with_text: Counter[str] = field(default_factory=Counter)
    management_claims: Counter[str] = field(default_factory=Counter)
    due_claims: Counter[str] = field(default_factory=Counter)


async def source_counts(
    session: AsyncSession,
    tickers: list[str],
    calendars: dict[str, FiscalCalendar],
    today: date,
) -> SourceCounts:
    from talk_is_cheap.promises.resolve import plan_resolution

    cluster = func.coalesce(Article.cluster_id, Article.id)
    counts = SourceCounts()
    found: dict[int, set[str]] = defaultdict(set)
    article_sources: dict[int, set[str]] = defaultdict(set)
    feed_rows = await session.execute(
        select(Article.id, cluster, Feed.type)
        .join(ArticleFeed, ArticleFeed.article_id == Article.id)
        .join(Feed, Feed.id == ArticleFeed.feed_id)
    )
    for article_id, cluster_id, kind in feed_rows:
        source = FEED_SOURCES.get(kind)
        if source is not None:
            found[cluster_id].add(source)
            article_sources[article_id].add(source)
    matched_rows = await session.execute(
        select(Article.id, cluster).where(Article.publisher_url_source == "finnhub")
    )
    for article_id, cluster_id in matched_rows:
        found[cluster_id].add("Finnhub")
        article_sources[article_id].add("Finnhub")
    for sources in article_sources.values():
        counts.articles.update(sources)
    for sources in found.values():
        counts.stories.update(sources)
    only = {c: next(iter(s)) for c, s in found.items() if len(s) == 1}
    counts.only_stories.update(only.values())
    counts.shared_stories = sum(len(s) > 1 for s in found.values())

    linked_clusters: set[int] = set()
    linked_rows = await session.execute(
        select(cluster, func.count(func.distinct(Article.id)))
        .join(ArticleCompany, ArticleCompany.article_id == Article.id)
        .where(ArticleCompany.confidence >= MIN_CONFIDENCE)
        .group_by(cluster)
    )
    for cluster_id, n in linked_rows:
        linked_clusters.add(cluster_id)
        if cluster_id in only:
            counts.linked[only[cluster_id]] += n
    text_rows = await session.execute(
        select(cluster)
        .join(ArticleText, ArticleText.article_id == Article.id)
        .where(ArticleText.status == "ok")
        .distinct()
    )
    for (cluster_id,) in text_rows:
        if cluster_id in only and cluster_id in linked_clusters:
            counts.with_text[only[cluster_id]] += 1
    claim_rows = await session.execute(
        select(Statement.cluster_id, func.count())
        .where(Statement.management)
        .group_by(Statement.cluster_id)
    )
    for cluster_id, n in claim_rows:
        if cluster_id in only:
            counts.management_claims[only[cluster_id]] += n

    plans = await plan_resolution(session, tickers, calendars, today)
    heads = [
        c.promise
        for p in plans
        for c in p.candidates
        if c.promise.due_date is None or (c.grace is not None and c.grace <= today)
    ]
    article_cluster = dict(
        (
            await session.execute(
                select(Article.id, cluster).where(Article.id.in_([p.article_id for p in heads]))
            )
        ).all()
    )
    for promise in heads:
        head_cluster = article_cluster.get(promise.article_id)
        if head_cluster is not None and head_cluster in only:
            counts.due_claims[only[head_cluster]] += 1
    return counts


async def sources_command(report: bool) -> int:
    from talk_is_cheap.config import get_settings
    from talk_is_cheap.db import create_engine, create_session_factory
    from talk_is_cheap.statements.extract import calendars
    from talk_is_cheap.universe import load_universe

    settings = get_settings()
    try:
        universe = load_universe(settings.universe_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}")
        return 1
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            fiscal = await calendars(session)
            counts = await source_counts(session, universe.tickers, fiscal, date.today())
    finally:
        await engine.dispose()
    if not report:
        print(f"{'SOURCE':<14}{'ARTICLES':>10}{'STORIES':>10}{'ONLY THIS SOURCE':>18}")
        for source in SOURCES:
            print(
                f"{source:<14}{counts.articles[source]:>10,}{counts.stories[source]:>10,}"
                f"{counts.only_stories[source]:>18,}"
            )
        print(f"\n{counts.shared_stories:,} stories were found by more than one source")
        return 0
    print(
        "What only this source found (stories whose articles all come from it; a Google "
        "News story matched on Finnhub counts for both):\n"
    )
    print("| Source | Articles linked | Stories with text | Management claims | Due claims |")
    print("| --- | --- | --- | --- | --- |")
    for source in SOURCES:
        print(
            f"| {source} | {counts.linked[source]:,} | {counts.with_text[source]:,} "
            f"| {counts.management_claims[source]:,} | {counts.due_claims[source]:,} |"
        )
    print(
        f"\n{sum(counts.only_stories.values()):,} stories found by one source, "
        f"{counts.shared_stories:,} by several (not counted above)"
    )
    return 0
