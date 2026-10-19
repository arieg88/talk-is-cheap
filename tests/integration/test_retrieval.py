"""Indexing and search end to end against a real database, with the fake company's
filings, a few articles, and deterministic stand-ins for the models."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    Chunk,
    Company,
    Document,
    Embedding,
)
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.edgar.ingest import ingest_edgar
from talk_is_cheap.filings.ingest import ingest_filings
from talk_is_cheap.retrieval import encoders
from talk_is_cheap.retrieval.index import IndexResult, index, vector_index_name
from talk_is_cheap.retrieval.search import Mode, Searcher
from tests.integration.test_ingest import CIK, TICKER, UNIVERSE, FakeSource
from tests.retrieval_fakes import FakeEmbedder, FakeReranker

pytestmark = pytest.mark.integration

NAMES = {TICKER: "Ticacorp"}
NEWS = "https://retrieval.example.test"
DAY = datetime(2025, 4, 25, 14, tzinfo=UTC)
# (path, title, summary, links as (ticker, confidence))
ARTICLES = [
    ("/beat", "Ticacorp beats revenue estimates", "Cloud sales jumped.", [("TICA", "0.90")]),
    ("/feed-only", "Chipmakers rally on revenue hopes", None, [("TICA", "0.30")]),
    ("/other", "Ticonium wins a defense order", "Revenue will follow.", [("TICN", "0.90")]),
]


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(Company).where(Company.cik == CIK))
        await session.execute(delete(Article).where(Article.url.like(f"{NEWS}/%")))
        await session.execute(delete(Chunk))
        await session.execute(delete(Embedding))


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    await ingest_edgar(FakeSource(), factory, UNIVERSE, tickers=[TICKER])
    await ingest_filings(FakeSource(), factory, UNIVERSE, tickers=[TICKER])
    async with factory() as session, session.begin():
        for path, title, summary, links in ARTICLES:
            article = Article(
                url_hash=hashlib.sha256((NEWS + path).encode()).hexdigest(),
                url=NEWS + path,
                title=title,
                summary=summary,
                publisher="Test Wire",
                published_at=DAY,
                time_precision="published",
            )
            session.add(article)
            await session.flush()
            session.add_all(
                ArticleCompany(
                    article_id=article.id,
                    ticker=ticker,
                    method="headline",
                    confidence=Decimal(confidence),
                )
                for ticker, confidence in links
            )
    yield factory
    await _clean(factory)


def by_name(results: list[IndexResult]) -> dict[str, IndexResult]:
    assert all(r.ok for r in results), [r.error for r in results]
    return {r.name: r for r in results}


async def chunk_count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(Chunk)) or 0)


async def test_index_then_reindex_embeds_nothing_new(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    embedder = FakeEmbedder()
    first = by_name(await index(engine, embedder, NAMES))

    filings, news = first[TICKER], first["news"]
    assert filings.added > 0 and (filings.removed, filings.unchanged) == (0, 0)
    assert (news.added, news.removed) == (3, 0)
    assert len(embedder.embedded) == filings.added + news.added == filings.embedded + news.embedded
    assert await chunk_count(sessions) == filings.added + news.added

    async with sessions() as session:
        chunks = (await session.execute(select(Chunk))).scalars().all()
        documents = {d.id: d.text for d in (await session.execute(select(Document))).scalars()}
        vectors = set(await session.scalars(select(Embedding.content_hash)))
    for chunk in chunks:
        assert chunk.content_hash in vectors  # every chunk has its vector
        if chunk.source == "filing":
            assert chunk.document_id is not None
            assert chunk.text == documents[chunk.document_id][chunk.char_start : chunk.char_end]
            assert chunk.header.startswith("TICA · Ticacorp · FY")
    beat = next(c for c in chunks if c.text.startswith("Ticacorp beats"))
    assert (
        beat.tickers == ["TICA"]
        and beat.header == "TICA · Ticacorp · news · Test Wire · 2025-04-25"
    )
    feed_only = next(c for c in chunks if c.text.startswith("Chipmakers"))
    assert feed_only.tickers == []  # a feed link alone (0.3) does not tie it to TICA

    embedder.embedded.clear()
    again = by_name(await index(engine, embedder, NAMES))
    assert (again[TICKER].added, again[TICKER].removed) == (0, 0)
    assert again[TICKER].unchanged == filings.added
    assert (again["news"].added, again["news"].unchanged) == (0, 3)
    assert embedder.embedded == []

    async with sessions() as session:
        index_name = await session.scalar(
            text("SELECT indexname FROM pg_indexes WHERE indexname = :name"),
            {"name": vector_index_name(embedder.model_name)},
        )
    assert index_name is not None


async def test_reparsed_and_deleted_sources_lose_their_chunks(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    embedder = FakeEmbedder()
    first = by_name(await index(engine, embedder, NAMES))
    async with sessions() as session:
        old_documents = set(await session.scalars(select(Document.id).where(Document.cik == CIK)))

    # Re-parsing replaces every document with a new row; its text is unchanged.
    await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER], parser_version="t2")
    async with sessions() as session, session.begin():
        await session.execute(delete(Article).where(Article.url == NEWS + "/other"))

    embedder.embedded.clear()
    second = by_name(await index(engine, embedder, NAMES))
    assert second[TICKER].removed == second[TICKER].added == first[TICKER].added
    assert second[TICKER].unchanged == 0
    assert (second["news"].removed, second["news"].unchanged) == (1, 2)
    assert embedder.embedded == []  # same texts: their vectors are reused

    async with sessions() as session:
        stale = await session.scalar(
            select(func.count()).where(Chunk.document_id.in_(old_documents))
        )
    assert stale == 0
    assert await chunk_count(sessions) == first[TICKER].added + 2


async def test_rebuild_embeds_everything_again(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    embedder = FakeEmbedder()
    first = by_name(await index(engine, embedder, NAMES))
    embedder.embedded.clear()
    rebuilt = by_name(await index(engine, embedder, NAMES, tickers=[TICKER], rebuild=True))

    assert rebuilt[TICKER].removed == rebuilt[TICKER].added == first[TICKER].added
    assert rebuilt[TICKER].embedded == first[TICKER].added
    # With --tickers, news means the articles linked to them, at any confidence.
    assert rebuilt["news"].added == rebuilt["news"].removed == 2


async def test_a_new_embedding_model_replaces_every_vector(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await index(engine, FakeEmbedder(), NAMES)
    notes: list[str] = []
    other = FakeEmbedder(dimensions=32, model_name="fake/bag-of-words-32")
    results = by_name(await index(engine, other, NAMES, on_note=notes.append))

    total = await chunk_count(sessions)
    assert sum(r.unchanged for r in results.values()) == total  # chunks are the same
    assert len(other.embedded) == total
    assert notes and notes[0].startswith("removed ") and "other embedding models" in notes[0]
    async with sessions() as session:
        models = set(await session.scalars(select(Embedding.model).distinct()))
    assert models == {"fake/bag-of-words-32"}
    hits = await search(sessions, "revenue expected", mode=Mode.VECTOR, embedder=other)
    assert hits


async def search(
    sessions: async_sessionmaker[AsyncSession],
    query: str,
    *,
    embedder: FakeEmbedder | None = None,
    **filters: object,
) -> list:  # type: ignore[type-arg]
    model = embedder or FakeEmbedder()
    async with sessions() as session:
        searcher = Searcher(session, lambda: model, FakeReranker)
        return await searcher.search(query, **filters)  # type: ignore[arg-type]


async def test_search_modes_and_citations(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await index(engine, FakeEmbedder(), NAMES)
    query = "second quarter revenue expected"
    for mode in Mode:
        results = await search(sessions, query, tickers=["TICA"], mode=mode, k=3)
        assert results, mode
        top = results[0]
        assert "Second quarter revenue is expected to be $110" in top.text, mode
        assert (top.source, top.section_type, top.doc_type) == (
            "filing",
            "outlook",
            "press_release",
        )
        assert top.period == FiscalQuarter(2025, 1)
        assert top.url.endswith("/000099000125000004/ex991.htm")
        assert top.section_title == "Outlook"
        assert top.citation == f"{top.url} characters {top.char_start}-{top.char_end}"
        async with sessions() as session:
            document = await session.scalar(select(Document.text).where(Document.url == top.url))
        assert document is not None and document[top.char_start : top.char_end] == top.text
        assert len(results) <= 3
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


async def test_search_filters(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await index(engine, FakeEmbedder(), NAMES)
    query = "revenue"

    def kinds(results: list) -> set[tuple[str, str | None]]:  # type: ignore[type-arg]
        return {(r.doc_type, r.section_type) for r in results}

    risk = await search(sessions, "customers", section_types=["risk_factors"], k=20)
    assert risk and kinds(risk) <= {("10q", "risk_factors"), ("10k", "risk_factors")}

    annual = await search(sessions, query, doc_types=["10k"], k=20)
    assert annual and {r.period for r in annual} == {FiscalQuarter(2025, 4)}

    q1 = await search(sessions, query, periods="FY2025Q1", k=20)
    assert q1 and {r.period for r in q1} == {FiscalQuarter(2025, 1)}
    assert all(r.source == "filing" for r in q1)  # news has no fiscal period
    later = await search(sessions, query, periods="FY2025Q2..FY2026Q1", k=20)
    assert {r.period for r in later} == {FiscalQuarter(2025, q) for q in (2, 3, 4)} | {
        FiscalQuarter(2026, 1)
    }

    news = await search(sessions, query, sources=["news"], k=20)
    assert {r.url for r in news} == {NEWS + "/beat", NEWS + "/feed-only", NEWS + "/other"}
    assert all(r.citation == r.url and r.publisher == "Test Wire" for r in news)

    # Filing dates and publish times: the release (2025-04-24) and the news (2025-04-25).
    april = await search(sessions, query, since=date(2025, 4, 24), until=date(2025, 4, 25), k=50)
    assert {r.doc_type for r in april} == {"press_release", "earnings_exhibit", "news"}
    assert await search(sessions, query, since=date(2027, 1, 1)) == []


async def test_news_counts_for_a_ticker_only_through_confident_links(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await index(engine, FakeEmbedder(), NAMES)
    for mode in (Mode.KEYWORD, Mode.VECTOR, Mode.HYBRID):
        results = await search(sessions, "revenue", tickers=["TICA"], sources=["news"], mode=mode)
        assert [r.url for r in results] == [NEWS + "/beat"], mode
        assert results[0].tickers == ["TICA"]

    # Search reads the links as they are now, not as they were when indexed.
    async with sessions() as session, session.begin():
        await session.execute(
            ArticleCompany.__table__.update()
            .where(ArticleCompany.ticker == "TICA")
            .values(confidence=Decimal("0.80"))
        )
    results = await search(sessions, "revenue", tickers=["TICA"], sources=["news"], k=10)
    assert {r.url for r in results} == {NEWS + "/beat", NEWS + "/feed-only"}


async def test_cli_index_and_search(
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(encoders, "load_embedder", lambda settings: FakeEmbedder())
    monkeypatch.setattr(encoders, "load_reranker", lambda settings: FakeReranker())

    def run(*argv: str) -> int:
        return main(list(argv))

    assert await asyncio.to_thread(run, "index") == 0
    out = capsys.readouterr().out
    assert "embedding model: fake/bag-of-words-64" in out
    assert "TICA " in out and "0 removed" in out and "news       3 added" in out
    assert await asyncio.to_thread(run, "index", "--tickers", "TICA") == 0
    assert "0 added,     0 removed" in capsys.readouterr().out
    assert await asyncio.to_thread(run, "index", "--tickers", "ZZZZ") == 1
    assert "not loaded: ZZZZ" in capsys.readouterr().err

    args = ("search", "second quarter revenue expected", "--ticker", "TICA", "--k", "2")
    assert await asyncio.to_thread(run, *args, "--periods", "FY2025Q1", "--types", "outlook") == 0
    out = capsys.readouterr().out
    assert out.startswith(" 1. [")
    assert "TICA FY2025Q1 press_release outlook: Outlook" in out
    assert "/000099000125000004/ex991.htm characters " in out
    assert "Second quarter revenue is expected to be $110" in out
    assert "passages in" in out and "(rerank)" in out  # the default mode

    assert await asyncio.to_thread(run, *args, "--sources", "news", "--mode", "rerank") == 0
    out = capsys.readouterr().out
    assert "TICA news 2025-04-25 14:00Z Test Wire" in out and f"{NEWS}/beat" in out

    assert await asyncio.to_thread(run, *args, "--periods", "FY2025Q9") == 1
    assert "FY2026Q3" in capsys.readouterr().err
