"""ORM models for companies, filings and financial data."""

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
