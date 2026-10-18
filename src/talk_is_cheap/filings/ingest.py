"""Parse the text of filings already in the `filings` table into documents and sections.

For each company, the 10-Qs, 10-Ks and earnings 8-Ks (item 2.02) in `filings` are
selected; amendments are not. A filing is skipped when its documents were parsed with the
current `PARSER_VERSION`; otherwise its documents are fetched, parsed and written in place
of any older version. Each company is written in one transaction, so an interrupted run
keeps the companies already finished. A filing that cannot be fetched is reported in the
company's result and retried on the next run; a company that fails does not stop the
others.
"""

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Company, Document, Filing
from talk_is_cheap.db.models import Section as SectionRow
from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.edgar.records import FilingRecord
from talk_is_cheap.edgar.source import EdgarSource, EdgarSourceError
from talk_is_cheap.filings.documents import (
    PERIODIC_FORMS,
    ParsedDocument,
    is_parsed_form,
    parse_filing,
)
from talk_is_cheap.universe import Universe

# Raise this whenever a change to conversion or section finding should re-parse documents.
PARSER_VERSION = "2"


@dataclass(frozen=True, slots=True)
class FilingsIngestResult:
    ticker: str
    name: str | None = None
    filings: int = 0  # 10-Qs, 10-Ks and earnings 8-Ks selected
    parsed: int = 0  # filings parsed in this run
    documents: int = 0
    sections: int = 0
    failed_filings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed_filings

    @property
    def skipped(self) -> int:
        return self.filings - self.parsed - len(self.failed_filings)


async def ingest_filings(
    source: EdgarSource,
    session_factory: async_sessionmaker[AsyncSession],
    universe: Universe,
    *,
    tickers: Sequence[str] | None = None,
    parser_version: str = PARSER_VERSION,
    on_result: Callable[[FilingsIngestResult], None] | None = None,
) -> list[FilingsIngestResult]:
    """Parse the selected tickers' filings (all in the universe by default)."""
    results = []
    for ticker in universe.select(tickers):
        try:
            result = await _ingest_company(source, session_factory, ticker, parser_version)
        except (EdgarSourceError, ValueError) as exc:
            result = FilingsIngestResult(ticker=ticker, error=f"{type(exc).__name__}: {exc}")
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


async def _ingest_company(
    source: EdgarSource,
    session_factory: async_sessionmaker[AsyncSession],
    ticker: str,
    parser_version: str,
) -> FilingsIngestResult:
    async with session_factory() as session:
        company = (
            await session.execute(select(Company).where(Company.ticker == ticker))
        ).scalar_one_or_none()
        if company is None:
            raise ValueError(f"{ticker} is not loaded; run `talk-is-cheap ingest edgar` first")
        if company.fiscal_year_end is None:
            raise ValueError(f"no fiscal year end recorded for {ticker}")
        filings = await _company_filings(session, company.cik)
        current = await _parsed_accessions(session, company.cik, parser_version)

    fiscal_calendar = FiscalCalendar.parse(company.fiscal_year_end)
    selected = [f for f in filings if is_parsed_form(f)]
    report_dates = _report_dates(filings, fiscal_calendar)

    parsed: dict[str, list[ParsedDocument]] = {}
    failed = []
    for filing in selected:
        if filing.accession_no in current:
            continue
        try:
            # edgartools is synchronous and parsing is CPU-bound: keep the event loop free.
            documents = await asyncio.to_thread(source.documents, filing)
        except EdgarSourceError:
            failed.append(filing.accession_no)
            continue
        parsed[filing.accession_no] = await asyncio.to_thread(
            parse_filing, filing, documents, fiscal_calendar, report_dates
        )

    async with session_factory() as session, session.begin():
        by_accession = {f.accession_no: f for f in selected}
        for accession, parsed_documents in parsed.items():
            await _replace_documents(
                session, by_accession[accession], parsed_documents, parser_version
            )

    return FilingsIngestResult(
        ticker=ticker,
        name=company.name,
        filings=len(selected),
        parsed=len(parsed),
        documents=sum(len(documents) for documents in parsed.values()),
        sections=sum(len(d.sections) for documents in parsed.values() for d in documents),
        failed_filings=failed,
    )


async def _company_filings(session: AsyncSession, cik: int) -> list[FilingRecord]:
    rows = (
        await session.execute(
            select(Filing)
            .where(Filing.cik == cik)
            .order_by(Filing.filing_date, Filing.accession_no)
        )
    ).scalars()
    return [
        FilingRecord(
            accession_no=row.accession_no,
            cik=row.cik,
            form=row.form,
            filing_date=row.filing_date,
            report_date=row.report_date,
            acceptance_datetime=row.acceptance_datetime,
            primary_document=row.primary_document,
            items=row.items,
            is_xbrl=row.is_xbrl,
        )
        for row in rows
    ]


async def _parsed_accessions(session: AsyncSession, cik: int, parser_version: str) -> set[str]:
    """Filings whose documents were all parsed with `parser_version`."""
    rows = await session.execute(
        select(Document.accession_no, Document.parser_version).where(Document.cik == cik)
    )
    versions: dict[str, set[str]] = {}
    for accession, version in rows:
        versions.setdefault(accession, set()).add(version)
    return {accession for accession, found in versions.items() if found == {parser_version}}


def _report_dates(
    filings: Iterable[FilingRecord], fiscal_calendar: FiscalCalendar
) -> dict[FiscalQuarter, date]:
    """The period of report of each quarter's 10-Q or 10-K."""
    dates = {}
    for filing in filings:
        if filing.form in PERIODIC_FORMS and filing.report_date is not None:
            dates[fiscal_calendar.quarter_of(filing.report_date)] = filing.report_date
    return dates


async def _replace_documents(
    session: AsyncSession,
    filing: FilingRecord,
    documents: Sequence[ParsedDocument],
    parser_version: str,
) -> None:
    await session.execute(delete(Document).where(Document.accession_no == filing.accession_no))
    for parsed in documents:
        document = Document(
            accession_no=filing.accession_no,
            cik=filing.cik,
            doc_type=parsed.doc_type.value,
            document_name=parsed.document_name,
            exhibit=parsed.exhibit,
            url=parsed.url,
            fiscal_year=parsed.quarter.fiscal_year,
            fiscal_quarter=parsed.quarter.fiscal_quarter,
            period_end=parsed.period_end,
            filed_at=filing.filing_date,
            text=parsed.text,
            content_hash=parsed.content_hash,
            parser_version=parser_version,
        )
        session.add(document)
        await session.flush()
        session.add_all(
            SectionRow(
                document_id=document.id,
                section_type=section.section_type.value,
                title=section.title,
                position=position,
                text=section.text(parsed.text),
                char_start=section.char_start,
                char_end=section.char_end,
            )
            for position, section in enumerate(parsed.sections)
        )
    await session.flush()
