"""Build and update the search index: chunks of every filing section and news article,
and their embeddings.

`index` is incremental. For each company it chunks the stored sections again and
compares the result with the stored chunks: an identical chunk is left alone, a changed
or new one is written, and one whose document was deleted or re-parsed is removed. News
is handled the same way per article. Only texts that no stored vector of the current
model covers are embedded (vectors are keyed by model and content hash), so re-running
embeds nothing, and re-parsing a filing re-embeds only text that changed. Switching the
embedding model removes the old model's vectors and embeds everything again.
"""

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as day_start
from decimal import Decimal

from sqlalchemy import ColumnElement, delete, func, insert, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    Chunk,
    Company,
    Document,
    Embedding,
    Section,
)
from talk_is_cheap.retrieval.chunking import (
    chunk_article,
    chunk_section,
    content_hash,
    embedded_text,
)
from talk_is_cheap.retrieval.encoders import Embedder

# News links below this confidence (a per-company feed alone) do not tie an article to
# a company (news/queries.py uses the same default).
MIN_LINK_CONFIDENCE = Decimal("0.5")
EMBED_BATCH = 64  # texts embedded and stored per transaction

DOC_LABELS = {
    "press_release": "press release",
    "earnings_exhibit": "earnings exhibit",
    "10q": "10-Q",
    "10k": "10-K",
    "deliveries_report": "deliveries report",
    "other_exhibit": "other exhibit",
}
SECTION_LABELS = {
    "outlook": "outlook",
    "results": "results",
    "other": "other",
    "commentary": "commentary",
    "mdna": "MD&A",
    "risk_factors": "risk factors",
}
NEWS = "news"
SEPARATOR = " · "


def filing_header(
    ticker: str,
    name: str | None,
    fiscal_year: int,
    fiscal_quarter: int,
    doc_type: str,
    section_type: str,
) -> str:
    parts = [ticker, name, f"FY{fiscal_year} Q{fiscal_quarter}"]
    parts += [DOC_LABELS.get(doc_type, doc_type), SECTION_LABELS.get(section_type, section_type)]
    return SEPARATOR.join(p for p in parts if p)


def news_header(
    tickers: Sequence[str], names: Sequence[str], publisher: str | None, published: datetime
) -> str:
    parts = [", ".join(tickers), ", ".join(names), NEWS, publisher, f"{published:%Y-%m-%d}"]
    return SEPARATOR.join(p for p in parts if p)


@dataclass(frozen=True, slots=True)
class IndexResult:
    """What one company's filings, or the news, changed in the index."""

    name: str  # a ticker, or "news"
    added: int = 0
    removed: int = 0
    unchanged: int = 0
    embedded: int = 0  # texts sent to the model; the rest already had vectors
    seconds: float = 0.0
    largest: int = 0  # tokens of the longest embedded input
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class _Draft:
    """A chunk as indexing would store it."""

    source: str
    document_id: int | None
    section_id: int | None
    article_id: int | None
    position: int
    tickers: tuple[str, ...]
    fiscal_year: int | None
    fiscal_quarter: int | None
    published_at: datetime
    doc_type: str
    section_type: str | None
    header: str
    text: str
    char_start: int
    char_end: int
    token_count: int
    content_hash: str
    text_hash: str

    def row(self) -> dict[str, object]:
        values = {f: getattr(self, f) for f in self.__dataclass_fields__}
        values["tickers"] = list(self.tickers)
        return values


# Columns that decide whether a stored chunk is the same as a new draft.
_IDENTITY = (
    "source", "document_id", "section_id", "article_id", "position", "tickers", "fiscal_year",
    "fiscal_quarter", "published_at", "doc_type", "section_type", "header", "char_start",
    "char_end", "token_count", "content_hash", "text_hash",
)  # fmt: skip


def _identity(values: object) -> tuple[object, ...]:
    key = []
    for column in _IDENTITY:
        value = getattr(values, column)
        key.append(tuple(value) if column == "tickers" else value)
    return tuple(key)


