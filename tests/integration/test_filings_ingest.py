"""Filing text ingestion end to end against a real database, with an in-memory source."""

import asyncio
from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import Company, Document, Section
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.edgar.ingest import ingest_edgar
from talk_is_cheap.filings.coverage import coverage, find_sections
from talk_is_cheap.filings.ingest import PARSER_VERSION, ingest_filings
from tests.integration.test_ingest import (
    CIK,
    NEXT_Q1,
    Q1_EARNINGS,
    Q2_DELIVERIES,
    TICKER,
    UNIVERSE,
    FakeSource,
)

pytestmark = pytest.mark.integration

DOCUMENTS = 8  # five 10-Qs and 10-Ks, a release, a CFO commentary, a deliveries report
SECTIONS = 15  # MD&A and risk factors per report, 3 release, 1 commentary, 1 deliveries


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)

    async def clean() -> None:
        async with factory() as session, session.begin():
            await session.execute(delete(Company).where(Company.cik == CIK))

    await clean()
    await ingest_edgar(FakeSource(), factory, UNIVERSE, tickers=[TICKER])
    yield factory
    await clean()


async def counts(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with sessions() as session:
        documents = await session.scalar(
            select(func.count()).select_from(Document).where(Document.cik == CIK)
        )
        sections = await session.scalar(
            select(func.count())
            .select_from(Section)
            .join(Document, Document.id == Section.document_id)
            .where(Document.cik == CIK)
        )
    return int(documents or 0), int(sections or 0)


async def test_documents_and_sections_are_written(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    (result,) = await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    assert result.ok
    assert (result.filings, result.parsed, result.skipped) == (7, 7, 0)
    assert (result.documents, result.sections) == (DOCUMENTS, SECTIONS)
    assert await counts(sessions) == (DOCUMENTS, SECTIONS)

    async with sessions() as session:
        release = (
            await session.execute(
                select(Document).where(
                    Document.accession_no == Q1_EARNINGS[0], Document.document_name == "ex991.htm"
                )
            )
        ).scalar_one()
        assert (release.doc_type, release.exhibit) == ("press_release", "EX-99.1")
        assert (release.fiscal_year, release.fiscal_quarter) == (2025, 1)
        assert (release.period_end, release.filed_at) == (date(2025, 3, 31), Q1_EARNINGS[2])
        assert release.parser_version == PARSER_VERSION
        assert release.url.endswith("/000099000125000004/ex991.htm")

        rows = (
            await session.execute(
                select(Section, Document.text)
                .join(Document, Document.id == Section.document_id)
                .where(Document.cik == CIK)
            )
        ).all()
    for section, text in rows:
        assert section.text == text[section.char_start : section.char_end]
    outlook = next(s for s, _ in rows if s.section_type == "outlook")
    assert "expected to be $110" in outlook.text


async def test_rerunning_is_idempotent(sessions: async_sessionmaker[AsyncSession]) -> None:
    await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])
    source = FakeSource()
    (again,) = await ingest_filings(source, sessions, UNIVERSE, tickers=[TICKER])

    assert again.ok and (again.parsed, again.skipped, again.documents) == (0, 7, 0)
    assert source.fetched == []
    assert await counts(sessions) == (DOCUMENTS, SECTIONS)


async def test_a_new_parser_version_reparses(sessions: async_sessionmaker[AsyncSession]) -> None:
    await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])
    (reparsed,) = await ingest_filings(
        FakeSource(), sessions, UNIVERSE, tickers=[TICKER], parser_version="test-2"
    )

    assert (reparsed.parsed, reparsed.documents) == (7, DOCUMENTS)
    assert await counts(sessions) == (DOCUMENTS, SECTIONS)
    async with sessions() as session:
        versions = await session.scalars(
            select(Document.parser_version).where(Document.cik == CIK).distinct()
        )
        assert list(versions) == ["test-2"]


async def test_failures_do_not_stop_other_filings_or_companies(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    failing = FakeSource(failing=frozenset({NEXT_Q1[0]}))
    reported = []
    missing, company = await ingest_filings(
        failing, sessions, UNIVERSE, tickers=["MISSING", TICKER], on_result=reported.append
    )

    assert not missing.ok and "not loaded" in (missing.error or "")
    assert not company.ok and company.error is None
    assert company.failed_filings == [NEXT_Q1[0]]
    assert (company.parsed, company.documents) == (6, DOCUMENTS - 1)
    assert reported == [missing, company]

    (retried,) = await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])
    assert retried.ok and (retried.parsed, retried.skipped) == (1, 6)
    assert await counts(sessions) == (DOCUMENTS, SECTIONS)


