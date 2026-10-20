"""The agent against a real database: every tool called directly, `run_sql`'s guards in
the database, and the whole graph with a scripted model (never the API): routing,
tool calls, the verifier's revisions and what it records, memory across a thread, the
response cache, the tool-call cap, the budget, run logging and the commands."""

import hashlib
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.agent import commands
from talk_is_cheap.agent.graph import MAX_TOOL_CALLS, AgentDeps, build_graph
from talk_is_cheap.agent.llm import ScriptedChat, text_of
from talk_is_cheap.agent.memory import open_checkpointer
from talk_is_cheap.agent.router import ADVICE_NOTE
from talk_is_cheap.agent.run import RunResult, ask, log_run
from talk_is_cheap.agent.sql import execute_read_only
from talk_is_cheap.agent.tools import (
    ComputeInput,
    GetActualsInput,
    GetGuidanceInput,
    ResolveCompanyInput,
    RunSqlInput,
    SearchFilingsInput,
    SearchNewsInput,
    ToolContext,
    call_tool,
    compute,
    get_actuals,
    get_guidance,
    resolve_company,
    run_sql,
    search_filings,
    search_news,
)
from talk_is_cheap.cli import main
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    AgentRun,
    Article,
    ArticleCompany,
    Chunk,
    Company,
    Document,
    Embedding,
    Filing,
    FinancialValue,
    Guidance,
    GuidanceSource,
    LlmSpend,
    Section,
)
from talk_is_cheap.guidance.llm import Budget, BudgetExhausted, Pricing, ResponseCache
from talk_is_cheap.retrieval.index import index
from talk_is_cheap.universe import Universe, UniverseCompany
from tests.retrieval_fakes import FakeEmbedder, FakeReranker

pytestmark = pytest.mark.integration

CIK, TICKER = 990_101, "AGNT"
OTHER_CIK, OTHER = 990_102, "OTHR"
B = Decimal(10) ** 9
ARCHIVE = f"https://www.sec.gov/Archives/edgar/data/{CIK}"
NEWS = "https://agent.example.test"
UNIVERSE = Universe(
    history_start=date(2025, 1, 1),
    forms=["10-Q", "10-K", "8-K"],
    companies=[
        UniverseCompany(ticker=TICKER, name="Agentco"),
        UniverseCompany(ticker=OTHER, name="Otherco"),
    ],
)
# accession, form, filed, primary document
FILINGS = [
    ("0000990101-26-000005", "10-K", date(2026, 2, 15), "agnt-10k.htm"),
    ("0000990101-26-000009", "8-K", date(2026, 4, 28), "release-q1.htm"),
    ("0000990101-26-000010", "10-Q", date(2026, 5, 1), "agnt-q1.htm"),
    ("0000990101-26-000020", "10-Q", date(2026, 8, 1), "agnt-q2.htm"),
]
Q1_10Q, Q2_10Q, RELEASE, TEN_K = (
    "0000990101-26-000010",
    "0000990101-26-000020",
    "0000990101-26-000009",
    "0000990101-26-000005",
)
OUTLOOK = (
    "## Outlook\n\nFor the second quarter of 2026, Agentco expects revenue of $11.0 billion, "
    "plus or minus 2%, and GAAP EPS of approximately $0.50."
)
MDNA = (
    "## Results of Operations\n\nRevenue increased 10% sequentially, driven by strong demand "
    "for Widget Pro in the data center market."
)
RISKS = "## Risk Factors\n\nWe depend on a single supplier for our widget chips."


def url(accession: str, name: str) -> str:
    return f"{ARCHIVE}/{accession.replace('-', '')}/{name}"


def value(
    metric: str,
    year: int,
    quarter: int | None,
    amount: Decimal,
    accession: str,
    filed: date,
    *,
    unit: str = "USD",
    derived: bool = False,
) -> FinancialValue:
    start = date(year, 1 + 3 * ((quarter or 1) - 1), 1)
    end = date(year, 3 * (quarter or 4), 28)
    return FinancialValue(
        cik=CIK,
        metric=metric,
        period_type="quarter" if quarter else "year",
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_start=start,
        period_end=end,
        value=amount,
        unit=unit,
        source_concept="us-gaap:Test",
        accession_no=accession,
        form="10-K" if accession == TEN_K else "10-Q",
        filed=filed,
        derived=derived,
        derivation="annual minus nine months" if derived else None,
    )


