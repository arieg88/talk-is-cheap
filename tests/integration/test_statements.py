"""Statements in the news end to end against a real database, with a fake model: a
company with two quarters reported, formal guidance, and two stories quoting its CEO, an
analyst, a vice president who is not on the roster and the company itself."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.agent.tools import GetStatementsInput, ToolContext, get_statements
from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    ArticleText,
    Company,
    FinancialValue,
    Guidance,
    LlmSpend,
    Statement,
    StatementOutcome,
)
from talk_is_cheap.db.models import (
    StatementExtraction as ExtractionRow,
)
from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.guidance.llm import Budget, Pricing, ResponseCache, Usage
from talk_is_cheap.statements.extract import calendars, extract_statements
from talk_is_cheap.statements.outcomes import Context
from talk_is_cheap.statements.review import review
from talk_is_cheap.statements.schema import ExtractedClaim, StatementExtraction
from talk_is_cheap.universe import Executive, Universe, UniverseCompany
from tests.retrieval_fakes import FakeEmbedder, FakeReranker

pytestmark = pytest.mark.integration

CIK = 990_002
TICKER = "STMT"
B = Decimal(10) ** 9
COMPANY = UniverseCompany(
    ticker=TICKER,
    name="Statco",
    executives=[
        Executive(name="Jane Roe", role="Chief Executive Officer", start=date(2025, 1, 1)),
        Executive(name="Old Boss", role="Chief Executive Officer", end=date(2024, 12, 31)),
    ],
)
UNIVERSE = Universe(history_start=date(2024, 1, 1), forms=["10-Q"], companies=[COMPANY])

APRIL = (
    "Statco shares rose on Friday.\n"
    "“Revenue this quarter will be about $13 billion,” chief executive Jane Roe told "
    "reporters on Thursday. Analysts expect $10 billion for the quarter.\n"
    "Roe said sales will rise this quarter as new products ship.\n"
    "John Doe, a vice president, said the company will hire 500 engineers.\n"
    "Statco said it plans to open a second factory in Ohio next year.\n"
    "Old Boss, the former chief executive, said revenue will fall this quarter."
)
JULY = (
    "Statco said it expects third-quarter revenue of $12 billion, in line with its "
    "outlook. The company did not comment further."
)


def claim(**values: Any) -> ExtractedClaim:
    base: dict[str, Any] = {
        "attribution": "company_statement",
        "is_direct_quote": False,
        "venue": "unknown",
        "claim_type": "qualitative",
        "claim": "a claim",
    }
    return ExtractedClaim(**{**base, **values})


ANSWERS = {
    "april": [
        claim(
            quote="“Revenue this quarter will be about $13 billion,” chief executive Jane Roe told",
            attribution="named_executive",
            speaker_name="Jane Roe",
            speaker_role="chief executive",
            is_direct_quote=True,
            venue="interview",
            statement_date="2026-04-23",
            claim_type="numeric_forecast",
            claim="Revenue of about $13 billion this quarter",
            metric="revenue",
            period_text="this quarter",
            value_text="about $13 billion",
        ),
        claim(
            quote="Analysts expect $10 billion for the quarter.",
            attribution="analyst",
            claim_type="numeric_forecast",
            metric="revenue",
            period_text="the quarter",
            value_text="$10 billion",
        ),
        claim(
            quote="Roe said sales will rise this quarter as new products ship.",
            attribution="named_executive",
            speaker_name="Roe",
            claim_type="directional",
            claim="Sales will rise this quarter",
            metric="revenue",
            period_text="this quarter",
            direction="increase",
        ),
        claim(
            quote="John Doe, a vice president, said the company will hire 500 engineers.",
            attribution="named_executive",
            speaker_name="John Doe",
            claim_type="plan",
        ),
        claim(
            quote="Statco said it plans to open a second factory in Ohio next year.",
            claim_type="plan",
            claim="A second factory in Ohio",
        ),
        claim(
            quote="Old Boss, the former chief executive, said revenue will fall this quarter.",
            attribution="named_executive",
            speaker_name="Old Boss",
            claim_type="directional",
            metric="revenue",
            period_text="this quarter",
            direction="decrease",
        ),
        claim(quote="Statco will triple revenue, Roe said."),  # not in the text: dropped
    ],
    "july": [
        claim(
            quote="Statco said it expects third-quarter revenue of $12 billion",
            claim_type="numeric_forecast",
            claim="Third-quarter revenue of $12 billion",
            metric="revenue",
            period_text="third-quarter",
            value_text="$12 billion",
        )
    ],
}


class FakeStatementModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "fake-model@test"

    @property
    def schema(self) -> type[StatementExtraction]:
        return StatementExtraction

    async def extract(self, prompt: str) -> tuple[StatementExtraction, Usage]:
        self.prompts.append(prompt)
        key = "april" if "Statco shares rose" in prompt else "july"
        return StatementExtraction(claims=ANSWERS[key]), Usage(1_000, 500)


async def find_context(session: AsyncSession, row: Statement) -> Context | None:
    return Context("https://www.sec.gov/statco-10q.htm#chars=1-9", "10-Q FY2026Q2 · mdna", "Ohio")


def value(
    metric: str, year: int, quarter: int | None, amount: Decimal, filed: date
) -> FinancialValue:
    return FinancialValue(
        cik=CIK,
        metric=metric,
        period_type="quarter" if quarter else "year",
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_start=date(year, 3 * (quarter or 4) - 2, 1),
        period_end=date(year, 3 * (quarter or 4), 28),
        value=amount,
        unit="USD",
        source_concept="us-gaap:Revenues",
        accession_no=f"0000990002-{year % 100}-{quarter or 0:06d}",
        form="10-Q",
        filed=filed,
        derived=False,
        derivation=None,
    )


def guidance(issued: date, quarter: int, low: Decimal, high: Decimal) -> Guidance:
    return Guidance(
        cik=CIK,
        accession_no=f"0000990002-26-1{quarter:05d}",
        fiscal_year=2026,
        fiscal_quarter=quarter - 1,
        issued_at=issued,
        metric="revenue",
        metric_raw="revenue",
        basis="unspecified",
        measure="level",
        constant_currency=False,
        period_text=f"Q{quarter} 2026",
        target_fiscal_year=2026,
        target_fiscal_quarter=quarter,
        low=low,
        high=high,
        unit="usd",
        model="fake",
        prompt_version="v1",
        verdict="pending",
        item_key=f"k-{quarter}",
    )


async def _article(
    session: AsyncSession, url: str, title: str, published: datetime, text: str
) -> int:
    article = Article(
        url_hash=url[-64:].rjust(64, "0"),
        url=url,
        title=title,
        publisher="Example News",
        published_at=published,
        time_precision="published",
    )
    session.add(article)
    await session.flush()
    session.add(
        ArticleCompany(
            article_id=article.id, ticker=TICKER, method="headline", confidence=Decimal("0.9")
        )
    )
    session.add(ArticleText(article_id=article.id, status="ok", text=text, content_hash="h"))
    return article.id


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(ExtractionRow).where(ExtractionRow.ticker == TICKER))
        await session.execute(delete(Article).where(Article.url.like("https://news.example/%")))
        await session.execute(delete(Guidance).where(Guidance.cik == CIK))
        await session.execute(delete(Company).where(Company.cik == CIK))
        await session.execute(delete(LlmSpend))


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    async with factory() as session, session.begin():
        session.add(Company(cik=CIK, ticker=TICKER, name="STATCO INC", fiscal_year_end="1231"))
        await session.flush()
        session.add_all(
            [
                value("revenue", 2026, 1, 10 * B, date(2026, 5, 1)),
                value("revenue", 2026, 2, 11 * B, date(2026, 8, 1)),
                guidance(date(2026, 4, 20), 2, Decimal("10.5") * B, Decimal("11.5") * B),
                guidance(date(2026, 7, 20), 3, Decimal("11.5") * B, Decimal("12.5") * B),
            ]
        )
        await _article(
            session,
            "https://news.example/april",
            "Statco sees a record quarter",
            datetime(2026, 4, 24, 14, tzinfo=UTC),
            APRIL,
        )
        await _article(
            session,
            "https://news.example/july",
            "Statco keeps its outlook",
            datetime(2026, 7, 25, 9, tzinfo=UTC),
            JULY,
        )
    yield factory
    await _clean(factory)


async def run(
    sessions: async_sessionmaker[AsyncSession], model: FakeStatementModel, cache: ResponseCache
) -> list[Any]:
    async with sessions() as session:
        fiscal = await calendars(session)
    return await extract_statements(
        sessions,
        model,
        cache,
        Budget(Decimal(1), Pricing(Decimal(2), Decimal(10))),
        companies=[COMPANY],
        calendars=fiscal,
        find_context=find_context,
    )


async def statements(
    sessions: async_sessionmaker[AsyncSession],
) -> dict[str, tuple[Statement, StatementOutcome | None]]:
    async with sessions() as session:
        rows = await session.execute(
            select(Statement, StatementOutcome)
            .outerjoin(StatementOutcome, StatementOutcome.statement_id == Statement.id)
            .where(Statement.ticker == TICKER)
        )
        return {s.quote.lstrip("\u201c")[:19]: (s, o) for s, o in rows}


async def test_claims_are_attributed_scored_and_compared_with_guidance(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    model = FakeStatementModel()
    (result,) = await run(sessions, model, ResponseCache(tmp_path))
    assert (result.candidates, result.called, result.claims, result.dropped) == (2, 2, 7, 1)
    assert result.management == 4

    found = await statements(sessions)
    ceo, ceo_outcome = found["Revenue this quarte"]
    assert ceo.executive_name == "Jane Roe" and ceo.management and ceo.venue == "interview"
    assert ceo.statement_date == date(2026, 4, 23) and not ceo.date_from_article
    # Said in fiscal Q2 2026 (the last quarter ended is Q1): "this quarter" is Q2.
    assert (ceo.target_fiscal_year, ceo.target_fiscal_quarter) == (2026, 2)
    assert ceo_outcome is not None
    assert ceo_outcome.verdict == "missed" and ceo_outcome.actual == 11 * B
    assert ceo_outcome.guidance_comparison == "differs" and ceo_outcome.guidance_id is not None

    analyst, analyst_outcome = found["Analysts expect $10"]
    assert analyst.attribution == "analyst" and not analyst.management
    assert analyst_outcome is None  # stored, never scored

    rise, rise_outcome = found["Roe said sales will"]
    assert rise.executive_name == "Jane Roe"  # by surname
    assert rise_outcome is not None and rise_outcome.verdict == "confirmed"
    assert rise_outcome.prior_actual == 10 * B and rise_outcome.actual == 11 * B
    assert rise_outcome.guidance_comparison == "consistent"  # guided 11B against Q1's 10B

    vice, vice_outcome = found["John Doe, a vice pr"]
    assert vice.attribution == "unverified_executive" and vice_outcome is None

    former, former_outcome = found["Old Boss, the forme"]
    assert former.attribution == "unverified_executive" and former_outcome is None

    _, plan_outcome = found["Statco said it plan"]
    assert plan_outcome is not None and plan_outcome.verdict == "not_checkable"
    assert plan_outcome.context_label == "10-Q FY2026Q2 · mdna"

    july, july_outcome = found["Statco said it expe"]
    assert july.date_from_article and july.statement_date == date(2026, 7, 25)
    assert july_outcome is not None and july_outcome.verdict == "pending"
    assert july_outcome.guidance_comparison == "consistent"

    # A second run makes no call; claims and outcomes are the same.
    (again,) = await run(sessions, model, ResponseCache(tmp_path))
    assert (again.up_to_date, again.called, len(model.prompts)) == (2, 0, 2)


async def test_review_decisions_survive_re_extraction_and_keep_plan_context(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    cache = ResponseCache(tmp_path)
    await run(sessions, FakeStatementModel(), cache)
    ceo, _ = (await statements(sessions))["Revenue this quarte"]
    async with sessions() as session, session.begin():
        line = await review(session, ceo.id, "wrong")
    assert "reviewed as wrong" in line

    found = await statements(sessions)
    assert found["Revenue this quarte"][1].review == "wrong"  # type: ignore[union-attr]
    assert found["Statco said it plan"][1].context_url  # type: ignore[union-attr]

    async with sessions() as session, session.begin():
        await session.execute(delete(ExtractionRow).where(ExtractionRow.ticker == TICKER))
    (result,) = await run(sessions, FakeStatementModel(), cache)
    assert result.cached == 2 and result.outcomes.get("wrong") == 1
    assert (await statements(sessions))["Revenue this quarte"][1].review == "wrong"  # type: ignore[union-attr]


async def test_statements_command_and_summary(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    await run(sessions, FakeStatementModel(), ResponseCache(tmp_path))
    assert await asyncio.to_thread(main, ["statements", TICKER]) == 0
    out = capsys.readouterr().out
    assert "Jane Roe" in out and "missed" in out and "differs" in out
    assert "reported by Example News: https://news.example/april" in out
    assert "Analysts expect" not in out

    assert (
        await asyncio.to_thread(
            main, ["statements", TICKER, "--all-attributions", "--speaker", "John Doe"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "John Doe (unverified" in out and "Jane Roe" not in out

    assert await asyncio.to_thread(main, ["statements", "all", "--summary"]) == 0
    summary = capsys.readouterr().out
    line = next(row for row in summary.splitlines() if row.startswith(TICKER))
    # 4 management claims: 1 numeric, 1 directional, 2 plans (one statement each).
    assert line.split()[1:6] == ["4", "2", "1", "1", "0"]
    assert "1/2 (50%)" in line  # confirmed, of missed and confirmed

    assert await asyncio.to_thread(main, ["statements", "review", "abc", "--ok"]) == 2


async def test_get_statements_tool(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine, tmp_path: Any
) -> None:
    await run(sessions, FakeStatementModel(), ResponseCache(tmp_path))
    ctx = ToolContext(
        sessions=sessions,
        engine=engine,
        universe=UNIVERSE,
        embedder=FakeEmbedder,
        reranker=FakeReranker,
    )
    output = await get_statements(ctx, GetStatementsInput(ticker=TICKER, metric="revenue"))
    items = output.evidence
    assert {i["provenance"] for i in items} == {"news_report"}
    by_speaker = {i["data"].get("speaker"): i["data"] for i in items}
    ceo = by_speaker["Jane Roe"]
    assert ceo["verdict"] == "missed" and ceo["guidance_comparison"] == "differs"
    assert ceo["formal_guidance"] == {"issued_on": "2026-04-20", "basis": "unspecified",
                                      "low": 10.5e9, "high": 11.5e9}  # fmt: skip
    assert ceo["provenance_note"] == "reported by Example News"
    assert all(i["data"]["management"] for i in items)

    analysts = await get_statements(
        ctx, GetStatementsInput(ticker=TICKER, speaker="nobody", management_only=True)
    )
    assert analysts.evidence == [] and "not_found" in analysts.content

    everyone = await get_statements(
        ctx, GetStatementsInput(ticker=TICKER, management_only=False, period="FY2026Q2")
    )
    analyst = next(i["data"] for i in everyone.evidence if i["data"]["attribution"] == "analyst")
    assert analyst["outcome"] == "not scored: not a management claim"


def test_calendar_of_the_fixture_is_a_december_year() -> None:
    assert FiscalCalendar.parse("1231").quarter_ended_before(date(2026, 4, 23)).fiscal_quarter == 1
