"""The promise tracker end to end against a real database, with fake models: a company
with two quarters reported and a 10-Q covering the second, and a story in which its CEO
promises second-quarter revenue of $12 billion and says something vague."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.agent.tools import ToolContext, call_tool
from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    Article,
    ArticleText,
    Company,
    Document,
    Filing,
    FinancialValue,
    LlmSpend,
    Promise,
    PromiseReview,
    PromiseVerdict,
    Statement,
)
from talk_is_cheap.db.models import StatementExtraction as ExtractionRow
from talk_is_cheap.guidance.llm import Budget, Pricing, ResponseCache, Usage
from talk_is_cheap.promises.frame import frame_claims, version_label
from talk_is_cheap.promises.queries import promise_rows
from talk_is_cheap.promises.resolve import ResolveDeps, plan_resolution, resolve_promises
from talk_is_cheap.promises.review import review
from talk_is_cheap.promises.schema import (
    DeadlineAnswer,
    EvidencePlan,
    FramingAnswer,
    Judgment,
    MeasuredSource,
)
from talk_is_cheap.promises.track import TOO_FEW, company_track_records
from talk_is_cheap.statements.extract import calendars
from talk_is_cheap.universe import Executive, Universe, UniverseCompany

pytestmark = pytest.mark.integration

CIK = 990_003
TICKER = "PRMS"
B = Decimal(10) ** 9
TODAY = date(2026, 10, 1)
COMPANY = UniverseCompany(
    ticker=TICKER,
    name="Promco",
    executives=[Executive(name="Jane Roe", role="Chief Executive Officer")],
)
UNIVERSE = Universe(history_start=date(2024, 1, 1), forms=["10-Q"], companies=[COMPANY])
TEXT = (
    "Promco shares rose on Friday.\n"
    "“Revenue will reach $12 billion in the second quarter,” chief executive Jane Roe said.\n"
    "“We are poised for our next wave of growth,” she added."
)
PROMISE_QUOTE = "Revenue will reach $12 billion in the second quarter"
VAGUE_QUOTE = "We are poised for our next wave of growth"


class Fake[S]:
    """A structured-output model answering from `respond(prompt)`."""

    def __init__(self, schema: type[Any], respond: Any) -> None:
        self._schema = schema
        self._respond = respond
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "fake-model@test"

    @property
    def schema(self) -> type[Any]:
        return self._schema

    async def extract(self, prompt: str) -> tuple[Any, Usage]:
        self.prompts.append(prompt)
        return self._respond(prompt), Usage(1_000, 300)


def framing(prompt: str) -> FramingAnswer:
    if "Revenue will reach $12 billion" in prompt.split("Quote:")[1].split("Paragraph")[0]:
        return FramingAnswer(
            subject="revenue",
            metric="revenue",
            measure_period="quarter",
            target_kind="absolute",
            target_text="$12 billion",
            target_value=12e9,
            unit="usd",
            deadline=DeadlineAnswer(
                phrase="in the second quarter", type="fiscal_quarter", quarter=2, year=2026
            ),
        )
    return FramingAnswer(subject="growth", target_kind="none")


def plan(prompt: str) -> EvidencePlan:
    return EvidencePlan(actuals_periods=["FY2026Q2"], check_later_statements=False)


def judge(prompt: str) -> Judgment:
    return Judgment(
        verdict="delivered",
        measured=[MeasuredSource(evidence_id="E1")],
        evidence_ids=["E1"],
        first_proof_id="E1",
        reasoning="Second-quarter revenue was $13 billion, above the $12 billion promised.",
        confidence=0.9,
    )


def budget() -> Budget:
    return Budget(Decimal(1), Pricing(Decimal(2), Decimal(10)))


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(PromiseReview).where(PromiseReview.ticker == TICKER))
        await session.execute(delete(Promise).where(Promise.ticker == TICKER))
        await session.execute(delete(ExtractionRow).where(ExtractionRow.ticker == TICKER))
        await session.execute(delete(Article).where(Article.url.like("https://promises.example/%")))
        await session.execute(delete(Company).where(Company.cik == CIK))
        await session.execute(delete(LlmSpend))


def revenue(quarter: int, amount: Decimal, filed: date) -> FinancialValue:
    return FinancialValue(
        cik=CIK,
        metric="revenue",
        period_type="quarter",
        fiscal_year=2026,
        fiscal_quarter=quarter,
        period_start=date(2026, 3 * quarter - 2, 1),
        period_end=date(2026, 3 * quarter, 28),
        value=amount,
        unit="USD",
        source_concept="us-gaap:Revenues",
        accession_no=f"0000990003-26-00000{quarter}",
        form="10-Q",
        filed=filed,
        derived=False,
        derivation=None,
    )


def statement(extraction: ExtractionRow, quote: str, claim_type: str, key: str) -> Statement:
    start = TEXT.index(quote)
    return Statement(
        extraction_id=extraction.id,
        article_id=extraction.article_id,
        cluster_id=extraction.cluster_id,
        ticker=TICKER,
        quote=quote,
        char_start=start,
        char_end=start + len(quote),
        speaker_name="Jane Roe",
        speaker_role="chief executive",
        attribution="named_executive",
        executive_name="Jane Roe",
        executive_role="Chief Executive Officer",
        management=True,
        is_direct_quote=True,
        venue="interview",
        statement_date=date(2026, 4, 24),
        date_from_article=False,
        claim_type=claim_type,
        claim=quote,
        metric="revenue" if claim_type == "plan" else None,
        basis="unspecified",
        measure="level",
        unit="usd",
        item_key=key,
        model="fake",
        prompt_version="v1",
    )


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    async with factory() as session, session.begin():
        session.add(Company(cik=CIK, ticker=TICKER, name="PROMCO INC", fiscal_year_end="1231"))
        await session.flush()
        session.add_all(
            [
                revenue(1, 10 * B, date(2026, 4, 20)),
                revenue(2, 13 * B, date(2026, 8, 1)),
                Filing(accession_no="0000990003-26-000002", cik=CIK, form="10-Q",
                       filing_date=date(2026, 8, 1)),
            ]
        )  # fmt: skip
        await session.flush()
        session.add(
            Document(
                accession_no="0000990003-26-000002", cik=CIK, doc_type="10q",
                document_name="q2.htm", url="https://www.sec.gov/promco-q2.htm",
                fiscal_year=2026, fiscal_quarter=2, period_end=date(2026, 6, 30),
                filed_at=date(2026, 8, 1), text="10-Q", content_hash="h", parser_version="1",
            )
        )  # fmt: skip
        article = Article(
            url_hash="p" * 64,
            url="https://promises.example/april",
            title="Promco sees a record quarter",
            publisher="Example News",
            published_at=datetime(2026, 4, 24, 14, tzinfo=UTC),
            time_precision="published",
        )
        session.add(article)
        await session.flush()
        session.add(ArticleText(article_id=article.id, status="ok", text=TEXT, content_hash="h"))
        extraction = ExtractionRow(
            article_id=article.id, cluster_id=article.id, ticker=TICKER, model="fake",
            prompt_version="v1", input_hash="h", processing_version="3", claims=[], dropped=[],
            input_tokens=0, output_tokens=0, cost=Decimal(0),
        )  # fmt: skip
        session.add(extraction)
        await session.flush()
        session.add_all(
            [
                statement(extraction, PROMISE_QUOTE, "plan", "key-promise"),
                statement(extraction, VAGUE_QUOTE, "qualitative", "key-vague"),
            ]
        )
    yield factory
    await _clean(factory)


def tool_context(sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine) -> ToolContext:
    def unused() -> Any:
        raise AssertionError("no search in these tests")

    return ToolContext(
        sessions=sessions, engine=engine, universe=UNIVERSE, embedder=unused, reranker=unused
    )


async def test_claims_are_framed_resolved_reviewed_and_scored(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine, tmp_path: Any
) -> None:
    cache = ResponseCache(tmp_path / "llm")
    framer = Fake(FramingAnswer, framing)
    async with sessions() as session:
        fiscal = await calendars(session)
    first = await frame_claims(
        sessions, framer, cache, budget(), companies=[COMPANY], calendars=fiscal
    )
    assert first[0].called == 2 and first[0].by_specificity == {"specific": 1, "vague": 1}
    again = await frame_claims(
        sessions, framer, cache, budget(), companies=[COMPANY], calendars=fiscal
    )
    assert again[0].up_to_date == 2 and len(framer.prompts) == 2

    async with sessions() as session:
        promises = {
            p.specificity: p
            for p in (await session.scalars(select(Promise).where(Promise.ticker == TICKER))).all()
        }
    promise = promises["specific"]
    assert promise.version == version_label() and promise.current
    assert promise.due_date == date(2026, 6, 30) and promise.due_basis == "fiscal_period"
    assert promise.baseline_source == "actuals" and promise.baseline_period == "FY2026Q1"
    assert promise.resolution_rule == (
        "True if Promco's revenue reaches at least $12 billion in any quarter ending on or "
        "before 2026-06-30."
    )
    assert "Fiscal calendar: fiscal years end on December 31" in framer.prompts[0]

    async with sessions() as session:
        plans = await plan_resolution(session, [TICKER], fiscal, TODAY)
    assert [len(p.candidates) for p in plans] == [1]  # the vague claim is never resolved
    assert plans[0].candidates[0].grace == date(2026, 8, 1)

    planner, judging = Fake(EvidencePlan, plan), Fake(Judgment, judge)
    deps = ResolveDeps(planner, judging, cache, budget(), tool_context(sessions, engine), TODAY)
    results = await resolve_promises(sessions, deps, plans, {TICKER: "December years"})
    assert results[0].verdicts == {"delivered": 1} and results[0].called == 1
    async with sessions() as session:
        verdict = await session.get(PromiseVerdict, promise.id)
    assert verdict is not None
    assert verdict.measured_value == 13 * B and verdict.measured_unit == "USD"
    assert verdict.proof_date == date(2026, 8, 1) and verdict.days_late is None
    assert verdict.rule_version == promise.version and not verdict.failed_checks
    assert verdict.measured is not None and verdict.measured["tool"] == "compute"
    assert "Only evidence dated after the statement is gathered" in planner.prompts[0]

    rerun = await resolve_promises(sessions, deps, plans, {TICKER: "December years"})
    assert rerun[0].cached == 1 and rerun[0].up_to_date == 1 and len(judging.prompts) == 1

    async with sessions() as session, session.begin():
        line = await review(session, promise.id, "delivered_late", "the release came first")
    assert "reviewed as delivered_late" in line
    async with sessions() as session:
        rows = await promise_rows(session, TICKER)
        records = await company_track_records(session, ticker=TICKER)
    effective = {r.promise.specificity: r.effective for r in rows}
    assert effective == {"specific": "delivered_late", "vague": None}
    assert records[0].key == "Jane Roe (PRMS)" and records[0].vague == 1
    assert not records[0].enough and records[0].resolved == 1

    # Re-extraction replaces statement rows; the claim keeps its key, promise and review.
    async with sessions() as session, session.begin():
        await session.execute(delete(Statement).where(Statement.ticker == TICKER))
        extraction = await session.scalar(
            select(ExtractionRow).where(ExtractionRow.ticker == TICKER)
        )
        assert extraction is not None
        session.add(statement(extraction, PROMISE_QUOTE, "plan", "key-promise"))
    after = await frame_claims(
        sessions, framer, cache, budget(), companies=[COMPANY], calendars=fiscal
    )
    assert after[0].up_to_date == 1 and len(framer.prompts) == 2
    async with sessions() as session:
        rows = await promise_rows(session, TICKER)
        current = await session.scalar(
            select(func.count())
            .select_from(Promise)
            .where(Promise.ticker == TICKER, Promise.current)
        )
    assert [r.effective for r in rows] == ["delivered_late"] and current == 1


async def test_promise_commands_and_agent_tools(
    sessions: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = ResponseCache(tmp_path / "llm")
    async with sessions() as session:
        fiscal = await calendars(session)
    await frame_claims(
        sessions, Fake(FramingAnswer, framing), cache, budget(), companies=[COMPANY],
        calendars=fiscal,
    )  # fmt: skip
    async with sessions() as session:
        plans = await plan_resolution(session, [TICKER], fiscal, TODAY)
    deps = ResolveDeps(
        Fake(EvidencePlan, plan), Fake(Judgment, judge), cache, budget(),
        tool_context(sessions, engine), TODAY,
    )  # fmt: skip
    await resolve_promises(sessions, deps, plans, {})
    async with sessions() as session:
        promise_id = await session.scalar(
            select(Promise.id).where(Promise.ticker == TICKER, Promise.specificity == "specific")
        )

    assert await asyncio.to_thread(main, ["promises", TICKER]) == 0
    listing = capsys.readouterr().out
    assert "delivered" in listing and "$13 billion / $12 billion" in listing and "vague" in listing
    assert await asyncio.to_thread(main, ["promises", "show", str(promise_id)]) == 0
    shown = capsys.readouterr().out
    assert "rule: True if Promco's revenue reaches at least $12 billion" in shown
    assert "pass evidence_after_statement" in shown and "reported by Example News" in shown
    assert await asyncio.to_thread(main, ["promises", "track-record", "--ticker", TICKER]) == 0
    assert TOO_FEW in capsys.readouterr().out
    assert (
        await asyncio.to_thread(
            main, ["promises", "review", str(promise_id), "--verdict", "not_delivered"]
        )
        == 0
    )
    assert "reviewed as not_delivered" in capsys.readouterr().out
    assert await asyncio.to_thread(main, ["promises", "review", str(promise_id)]) == 2

    ctx = tool_context(sessions, engine)
    found = await call_tool(ctx, "get_promises", {"ticker": TICKER})
    items = found.evidence
    claim = next(
        i for i in items if i["tool"] == "get_promises" and i["data"]["specificity"] == "specific"
    )
    assert claim["data"]["verdict"] == "not_delivered"  # the review wins
    assert claim["url"] == "https://promises.example/april"
    vague = next(i for i in items if i["data"].get("specificity") == "vague")
    assert vague["data"]["scored"] is False
    narrowed = await call_tool(ctx, "get_promises", {"ticker": TICKER, "query": "poised"})
    assert [i["data"]["specificity"] for i in narrowed.evidence] == ["vague"]
    record = await call_tool(ctx, "get_track_record", {"ticker": TICKER})
    summary = next(i for i in record.evidence if i["tool"] == "get_track_record")
    assert summary["data"]["judgment"] == TOO_FEW and summary["data"]["inputs"]