def text_only_model(model: str) -> str:
    """The name under which vectors of chunk text without its context header are stored.
    They come from the same model but have their own HNSW index, so the two kinds never
    crowd each other out of a search. Only the retrieval benchmark makes them."""
    return f"{model} (text only)"


def vector_index_name(model: str) -> str:
    return ("ix_embeddings_hnsw_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_"))[:63]


def quoted(value: str) -> str:
    """A SQL string literal. Model names go into partial index predicates and the queries
    that must match them, which cannot be bind parameters."""
    return "'" + value.replace("'", "''") + "'"


async def ensure_vector_index(engine: AsyncEngine, model: str, dimensions: int) -> None:
    """The HNSW index over one model's vectors (migration 0005 creates the default's)."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {vector_index_name(model)} ON embeddings "
                f"USING hnsw ((embedding::vector({int(dimensions)})) vector_cosine_ops) "
                f"WHERE model = {quoted(model)}"
            )
        )


async def index(
    engine: AsyncEngine,
    embedder: Embedder,
    company_names: dict[str, str],
    *,
    tickers: Sequence[str] | None = None,
    rebuild: bool = False,
    on_result: Callable[[IndexResult], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> list[IndexResult]:
    """Index the filings of the selected companies (all loaded ones by default) and the
    news linked to them (all news by default). `company_names` gives the names used in
    context headers ("Nvidia" rather than "NVIDIA CORP")."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    note = on_note or (lambda _: None)
    await ensure_vector_index(engine, embedder.model_name, embedder.dimensions)
    async with sessions() as session, session.begin():
        current = [embedder.model_name, text_only_model(embedder.model_name)]
        others = await session.scalar(select(func.count()).where(Embedding.model.not_in(current)))
        if others:
            # One model per index: vectors of another model are never searched with these.
            await session.execute(delete(Embedding).where(Embedding.model.not_in(current)))
            note(f"removed {others} vectors of other embedding models")
        statement = select(Company.ticker, Company.cik).order_by(Company.ticker)
        if tickers:
            statement = statement.where(Company.ticker.in_([t.upper() for t in tickers]))
        companies = (await session.execute(statement)).all()

    results = []
    for ticker, cik in companies:
        started = time.monotonic()
        try:
            drafts = await _filing_drafts(sessions, embedder, ticker, cik, company_names)
            scope = (Chunk.source == "filing") & Chunk.tickers.contains([ticker])
            result = await _apply(sessions, embedder, ticker, drafts, scope, rebuild)
        except Exception as exc:  # one company failing does not stop the others
            result = IndexResult(ticker, error=f"{type(exc).__name__}: {exc}")
        result = _timed(result, started)
        results.append(result)
        if on_result is not None:
            on_result(result)

    started = time.monotonic()
    try:
        selected = [t.upper() for t in tickers] if tickers else None
        drafts, article_ids = await _news_drafts(sessions, embedder, company_names, selected)
        news_scope: ColumnElement[bool] = Chunk.source == "news"
        if selected is not None:
            gone = ~select(Article.id).where(Article.id == Chunk.article_id).exists()
            news_scope = news_scope & or_(Chunk.article_id.in_(article_ids), gone)
        result = await _apply(sessions, embedder, NEWS, drafts, news_scope, rebuild)
    except Exception as exc:
        result = IndexResult(NEWS, error=f"{type(exc).__name__}: {exc}")
    result = _timed(result, started)
    results.append(result)
    if on_result is not None:
        on_result(result)

    if not tickers and all(r.ok for r in results):
        pruned = await prune_embeddings(sessions, embedder.model_name)
        if pruned:
            note(f"removed {pruned} vectors no chunk uses any more")
    return results


