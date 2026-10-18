"""Plain records for EDGAR data. Everything past the source adapter works with these."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CompanyInfo:
    cik: int
    ticker: str
    name: str
    fiscal_year_end: str | None  # "MMDD", e.g. "0927"
    sic: str | None
    sic_description: str | None


@dataclass(frozen=True, slots=True)
class FilingRecord:
    accession_no: str
    cik: int
    form: str
    filing_date: date
    report_date: date | None
    acceptance_datetime: datetime | None
    primary_document: str | None
    items: str | None  # 8-K item codes, e.g. "2.02,9.01"
    is_xbrl: bool


@dataclass(frozen=True, slots=True)
class RawFact:
    """One XBRL value as reported in one filing. The same period appears once per filing
    that reports it, which is how restatements are kept."""

    taxonomy: str
    concept: str
    unit: str  # SEC unit string, e.g. "USD" or "USD/shares"
    value: Decimal
    period_start: date | None  # None for point-in-time (balance sheet) values
    period_end: date
    accession_no: str
    form: str
    filed: date
    fiscal_year: int | None  # fy/fp describe the filing, not the fact's own period
    fiscal_period: str | None
    frame: str | None = None

    @property
    def qualified_concept(self) -> str:
        return f"{self.taxonomy}:{self.concept}"


@dataclass(frozen=True, slots=True)
class FilingDocument:
    """One HTML document of a filing, with its content."""

    accession_no: str
    document_name: str  # file name within the filing, e.g. "q2fy27pr.htm"
    document_type: str  # SEC document type: the form ("10-Q") or the exhibit ("EX-99.1")
    description: str | None
    url: str
    html: str