def guidance_row(**values: Any) -> Guidance:
    base: dict[str, Any] = {
        "cik": CIK,
        "accession_no": RELEASE,
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "issued_at": date(2026, 4, 28),
        "metric": "revenue",
        "metric_raw": "revenue",
        "basis": "unspecified",
        "measure": "level",
        "constant_currency": False,
        "period_text": "second quarter of 2026",
        "target_fiscal_year": 2026,
        "target_fiscal_quarter": 2,
        "unit": "usd",
        "model": "fake",
        "prompt_version": "v1",
        "verdict": "met",
        "item_key": "k-revenue",
        "plausibility": "passed",
    }
    return Guidance(**{**base, **values})


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(Company).where(Company.cik.in_([CIK, OTHER_CIK])))
        await session.execute(delete(Article).where(Article.url.like(f"{NEWS}/%")))
        await session.execute(delete(Chunk))
        await session.execute(delete(Embedding))
        await session.execute(delete(AgentRun))
        await session.execute(delete(LlmSpend))


@pytest.fixture(scope="module")
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    async with factory() as session, session.begin():
        session.add(Company(cik=CIK, ticker=TICKER, name="AGENTCO INC", fiscal_year_end="1231"))
        session.add(
            Company(cik=OTHER_CIK, ticker=OTHER, name="OTHERCO CORP", fiscal_year_end="1231")
        )
        await session.flush()
        for accession, form, filed, primary in FILINGS:
            session.add(
                Filing(
                    accession_no=accession,
                    cik=CIK,
                    form=form,
                    filing_date=filed,
                    primary_document=primary,
                )
            )
        session.add(
            Filing(
                accession_no="0000990102-26-000001",
                cik=OTHER_CIK,
                form="8-K",
                filing_date=date(2026, 5, 2),
                primary_document="othr-release.htm",
            )
        )
        await session.flush()
        session.add_all(
            [
                value("revenue", 2026, 1, 10 * B, Q1_10Q, date(2026, 5, 1)),
                value(
                    "revenue", 2026, 1, Decimal("10.2") * B, Q2_10Q, date(2026, 8, 1)
                ),  # restated
                value("revenue", 2026, 2, 11 * B, Q2_10Q, date(2026, 8, 1)),
                value("gross_profit", 2026, 2, Decimal("5.5") * B, Q2_10Q, date(2026, 8, 1)),
                value(
                    "eps_diluted",
                    2026,
                    2,
                    Decimal("0.60"),
                    Q2_10Q,
                    date(2026, 8, 1),
                    unit="USD/shares",
                ),
                value("revenue", 2025, 4, 9 * B, TEN_K, date(2026, 2, 15), derived=True),
                value("revenue", 2025, None, 36 * B, TEN_K, date(2026, 2, 15)),
            ]
        )
        documents = [
            (
                RELEASE,
                "press_release",
                "release-q1.htm",
                2026,
                1,
                date(2026, 4, 28),
                [("outlook", "Outlook", OUTLOOK)],
            ),
            (
                Q2_10Q,
                "10q",
                "agnt-q2.htm",
                2026,
                2,
                date(2026, 8, 1),
                [("mdna", "Results of Operations", MDNA), ("risk_factors", "Risk Factors", RISKS)],
            ),
            (
                "0000990102-26-000001",
                "press_release",
                "othr-release.htm",
                2026,
                1,
                date(2026, 5, 2),
                [("results", "Results", "## Results\n\nRevenue grew.")],
            ),
        ]
        for accession, doc_type, name, year, quarter, filed, sections in documents:
            cik = OTHER_CIK if accession.startswith("0000990102") else CIK
            text = "\n\n".join(body for _, _, body in sections)
            document = Document(
                accession_no=accession,
                cik=cik,
                doc_type=doc_type,
                document_name=name,
                url=f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                f"{accession.replace('-', '')}/{name}",
                fiscal_year=year,
                fiscal_quarter=quarter,
                period_end=date(year, 3 * quarter, 28),
                filed_at=filed,
                text=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                parser_version="test",
            )
            session.add(document)
            await session.flush()
            start = 0
            for position, (section_type, title, body) in enumerate(sections):
                session.add(
                    Section(
                        document_id=document.id,
                        section_type=section_type,
                        title=title,
                        position=position,
                        text=body,
                        char_start=start,
                        char_end=start + len(body),
                    )
                )
                start += len(body) + 2
        rows = [
            guidance_row(
                low=Decimal("10.78") * B,
                high=Decimal("11.22") * B,
                point=11 * B,
                value_text="$11.0 billion, plus or minus 2%",
                actual=11 * B,
                difference=Decimal(0),
                percent_difference=Decimal(0),
                actual_accession_no=Q2_10Q,
            ),
            guidance_row(
                metric="eps",
                basis="gaap",
                unit="usd_per_share",
                point=Decimal("0.50"),
                value_text="approximately $0.50",
                verdict="beat",
                actual=Decimal("0.60"),
                item_key="k-eps",
                plausibility="flagged",
                plausibility_rule="ratio",
                plausibility_note="guided 0.50 is 20% of prior 2.50",
                review="ok",
            ),
            guidance_row(
                metric="capex",
                point=Decimal("0.2") * B,
                value_text="$200 million",
                verdict="beat",
                item_key="k-capex",
                review="wrong",
            ),
        ]
        session.add_all(rows)
        await session.flush()
        for row in rows:
            session.add(
                GuidanceSource(
                    guidance_id=row.id,
                    position=0,
                    accession_no=RELEASE,
                    document_name="release-q1.htm",
                    doc_type="press_release",
                    url=url(RELEASE, "release-q1.htm"),
                    section_title="Outlook",
                    char_start=12,
                    char_end=120,
                    quote=OUTLOOK[12:120],
                )
            )
        article = Article(
            url_hash=hashlib.sha256(f"{NEWS}/raise".encode()).hexdigest(),
            url=f"{NEWS}/raise",
            title="Agentco raises its outlook",
            summary="Agentco now sees revenue of $12 billion.",
            publisher="Test Wire",
            published_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            time_precision="published",
        )
        session.add(article)
        await session.flush()
        session.add(
            ArticleCompany(
                article_id=article.id, ticker=TICKER, method="headline", confidence=Decimal("0.90")
            )
        )
    await index(engine, FakeEmbedder(), {TICKER: "Agentco", OTHER: "Otherco"})
    yield factory
    await _clean(factory)