def _timed(result: IndexResult, started: float) -> IndexResult:
    return IndexResult(
        result.name,
        result.added,
        result.removed,
        result.unchanged,
        result.embedded,
        time.monotonic() - started,
        result.largest,
        result.error,
    )


async def _filing_drafts(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    ticker: str,
    cik: int,
    company_names: dict[str, str],
) -> list[_Draft]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    Section.id,
                    Section.document_id,
                    Section.section_type,
                    Section.text,
                    Section.char_start,
                    Document.doc_type,
                    Document.fiscal_year,
                    Document.fiscal_quarter,
                    Document.filed_at,
                )
                .join(Document, Document.id == Section.document_id)
                .where(Document.cik == cik)
                .order_by(Section.document_id, Section.position)
            )
        ).all()

    def build() -> list[_Draft]:
        drafts = []
        for row in rows:
            header = filing_header(
                ticker,
                company_names.get(ticker),
                row.fiscal_year,
                row.fiscal_quarter,
                row.doc_type,
                row.section_type,
            )
            filed = datetime.combine(row.filed_at, day_start(), tzinfo=UTC)
            chunks = chunk_section(row.text, row.char_start, header, embedder.tokenizer)
            for position, chunk in enumerate(chunks):
                drafts.append(
                    _Draft(
                        source="filing",
                        document_id=row.document_id,
                        section_id=row.id,
                        article_id=None,
                        position=position,
                        tickers=(ticker,),
                        fiscal_year=row.fiscal_year,
                        fiscal_quarter=row.fiscal_quarter,
                        published_at=filed,
                        doc_type=row.doc_type,
                        section_type=row.section_type,
                        header=header,
                        text=chunk.text,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        token_count=chunk.token_count,
                        content_hash=content_hash(embedded_text(header, chunk.text)),
                        text_hash=content_hash(chunk.text),
                    )
                )
        return drafts

    # Tokenizing is CPU-bound: keep the event loop free.
    return await asyncio.to_thread(build)


async def _news_drafts(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    company_names: dict[str, str],
    tickers: Sequence[str] | None,
) -> tuple[list[_Draft], list[int]]:
    async with sessions() as session:
        statement = select(
            Article.id, Article.title, Article.summary, Article.publisher, Article.published_at
        ).order_by(Article.id)
        if tickers is not None:
            statement = statement.where(
                select(ArticleCompany.article_id)
                .where(ArticleCompany.article_id == Article.id, ArticleCompany.ticker.in_(tickers))
                .exists()
            )
        articles = (await session.execute(statement)).all()
        links: dict[int, list[str]] = {}
        for article_id, ticker in await session.execute(
            select(ArticleCompany.article_id, ArticleCompany.ticker)
            .where(ArticleCompany.confidence >= MIN_LINK_CONFIDENCE)
            .order_by(ArticleCompany.article_id, ArticleCompany.confidence.desc())
        ):
            links.setdefault(article_id, []).append(ticker)

    def build() -> list[_Draft]:
        drafts = []
        for article in articles:
            linked = tuple(links.get(article.id, []))
            names = [company_names[t] for t in linked if t in company_names]
            header = news_header(linked, names, article.publisher, article.published_at)
            chunk = chunk_article(article.title, article.summary, header, embedder.tokenizer)
            drafts.append(
                _Draft(
                    source="news",
                    document_id=None,
                    section_id=None,
                    article_id=article.id,
                    position=0,
                    tickers=linked,
                    fiscal_year=None,
                    fiscal_quarter=None,
                    published_at=article.published_at,
                    doc_type=NEWS,
                    section_type=None,
                    header=header,
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    token_count=chunk.token_count,
                    content_hash=content_hash(embedded_text(header, chunk.text)),
                    text_hash=content_hash(chunk.text),
                )
            )
        return drafts

    return await asyncio.to_thread(build), [a.id for a in articles]


