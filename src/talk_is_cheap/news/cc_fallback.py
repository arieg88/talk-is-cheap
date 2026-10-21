"""Common Crawl copies of articles that failed live: `talk-is-cheap ingest article-text
--common-crawl`.

An article whose live page is gone (404, 410) or gave no article text is looked up, by
its publisher URL, in the Common Crawl index: the crawls
that ran after it was published, newest first, at most MAX_CRAWLS. The first successful
HTML capture is read (one range request for its WARC record) and its text extracted as
for a live page; the text is stored with the crawl it came from in `article_texts.reason`.
Refusals are never got around with a copy: pages robots.txt refuses, 401, 403 and 405
answers, paywalls and interstitials (a bot check, retried live with `--slow`) are never
looked up. The Wayback Machine is not used. An article
looked up without success is marked so (`NO_COPY`) and not looked up again.

Requests go through `http_cache.PoliteFetcher`: at most one a second, backoff on 429 and
5xx, every answer cached on disk.
"""

import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Article, ArticleCompany, ArticleText
from talk_is_cheap.news.archive_index import (
    ArchiveError,
    Crawl,
    cc_lookup,
    decode_page,
    read_capture,
)
from talk_is_cheap.news.article_text import (
    MIN_CONFIDENCE,
    PAYWALL_MARKERS,
    RESOLVED_HOSTS,
    Fetched,
    extract_text,
    store,
)
from talk_is_cheap.news.http_cache import PoliteFetcher, ServiceUnavailable

# Live failures worth looking up: the page is gone or has no text (as for archived
# articles, `archives.LIVE_GONE`); robots.txt allowed the request.
FALLBACK_REASONS = ("HTTP 404", "HTTP 410", "no article text")
MAX_CRAWLS = 3
NO_COPY = "no Common Crawl copy"
SOURCE = "text from Common Crawl"


@dataclass(frozen=True, slots=True)
class Failed:
    article_id: int
    url: str
    published_at: datetime
    reason: str

    @property
    def domain(self) -> str:
        return (urlsplit(self.url).hostname or "?").removeprefix("www.")


async def failed_articles(
    session: AsyncSession, tickers: Sequence[str] | None = None, limit: int | None = None
) -> list[Failed]:
    """Articles of linked stories that failed live for a reason worth a lookup, never
    looked up before, oldest first."""
    url = func.coalesce(Article.publisher_url, Article.url)
    linked = select(ArticleCompany.article_id).where(ArticleCompany.confidence >= MIN_CONFIDENCE)
    if tickers:
        linked = linked.where(ArticleCompany.ticker.in_([t.upper() for t in tickers]))
    rows = await session.execute(
        select(Article.id, url, Article.published_at, ArticleText.reason)
        .join(ArticleText, ArticleText.article_id == Article.id)
        .where(ArticleText.status == "unavailable", Article.id.in_(linked))
        .order_by(Article.published_at, Article.id)
    )
    found = []
    for article_id, link, published_at, reason in rows:
        text = reason or ""
        host = urlsplit(link).hostname or ""
        if not text.startswith(FALLBACK_REASONS) or NO_COPY in text:
            continue
        if host in RESOLVED_HOSTS or host == "news.google.com":
            continue  # no publisher URL to look up
        found.append(Failed(int(article_id), link, published_at, text))
    return found[:limit] if limit else found


@dataclass(slots=True)
class FallbackSummary:
    tried: Counter[str] = field(default_factory=Counter)  # by domain
    recovered: Counter[str] = field(default_factory=Counter)
    failed: Counter[str] = field(default_factory=Counter)  # the archive kept failing
    by_reason: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))


async def recover(
    sessions: async_sessionmaker[AsyncSession],
    fetcher: PoliteFetcher,
    crawls: Sequence[Crawl],
    failed: Sequence[Failed],
    *,
    on_result: Callable[[Failed, str], None] | None = None,
) -> FallbackSummary:
    """Look each article up in the crawls that ran after it was published, newest first,
    and store the text of the first copy found."""
    summary = FallbackSummary()
    newest_first = sorted(crawls, key=lambda c: c.end, reverse=True)
    for item in failed:
        summary.tried[item.domain] += 1
        kind = item.reason.split(":")[0]
        summary.by_reason[kind]["tried"] += 1
        try:
            outcome = await _recover_one(sessions, fetcher, newest_first, item)
        except (ServiceUnavailable, ArchiveError) as exc:
            summary.failed[item.domain] += 1
            outcome = f"lookup failed ({str(exc)[:80]}); left for the next run"
        if outcome.startswith(SOURCE):
            summary.recovered[item.domain] += 1
            summary.by_reason[kind]["recovered"] += 1
        if on_result is not None:
            on_result(item, outcome)
    return summary


async def _recover_one(
    sessions: async_sessionmaker[AsyncSession],
    fetcher: PoliteFetcher,
    crawls: Sequence[Crawl],
    item: Failed,
) -> str:
    after = item.published_at.date()
    for crawl in [c for c in crawls if c.end >= after][:MAX_CRAWLS]:
        capture = await cc_lookup(fetcher, crawl.id, item.url)
        if capture is None:
            continue
        page = await read_capture(fetcher, capture)
        if page.status != 200:
            continue
        html = decode_page(page)
        text = await asyncio.to_thread(extract_text, html, item.url)
        if text is None or any(m in text[-600:].lower() for m in PAYWALL_MARKERS):
            continue
        reason = f"{SOURCE} {crawl.id}"
        await store(sessions, Fetched(item.article_id, "ok", reason, 200, text))
        return reason
    async with sessions() as session, session.begin():
        await session.execute(
            update(ArticleText)
            .where(ArticleText.article_id == item.article_id)
            .values(reason=f"{item.reason}; {NO_COPY}"[:2000])
        )
    return NO_COPY