@pytest.fixture
def ctx(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]) -> ToolContext:
    return ToolContext(
        sessions=sessions,
        engine=engine,
        universe=UNIVERSE,
        embedder=FakeEmbedder,
        reranker=FakeReranker,
    )


# Tools, called directly.


async def test_resolve_company(ctx: ToolContext) -> None:
    for query in ("Agentco", "agnt", "AGENTCO INC", "Agentco's"):
        found = await resolve_company(ctx, ResolveCompanyInput(query=query))
        assert found.content["found"], query
    data = found.evidence[-1]["data"]
    assert (data["ticker"], data["cik"], data["fiscal_year_end"]) == (TICKER, CIK, "1231")
    assert data["latest_reported_quarter"] == "FY2026Q2"
    assert found.evidence[-1]["url"].endswith(f"CIK={CIK:010d}")
    missing = await resolve_company(ctx, ResolveCompanyInput(query="Agentko"))
    assert missing.content["found"] is False and missing.content["closest"] == [TICKER]
    assert missing.evidence == []


async def test_get_actuals_vintages_periods_and_urls(ctx: ToolContext) -> None:
    first = await get_actuals(
        ctx,
        GetActualsInput(
            ticker=TICKER,
            metrics=["revenue"],
            periods=["FY2026Q1", "latest", "FY2025Q4", "FY2025"],
            vintage="first",
        ),
    )
    data = {item["data"]["period"]: item for item in first.evidence}
    assert data["FY2026Q1"]["data"]["value"] == 10e9
    assert data["FY2026Q1"]["url"] == url(Q1_10Q, "agnt-q1.htm")
    assert data["FY2026Q2"]["data"]["display"] == "$11,000M"
    assert data["FY2025Q4"]["data"]["derived"] == "annual minus nine months"
    assert data["FY2025"]["data"]["value"] == 36e9 and data["FY2025"]["data"]["form"] == "10-K"
    assert {i["provenance"] for i in first.evidence} == {"sec_filing"}
    latest = await get_actuals(
        ctx,
        GetActualsInput(
            ticker=TICKER,
            metrics=["revenue", "net_income"],
            periods=["latest-1", "FY2030Q1"],
        ),
    )
    [restated] = latest.evidence
    assert (restated["data"]["period"], restated["data"]["value"]) == ("FY2026Q1", 10.2e9)
    assert restated["url"] == url(Q2_10Q, "agnt-q2.htm")
    assert latest.content["not_found"] == [
        "net_income FY2026Q1: not reported",
        "FY2030Q1: no such period reported",
    ]
    failed = await call_tool(
        ctx, "get_actuals", {"ticker": "NOPE", "metrics": ["revenue"], "periods": ["latest"]}
    )
    assert "not a loaded company" in failed.content["error"]


