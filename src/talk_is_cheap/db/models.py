"""ORM models for companies, filings, financial data, filing text and news."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
