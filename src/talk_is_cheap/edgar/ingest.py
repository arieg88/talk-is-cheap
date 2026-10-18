"""Load companies, filings and XBRL facts from EDGAR into the database.

Every write is idempotent: filings are upserted by accession number, raw facts are
insert-only (a value reported by a filing never changes), and the normalized
`financial_values` are rebuilt from all stored facts for the company. Re-running for
the same companies is safe, and adding companies to the universe only loads the new ones
when they are selected with `tickers`.
"""

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Company, Filing, FinancialValue, XbrlFact
from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.edgar.normalize import FinancialValue as NormalizedValue
from talk_is_cheap.edgar.normalize import (
    MetricSpec,
    load_metric_specs,
    normalize,
    required_concepts,
)
from talk_is_cheap.edgar.records import CompanyInfo, FilingRecord, RawFact
from talk_is_cheap.edgar.source import EdgarSource, EdgarSourceError
from talk_is_cheap.universe import Universe

BATCH_SIZE = 1000


@dataclass(frozen=True, slots=True)
class CompanyIngestResult:
    ticker: str
    cik: int | None = None
    name: str | None = None
    filings: int = 0
    new_facts: int = 0
    financial_values: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def ingest_edgar(
    source: EdgarSource,
    session_factory: async_sessionmaker[AsyncSession],
    universe: Universe,
    *,
    tickers: Sequence[str] | None = None,
    on_result: Callable[[CompanyIngestResult], None] | None = None,
) -> list[CompanyIngestResult]:
    """Ingest the selected tickers (all in the universe by default), one company at a time.

    Each company is written in its own transaction, so an interrupted run keeps the
    companies already finished. A failure for one company is recorded in its result and
    does not stop the others.
    """
    selected = universe.select(tickers)
    specs = load_metric_specs()

    results = []
    for ticker in selected:
        try:
            result = await _ingest_company(source, session_factory, universe, specs, ticker)
        except (EdgarSourceError, ValueError) as exc:
            result = CompanyIngestResult(ticker=ticker, error=f"{type(exc).__name__}: {exc}")
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


async def _ingest_company(
    source: EdgarSource,
    session_factory: async_sessionmaker[AsyncSession],
    universe: Universe,
    specs: dict[str, MetricSpec],
    ticker: str,
) -> CompanyIngestResult:
    # edgartools is synchronous; keep the event loop free while it waits on the SEC.
    company = await asyncio.to_thread(source.company, ticker)
    if company.fiscal_year_end is None:
        raise ValueError(f"no fiscal year end reported for {ticker}")
    fiscal_calendar = FiscalCalendar.parse(company.fiscal_year_end)
    cik = company.cik

    filings = await asyncio.to_thread(
        source.filings, cik, forms=universe.forms, since=universe.history_start
    )
    facts = await asyncio.to_thread(
        source.facts,
        cik,
        concepts=required_concepts(specs),
        period_end_from=earliest_fact_date(universe.history_start, fiscal_calendar),
    )

    async with session_factory() as session, session.begin():
        await _upsert_company(session, company)
        await _upsert_filings(session, filings)
        new_facts = await _insert_facts(session, cik, facts)
        stored = await _load_facts(session, cik)
        values = normalize(stored, specs, fiscal_calendar)
        await _replace_financial_values(session, cik, values)

    return CompanyIngestResult(
        ticker=ticker,
        cik=cik,
        name=company.name,
        filings=len(filings),
        new_facts=new_facts,
        financial_values=len(values),
    )


async def _upsert_company(session: AsyncSession, company: CompanyInfo) -> None:
    statement = insert(Company).values(
        cik=company.cik,
        ticker=company.ticker,
        name=company.name,
        fiscal_year_end=company.fiscal_year_end,
        sic=company.sic,
        sic_description=company.sic_description,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Company.cik],
            set_={
                "ticker": statement.excluded.ticker,
                "name": statement.excluded.name,
                "fiscal_year_end": statement.excluded.fiscal_year_end,
                "sic": statement.excluded.sic,
                "sic_description": statement.excluded.sic_description,
                "updated_at": func.now(),
            },
        )
    )


