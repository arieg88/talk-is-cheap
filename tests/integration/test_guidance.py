"""Guidance extraction end to end against a real database, with a fake model: a company
whose releases give guidance in a press release and a CFO commentary, results reported
later, and one outlook that only points to the earnings call."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.cli import main
from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    Company,
    Document,
    Filing,
    FinancialValue,
    Guidance,
    GuidanceExtraction,
    GuidanceSource,
    LlmSpend,
    Section,
    StockSplit,
)
from talk_is_cheap.guidance.extract import ExtractResult, extract_guidance
from talk_is_cheap.guidance.items import PROCESSING_VERSION
from talk_is_cheap.guidance.llm import Budget, Pricing, ResponseCache, Usage
from talk_is_cheap.guidance.matching import ConfiguredSplit, sync_splits
from talk_is_cheap.guidance.schema import ExtractedItem, Extraction
from talk_is_cheap.spend import DatabaseLedger, add_note, counted_spend, spend_report
from tests.guidance_fakes import FakeModel

pytestmark = pytest.mark.integration

CIK = 990_001
TICKER = "GUID"
B = Decimal(10) ** 9
PRICING = Pricing(Decimal("2.00"), Decimal("10.00"))

Q1_RELEASE = "0000990001-26-000010"
Q2_RELEASE = "0000990001-26-000020"
Q3_RELEASE = "0000990001-26-000030"
Q2_10Q = "0000990001-26-000025"

OUTLOOK_Q1 = (
    "## Outlook\n\n"
    "For the second quarter of 2026, Guidco expects revenue to be $10.0 billion, plus or "
    "minus 2%. Non-GAAP gross margin is expected to be approximately 55%.\n\n"
    "For the full year 2026, revenue is expected to be between $40 billion and $42 billion."
)
COMMENTARY_Q1 = (
    "## Outlook\n\nWe expect second quarter 2026 revenue to be $10.0 billion, plus or minus 2%."
)
OUTLOOK_Q2 = (
    "## Outlook\n\n"
    "For the third quarter of 2026, Guidco expects revenue to be $11.0 billion, plus or "
    "minus 2%.\n\n"
    "We now expect full year 2026 revenue to be between $42 billion and $44 billion."
)
CALL_ONLY = (
    "## Business Outlook\n\n"
    "Guidco will provide forward-looking guidance on its earnings conference call."
)


def item(**values: Any) -> ExtractedItem:
    base: dict[str, Any] = {
        "metric": "revenue",
        "metric_raw": "revenue",
        "segment": None,
        "basis": "unspecified",
        "measure": "level",
        "constant_currency": False,
        "qualitative": None,
        "conditions": None,
    }
    return ExtractedItem(**{**base, **values})


# What the fake model answers for each document, recognized by its file name.
ANSWERS = {
    "release-q1.htm": [
        item(period_text="second quarter of 2026", value_text="$10.0 billion, plus or minus 2%",
             conditions="no acquisitions assumed",
             quote="Guidco expects revenue to be $10.0 billion, plus or minus 2%."),
        item(metric="gross_margin", basis="non_gaap", period_text="second quarter of 2026",
             value_text="approximately 55%",
             quote="Non-GAAP gross margin is expected to be approximately 55%."),
        item(period_text="full year 2026", value_text="between $40 billion and $42 billion",
             quote="For the full year 2026, revenue is expected to be between $40 billion and "
                   "$42 billion."),
        item(period_text="second quarter of 2026", value_text="$12 billion",
             quote="Revenue is expected to be $12 billion."),  # not in the text: dropped
    ],
    "commentary-q1.htm": [
        item(period_text="second quarter 2026", value_text="$10.0 billion, plus or minus 2%",
             quote="We expect second quarter 2026 revenue to be $10.0 billion, plus or minus "
                   "2%."),
    ],
    "release-q2.htm": [
        item(period_text="third quarter of 2026", value_text="$11.0 billion, plus or minus 2%",
             quote="Guidco expects revenue to be $11.0 billion, plus or minus 2%."),
        item(period_text="full year 2026", value_text="between $42 billion and $44 billion",
             quote="We now expect full year 2026 revenue to be between $42 billion and $44 "
                   "billion."),
    ],
}  # fmt: skip


def respond(prompt: str) -> Extraction:
    for name, items in ANSWERS.items():
        if name in prompt:
            return Extraction(items=items)
    raise AssertionError("prompt for an unexpected document")


def model() -> FakeModel:
    return FakeModel(respond)


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(Guidance).where(Guidance.cik == CIK))
        await session.execute(delete(Company).where(Company.cik == CIK))
        await session.execute(delete(LlmSpend))


async def _document(
    session: AsyncSession,
    accession: str,
    name: str,
    doc_type: str,
    quarter: int,
    filed: date,
    sections: list[tuple[str, str, str]],
) -> None:
    prefix = "Guidco reports results.\n\n"
    text = prefix + "\n\n".join(body for _, _, body in sections)
    document = Document(
        accession_no=accession,
        cik=CIK,
        doc_type=doc_type,
        document_name=name,
        exhibit="EX-99.1",
        url=f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession.replace('-', '')}/{name}",
        fiscal_year=2026,
        fiscal_quarter=quarter,
        period_end=date(2026, 3 * quarter, 28),
        filed_at=filed,
        text=text,
        content_hash="x",
        parser_version="test",
    )
    session.add(document)
    await session.flush()
    position = len(prefix)
    for index, (section_type, title, body) in enumerate(sections):
        session.add(
            Section(
                document_id=document.id,
                section_type=section_type,
                title=title,
                position=index,
                text=body,
                char_start=position,
                char_end=position + len(body),
            )
        )
        position += len(body) + 2


def _value(metric: str, year: int, quarter: int | None, value: Decimal, accession: str,
           filed: date) -> FinancialValue:  # fmt: skip
    return FinancialValue(
        cik=CIK,
        metric=metric,
        period_type="quarter" if quarter else "year",
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_start=date(year, 1, 1),
        period_end=date(year, 3 * (quarter or 4), 28),
        value=value,
        unit="USD",
        source_concept="us-gaap:Revenues",
        accession_no=accession,
        form="10-Q",
        filed=filed,
        derived=False,
        derivation=None,
    )


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    async with factory() as session, session.begin():
        session.add(Company(cik=CIK, ticker=TICKER, name="Guidco", fiscal_year_end="1231"))
        for accession, filed in (
            (Q1_RELEASE, date(2026, 4, 20)),
            (Q2_RELEASE, date(2026, 7, 20)),
            (Q3_RELEASE, date(2026, 10, 20)),
            (Q2_10Q, date(2026, 8, 1)),
        ):
            session.add(Filing(accession_no=accession, cik=CIK, form="8-K", filing_date=filed))
        await session.flush()
        for args in (
            (Q1_RELEASE, "release-q1.htm", "press_release", 1, date(2026, 4, 20),
             [("results", "Results", "Revenue was $9 billion."),
              ("outlook", "Outlook", OUTLOOK_Q1)]),
            (Q1_RELEASE, "commentary-q1.htm", "earnings_exhibit", 1, date(2026, 4, 20),
             [("outlook", "Outlook", COMMENTARY_Q1)]),
            (Q2_RELEASE, "release-q2.htm", "press_release", 2, date(2026, 7, 20),
             [("outlook", "Outlook", OUTLOOK_Q2)]),
            (Q3_RELEASE, "release-q3.htm", "press_release", 3, date(2026, 10, 20),
             [("outlook", "Business Outlook", CALL_ONLY)]),
        ):  # fmt: skip
            await _document(session, *args)  # type: ignore[arg-type]
        session.add_all(
            [
                # Q2 revenue as first reported, then restated.
                _value("revenue", 2026, 2, Decimal("10.3") * B, Q2_10Q, date(2026, 8, 1)),
                _value("revenue", 2026, 2, Decimal("9.0") * B, "restated", date(2027, 2, 1)),
            ]
        )
    yield factory
    await _clean(factory)


async def run(
    sessions: async_sessionmaker[AsyncSession],
    fake: FakeModel,
    cache_dir: Path,
    *,
    budget: Budget | None = None,
    missing_only: bool = False,
    ok: bool = True,
) -> ExtractResult:
    (result,) = await extract_guidance(
        sessions,
        fake,
        ResponseCache(cache_dir),
        budget or Budget(Decimal(1), PRICING),
        ciks=[CIK],
        names={TICKER: "Guidco"},
        missing_only=missing_only,
    )
    assert result.ok == ok, result.errors
    return result


async def rows(sessions: async_sessionmaker[AsyncSession]) -> list[Guidance]:
    async with sessions() as session:
        statement = (
            select(Guidance)
            .where(Guidance.cik == CIK)
            .order_by(
                Guidance.issued_at, Guidance.target_fiscal_quarter.nulls_last(), Guidance.metric
            )
        )
        return list((await session.scalars(statement)).all())


async def test_extraction_end_to_end(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    fake = model()
    result = await run(sessions, fake, tmp_path)

    # The outlook that only points to the call is not sent to the model.
    assert (result.documents, result.called, result.cached) == (3, 3, 0)
    assert all("release-q3.htm" not in p for p in fake.prompts)
    assert result.dropped == 1  # the invented quote
    assert result.cost == Decimal("0.021") and result.usage.input_tokens == 3_000
    # The prompt names the company, the quarter reported and the outlook sections only.
    q1_prompt = next(p for p in fake.prompts if "release-q1.htm" in p)
    assert "Guidco (GUID)" in q1_prompt and "fiscal 2026 Q1" in q1_prompt
    assert OUTLOOK_Q1 in q1_prompt and "Revenue was $9 billion." not in q1_prompt

    found = await rows(sessions)
    summary = [
        (r.accession_no, r.metric, r.basis, r.target_fiscal_year, r.target_fiscal_quarter,
         r.verdict) for r in found
    ]  # fmt: skip
    assert summary == [
        (Q1_RELEASE, "gross_margin", "non_gaap", 2026, 2, "not_comparable"),
        # Compared with 10.3 as first reported, above the 9.8-10.2 range.
        (Q1_RELEASE, "revenue", "unspecified", 2026, 2, "beat"),
        (Q1_RELEASE, "revenue", "unspecified", 2026, None, "pending"),
        (Q2_RELEASE, "revenue", "unspecified", 2026, 3, "pending"),
        # Raised full-year guidance is a row of its own.
        (Q2_RELEASE, "revenue", "unspecified", 2026, None, "pending"),
    ]
    revenue_q2 = found[1]
    assert (revenue_q2.low, revenue_q2.high, revenue_q2.point) == (
        Decimal("9.8") * B,
        Decimal("10.2") * B,
        Decimal(10) * B,
    )


async def test_first_reported_values_decide(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await run(sessions, model(), tmp_path)
    revenue_q2 = (await rows(sessions))[1]
    assert revenue_q2.actual == Decimal("10.3") * B  # not the later 9.0 restatement
    assert revenue_q2.verdict == "beat"
    assert revenue_q2.difference == Decimal("0.3") * B
    assert revenue_q2.percent_difference == Decimal("3.0000")
    assert (revenue_q2.actual_accession_no, revenue_q2.actual_filed) == (Q2_10Q, date(2026, 8, 1))
    margin = (await rows(sessions))[0]
    assert margin.verdict_reason == "non-GAAP guidance; reported values are GAAP"
    # Revenue without a stated basis is compared with the GAAP actual, and flagged.
    assert (revenue_q2.basis, revenue_q2.basis_assumed) == ("unspecified", True)
    assert margin.basis_assumed is False
    assert revenue_q2.conditions == "no acquisitions assumed"


async def test_duplicates_across_documents_are_one_row_with_cited_sources(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await run(sessions, model(), tmp_path)
    revenue_q2 = (await rows(sessions))[1]
    async with sessions() as session:
        sources = (
            await session.scalars(
                select(GuidanceSource)
                .where(GuidanceSource.guidance_id == revenue_q2.id)
                .order_by(GuidanceSource.position)
            )
        ).all()
        texts = {
            d.document_name: d.text
            for d in (await session.scalars(select(Document).where(Document.cik == CIK))).all()
        }
    assert [s.document_name for s in sources] == ["release-q1.htm", "commentary-q1.htm"]
    for source in sources:
        assert texts[source.document_name][source.char_start : source.char_end] == source.quote
        assert source.url.endswith(source.document_name)


async def test_reruns_skip_documents_and_reuse_cached_answers(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    first = model()
    await run(sessions, first, tmp_path)
    before = [(r.metric, r.target_fiscal_quarter, r.verdict) for r in await rows(sessions)]

    again = model()
    result = await run(sessions, again, tmp_path)
    assert (result.up_to_date, result.cached, result.called) == (3, 0, 0)
    assert again.prompts == []

    # Without stored extractions, answers come from the cache: nothing is called or spent.
    async with sessions() as session, session.begin():
        await session.execute(delete(GuidanceExtraction).where(GuidanceExtraction.cik == CIK))
    cached = model()
    result = await run(sessions, cached, tmp_path)
    assert (result.cached, result.called, result.cost) == (3, 0, Decimal(0))
    assert cached.prompts == []
    assert [(r.metric, r.target_fiscal_quarter, r.verdict) for r in await rows(sessions)] == before


async def test_changed_text_is_extracted_again_and_old_rows_replaced(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await run(sessions, model(), tmp_path)
    async with sessions() as session, session.begin():
        # Re-parsing removed the commentary's outlook.
        await session.execute(delete(Document).where(Document.document_name == "commentary-q1.htm"))
    fake = model()
    result = await run(sessions, fake, tmp_path)
    assert (result.documents, result.up_to_date, result.called) == (2, 2, 0)
    revenue_q2 = (await rows(sessions))[1]
    async with sessions() as session:
        names = (
            await session.scalars(
                select(GuidanceSource.document_name).where(
                    GuidanceSource.guidance_id == revenue_q2.id
                )
            )
        ).all()
        extractions = await session.scalar(
            select(func.count())
            .select_from(GuidanceExtraction)
            .where(GuidanceExtraction.cik == CIK)
        )
    assert names == ["release-q1.htm"] and extractions == 2

    async with sessions() as session, session.begin():
        await session.execute(
            update(Section)
            .where(Section.text == OUTLOOK_Q2)
            .values(text=OUTLOOK_Q2 + "\n\nThis outlook excludes acquisitions.")
        )
    changed = model()
    result = await run(sessions, changed, tmp_path)
    assert result.called == 1 and "release-q2.htm" in changed.prompts[0]


async def test_new_results_turn_pending_into_verdicts(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await run(sessions, model(), tmp_path)
    async with sessions() as session, session.begin():
        session.add(_value("revenue", 2026, 3, Decimal("10.5") * B, "q3-10q", date(2026, 11, 1)))
    result = await run(sessions, model(), tmp_path)
    assert result.called == 0
    q3 = next(r for r in await rows(sessions) if r.target_fiscal_quarter == 3)
    assert (q3.verdict, q3.actual) == ("missed", Decimal("10.5") * B)  # below 10.78


async def test_splits_are_synced_from_configuration(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    split = ConfiguredSplit(TICKER, date(2026, 6, 1), Decimal(4), "test")
    async with sessions() as session, session.begin():
        unknown = ConfiguredSplit("ZZZZ", date(2026, 1, 1), Decimal(2), "x")
        assert await sync_splits(session, [split, unknown]) == 1
    async with sessions() as session, session.begin():
        await sync_splits(session, [])
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(StockSplit).where(StockSplit.cik == CIK)
            )
            == 0
        )


@pytest.fixture
def llm_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-5-5")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_cli_dry_run_extract_and_report(
    sessions: async_sessionmaker[AsyncSession],
    llm_settings: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def cli(*args: str) -> int:
        # main() runs its own event loop, so it runs in a thread.
        return await asyncio.to_thread(main, list(args))

    assert await cli("extract", "guidance", "--tickers", TICKER, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run: model claude-haiku-5-5@low, no calls made" in out
    assert "3 calls" in out and "estimated cost at most $0.0" in out

    # Calls needed and no key: stops before calling anything.
    assert await cli("extract", "guidance", "--tickers", TICKER) == 1
    assert "no API key" in capsys.readouterr().err

    # With every answer cached, no key is needed.
    fake = FakeModel(respond, "claude-haiku-5-5@low")
    await run(sessions, fake, llm_settings)
    assert await cli("extract", "guidance", "--tickers", TICKER) == 0
    out = capsys.readouterr().out
    assert "3 documents (3 up to date, 0 cached, 0 called)" in out
    assert "done: 5 guidance rows" in out and "$0.0000" in out

    assert await cli("guidance", TICKER, "--metric", "revenue") == 0
    out = capsys.readouterr().out
    assert "FY2026Q1  FY2026Q2  revenue" in out
    assert "$9.8B to $10.2B" in out and "$10.3B" in out and "beat" in out
    assert "+$300.0M (+3.0%)" in out
    assert "release-q1.htm chars" in out
    assert "basis not stated, compared as GAAP" in out
    assert "conditions: no acquisitions assumed" in out


async def test_new_post_processing_reprocesses_from_the_cache(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await run(sessions, model(), tmp_path)
    async with sessions() as session, session.begin():
        await session.execute(
            update(GuidanceExtraction)
            .where(GuidanceExtraction.cik == CIK)
            .values(processing_version="1")
        )
    same = model()
    result = await run(sessions, same, tmp_path)
    assert (result.reprocessed, result.called, result.cached) == (3, 0, 0)
    assert same.prompts == []
    # With --missing-only, documents of another model are kept and reprocessed from that
    # model's cached answers.
    async with sessions() as session, session.begin():
        await session.execute(
            update(GuidanceExtraction)
            .where(GuidanceExtraction.cik == CIK)
            .values(processing_version="1")
        )
    other = FakeModel(respond, "another-model@low")
    result = await run(sessions, other, tmp_path, missing_only=True)
    assert (result.reprocessed, result.called) == (3, 0) and other.prompts == []
    async with sessions() as session:
        stored = (
            await session.scalars(select(GuidanceExtraction).where(GuidanceExtraction.cik == CIK))
        ).all()
    assert {(e.model, e.processing_version) for e in stored} == {
        ("fake-model@test", PROCESSING_VERSION)
    }


async def test_missing_only_keeps_documents_extracted_with_another_model(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await run(sessions, model(), tmp_path)
    other = FakeModel(respond, "another-model@low")
    result = await run(sessions, other, tmp_path, missing_only=True)
    assert (result.up_to_date, result.called) == (3, 0) and other.prompts == []
    result = await run(sessions, other, tmp_path)  # by default, the run's model decides
    assert result.called == 3


async def test_paid_calls_go_to_the_ledger_and_the_budget_is_enforced(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with sessions() as session, session.begin():
        await add_note(session, "old-model", Usage(10, 5), Decimal("5.2"), "before the ledger")
    ledger = DatabaseLedger(sessions, "extract guidance")
    await run(sessions, model(), tmp_path, budget=Budget(Decimal(1), PRICING, ledger=ledger))
    async with sessions() as session:
        assert await counted_spend(session) == Decimal("0.021")  # notes are not counted
        report = await spend_report(session)
    assert [(line.key, line.calls) for line in report.by_command] == [("extract guidance", 3)]
    assert [line.key for line in report.by_model] == ["fake-model@test"]

    # A budget too small for the next call: nothing is called, and the run says why.
    async with sessions() as session, session.begin():
        await session.execute(delete(GuidanceExtraction).where(GuidanceExtraction.cik == CIK))
    fresh = model()
    tight = Budget(Decimal(1), PRICING, limit=Decimal("0.03"), spent_before=Decimal("0.021"))
    result = await run(sessions, fresh, tmp_path / "empty", budget=tight, ok=False)
    assert fresh.prompts == [] and result.called == 0
    assert all("LLM budget: $0.0210 of $0.03 spent" in e for e in result.errors)

    assert await asyncio.to_thread(main, ["spend"]) == 0
    out = capsys.readouterr().out
    assert "LLM spending: $0.0210 of $3.00 budget" in out and "$2.9790 remaining" in out
    assert "extract guidance" in out and "Not counted toward the budget" in out
    assert "old-model: $5.2000 (before the ledger)" in out


async def test_implausible_rows_are_flagged_and_reviews_survive_rebuilds(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def cli(*args: str) -> int:
        return await asyncio.to_thread(main, list(args))

    await run(sessions, model(), tmp_path)
    async with sessions() as session, session.begin():
        # Q1 revenue of $1 billion makes $10 billion guided for Q2 implausible.
        session.add(_value("revenue", 2026, 1, Decimal(1) * B, "q1-10q", date(2026, 5, 1)))
    result = await run(sessions, model(), tmp_path)
    assert result.verdicts.get("needs_review") == 1 and "beat" not in result.verdicts

    flagged = (await rows(sessions))[1]
    assert (flagged.verdict, flagged.plausibility, flagged.plausibility_rule) == (
        "beat",  # the verdict is kept
        "flagged",
        "ratio",
    )
    assert flagged.plausibility_note == (
        "guided $10.00B is 1000% of FY2026Q1's $1.00B (plausible: 50% to 200%)"
    )
    by_target = {
        (r.accession_no, r.target_fiscal_quarter): r.plausibility for r in await rows(sessions)
    }
    assert by_target[(Q2_RELEASE, 3)] == "passed"  # $11B against Q2's $10.3B
    assert by_target[(Q1_RELEASE, None)] == "skipped"  # no full year 2025 stored
    assert by_target[(Q1_RELEASE, 2)] == "flagged"

    assert await cli("guidance", TICKER, "--flagged") == 0
    out = capsys.readouterr().out
    assert "REVIEW beat" in out and "needs review (ratio): guided $10.00B" in out
    assert "1 need review, 0 reviewed as wrong (not counted)" in out

    assert await cli("guidance", "review", str(flagged.id), "--wrong") == 0
    assert "reviewed as wrong (left out of verdicts)" in capsys.readouterr().out

    # Rebuilding the filing's rows gives them new IDs; the decision follows the item.
    async with sessions() as session, session.begin():
        await session.execute(
            update(GuidanceExtraction)
            .where(GuidanceExtraction.cik == CIK)
            .values(processing_version="0")
        )
    result = await run(sessions, model(), tmp_path)
    assert result.reprocessed == 3 and result.verdicts.get("wrong") == 1
    rebuilt = (await rows(sessions))[1]
    assert rebuilt.id != flagged.id and rebuilt.review == "wrong"

    assert await cli("guidance", "review", str(rebuilt.id), "--ok") == 0
    capsys.readouterr()
    async with sessions() as session:
        cleared = await session.get(Guidance, rebuilt.id)
    assert cleared is not None and (cleared.review, cleared.plausibility) == ("ok", "flagged")
    assert await cli("guidance", TICKER) == 0
    out = capsys.readouterr().out
    assert "reviewed: ok (ratio)" in out and "0 need review" in out and "1 beat" in out

    assert await cli("guidance", "review", "999999999", "--ok") == 1
    assert "no guidance row 999999999" in capsys.readouterr().err
    assert await cli("guidance", "review", str(rebuilt.id)) == 2
    assert await cli("guidance", TICKER, "--ok") == 2