async def _apply(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    name: str,
    drafts: Sequence[_Draft],
    scope: ColumnElement[bool],
    rebuild: bool,
) -> IndexResult:
    """Make the stored chunks in `scope` equal `drafts`, embedding what has no vector."""
    async with sessions() as session:
        stored = (await session.execute(select(Chunk).where(scope))).scalars().all()
    if rebuild:
        async with sessions() as session, session.begin():
            hashes = {c.content_hash for c in stored}
            await session.execute(delete(Chunk).where(Chunk.id.in_([c.id for c in stored])))
            await _delete_vectors(session, embedder.model_name, hashes)
        existing: dict[tuple[object, ...], list[int]] = {}
        removed = len(stored)
    else:
        existing = {}
        for chunk in stored:
            existing.setdefault(_identity(chunk), []).append(chunk.id)
        removed = 0

    keep: set[int] = set()
    added: list[_Draft] = []
    for draft in drafts:
        ids = existing.get(_identity(draft))
        if ids:
            keep.add(ids.pop())
        else:
            added.append(draft)
    stale = [] if rebuild else [c.id for c in stored if c.id not in keep]

    # Every chunk, not only new ones: after a model change unchanged chunks need vectors.
    embedded = await embed_missing(
        sessions, embedder, {d.content_hash: embedded_text(d.header, d.text) for d in drafts}
    )
    async with sessions() as session, session.begin():
        if stale:
            await session.execute(delete(Chunk).where(Chunk.id.in_(stale)))
        for batch in _batches([d.row() for d in added], 500):
            await session.execute(insert(Chunk), batch)
    return IndexResult(
        name,
        added=len(added),
        removed=removed + len(stale),
        unchanged=len(keep),
        embedded=embedded,
        largest=max((d.token_count for d in drafts), default=0),
    )


async def embed_missing(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    texts: dict[str, str],
    *,
    stored_as: str | None = None,
) -> int:
    """Embed the texts (by content hash) that have no vector of this model yet. Vectors are
    stored a batch at a time, so an interrupted run keeps what it embedded. `stored_as`
    names the vectors differently from the model (see `text_only_model`)."""
    model = stored_as or embedder.model_name
    if not texts:
        return 0
    async with sessions() as session:
        have: set[str] = set()
        for hashes in _batches(list(texts), 5000):
            have.update(
                await session.scalars(
                    select(Embedding.content_hash).where(
                        Embedding.model == model, Embedding.content_hash.in_(hashes)
                    )
                )
            )
    missing = [(h, t) for h, t in texts.items() if h not in have]
    for batch in _batches(missing, EMBED_BATCH):
        vectors = await asyncio.to_thread(embedder.embed_passages, [t for _, t in batch])
        async with sessions() as session, session.begin():
            await session.execute(
                pg_insert(Embedding)
                .values(
                    [
                        {"model": model, "content_hash": h, "embedding": v}
                        for (h, _), v in zip(batch, vectors, strict=True)
                    ]
                )
                .on_conflict_do_nothing()  # another run may have embedded the same text
            )
    return len(missing)


async def _delete_vectors(session: AsyncSession, model: str, hashes: Iterable[str]) -> None:
    for batch in _batches(list(hashes), 5000):
        await session.execute(
            delete(Embedding).where(Embedding.model == model, Embedding.content_hash.in_(batch))
        )


async def prune_embeddings(sessions: async_sessionmaker[AsyncSession], model: str) -> int:
    """Delete this model's vectors that no chunk refers to: header-and-text vectors by
    content hash, text-only vectors by text hash."""
    pruned = 0
    async with sessions() as session, session.begin():
        for stored_as, column in (
            (model, Chunk.content_hash),
            (text_only_model(model), Chunk.text_hash),
        ):
            result = await session.execute(
                delete(Embedding).where(
                    Embedding.model == stored_as,
                    ~select(Chunk.id).where(column == Embedding.content_hash).exists(),
                )
            )
            pruned += int(result.rowcount)  # type: ignore[attr-defined]
    return pruned


def _batches[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
