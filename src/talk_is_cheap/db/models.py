"""ORM models for companies, filings, financial data, filing text, news, the
retrieval index and guidance."""

from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from talk_is_cheap.db.base import Base

# Wide enough for any reported dollar amount and for per-share values with 6 decimals.
AMOUNT = Numeric(28, 6)


class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    ticker: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))
    sic: Mapped[str | None] = mapped_column(String(8))
    sic_description: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (Index("ix_filings_cik_form_filing_date", "cik", "form", "filing_date"),)

    accession_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    form: Mapped[str] = mapped_column(String(16))
    filing_date: Mapped[date] = mapped_column(Date)
    report_date: Mapped[date | None] = mapped_column(Date)
    acceptance_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    primary_document: Mapped[str | None] = mapped_column(String(255))
    items: Mapped[str | None] = mapped_column(String(255))
    is_xbrl: Mapped[bool] = mapped_column(Boolean, default=False)


class XbrlFact(Base):
    """A raw XBRL value exactly as one filing reported it. Never updated or overwritten."""

    __tablename__ = "xbrl_facts"
    __table_args__ = (
        UniqueConstraint(
            "cik",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "accession_no",
            name="uq_xbrl_facts_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_xbrl_facts_cik_concept", "cik", "taxonomy", "concept"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    taxonomy: Mapped[str] = mapped_column(String(32))
    concept: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(32))
    value: Mapped[Decimal] = mapped_column(AMOUNT)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    accession_no: Mapped[str] = mapped_column(String(20))
    form: Mapped[str] = mapped_column(String(16))
    filed: Mapped[date] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    fiscal_period: Mapped[str | None] = mapped_column(String(4))
    frame: Mapped[str | None] = mapped_column(String(16))


class FinancialValue(Base):
    """A canonical metric for one period, as one filing reported or implied it.

    A period has one row per filing that reported it: the earliest is the value as first
    reported, later rows are restatements or repeats. Rows are rebuilt from `xbrl_facts`
    on every ingestion run.
    """

    __tablename__ = "financial_values"
    __table_args__ = (
        UniqueConstraint(
            "cik",
            "metric",
            "period_start",
            "period_end",
            "accession_no",
            name="uq_financial_values_version",
        ),
        CheckConstraint(
            "(period_type = 'quarter' AND fiscal_quarter BETWEEN 1 AND 4) "
            "OR (period_type = 'year' AND fiscal_quarter IS NULL)",
            name="period_shape",
        ),
        Index(
            "ix_financial_values_lookup",
            "cik",
            "metric",
            "fiscal_year",
            "fiscal_quarter",
            "filed",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(64))
    period_type: Mapped[str] = mapped_column(String(8))
    fiscal_year: Mapped[int] = mapped_column(SmallInteger)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(AMOUNT)
    unit: Mapped[str] = mapped_column(String(32))
    source_concept: Mapped[str] = mapped_column(String(255))
    accession_no: Mapped[str] = mapped_column(String(20))
    form: Mapped[str] = mapped_column(String(16))
    filed: Mapped[date] = mapped_column(Date)
    derived: Mapped[bool] = mapped_column(Boolean, default=False)
    derivation: Mapped[str | None] = mapped_column(String(128))


class Document(Base):
    """One parsed filing document: an earnings release or exhibit, a 10-Q or 10-K, or a
    deliveries report or unrelated exhibit filed under item 2.02.

    `text` is the document converted to structured plain text; sections are character
    ranges of it. A document is re-parsed when `parser_version` changes.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("accession_no", "document_name", name="uq_documents_document"),
        CheckConstraint(
            "doc_type IN ('press_release', 'earnings_exhibit', '10q', '10k', "
            "'deliveries_report', 'other_exhibit')",
            name="doc_type",
        ),
        CheckConstraint("fiscal_quarter BETWEEN 1 AND 4", name="fiscal_quarter"),
        Index("ix_documents_company_period", "cik", "fiscal_year", "fiscal_quarter", "doc_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    accession_no: Mapped[str] = mapped_column(
        ForeignKey("filings.accession_no", ondelete="CASCADE")
    )
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(String(32))
    document_name: Mapped[str] = mapped_column(String(255))
    exhibit: Mapped[str | None] = mapped_column(String(32))  # e.g. "EX-99.1"
    url: Mapped[str] = mapped_column(String(512))
    fiscal_year: Mapped[int] = mapped_column(SmallInteger)
    fiscal_quarter: Mapped[int] = mapped_column(SmallInteger)
    period_end: Mapped[date] = mapped_column(Date)
    filed_at: Mapped[date] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(16))
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Section(Base):
    """A tagged part of a document: `documents.text[char_start:char_end]`."""

    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("document_id", "position", name="uq_sections_position"),
        CheckConstraint("char_start >= 0 AND char_end >= char_start", name="char_range"),
        Index("ix_sections_section_type", "section_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    section_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)


class Feed(Base):
    """A news source polled as one feed (or a backfill source for one company), with its
    HTTP validators and health."""

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)  # "yahoo_finance:NVDA"
    type: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(String(16))
    etag: Mapped[str | None] = mapped_column(String(255))
    last_modified: Mapped[str | None] = mapped_column(String(64))
    items_hash: Mapped[str | None] = mapped_column(String(64))  # adapters.items_hash
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_new_item_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Article(Base):
    """A news item's metadata, stored once per canonical URL. Full articles are never
    stored. Near-duplicate headlines share a `cluster_id` (the id of the cluster's first
    article)."""

    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint(
            "time_precision IN ('published', 'seen', 'fetched')", name="time_precision"
        ),
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_cluster_id", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    time_precision: Mapped[str] = mapped_column(String(16))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ArticleFeed(Base):
    """Which feeds supplied an article, and when each first did."""

    __tablename__ = "article_feeds"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    feed_id: Mapped[int] = mapped_column(
        ForeignKey("feeds.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ArticleCompany(Base):
    """A link from an article to a company, by ticker (news-only companies have no
    `companies` row), with how it was found and how sure it is."""

    __tablename__ = "article_companies"
    __table_args__ = (
        CheckConstraint(
            "method IN ('issuer', 'ticker', 'headline', 'summary', 'feed')", name="method"
        ),
        CheckConstraint("confidence > 0 AND confidence <= 1", name="confidence"),
        Index("ix_article_companies_ticker", "ticker", "confidence"),
    )

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    method: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2))


class BackfillWindow(Base):
    """A date window of historical news fetched for one company from one backfill
    source. A window is recorded when fully fetched, so a re-run skips it."""

    __tablename__ = "backfill_windows"
    __table_args__ = (
        UniqueConstraint("feed_id", "window_start", name="uq_backfill_windows_window"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(Integer)
    items: Mapped[int] = mapped_column(Integer)
    new_articles: Mapped[int] = mapped_column(Integer)
    # Some part of the window still hit the record limit at the smallest split.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Chunk(Base):
    """A passage of filing text or a news article, as indexed for search.

    Filing chunks lie inside one section: `text` is `documents.text[char_start:char_end]`,
    except that a piece of a split table starts with the table's header rows. A news chunk
    is the article's headline and summary. `header` is the context header embedded before
    the text ("NVDA · Nvidia · FY2027 Q2 · press release · outlook").

    `document_id`, `section_id` and `article_id` are not foreign keys: re-parsing a filing
    replaces its documents, and the next `talk-is-cheap index` removes the chunks left
    behind and counts them. Search joins the documents and articles, so it never returns
    such a chunk.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("source IN ('filing', 'news')", name="source"),
        CheckConstraint(
            "(source = 'filing') = (document_id IS NOT NULL AND section_id IS NOT NULL "
            "AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL)",
            name="filing_fields",
        ),
        CheckConstraint("(source = 'news') = (article_id IS NOT NULL)", name="news_fields"),
        CheckConstraint("char_start >= 0 AND char_end >= char_start", name="char_range"),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_article_id", "article_id"),
        Index("ix_chunks_tickers", "tickers", postgresql_using="gin"),
        Index("ix_chunks_period", "fiscal_year", "fiscal_quarter"),
        Index("ix_chunks_published_at", "published_at"),
        Index("ix_chunks_content_hash", "content_hash"),
        Index("ix_chunks_text_hash", "text_hash"),
        Index("ix_chunks_search_text", "search_text", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(8))
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    section_id: Mapped[int | None] = mapped_column(BigInteger)
    article_id: Mapped[int | None] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer)  # within the section
    # The company's ticker for filings; for news, the companies linked with confidence of
    # at least 0.5 when the chunk was indexed (search reads current links instead).
    tickers: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    # News: publish time. Filings: the filing date, at midnight UTC.
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    doc_type: Mapped[str] = mapped_column(String(32))  # documents.doc_type, or "news"
    section_type: Mapped[str | None] = mapped_column(String(32))
    header: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)  # embedded input, specials included
    content_hash: Mapped[str] = mapped_column(String(64))  # of header, separator and text
    text_hash: Mapped[str] = mapped_column(String(64))  # of the text alone
    search_text: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Embedding(Base):
    """A vector of one embedded text by one model, shared by every chunk with that text.

    Chunks find theirs by `content_hash` (header and text) or `text_hash` (text alone).
    Each model has its own HNSW index, a partial index over its rows, so vectors of
    different models are never searched together.
    """

    __tablename__ = "embeddings"

    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StockSplit(Base):
    """A stock split, from `guidance/splits.yaml`: on `effective_date`, the first day the
    shares traded split-adjusted, each share became `ratio` shares."""

    __tablename__ = "stock_splits"
    __table_args__ = (
        UniqueConstraint("cik", "effective_date", name="uq_stock_splits_split"),
        CheckConstraint("ratio > 0", name="ratio"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    effective_date: Mapped[date] = mapped_column(Date)
    ratio: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    source: Mapped[str] = mapped_column(Text)


class GuidanceExtraction(Base):
    """The guidance items extracted from one document's outlook sections by one model
    with one prompt version: what the model returned, after parsing, period resolution
    and grounding. One row per document, replaced when the document is extracted again.

    `items` holds the grounded items, `dropped` the ones whose quote was not found in the
    source, with the reason. Documents are identified by accession number and name, which
    survive re-parsing; `input_hash` is the hash of the full prompt, so changed text or a
    changed prompt is extracted again.
    """

    __tablename__ = "guidance_extractions"
    __table_args__ = (
        UniqueConstraint("accession_no", "document_name", name="uq_guidance_extractions_document"),
        Index("ix_guidance_extractions_cik", "cik"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    accession_no: Mapped[str] = mapped_column(String(20))
    document_name: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64))
    # guidance/items.py: PROCESSING_VERSION. A newer one re-processes the cached response.
    processing_version: Mapped[str] = mapped_column(String(16))
    items: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    dropped: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6))  # when the response was made
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Guidance(Base):
    """One guidance statement as issued in one earnings filing, with its verdict.

    The same statement in several documents of the filing (a press release and a CFO
    commentary, or a text and a table) is one row with several `guidance_sources`. Each
    filing's statements are their own rows, so guidance raised over three quarters is three
    rows. Figures are in dollars, dollars per share or percent (74.0 is 74%); `low` and
    `high` include plus-or-minus ranges. The verdict compares the target period's value as
    first reported (`guidance/matching.py`).
    """

    __tablename__ = "guidance"
    __table_args__ = (
        CheckConstraint(
            "metric IN ('revenue', 'gross_margin', 'operating_margin', 'operating_expenses', "
            "'operating_income', 'eps', 'capex', 'tax_rate', 'other')",
            name="metric",
        ),
        CheckConstraint("basis IN ('gaap', 'non_gaap', 'unspecified')", name="basis"),
        CheckConstraint(
            "measure IN ('level', 'yoy_growth', 'sequential_growth', 'share_of_revenue')",
            name="measure",
        ),
        CheckConstraint("unit IN ('usd', 'pct', 'usd_per_share', 'other')", name="unit"),
        CheckConstraint(
            "plus_minus_kind IS NULL OR plus_minus_kind IN ('absolute', 'percent')",
            name="plus_minus_kind",
        ),
        CheckConstraint(
            "verdict IN ('beat', 'met', 'missed', 'pending', 'not_comparable')", name="verdict"
        ),
        CheckConstraint(
            "target_fiscal_quarter IS NULL OR target_fiscal_quarter BETWEEN 1 AND 4",
            name="target_fiscal_quarter",
        ),
        Index("ix_guidance_company_issued", "cik", "fiscal_year", "fiscal_quarter"),
        Index("ix_guidance_accession_no", "accession_no"),
        CheckConstraint(
            "plausibility IS NULL OR plausibility IN ('passed', 'flagged', 'skipped')",
            name="plausibility",
        ),
        CheckConstraint("review IS NULL OR review IN ('ok', 'wrong')", name="review"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    accession_no: Mapped[str] = mapped_column(String(20))  # the issuing filing
    fiscal_year: Mapped[int] = mapped_column(SmallInteger)  # quarter the release reports
    fiscal_quarter: Mapped[int] = mapped_column(SmallInteger)
    issued_at: Mapped[date] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(32))
    metric_raw: Mapped[str] = mapped_column(Text)
    segment: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(String(16))
    measure: Mapped[str] = mapped_column(String(24))
    constant_currency: Mapped[bool] = mapped_column(Boolean)
    period_text: Mapped[str] = mapped_column(Text)
    target_fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    target_fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)  # None: full year
    low: Mapped[Decimal | None] = mapped_column(AMOUNT)
    high: Mapped[Decimal | None] = mapped_column(AMOUNT)
    point: Mapped[Decimal | None] = mapped_column(AMOUNT)
    plus_minus: Mapped[Decimal | None] = mapped_column(AMOUNT)
    plus_minus_kind: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(16))
    value_text: Mapped[str | None] = mapped_column(Text)
    qualitative: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str] = mapped_column(String(16))
    # Why the verdict is not_comparable or pending.
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    # Guidance without a GAAP or non-GAAP label, compared with the GAAP actual.
    basis_assumed: Mapped[bool] = mapped_column(Boolean, default=False)
    # The guided value against the prior period's actual (matching.check_plausibility):
    # "passed", "flagged" (needs review) or "skipped"; None when not comparable. The rule
    # applied and the numbers, or why it was skipped.
    plausibility: Mapped[str | None] = mapped_column(String(16))
    plausibility_rule: Mapped[str | None] = mapped_column(String(32))
    plausibility_note: Mapped[str | None] = mapped_column(Text)
    # A reviewer's decision from `guidance_reviews`: "ok" clears the flag, "wrong" leaves
    # the row out of verdicts.
    review: Mapped[str | None] = mapped_column(String(8))
    # Hash of what makes the statement (items.GuidanceItem.key): the same statement has
    # the same key when its filing is rebuilt, so review decisions survive.
    item_key: Mapped[str] = mapped_column(String(64))
    actual: Mapped[Decimal | None] = mapped_column(AMOUNT)  # in the guidance's unit
    # actual minus the guided reference (point, else range midpoint, else the bound), and
    # that difference as a percentage of the reference.
    difference: Mapped[Decimal | None] = mapped_column(AMOUNT)
    percent_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    actual_accession_no: Mapped[str | None] = mapped_column(String(20))
    actual_filed: Mapped[date | None] = mapped_column(Date)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GuidanceReview(Base):
    """A reviewer's decision on a guidance statement, keyed by its filing and item key
    rather than its row, so it survives rebuilding the filing's rows."""

    __tablename__ = "guidance_reviews"
    __table_args__ = (
        UniqueConstraint("accession_no", "item_key", name="uq_guidance_reviews_item"),
        CheckConstraint("decision IN ('ok', 'wrong')", name="decision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik", ondelete="CASCADE"))
    accession_no: Mapped[str] = mapped_column(String(20))
    item_key: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(8))
    # What was reviewed, for the record: metric, period, guided value, flag.
    summary: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GuidanceSource(Base):
    """Where a guidance statement is written: `quote` is `documents.text[char_start:
    char_end]` of the document named by accession number and document name."""

    __tablename__ = "guidance_sources"

    guidance_id: Mapped[int] = mapped_column(
        ForeignKey("guidance.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    accession_no: Mapped[str] = mapped_column(String(20))
    document_name: Mapped[str] = mapped_column(String(255))
    doc_type: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(512))
    section_title: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)


class LlmSpend(Base):
    """One paid LLM call, or a note of spending from before the ledger.

    Every call that reaches a provider is recorded with its model, tokens and cost,
    including calls whose answer could not be used. Cached responses cost nothing and are
    not recorded. `counted` rows count toward LLM_BUDGET_USD; notes (`counted` false)
    record earlier spending without counting it.
    """

    __tablename__ = "llm_spend"
    __table_args__ = (Index("ix_llm_spend_recorded_at", "recorded_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    command: Mapped[str] = mapped_column(String(64))  # "extract guidance", "eval guidance"
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    counted: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