async def test_get_guidance_treats_flags_and_reviews_like_the_command(ctx: ToolContext) -> None:
    found = await get_guidance(ctx, GetGuidanceInput(ticker=TICKER, target_period="FY2026Q2"))
    rows = {item["data"]["metric"]: item for item in found.evidence}
    revenue = rows["revenue"]
    assert revenue["provenance"] == "company_press_release"
    assert revenue["url"] == url(RELEASE, "release-q1.htm")
    data = revenue["data"]
    assert (data["low"], data["high"], data["point"], data["unit"]) == (
        10.78e9,
        11.22e9,
        11e9,
        "USD",
    )
    assert data["verdict"] == "met" and data["counts_in_verdicts"]
    assert data["quote"].startswith("For the second quarter")
    eps = rows["eps"]["data"]
    assert (
        eps["review"].startswith("flagged by the plausibility check") and eps["verdict"] == "beat"
    )
    capex = rows["capex"]["data"]
    assert capex["verdict"] is None and not capex["counts_in_verdicts"]
    assert capex["review"].startswith("reviewed as wrong")
    only_eps = await get_guidance(ctx, GetGuidanceInput(ticker=TICKER, metric="eps"))
    assert [i["data"]["metric"] for i in only_eps.evidence] == ["eps"]
    nothing = await get_guidance(ctx, GetGuidanceInput(ticker=TICKER, target_period="FY2027"))
    assert nothing.evidence == [] and nothing.content["targets_guided"] == ["FY2026Q2"]


async def test_a_company_without_guidance_gets_citable_evidence_of_that(ctx: ToolContext) -> None:
    found = await get_guidance(ctx, GetGuidanceInput(ticker=OTHER))
    [item] = found.evidence
    assert item["data"]["guidance_rows"] == 0 and item["data"]["earnings_releases_checked"] == 1
    assert item["url"].endswith("othr-release.htm")


async def test_search_filings_and_news(ctx: ToolContext) -> None:
    found = await search_filings(
        ctx,
        SearchFilingsInput(
            query="what drove revenue growth widget pro demand",
            ticker=TICKER,
            periods="FY2026Q2",
            section_types=["mdna"],
        ),
    )
    top = found.evidence[0]
    assert top["provenance"] == "sec_filing" and top["url"] == url(Q2_10Q, "agnt-q2.htm")
    assert top["data"]["section"] == "mdna" and "Widget Pro" in top["data"]["text"]
    start, end = (int(n) for n in top["data"]["chars"].split("-"))
    async with ctx.sessions() as session:
        text = await session.scalar(select(Document.text).where(Document.url == top["url"]))
    assert text is not None and "Widget Pro" in text[start:end]
    outlook = await search_filings(
        ctx, SearchFilingsInput(query="expects revenue", section_types=["outlook"])
    )
    assert outlook.evidence[0]["provenance"] == "company_press_release"
    news = await search_news(ctx, SearchNewsInput(query="Agentco outlook", ticker=TICKER))
    [article] = news.evidence
    assert article["provenance"] == "news_report" and article["url"] == f"{NEWS}/raise"
    assert article["data"]["publisher"] == "Test Wire"
    assert article["data"]["link_confidence"] == 0.9
    assert article["data"]["published"] == "2026-09-01"


