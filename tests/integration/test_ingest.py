"""EDGAR ingestion end to end against a real database, with an in-memory source."""

import asyncio
from collections.abc import AsyncIterator, Collection, Sequence
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import Company, Filing, FinancialValue, XbrlFact
from talk_is_cheap.edgar.ingest import ingest_edgar
from talk_is_cheap.edgar.records import CompanyInfo, FilingRecord, RawFact
from talk_is_cheap.edgar.source import EdgarSourceError
from talk_is_cheap.financials import (
    company_summaries,
    find_company,
    quarterly_financials,
    restatements,
)
from talk_is_cheap.universe import Universe

pytestmark = pytest.mark.integration

CIK = 990001  # outside the real CIK range used by the companies in universe.yaml
TICKER = "TICA"

Q1 = ("0000990001-25-000001", "10-Q", date(2025, 4, 30))
Q2 = ("0000990001-25-000002", "10-Q", date(2025, 7, 30))
Q3 = ("0000990001-25-000003", "10-Q", date(2025, 10, 30))
FY = ("0000990001-26-000001", "10-K", date(2026, 2, 20))
NEXT_Q1 = ("0000990001-26-000002", "10-Q", date(2026, 4, 29))


def fact(
    concept: str,
    value: str,
    start: date,
    end: date,
    filing: tuple[str, str, date],
    unit: str = "USD",
) -> RawFact:
    accession, form, filed = filing
    return RawFact(
        taxonomy="us-gaap",
        concept=concept,
        unit=unit,
        value=Decimal(value),
        period_start=start,
        period_end=end,
        accession_no=accession,
        form=form,
        filed=filed,
        fiscal_year=None,
        fiscal_period=None,
    )


JAN1 = date(2025, 1, 1)
FACTS = [
    # Revenue: three reported quarters, the full year, then a restated Q1 comparative.
    fact("Revenues", "100", JAN1, date(2025, 3, 31), Q1),
    fact("Revenues", "110", date(2025, 4, 1), date(2025, 6, 30), Q2),
    fact("Revenues", "210", JAN1, date(2025, 6, 30), Q2),
    fact("Revenues", "120", date(2025, 7, 1), date(2025, 9, 30), Q3),
    fact("Revenues", "330", JAN1, date(2025, 9, 30), Q3),
    fact("Revenues", "460", JAN1, date(2025, 12, 31), FY),
    fact("Revenues", "140", date(2026, 1, 1), date(2026, 3, 31), NEXT_Q1),
    fact("Revenues", "105", JAN1, date(2025, 3, 31), NEXT_Q1),
    # Cost of revenue only: gross profit comes from the fallback.
    fact("CostOfRevenue", "40", JAN1, date(2025, 3, 31), Q1),
    # Cash flow is reported year to date only.
    fact("NetCashProvidedByUsedInOperatingActivities", "30", JAN1, date(2025, 3, 31), Q1),
    fact("NetCashProvidedByUsedInOperatingActivities", "70", JAN1, date(2025, 6, 30), Q2),
    fact("EarningsPerShareDiluted", "0.50", JAN1, date(2025, 3, 31), Q1, unit="USD/shares"),
]

FILINGS = [
    FilingRecord(
        accession_no=accession,
        cik=CIK,
        form=form,
        filing_date=filed,
        report_date=None,
        acceptance_datetime=None,
        primary_document=None,
        items=None,
        is_xbrl=True,
    )
    for accession, form, filed in (Q1, Q2, Q3, FY, NEXT_Q1)
]


class FakeSource:
    def company(self, ticker: str) -> CompanyInfo:
        if ticker != TICKER:
            raise EdgarSourceError(f"{ticker} was not found on EDGAR")
        return CompanyInfo(
            cik=CIK,
            ticker=TICKER,
            name="Talk Is Cheap Test Co",
            fiscal_year_end="1231",
            sic=None,
            sic_description=None,
        )

    def filings(self, cik: int, *, forms: Sequence[str], since: date) -> list[FilingRecord]:
        return [f for f in FILINGS if f.form in forms and f.filing_date >= since]

    def facts(self, cik: int, *, concepts: Collection[str], period_end_from: date) -> list[RawFact]:
        return [
            f for f in FACTS if f.qualified_concept in concepts and f.period_end >= period_end_from
        ]


