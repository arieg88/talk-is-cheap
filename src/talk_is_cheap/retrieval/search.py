"""Find the passages of filings and news that answer a question.

Four modes, all with the same filters:

- `keyword`: Postgres full-text search (English stemming) over chunk text, any of the
  query's words, ranked by `ts_rank` normalized by the log of the chunk's length.
- `vector`: cosine similarity between the query's embedding and each chunk's, through
  the HNSW index of the configured model.
- `hybrid`: Reciprocal Rank Fusion of the top `CANDIDATES` of both.
- `rerank` (the default): the hybrid top `RERANK_DEPTH` re-ordered by a cross-encoder.
  It found the most on the retrieval benchmark, at about 1.5 seconds a search on CPU
  (docs/retrieval.md); `hybrid` takes about 60 ms.

Filters: tickers (a filing's company, or a news article linked with confidence of at
least 0.5), fiscal periods (filings only, so news is left out when periods are given),
a date range (news publish time, filing date), sources, document types and section
types. Every result carries its citation: the document's sec.gov URL and character
range, or the article's link.
"""

import asyncio
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy import (
    ColumnElement,
    Float,
    Text,
    and_,
    cast,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    Chunk,
    Document,
    Embedding,
    Filing,
    Section,
)
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.retrieval.chunking import embedded_text
from talk_is_cheap.retrieval.encoders import Embedder, Reranker
from talk_is_cheap.retrieval.index import MIN_LINK_CONFIDENCE, quoted, text_only_model

CANDIDATES = 50  # results taken from each of keyword and vector search before fusing
# Hybrid results the cross-encoder re-orders: 20 found as much as 30 on the retrieval
# benchmark in two thirds of the time; 10 found less (docs/retrieval.md).
RERANK_DEPTH = 20
RRF_K = 60  # the usual constant of Reciprocal Rank Fusion
LOG_LENGTH = 1  # ts_rank normalization: divide by 1 + log(document length)
EF_SEARCH = 200  # HNSW candidate list; iterative scans extend it when filters drop rows