async def test_compute(ctx: ToolContext) -> None:
    values = await get_actuals(
        ctx,
        GetActualsInput(
            ticker=TICKER,
            metrics=["revenue", "gross_profit", "eps_diluted"],
            periods=["FY2026Q1", "FY2026Q2"],
            vintage="first",
        ),
    )
    ids = {(i["data"]["metric"], i["data"]["period"]): i["id"] for i in values.evidence}
    q1, q2 = ids[("revenue", "FY2026Q1")], ids[("revenue", "FY2026Q2")]
    growth = await compute(ctx, ComputeInput(operation="pct_change", operands=[q1, q2]))
    [result] = growth.evidence
    assert result["data"]["result"] == 10.0 and result["data"]["inputs"] == [q1, q2]
    margin = await compute(
        ctx, ComputeInput(operation="margin", operands=[ids[("gross_profit", "FY2026Q2")], q2])
    )
    assert margin.evidence[0]["data"]["display"] == "50.00%"
    guided = (await get_guidance(ctx, GetGuidanceInput(ticker=TICKER, metric="revenue"))).evidence
    beat = await compute(
        ctx, ComputeInput(operation="difference", operands=[q2, f"{guided[0]['id']}.low"])
    )
    assert beat.evidence[0]["data"]["result"] == 0.22e9
    mixed = await call_tool(
        ctx, "compute", {"operation": "sum", "operands": [q2, ids[("eps_diluted", "FY2026Q2")]]}
    )
    assert "one unit" in mixed.content["error"]
    unknown = await call_tool(ctx, "compute", {"operation": "ratio", "operands": [q2, "E999"]})
    assert "not an evidence id" in unknown.content["error"]
    two = await call_tool(ctx, "compute", {"operation": "ratio", "operands": [q2]})
    assert "exactly 2 operands" in two.content["error"]


async def test_run_sql(ctx: ToolContext) -> None:
    rows = await run_sql(
        ctx,
        RunSqlInput(
            sql=f"SELECT metric, count(*) AS n FROM financial_values WHERE cik = {CIK} "
            "GROUP BY metric ORDER BY metric"
        ),
    )
    [item] = rows.evidence
    assert item["url"] == "" and item["data"]["columns"] == ["metric", "n"]
    assert item["data"]["rows"] == [["eps_diluted", 1], ["gross_profit", 1], ["revenue", 5]]
    refused = await call_tool(ctx, "run_sql", {"sql": "DELETE FROM companies"})
    assert refused.content["error"].startswith("rejected: only SELECT")
    failed = await call_tool(ctx, "run_sql", {"sql": "SELECT nope FROM companies"})
    assert failed.content["error"].startswith("query failed")
    assert [c.name for c in ctx.calls] == ["run_sql", "run_sql"]
    assert ctx.calls[0].error is not None and ctx.calls[0].seconds >= 0


async def test_the_database_enforces_read_only_and_the_timeout(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await execute_read_only(
                connection,
                "INSERT INTO stock_splits (cik, effective_date, ratio, source) "
                f"VALUES ({CIK}, '2026-01-01', 2, 'x')",
            )
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError, match="statement timeout"):
            await execute_read_only(connection, "SELECT pg_sleep(2)", timeout_seconds=0.2)
    async with engine.connect() as connection:
        result = await execute_read_only(
            connection, "SELECT generate_series(1, 300) AS n", max_rows=200
        )
    assert len(result.rows) == 200 and result.truncated


# The graph, with a scripted model.


def calls(*pairs: tuple[str, dict[str, Any]]) -> AIMessage:
    return AIMessage(
        "",
        tool_calls=[
            {"name": name, "args": args, "id": f"call-{name}-{n}"}
            for n, (name, args) in enumerate(pairs)
        ],
    )


def evidence_ids(messages: Sequence[BaseMessage], tool: str = "") -> list[str]:
    import json

    found = []
    for message in messages:
        if isinstance(message, ToolMessage) and (not tool or message.name == tool):
            found += [e["id"] for e in json.loads(text_of(message)).get("evidence", [])]
    return found


Step = Callable[[Sequence[BaseMessage]], AIMessage]