async def _upsert_filings(session: AsyncSession, filings: Iterable[FilingRecord]) -> None:
    rows = [
        {
            "accession_no": f.accession_no,
            "cik": f.cik,
            "form": f.form,
            "filing_date": f.filing_date,
            "report_date": f.report_date,
            "acceptance_datetime": f.acceptance_datetime,
            "primary_document": f.primary_document,
            "items": f.items,
            "is_xbrl": f.is_xbrl,
        }
        for f in {f.accession_no: f for f in filings}.values()
    ]
    for batch in _batches(rows):
        statement = insert(Filing).values(batch)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[Filing.accession_no],
                set_={
                    column: statement.excluded[column]
                    for column in (
                        "form",
                        "report_date",
                        "acceptance_datetime",
                        "primary_document",
                        "items",
                        "is_xbrl",
                    )
                },
            )
        )


async def _insert_facts(session: AsyncSession, cik: int, facts: Iterable[RawFact]) -> int:
    rows = [
        {
            "cik": cik,
            "taxonomy": f.taxonomy,
            "concept": f.concept,
            "unit": f.unit,
            "value": f.value,
            "period_start": f.period_start,
            "period_end": f.period_end,
            "accession_no": f.accession_no,
            "form": f.form,
            "filed": f.filed,
            "fiscal_year": f.fiscal_year,
            "fiscal_period": f.fiscal_period,
            "frame": f.frame,
        }
        for f in facts
    ]
    inserted = 0
    for batch in _batches(rows):
        result = await session.execute(
            insert(XbrlFact)
            .values(batch)
            .on_conflict_do_nothing(constraint="uq_xbrl_facts_identity")
            .returning(XbrlFact.id)
        )
        inserted += len(result.all())
    return inserted


async def _load_facts(session: AsyncSession, cik: int) -> list[RawFact]:
    rows = (await session.execute(select(XbrlFact).where(XbrlFact.cik == cik))).scalars()
    return [
        RawFact(
            taxonomy=r.taxonomy,
            concept=r.concept,
            unit=r.unit,
            value=r.value,
            period_start=r.period_start,
            period_end=r.period_end,
            accession_no=r.accession_no,
            form=r.form,
            filed=r.filed,
            fiscal_year=r.fiscal_year,
            fiscal_period=r.fiscal_period,
            frame=r.frame,
        )
        for r in rows
    ]


async def _replace_financial_values(
    session: AsyncSession, cik: int, values: Sequence[NormalizedValue]
) -> None:
    await session.execute(delete(FinancialValue).where(FinancialValue.cik == cik))
    rows = [
        {
            "cik": cik,
            "metric": v.metric,
            "period_type": v.period_type,
            "fiscal_year": v.fiscal_year,
            "fiscal_quarter": v.fiscal_quarter,
            "period_start": v.period_start,
            "period_end": v.period_end,
            "value": v.value,
            "unit": v.unit,
            "source_concept": v.source_concept,
            "accession_no": v.accession_no,
            "form": v.form,
            "filed": v.filed,
            "derived": v.derived,
            "derivation": v.derivation,
        }
        for v in values
    ]
    for batch in _batches(rows):
        await session.execute(insert(FinancialValue).values(batch))


def _batches(rows: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), BATCH_SIZE):
        yield rows[start : start + BATCH_SIZE]


def earliest_fact_date(history_start: date, fiscal_calendar: FiscalCalendar) -> date:
    """First period end kept for a company.

    Quarters are derived from year-to-date totals, so facts reach back to the start of the
    fiscal year that contains `history_start`.
    """
    return fiscal_calendar.year_start(fiscal_calendar.fiscal_year(history_start)) - timedelta(
        days=10
    )