UNIVERSE = Universe.model_validate(
    {
        "history_start": "2025-01-01",
        "forms": ["10-K", "10-Q", "8-K"],
        "companies": [{"ticker": TICKER}, {"ticker": "MISSING"}],
    }
)


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)

    async def clean() -> None:
        async with factory() as session, session.begin():
            await session.execute(delete(Company).where(Company.cik == CIK))

    await clean()
    yield factory
    await clean()


async def count(session: AsyncSession, model: type, cik: int) -> int:
    statement = select(func.count()).select_from(model).where(model.cik == cik)  # type: ignore[attr-defined]
    return int((await session.execute(statement)).scalar_one())


async def test_ingest_loads_company_filings_facts_and_values(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    results = await ingest_edgar(FakeSource(), sessions, UNIVERSE)

    ok, failed = results
    assert ok.ok and ok.cik == CIK and ok.filings == 5 and ok.new_facts == len(FACTS)
    assert not failed.ok and "not found" in (failed.error or "")

    async with sessions() as session:
        assert await count(session, Filing, CIK) == 5
        assert await count(session, XbrlFact, CIK) == len(FACTS)
        assert await count(session, FinancialValue, CIK) == ok.financial_values > 0


async def test_rerunning_is_idempotent(sessions: async_sessionmaker[AsyncSession]) -> None:
    (first,) = await ingest_edgar(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])
    (second,) = await ingest_edgar(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    assert second.new_facts == 0
    assert second.financial_values == first.financial_values
    async with sessions() as session:
        assert await count(session, Filing, CIK) == 5
        assert await count(session, XbrlFact, CIK) == len(FACTS)
        assert await count(session, FinancialValue, CIK) == first.financial_values


async def test_quarters_as_first_reported_and_latest(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await ingest_edgar(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    async with sessions() as session:
        first = {q.label: q for q in await quarterly_financials(session, CIK, vintage="first")}
        latest = {q.label: q for q in await quarterly_financials(session, CIK, vintage="latest")}

    assert list(first) == ["FY2026 Q1", "FY2025 Q4", "FY2025 Q3", "FY2025 Q2", "FY2025 Q1"]
    assert first["FY2025 Q1"].get("revenue") == Decimal("100")
    assert latest["FY2025 Q1"].get("revenue") == Decimal("105")

    q4 = first["FY2025 Q4"].metrics["revenue"]
    assert (q4.value, q4.derived) == (Decimal("130"), True)
    assert first["FY2025 Q2"].get("operating_cash_flow") == Decimal("40")
    assert first["FY2025 Q1"].gross_margin == Decimal("0.6")
    assert first["FY2025 Q1"].get("eps_diluted") == Decimal("0.5")


async def test_restatements_and_summaries(sessions: async_sessionmaker[AsyncSession]) -> None:
    await ingest_edgar(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    async with sessions() as session:
        changes = await restatements(session, CIK)
        summary = next(s for s in await company_summaries(session) if s.cik == CIK)
        company = await find_company(session, TICKER.lower())

    assert [(c.metric, c.fiscal_year, c.fiscal_quarter) for c in changes] == [("revenue", 2025, 1)]
    assert changes[0].change == Decimal("0.05")
    assert (summary.filings, summary.latest_quarter) == (5, "FY2026 Q1")
    assert company is not None and company.name == "Talk Is Cheap Test Co"


async def test_cli_reports_loaded_data(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    await ingest_edgar(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    # The CLI runs its own event loop, so it runs in a worker thread here.
    def run(*argv: str) -> int:
        return main(list(argv))

    assert (
        await asyncio.to_thread(run, "financials", TICKER, "--vintage", "latest", "--quarters", "2")
        == 0
    )
    table = capsys.readouterr().out
    assert "latest reported" in table
    assert "FY2026 Q1" in table and "FY2025 Q4" in table and "FY2025 Q3" not in table

    assert await asyncio.to_thread(run, "restatements", TICKER) == 0
    assert "revenue" in capsys.readouterr().out

    assert await asyncio.to_thread(run, "companies") == 0
    assert TICKER in capsys.readouterr().out

    assert await asyncio.to_thread(run, "financials", "NOPE") == 1