class Script:
    """Scripted replies per node: the executor's in order (the last repeats), the
    verifier's in order (default: supported), the answer echoes the draft."""

    def __init__(self, executor: Sequence[Step], verdicts: Sequence[bool] = ()) -> None:
        self.executor = list(executor)
        self.verdicts = list(verdicts)
        self.seen: list[Sequence[BaseMessage]] = []
        self.nodes: list[str] = []

    def __call__(self, node: str, messages: Sequence[BaseMessage], tools: Any) -> AIMessage:
        self.nodes.append(node)
        if node == "executor":
            self.seen.append(messages)
            step = self.executor.pop(0) if len(self.executor) > 1 else self.executor[0]
            return step(messages)
        if node == "verifier":
            ok = self.verdicts.pop(0) if self.verdicts else True
            claims = [
                {"claim": "Agentco's revenue was $11.0 billion", "verdict": "supported"},
                {
                    "claim": "in FY2026 Q2",
                    "verdict": "supported" if ok else "unsupported",
                    "reason": "wrong period",
                },
                # no news is cited, so this does not count
                {"claim": "revenue", "verdict": "prefer_primary", "reason": "news"},
            ]
            return calls(("verdict", {"claims": claims}))
        if node == "answer":
            draft = text_of(messages[0]).split("Draft:\n", 1)[1].split("\n\nEvidence cited:")[0]
            return AIMessage(draft)
        if node == "router":
            return calls(("route", {"question_type": "out_of_scope"}))
        return calls(("plan", {"steps": ["get the values", "compute", "answer"]}))


def fetch_revenue(periods: list[str]) -> Step:
    return lambda _: calls(
        ("get_actuals", {"ticker": TICKER, "metrics": ["revenue"], "periods": periods})
    )


def say(template: str) -> Step:
    """A draft citing the evidence of the latest tool results: {0}, {1}, ..."""
    return lambda messages: AIMessage(template.format(*evidence_ids(messages)))


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "llm")


def deps_for(
    ctx: ToolContext, script: Script, cache: ResponseCache, budget: Budget | None = None
) -> AgentDeps:
    return AgentDeps(
        model=ScriptedChat(script),
        cache=cache,
        budget=budget or Budget(Decimal(1), Pricing(Decimal("0.10"), Decimal("0.50"))),
        tools=ctx,
        today=date(2026, 10, 19),
    )


async def run(
    ctx: ToolContext,
    script: Script,
    cache: ResponseCache,
    question: str,
    thread: str,
    budget: Budget | None = None,
) -> RunResult:
    deps = deps_for(ctx, script, cache, budget)
    async with open_checkpointer(ctx.engine) as checkpointer:
        return await ask(build_graph(deps, checkpointer), deps, question, thread)


def thread_id(name: str) -> str:
    return f"{name}-{datetime.now(UTC).timestamp()}"


async def test_a_lookup_cites_its_filing_and_is_logged(
    ctx: ToolContext, cache: ResponseCache, sessions: async_sessionmaker[AsyncSession]
) -> None:
    script = Script(
        [
            fetch_revenue(["FY2026Q2"]),
            say("Agentco's revenue was $11.0 billion in FY2026 Q2 [{0}]."),
        ]
    )
    result = await run(
        ctx, script, cache, "What was Agentco's revenue in FY2026 Q2?", thread_id("lookup")
    )
    assert result.question_type == "lookup" and result.verified and result.revisions == 0
    assert result.answer == (
        "Agentco's revenue was $11.0 billion in FY2026 Q2 [1].\n\nSources:\n"
        f"[1] AGNT 10-Q filed 2026-08-01: {url(Q2_10Q, 'agnt-q2.htm')}"
    )
    assert script.nodes == ["executor", "executor", "verifier", "answer"]  # router: rules
    assert [c.name for c in result.tool_calls] == ["get_actuals"]
    assert result.total("calls") == 4 and result.cost > 0 and result.served_from_cache is False
    run_id = await log_run(sessions, result, "scripted@test")
    async with sessions() as session:
        logged = await session.get(AgentRun, run_id)
    assert logged is not None and logged.question_type == "lookup"
    assert logged.tool_calls[0]["name"] == "get_actuals"
    assert logged.tool_calls[0]["arguments"]["periods"] == ["FY2026Q2"]
    assert logged.prompt_version.startswith("router/v1 planner/v1")
    assert logged.llm_calls == 4 and logged.verifier_failures == {} and logged.verified