class Mode(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"
    RERANK = "rerank"


SOURCES = ("filing", "news")


@dataclass(frozen=True, slots=True)
class PeriodRange:
    first: FiscalQuarter
    last: FiscalQuarter

    @property
    def label(self) -> str:
        if self.first == self.last:
            return self.first.label
        return f"{self.first.label}..{self.last.label}"


def parse_periods(text: str) -> list[PeriodRange]:
    """Fiscal quarters and ranges, comma-separated: "FY2026Q3", "FY2025Q1..FY2026Q4",
    "FY2025Q1..FY2026Q4,FY2027Q2". A whole fiscal year "FY2026" is its four quarters."""
    ranges = []
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        first_text, _, last_text = part.partition("..")
        first = _period_bound(first_text, end=False)
        last = _period_bound(last_text or first_text, end=True)
        if last < first:
            raise ValueError(f"period range runs backwards: {part!r}")
        ranges.append(PeriodRange(first, last))
    if not ranges:
        raise ValueError("no fiscal period given")
    return ranges


_YEAR = re.compile(r"^FY(\d{4})$")


def _period_bound(text: str, *, end: bool) -> FiscalQuarter:
    compact = text.replace(" ", "").upper()
    year = _YEAR.match(compact)
    if year:
        return FiscalQuarter(int(year.group(1)), 4 if end else 1)
    return FiscalQuarter.parse(compact)


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk_id: int
    score: float
    text: str
    header: str
    source: str  # "filing" or "news"
    tickers: list[str]
    period: FiscalQuarter | None  # filings
    published_at: datetime  # news: publish time; filings: filing date
    doc_type: str
    section_type: str | None
    section_title: str | None  # filings
    publisher: str | None  # news
    url: str
    char_start: int
    char_end: int
    period_end: date | None = None  # filings: the end of the period the document reports
    accepted_at: datetime | None = None  # filings: when the SEC accepted the filing

    @property
    def citation(self) -> str:
        if self.source == "news":
            return self.url
        return f"{self.url} characters {self.char_start}-{self.char_end}"


@dataclass(frozen=True, slots=True)
class Filters:
    tickers: Sequence[str] | None = None
    periods: Sequence[PeriodRange] | None = None
    since: datetime | None = None
    until: datetime | None = None  # exclusive
    sources: Sequence[str] | None = None
    doc_types: Sequence[str] | None = None
    section_types: Sequence[str] | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        live = or_(
            and_(
                Chunk.source == "filing",
                select(Document.id).where(Document.id == Chunk.document_id).exists(),
            ),
            and_(
                Chunk.source == "news",
                select(Article.id).where(Article.id == Chunk.article_id).exists(),
            ),
        )
        conditions: list[ColumnElement[bool]] = [live]
        if self.tickers:
            tickers = [t.upper() for t in self.tickers]
            linked = (
                select(ArticleCompany.article_id)
                .where(
                    ArticleCompany.article_id == Chunk.article_id,
                    ArticleCompany.ticker.in_(tickers),
                    ArticleCompany.confidence >= MIN_LINK_CONFIDENCE,
                )
                .exists()
            )
            conditions.append(
                or_(
                    and_(Chunk.source == "filing", Chunk.tickers.overlap(tickers)),
                    and_(Chunk.source == "news", linked),
                )
            )
        if self.periods:
            ordinal = Chunk.fiscal_year * 4 + Chunk.fiscal_quarter
            conditions.append(
                or_(*(ordinal.between(_ordinal(p.first), _ordinal(p.last)) for p in self.periods))
            )
        if self.since is not None:
            conditions.append(Chunk.published_at >= self.since)
        if self.until is not None:
            conditions.append(Chunk.published_at < self.until)
        if self.sources:
            conditions.append(Chunk.source.in_(list(self.sources)))
        if self.doc_types:
            conditions.append(Chunk.doc_type.in_(list(self.doc_types)))
        if self.section_types:
            conditions.append(Chunk.section_type.in_(list(self.section_types)))
        return conditions


def _ordinal(quarter: FiscalQuarter) -> int:
    return quarter.fiscal_year * 4 + quarter.fiscal_quarter


def day_start(day: date | datetime) -> datetime:
    if isinstance(day, datetime):
        return day if day.tzinfo else day.replace(tzinfo=UTC)
    return datetime.combine(day, time(), tzinfo=UTC)


def rrf(rankings: Iterable[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: each id scores the sum of 1 / (k + rank) over the rankings
    it appears in (ranks from 1). Ties keep the order in which ids were first seen."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: -pair[1])


class Searcher:
    """Runs searches on one session. The models load on first use, so keyword search
    needs neither."""

    def __init__(
        self,
        session: AsyncSession,
        embedder: Callable[[], Embedder],
        reranker: Callable[[], Reranker],
    ) -> None:
        self._session = session
        self._embedder_factory = embedder
        self._reranker_factory = reranker
        self._embedder: Embedder | None = None
        self._reranker: Reranker | None = None

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder

    @property
    def reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = self._reranker_factory()
        return self._reranker

    async def search(
        self,
        query: str,
        tickers: Sequence[str] | None = None,
        periods: str | Sequence[PeriodRange] | None = None,
        since: date | datetime | None = None,
        until: date | datetime | None = None,
        sources: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
        section_types: Sequence[str] | None = None,
        k: int = 8,
        mode: Mode | str = Mode.RERANK,
        *,
        context_header: bool = True,
        rerank_depth: int = RERANK_DEPTH,
    ) -> list[SearchResult]:
        """The `k` best passages for `query`. `until` is inclusive for a date and
        exclusive for a datetime. `context_header=False` searches the vectors of chunk
        text without its header (they exist once `talk-is-cheap eval retrieval` made
        them) and reranks text alone. `rerank_depth` is how many hybrid results the
        cross-encoder re-orders."""
        mode = Mode(mode)
        if isinstance(periods, str):
            periods = parse_periods(periods)
        end = None
        if until is not None:
            end = day_start(until)
            if not isinstance(until, datetime):
                end += timedelta(days=1)
        filters = Filters(
            tickers=tickers,
            periods=periods or None,
            since=day_start(since) if since is not None else None,
            until=end,
            sources=sources,
            doc_types=doc_types,
            section_types=section_types,
        )
        if mode is Mode.KEYWORD:
            ranked = await self._keyword(query, filters, k)
        elif mode is Mode.VECTOR:
            ranked = await self._vector(query, filters, k, context_header)
        else:
            depth = max(k, CANDIDATES)
            keyword = await self._keyword(query, filters, depth)
            vector = await self._vector(query, filters, depth, context_header)
            ranked = rrf([[i for i, _ in keyword], [i for i, _ in vector]])
            if mode is Mode.RERANK:
                ranked = await self._rerank(query, ranked[:rerank_depth], context_header)
        return await self._results(ranked[:k])

    async def _keyword(self, query: str, filters: Filters, limit: int) -> list[tuple[int, float]]:
        # Any of the query's words, not all of them: a question has words no passage has.
        any_word = cast(
            func.replace(cast(func.plainto_tsquery("english", query), Text), " & ", " | "),
            TSQUERY,
        )
        # How often the words occur, over 1 + log(length) (normalization 1). Cover density
        # (`ts_rank_cd`) and plain length normalization both ranked far worse on the
        # retrieval benchmark (docs/retrieval.md).
        rank = func.ts_rank(Chunk.search_text, any_word, LOG_LENGTH)
        statement = (
            select(Chunk.id, rank)
            .where(Chunk.search_text.bool_op("@@")(any_word), *filters.conditions())
            .order_by(rank.desc(), Chunk.id)
            .limit(limit)
        )
        return [(i, float(score)) for i, score in await self._session.execute(statement)]

    async def _vector(
        self, query: str, filters: Filters, limit: int, context_header: bool
    ) -> list[tuple[int, float]]:
        embedder = self.embedder
        dimensions = int(embedder.dimensions)
        # In a worker thread: the model runs on the CPU (ONNX Runtime releases the GIL), so
        # concurrent searches overlap instead of blocking the event loop.
        query_vector = await asyncio.to_thread(embedder.embed_query, query)
        vector = ",".join(repr(float(x)) for x in query_vector)
        # The same expression and model predicate as the model's partial HNSW index
        # (index.py: ensure_vector_index), so the planner can use it.
        distance: ColumnElement[float] = literal_column(
            f"(embeddings.embedding::vector({dimensions})) <=> '[{vector}]'::vector({dimensions})",
            type_=Float,
        )
        joined_on = Chunk.content_hash if context_header else Chunk.text_hash
        model = embedder.model_name if context_header else text_only_model(embedder.model_name)
        statement = (
            select(Chunk.id, distance)
            .join(Embedding, Embedding.content_hash == joined_on)
            .where(
                literal_column("embeddings.model") == literal_column(quoted(model)),
                *filters.conditions(),
            )
            .order_by(distance)
            .limit(limit)
        )
        # Filters drop rows after the index scan; an iterative scan keeps reading the
        # index until `limit` rows pass them. Its order is approximate, so sort again.
        await self._session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        await self._session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
        rows = (await self._session.execute(statement)).all()
        return sorted(((i, 1.0 - float(d)) for i, d in rows), key=lambda pair: -pair[1])

    async def _rerank(
        self, query: str, ranked: Sequence[tuple[int, float]], context_header: bool
    ) -> list[tuple[int, float]]:
        if not ranked:
            return []
        ids = [i for i, _ in ranked]
        rows = await self._session.execute(
            select(Chunk.id, Chunk.header, Chunk.text).where(Chunk.id.in_(ids))
        )
        passages = {
            i: embedded_text(header, body) if context_header else body for i, header, body in rows
        }
        scores = await asyncio.to_thread(self.reranker.score, query, [passages[i] for i in ids])
        return sorted(zip(ids, scores, strict=True), key=lambda pair: -pair[1])

    async def _results(self, ranked: Sequence[tuple[int, float]]) -> list[SearchResult]:
        if not ranked:
            return []
        rows = await self._session.execute(
            select(
                Chunk,
                Document.url,
                Section.title,
                Article.url,
                Article.publisher,
                Document.period_end,
                Filing.acceptance_datetime,
            )
            .outerjoin(Document, Document.id == Chunk.document_id)
            .outerjoin(Filing, Filing.accession_no == Document.accession_no)
            .outerjoin(Section, Section.id == Chunk.section_id)
            .outerjoin(Article, Article.id == Chunk.article_id)
            .where(Chunk.id.in_([i for i, _ in ranked]))
        )
        found = {row[0].id: row for row in rows}
        results = []
        for chunk_id, score in ranked:
            chunk, document_url, section_title, article_url, publisher, period_end, accepted = (
                found[chunk_id]
            )
            period = None
            if chunk.fiscal_year is not None and chunk.fiscal_quarter is not None:
                period = FiscalQuarter(chunk.fiscal_year, chunk.fiscal_quarter)
            results.append(
                SearchResult(
                    chunk_id=chunk.id,
                    score=score,
                    text=chunk.text,
                    header=chunk.header,
                    source=chunk.source,
                    tickers=list(chunk.tickers),
                    period=period,
                    published_at=chunk.published_at,
                    doc_type=chunk.doc_type,
                    section_type=chunk.section_type,
                    section_title=section_title,
                    publisher=publisher,
                    url=document_url or article_url or "",
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    period_end=period_end,
                    accepted_at=accepted,
                )
            )
        return results
