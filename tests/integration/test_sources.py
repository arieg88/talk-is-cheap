"""What each news source contributes (`talk-is-cheap sources`), and which Finnhub stories
`ingest article-text --finnhub` reads, against a real database."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    ArticleFeed,
    ArticleText,
    Feed,
    Statement,
    StatementExtraction,
)
from talk_is_cheap.news.finnhub import text_candidates
from talk_is_cheap.news.normalize import url_hash
from talk_is_cheap.sources import source_counts
from talk_is_cheap.universe import Executive, UniverseCompany

pytestmark = pytest.mark.integration

HOST = "https://sources.example.test"
COMPANY = UniverseCompany(
    ticker="SRCA",
    name="Sourcery",
    exclude_phrases=["Sourcery Records"],
    executives=[Executive(name="Ida Bell", role="Chief Executive Officer")],
)
DAY = datetime(2025, 3, 3, tzinfo=UTC)


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(Article).where(Article.url.like(f"{HOST}/%")))
        await session.execute(delete(Feed).where(Feed.ticker == "SRCA"))


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    yield factory
    await _clean(factory)


async def _article(
    session: AsyncSession,
    path: str,
    title: str,
    feeds: list[Feed],
    *,
    cluster: Article | None = None,
    summary: str | None = None,
    published: datetime = DAY,
    text: bool = False,
    claims: int = 0,
) -> Article:
    article = Article(
        url_hash=url_hash(HOST + path), url=HOST + path, title=title, summary=summary,
        publisher="Example", published_at=published, time_precision="published",
    )  # fmt: skip
    session.add(article)
    await session.flush()
    article.cluster_id = cluster.id if cluster is not None else article.id
    for feed in feeds:
        session.add(ArticleFeed(article_id=article.id, feed_id=feed.id))
    session.add(ArticleCompany(article_id=article.id, ticker="SRCA", method="headline",
                               confidence=0.9))  # fmt: skip
    if text:
        session.add(ArticleText(article_id=article.id, status="ok", text="Body " * 100))
    if claims:
        extraction = StatementExtraction(
            article_id=article.id, cluster_id=article.cluster_id, ticker="SRCA", model="m",
            prompt_version="v1", input_hash="h", processing_version="1", claims=[],
            dropped=[], input_tokens=0, output_tokens=0, cost=0,
        )  # fmt: skip
        session.add(extraction)
        await session.flush()
        for n in range(claims):
            session.add(
                Statement(
                    extraction_id=extraction.id, article_id=article.id,
                    cluster_id=article.cluster_id, ticker="SRCA", quote="q", char_start=0,
                    char_end=1, attribution="company_statement", management=True,
                    is_direct_quote=False, venue="unknown", statement_date=published.date(),
                    date_from_article=True, claim_type="plan", claim="c", basis="gaap",
                    measure="level", unit="other", item_key=f"k{n}", model="m",
                    prompt_version="v1",
                )
            )  # fmt: skip
    await session.flush()
    return article


async def test_each_source_counts_only_what_no_other_source_found(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        feeds = {}
        for kind in ("yahoo_finance", "gdelt", "commoncrawl", "wayback", "finnhub"):
            feeds[kind] = Feed(key=f"{kind}:SRCA", type=kind, url=HOST, ticker="SRCA",
                               consecutive_failures=0)  # fmt: skip
            session.add(feeds[kind])
        await session.flush()
        # Found by one source each.
        await _article(session, "/rss", "Sourcery opens a plant", [feeds["yahoo_finance"]],
                       text=True, claims=2)  # fmt: skip
        await _article(session, "/cc", "Sourcery CEO says sales will double",
                       [feeds["commoncrawl"]], text=True, claims=3)  # fmt: skip
        await _article(session, "/wb", "Sourcery expects more", [feeds["wayback"]])
        # One story, two sources (GDELT and Finnhub): counted by neither.
        shared = await _article(session, "/gdelt", "Sourcery buys a rival", [feeds["gdelt"]],
                                text=True, claims=1)  # fmt: skip
        await _article(session, "/fh", "Sourcery buys rival", [feeds["finnhub"]], cluster=shared)
        # A Google News story matched on Finnhub counts for both RSS and Finnhub.
        matched = await _article(session, "/gn", "Sourcery hires", [feeds["yahoo_finance"]],
                                 text=True)  # fmt: skip
        matched.publisher_url, matched.publisher_url_source = HOST + "/pub", "finnhub"
    async with sessions() as session:
        counts = await source_counts(session, [], {}, date(2026, 10, 21))
    assert counts.articles == {"RSS": 2, "Common Crawl": 1, "Wayback": 1, "GDELT": 1,
                               "Finnhub": 2}  # fmt: skip
    assert counts.only_stories == {"RSS": 1, "Common Crawl": 1, "Wayback": 1}
    assert counts.shared_stories == 2
    assert counts.linked == {"RSS": 1, "Common Crawl": 1, "Wayback": 1}
    assert counts.with_text == {"RSS": 1, "Common Crawl": 1}
    assert counts.management_claims == {"RSS": 2, "Common Crawl": 3}
    assert not counts.due_claims


async def test_sources_command_prints_both_tables(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    assert await asyncio.to_thread(main, ["sources"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SOURCE") and "Finnhub" in out and "Wayback" in out
    assert await asyncio.to_thread(main, ["sources", "--report"]) == 0
    out = capsys.readouterr().out
    assert (
        "| Source | Articles linked | Stories with text | Management claims | Due claims |" in out
    )
    assert "| Common Crawl |" in out


async def test_finnhub_stories_are_read_only_when_their_feed_text_names_the_company_with_a_claim(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        finnhub = Feed(key="finnhub:SRCA", type="finnhub", url=HOST, ticker="SRCA",
                       consecutive_failures=0)  # fmt: skip
        session.add(finnhub)
        await session.flush()
        newer = await _article(session, "/a", "Sourcery CEO says sales will double", [finnhub],
                               published=datetime(2026, 5, 1, tzinfo=UTC))  # fmt: skip
        older = await _article(session, "/b", "Sourcery shares rise", [finnhub],
                               summary="Ida Bell said the company will open two plants.",
                               published=datetime(2025, 5, 1, tzinfo=UTC))  # fmt: skip
        await _article(session, "/c", "Sourcery stock: what to know", [finnhub])  # no claim word
        await _article(session, "/d", "Sourcery Records says the album will ship", [finnhub])
        await _article(session, "/e", "Sourcery expects more", [finnhub], text=True)  # has text
    async with sessions() as session:
        selection = await text_candidates(session, [COMPANY], ["says", "said", "will", "ceo"])
    assert selection.stories["SRCA"] == 4  # stories without text
    assert selection.passed["SRCA"] == 2
    assert [c.article_id for c in selection.candidates] == [older.id, newer.id]  # oldest first


def test_dry_run_goes_only_with_finnhub(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "article-text", "--dry-run"]) == 2
    assert "--dry-run only goes with --finnhub" in capsys.readouterr().err
    assert main(["ingest", "article-text", "--finnhub", "--recovered-only"]) == 2