async def test_coverage_and_sections(sessions: async_sessionmaker[AsyncSession]) -> None:
    await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    async with sessions() as session:
        rows = {r.quarter.label: r for r in await coverage(session, [TICKER])}
        latest = await find_sections(session, CIK, "mdna")
        outlook = await find_sections(session, CIK, "outlook", FiscalQuarter(2025, 1))
        none = await find_sections(session, CIK, "outlook", FiscalQuarter(2025, 2))

    assert list(rows) == ["FY2025Q1", "FY2025Q2", "FY2025Q3", "FY2025Q4", "FY2026Q1"]
    first = rows["FY2025Q1"]
    assert (first.earnings_8ks, first.press_releases, first.periodic_form) == (1, 1, "10-Q")
    assert first.outlook and not first.outlook_without_guidance
    assert first.mdna and first.risk_factors
    second = rows["FY2025Q2"]
    assert (second.earnings_8ks, second.press_releases, second.deliveries_reports) == (0, 0, 1)
    assert rows["FY2025Q4"].periodic_form == "10-K" and not rows["FY2025Q4"].press_releases

    assert latest[0].quarter == FiscalQuarter(2026, 1)
    assert [(s.doc_type, s.document_name) for s in outlook] == [("press_release", "ex991.htm")]
    assert none == []


async def test_deliveries_reports_are_not_releases(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    async with sessions() as session:
        report = (
            await session.execute(select(Document).where(Document.accession_no == Q2_DELIVERIES[0]))
        ).scalar_one()
        results = await find_sections(session, CIK, "results", FiscalQuarter(2025, 2))
        outlook = await find_sections(session, CIK, "outlook", FiscalQuarter(2025, 2))
    assert (report.doc_type, report.fiscal_year, report.fiscal_quarter) == (
        "deliveries_report",
        2025,
        2,
    )
    assert results == [] and outlook == []


async def test_cli_coverage_and_show(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    await ingest_filings(FakeSource(), sessions, UNIVERSE, tickers=[TICKER])

    # The CLI runs its own event loop, so it runs in a worker thread here.
    def run(*argv: str) -> int:
        return main(list(argv))

    assert await asyncio.to_thread(run, "coverage", "--tickers", TICKER) == 0
    table = capsys.readouterr().out
    assert f"{TICKER:<7}FY2025Q1   yes      yes          10-Q  yes   yes" in table
    assert f"{TICKER:<7}FY2025Q2   -        -            10-Q  yes   yes" in table
    assert f"{TICKER:<7}FY2025Q4   -        -            10-K  yes   yes" in table
    assert "press release                1/1  earnings 8-Ks" in table
    assert "outlook (no guidance)        0/1" in table
    assert "MD&A                         5/5" in table and "100.0%" in table
    assert "not counted as releases: 1 deliveries reports" in table

    assert await asyncio.to_thread(run, "show", TICKER, "FY2025Q1", "outlook") == 0
    shown = capsys.readouterr().out
    assert shown.startswith(
        f"{TICKER} FY2025Q1 outlook: EX-99.1 ex991.htm, filed 2025-04-24, period ended 2025-03-31\n"
    )
    assert "https://www.sec.gov/Archives/edgar/data/990001/000099000125000004/ex991.htm" in shown
    assert "\n--- Outlook (characters " in shown
    assert "Second quarter revenue is expected to be $110" in shown

    assert await asyncio.to_thread(run, "show", TICKER, "FY2025Q1", "results") == 0
    assert "--- Talk Is Cheap Reports First Quarter 2025 Results" in capsys.readouterr().out

    assert await asyncio.to_thread(run, "show", TICKER, "latest", "risk_factors") == 0
    assert "--- Item 1A. Risk Factors (characters" in capsys.readouterr().out

    assert await asyncio.to_thread(run, "show", TICKER, "FY2025Q2", "results") == 1
    assert "no results section" in capsys.readouterr().err
