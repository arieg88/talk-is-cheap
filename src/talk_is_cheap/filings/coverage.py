"""Read parsed filing text: which documents and sections exist per quarter, and one section."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Company, Document, Filing, Section
from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.filings.documents import EARNINGS_ITEM, PERIODIC_FORMS, DocType
from talk_is_cheap.filings.sections import SectionType, has_guidance


@dataclass(slots=True)
class QuarterCoverage:
    """What one company's filings for one fiscal quarter yielded."""

    ticker: str
    quarter: FiscalQuarter
    earnings_8ks: int = 0  # item 2.02 8-Ks, except deliveries reports
    periodic_forms: list[str] = field(default_factory=list)  # "10-Q" / "10-K" filed
    doc_types: Counter[str] = field(default_factory=Counter)
    sections: set[tuple[str, str]] = field(default_factory=set)  # (doc_type, section_type)
    outlooks: list[bool] = field(default_factory=list)  # whether each outlook gives guidance

    @property
    def press_releases(self) -> int:
        return self.doc_types[DocType.PRESS_RELEASE]

    @property
    def deliveries_reports(self) -> int:
        return self.doc_types[DocType.DELIVERIES_REPORT]

    @property
    def outlook(self) -> bool:
        """An outlook section that gives guidance."""
        return any(self.outlooks)

    @property
    def outlook_without_guidance(self) -> bool:
        """Outlook sections, none of which gives guidance ("guidance is given on the call")."""
        return bool(self.outlooks) and not any(self.outlooks)

    @property
    def mdna(self) -> bool:
        return any(section == SectionType.MDNA for _, section in self.sections)

    @property
    def risk_factors(self) -> bool:
        return any(section == SectionType.RISK_FACTORS for _, section in self.sections)

    @property
    def periodic_form(self) -> str | None:
        return self.periodic_forms[0] if self.periodic_forms else None


async def coverage(
    session: AsyncSession, tickers: Sequence[str] | None = None
) -> list[QuarterCoverage]:
    """One row per company and fiscal quarter that has an item 2.02 8-K, 10-Q or 10-K,
    ordered by ticker and quarter. Filings not parsed yet are placed by filing date."""
    statement = select(Company).order_by(Company.ticker)
    if tickers:
        statement = statement.where(Company.ticker.in_([t.upper() for t in tickers]))
    companies = (await session.execute(statement)).scalars().all()

    rows: list[QuarterCoverage] = []
    for company in companies:
        if company.fiscal_year_end is not None:
            rows.extend(await _company_coverage(session, company, company.fiscal_year_end))
    return rows


async def _company_coverage(
    session: AsyncSession, company: Company, fiscal_year_end: str
) -> list[QuarterCoverage]:
    fiscal_calendar = FiscalCalendar.parse(fiscal_year_end)
    quarters: dict[FiscalQuarter, QuarterCoverage] = {}

    def row(quarter: FiscalQuarter) -> QuarterCoverage:
        return quarters.setdefault(quarter, QuarterCoverage(company.ticker, quarter))

    documents = (
        await session.execute(
            select(
                Document.accession_no,
                Document.doc_type,
                Document.fiscal_year,
                Document.fiscal_quarter,
            ).where(Document.cik == company.cik)
        )
    ).all()
    placed = {accession: FiscalQuarter(year, q) for accession, _, year, q in documents}
    deliveries = {a for a, doc_type, _, _ in documents if doc_type == DocType.DELIVERIES_REPORT}
    for _, doc_type, year, q in documents:
        row(FiscalQuarter(year, q)).doc_types[doc_type] += 1
    section_rows = await session.execute(
        select(
            Document.fiscal_year, Document.fiscal_quarter, Document.doc_type, Section.section_type
        )
        .join(Section, Section.document_id == Document.id)
        .where(Document.cik == company.cik)
        .distinct()
    )
    for year, q, doc_type, section_type in section_rows:
        row(FiscalQuarter(year, q)).sections.add((doc_type, section_type))
    outlooks = await session.execute(
        select(Document.fiscal_year, Document.fiscal_quarter, Section.text)
        .join(Section, Section.document_id == Document.id)
        .where(Document.cik == company.cik, Section.section_type == SectionType.OUTLOOK)
    )
    for year, q, text in outlooks:
        row(FiscalQuarter(year, q)).outlooks.append(has_guidance(text))

    filings = (await session.execute(select(Filing).where(Filing.cik == company.cik))).scalars()
    for filing in filings:
        items = {item.strip() for item in (filing.items or "").split(",")}
        earnings = filing.form == "8-K" and EARNINGS_ITEM in items
        if not earnings and filing.form not in PERIODIC_FORMS:
            continue
        quarter = placed.get(filing.accession_no) or _estimated_quarter(filing, fiscal_calendar)
        if earnings and filing.accession_no in deliveries:
            row(quarter)  # the quarter is listed, but a deliveries report is no release
        elif earnings:
            row(quarter).earnings_8ks += 1
        else:
            row(quarter).periodic_forms.append(filing.form)
    return [quarters[q] for q in sorted(quarters)]


def _estimated_quarter(filing: Filing, fiscal_calendar: FiscalCalendar) -> FiscalQuarter:
    if filing.form in PERIODIC_FORMS and filing.report_date is not None:
        return fiscal_calendar.quarter_of(filing.report_date)
    return fiscal_calendar.quarter_ended_before(filing.filing_date)


@dataclass(frozen=True, slots=True)
class FoundSection:
    ticker: str
    quarter: FiscalQuarter
    section_type: str
    title: str
    text: str
    char_start: int
    char_end: int
    doc_type: str
    document_name: str
    exhibit: str | None
    url: str
    filed_at: date
    period_end: date


# Releases first, then other earnings exhibits, then the 10-Q or 10-K.
_DOC_ORDER = {doc_type.value: rank for rank, doc_type in enumerate(DocType)}


async def find_sections(
    session: AsyncSession,
    cik: int,
    section_type: str,
    quarter: FiscalQuarter | None = None,
) -> list[FoundSection]:
    """Sections of one type for a company's fiscal quarter (the latest quarter that has one
    when `quarter` is None), press release first, then in document order."""
    statement = (
        select(Section, Document, Company.ticker)
        .join(Document, Section.document_id == Document.id)
        .join(Company, Company.cik == Document.cik)
        .where(Document.cik == cik, Section.section_type == section_type)
    )
    if quarter is None:
        latest = (
            select(Document.fiscal_year, Document.fiscal_quarter)
            .join(Section, Section.document_id == Document.id)
            .where(Document.cik == cik, Section.section_type == section_type)
            .order_by(Document.fiscal_year.desc(), Document.fiscal_quarter.desc())
            .limit(1)
        )
        found = (await session.execute(latest)).first()
        if found is None:
            return []
        quarter = FiscalQuarter(found[0], found[1])
    statement = statement.where(
        Document.fiscal_year == quarter.fiscal_year,
        Document.fiscal_quarter == quarter.fiscal_quarter,
    ).order_by(
        case(_DOC_ORDER, value=Document.doc_type, else_=len(_DOC_ORDER)),
        Document.filed_at.desc(),
        Document.document_name,
        Section.position,
    )
    return [
        FoundSection(
            ticker=ticker,
            quarter=quarter,
            section_type=section.section_type,
            title=section.title,
            text=section.text,
            char_start=section.char_start,
            char_end=section.char_end,
            doc_type=document.doc_type,
            document_name=document.document_name,
            exhibit=document.exhibit,
            url=document.url,
            filed_at=document.filed_at,
            period_end=document.period_end,
        )
        for section, document, ticker in (await session.execute(statement)).all()
    ]