async def test_the_code_checks_send_an_ungrounded_draft_back(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    script = Script(
        [
            fetch_revenue(["FY2026Q2"]),
            say("Agentco's revenue was $12.5 billion [{0}]."),  # not in the evidence
            say("Agentco's revenue was $11.0 billion [{0}]."),
        ]
    )
    result = await run(
        ctx, script, cache, "What was Agentco's revenue in FY2026 Q2?", thread_id("revise")
    )
    assert result.revisions == 1 and result.verified
    assert result.failure_counts == {"numbers_grounded": 1}
    assert "$11.0 billion [1]" in result.answer
    feedback = script.seen[-1][-1]
    assert isinstance(feedback, HumanMessage) and "numbers_grounded" in text_of(feedback)
    # the model verifier ran once, on the draft that passed the code checks
    assert script.nodes.count("verifier") == 1


async def test_the_model_verifier_catches_what_code_cannot(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    script = Script(
        [
            fetch_revenue(["FY2026Q2"]),
            say("Agentco's revenue was $11.0 billion [{0}]."),
            say("Agentco's revenue was $11.0 billion in FY2026 Q2 [{0}]."),
        ],
        verdicts=[False, True],
    )
    result = await run(
        ctx, script, cache, "What was Agentco's revenue in FY2026 Q2?", thread_id("llm-check")
    )
    assert result.revisions == 1 and result.failure_counts == {"llm_unsupported": 1}
    assert result.verified


async def test_after_two_revisions_only_verified_sentences_remain(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    script = Script(
        [
            fetch_revenue(["FY2026Q2"]),
            say("Agentco's revenue was $11.0 billion [{0}]. Margins were 61% [{0}]."),
        ]
    )
    result = await run(
        ctx,
        script,
        cache,
        "What was Agentco's revenue and margin in FY2026 Q2?",
        thread_id("give-up"),
    )
    assert result.revisions == 2 and not result.verified
    assert result.failure_counts == {"numbers_grounded": 3}
    body = result.answer.split("\n\nSources:")[0]
    assert body.startswith("Agentco's revenue was $11.0 billion [1].")
    assert body.endswith(
        "Left out because they could not be verified (a figure not in the evidence): one statement."
    )
    assert "61%" not in body


async def test_out_of_scope_questions_get_no_tool_or_model_call(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    script = Script([fetch_revenue(["latest"])])
    advice = await run(ctx, script, cache, "Should I buy Agentco stock?", thread_id("advice"))
    assert advice.question_type == "out_of_scope" and ADVICE_NOTE in advice.answer
    assert advice.tool_calls == [] and script.nodes == []
    assert advice.served_from_cache is None
    unknown = await run(ctx, script, cache, "What was Initech's revenue?", thread_id("other"))
    assert unknown.question_type == "out_of_scope" and script.nodes == ["router"]
    assert ADVICE_NOTE not in unknown.answer and unknown.tool_calls == []


async def test_a_follow_up_on_the_same_thread_uses_the_conversation(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    thread = thread_id("follow-up")
    first = Script(
        [
            fetch_revenue(["latest"]),
            say("Agentco's revenue last quarter, FY2026 Q2, was $11.0 billion [{0}]."),
        ]
    )
    await run(ctx, first, cache, "What was Agentco's revenue last quarter?", thread)
    second = Script(
        [
            fetch_revenue(["latest-1"]),
            say("The quarter before, FY2026 Q1, revenue was $10.2 billion [{0}]."),
        ]
    )
    result = await run(ctx, second, cache, "And the quarter before?", thread)
    assert result.question_type == "lookup"  # by rule: a follow-up keeps the type
    history = second.seen[0]
    assert [type(m).__name__ for m in history[1:4]] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert "last quarter" in text_of(history[1]) and "$11.0 billion [E" in text_of(history[2])
    assert "$10.2 billion [1]" in result.answer and url(Q2_10Q, "agnt-q2.htm") in result.answer
    # evidence ids continue across the conversation
    assert sorted(result.evidence) == ["E1", "E2"]


async def test_an_identical_rerun_is_served_from_the_cache(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    script = Script(
        [
            fetch_revenue(["FY2026Q2"]),
            say("Agentco's revenue was $11.0 billion in FY2026 Q2 [{0}]."),
        ]
    )
    question = "What was Agentco's revenue in FY2026 Q2?"
    first = await run(ctx, script, cache, question, thread_id("cache-a"))
    calls_made = len(script.nodes)
    again = await run(ctx, script, cache, question, thread_id("cache-b"))
    assert again.served_from_cache is True and again.cost == 0
    assert len(script.nodes) == calls_made and again.answer == first.answer


async def test_the_executor_stops_after_eight_tool_calls(
    ctx: ToolContext, cache: ResponseCache
) -> None:
    def greedy(messages: Sequence[BaseMessage]) -> AIMessage:
        if "used all 8 tool calls" in text_of(messages[-1]):
            return AIMessage("I could not finish within the tool-call limit.")
        three = {"ticker": TICKER, "metrics": ["revenue"], "periods": ["latest"]}
        return calls(("get_actuals", three), ("get_actuals", three), ("get_actuals", three))

    result = await run(
        ctx, Script([greedy]), cache, "What was Agentco's revenue history?", thread_id("cap")
    )
    assert len(result.tool_calls) == MAX_TOOL_CALLS
    assert "tool-call limit" in result.answer


async def test_a_call_over_the_budget_is_refused(
    ctx: ToolContext, cache: ResponseCache, sessions: async_sessionmaker[AsyncSession]
) -> None:
    script = Script([fetch_revenue(["FY2026Q2"])])
    spent = Budget(Decimal(10), Pricing(Decimal("0.10"), Decimal("0.50")), limit=Decimal("0.0001"))
    with pytest.raises(BudgetExhausted, match="LLM budget"):
        await run(
            ctx,
            script,
            cache,
            "What was Agentco's revenue in FY2026 Q2?",
            thread_id("budget"),
            spent,
        )
    assert script.nodes == []


@pytest.fixture
def scripted_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: AsyncEngine, ctx: ToolContext
) -> Iterator[Script]:
    """`ask` with the scripted model, the test universe and fake search models."""
    script = Script(
        [fetch_revenue(["latest"]), say("Agentco's revenue last quarter was $11.0 billion [{0}].")]
    )
    monkeypatch.setattr(commands, "chat_model", lambda settings, model: ScriptedChat(script))
    monkeypatch.setattr(commands, "load_universe", lambda path: UNIVERSE)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "llm"))
    monkeypatch.setenv("LLM_INPUT_PRICE", "0.10")
    monkeypatch.setenv("LLM_OUTPUT_PRICE", "0.50")
    from talk_is_cheap.config import get_settings
    from talk_is_cheap.retrieval import encoders

    monkeypatch.setattr(encoders, "load_embedder", lambda settings: FakeEmbedder())
    monkeypatch.setattr(encoders, "load_reranker", lambda settings: FakeReranker())
    get_settings.cache_clear()
    yield script
    get_settings.cache_clear()


def test_cli_ask_prints_the_answer_a_summary_and_logs_the_run(
    scripted_cli: Script, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "ask",
                "What was Agentco's revenue last quarter?",
                "--show-trace",
                "--thread",
                "cli-thread",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "estimate for 1 question" in out and "LLM budget:" in out
    assert "· router: lookup (rules: a company and a metric or period)" in out
    assert '· executor -> get_actuals({"ticker": "AGNT"' in out
    assert "· tools <- get_actuals: {" in out
    assert "revenue last quarter was $11.0 billion [1]." in out
    summary = out.strip().splitlines()[-1]
    assert summary.startswith("1 tool calls, ") and "0 revisions" in summary
    assert summary.endswith(", 0 of 4 model calls cached")


def test_cli_ask_reads_questions_until_an_empty_line(
    scripted_cli: Script, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = iter(["What was Agentco's revenue last quarter?", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert main(["ask"]) == 0
    out = capsys.readouterr().out
    assert "Ask a question" in out and "$11.0 billion [1]" in out


def test_cli_agent_graph_writes_mermaid(tmp_path: Path) -> None:
    out = tmp_path / "graph.md"
    assert main(["agent-graph", "--output", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Agent graph") and "```mermaid" in text
    for node in ("router", "planner", "executor", "tools", "verifier", "answer"):
        assert node in text
