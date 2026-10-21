"""The HTTP API against a real database, with the scripted model (never a provider):
every endpoint's shape, filters, pagination and 404s on seeded rows; the chat stream's
events, citations, follow-ups, timeouts, a client going away, budgets and the ledger;
API keys, rate limits, the request log; and the new migration's round trip."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from langchain_core.messages import AIMessage, BaseMessage
from psycopg import sql
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.agent import tools as agent_tools
from talk_is_cheap.agent.fake import fake_chat
from talk_is_cheap.agent.llm import ChatModel
from talk_is_cheap.agent.tools import GetActualsInput, ToolContext, ToolOutput
from talk_is_cheap.api.app import ApiConfigError, Overrides, Services, create_app
from talk_is_cheap.api.chat import CHAT_COMMAND, STOPPED_ANSWER
from talk_is_cheap.api.security import key_id
from talk_is_cheap.config import Settings, get_settings
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    AgentRun,
    Article,
    ArticleCompany,
    Company,
    Document,
    Feedback,
    Filing,
    FinancialValue,
    Guidance,
    LlmSpend,
    Promise,
    PromiseReview,
    PromiseVerdict,
    RequestLog,
    Statement,
    StatementExtraction,
)
from talk_is_cheap.guidance.llm import ResponseCache, Usage
from talk_is_cheap.universe import Executive, Universe, UniverseCompany
from tests.retrieval_fakes import FakeEmbedder, FakeReranker

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CIK, TICKER = 990_201, "APIX"
OTHER_CIK, OTHER = 990_202, "APIY"
B = Decimal(10) ** 9
KEY, OTHER_KEY = "test-key-0123456789", "other-key-9876543210"
NEWS = "https://api.example.test"
UNIVERSE = Universe(
    history_start=date(2025, 1, 1),
    forms=["10-Q", "10-K", "8-K"],
    companies=[
        UniverseCompany(
            ticker=TICKER,
            name="Apixco",
            executives=[
                Executive(name="Jane Roe", role="Chief Executive Officer", aliases=["Janie Roe"]),
                Executive(name="John Doe", role="Chief Financial Officer"),
            ],
        ),
        UniverseCompany(ticker=OTHER, name="Apiyco"),
    ],
)
Q1_10Q, Q2_10Q = "0000990201-26-000010", "0000990201-26-000020"
FILINGS = [
    (Q1_10Q, "10-Q", date(2026, 5, 1), "apix-q1.htm"),
    (Q2_10Q, "10-Q", date(2026, 8, 1), "apix-q2.htm"),
]
QUESTION = "What was Apixco's revenue last quarter?"


def doc_url(accession: str, name: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession.replace('-', '')}/{name}"


def value(
    metric: str, quarter: int, amount: Decimal, accession: str, filed: date
) -> FinancialValue:
    return FinancialValue(
        cik=CIK,
        metric=metric,
        period_type="quarter",
        fiscal_year=2026,
        fiscal_quarter=quarter,
        period_start=date(2026, 3 * quarter - 2, 1),
        period_end=date(2026, 3 * quarter, 28),
        value=amount,
        unit="USD",
        source_concept="us-gaap:Test",
        accession_no=accession,
        form="10-Q",
        filed=filed,
        derived=False,
    )


def guidance(item_key: str, **values: Any) -> Guidance:
    base: dict[str, Any] = {
        "cik": CIK,
        "accession_no": Q1_10Q,
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "issued_at": date(2026, 5, 1),
        "metric": "revenue",
        "metric_raw": "revenue",
        "basis": "unspecified",
        "measure": "level",
        "constant_currency": False,
        "period_text": "second quarter",
        "target_fiscal_year": 2026,
        "target_fiscal_quarter": 2,
        "unit": "usd",
        "point": 11 * B,
        "model": "fake",
        "prompt_version": "v1",
        "verdict": "met",
        "item_key": item_key,
        "plausibility": "passed",
        "actual": 11 * B,
    }
    return Guidance(**{**base, **values})


def promise(n: int, article_id: int, **values: Any) -> Promise:
    base: dict[str, Any] = {
        "claim_key": f"api-claim-{n}",
        "version": "v3+5",
        "current": True,
        "article_id": article_id,
        "ticker": TICKER,
        "speaker": "Jane Roe",
        "statement_date": date(2026, 1, n),
        "quote": f"We will ship product {n} by June, and " + "it will be great " * 12,
        "subject": f"product {n} shipping",
        "direction": "at_least",
        "target_kind": "event",
        "event": f"Apixco ships product {n}",
        "due_date": date(2026, 6, 30),
        "due_basis": "explicit",
        "resolution_rule": f"True if Apixco ships product {n} on or before 2026-06-30.",
        "specificity": "specific",
        "bold": False,
        "group_key": f"api-group-{n}",
        "framing": {},
        "notes": [],
        "model": "fake",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": Decimal(0),
    }
    return Promise(**{**base, **values})


def verdict(promise_id: int, name: str, **values: Any) -> PromiseVerdict:
    base: dict[str, Any] = {
        "promise_id": promise_id,
        "verdict": name,
        "model_verdict": name,
        "assessments": [
            {"evidence_id": "E1", "finding": "satisfies", "note": "shipped"},
            {"evidence_id": "E2", "finding": "irrelevant"},
        ],
        "failed_checks": [],
        "checks": [{"check": "evidence_exist", "passed": True, "detail": ""}],
        "evidence": [
            {
                "id": "E1",
                "date": "2026-05-01",
                "tool": "search_filings",
                "url": doc_url(Q1_10Q, "apix-q1.htm"),
                "data": {"text": "Product shipped in April."},
            }
        ],
        "gathered": [{"id": "E1"}, {"id": "E2"}],
        "reasoning": "The 10-Q says the product shipped.",
        "rule_version": "v3+5",
        "rule_hash": "hash",
        "evidence_key": f"key-{promise_id}",
        "model": "fake",
        "prompt_version": "v3",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": Decimal(0),
    }
    return PromiseVerdict(**{**base, **values})


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(RequestLog))
        await session.execute(delete(AgentRun))
        await session.execute(delete(LlmSpend))
        await session.execute(delete(PromiseReview).where(PromiseReview.ticker == TICKER))
        await session.execute(delete(Promise).where(Promise.ticker == TICKER))
        await session.execute(delete(Article).where(Article.url.like(f"{NEWS}/%")))
        await session.execute(delete(Company).where(Company.cik.in_([CIK, OTHER_CIK])))


@pytest.fixture(scope="module")
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    async with factory() as session, session.begin():
        session.add(Company(cik=CIK, ticker=TICKER, name="APIXCO INC", fiscal_year_end="1231"))
        session.add(Company(cik=OTHER_CIK, ticker=OTHER, name="APIYCO INC", fiscal_year_end="1231"))
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
        await session.flush()
        for quarter, (accession, _, filed, primary) in enumerate(FILINGS, start=1):
            session.add(
                Document(
                    accession_no=accession,
                    cik=CIK,
                    doc_type="10q",
                    document_name=primary,
                    url=doc_url(accession, primary),
                    fiscal_year=2026,
                    fiscal_quarter=quarter,
                    period_end=date(2026, 3 * quarter, 28),
                    filed_at=filed,
                    text="Quarterly report.",
                    content_hash=hashlib.sha256(primary.encode()).hexdigest(),
                    parser_version="test",
                )
            )
        session.add_all(
            [
                value("revenue", 1, 10 * B, Q1_10Q, date(2026, 5, 1)),
                value("revenue", 1, Decimal("10.2") * B, Q2_10Q, date(2026, 8, 1)),  # restated
                value("revenue", 2, 11 * B, Q2_10Q, date(2026, 8, 1)),
                value("gross_profit", 2, Decimal("5.5") * B, Q2_10Q, date(2026, 8, 1)),
                guidance("g-met"),
                guidance(
                    "g-flagged",
                    metric="eps",
                    unit="usd_per_share",
                    point=Decimal("0.50"),
                    actual=Decimal("0.60"),
                    verdict="beat",
                    plausibility="flagged",
                    plausibility_rule="ratio",
                    plausibility_note="guided 0.50 is 20% of prior 2.50",
                ),
                guidance("g-wrong", metric="capex", point=Decimal("0.2") * B, review="wrong"),
            ]
        )
        article = Article(
            url_hash=hashlib.sha256(f"{NEWS}/interview".encode()).hexdigest(),
            url=f"{NEWS}/interview",
            title="Apixco's chief on the road ahead",
            publisher="Test Wire",
            published_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
            time_precision="published",
        )
        session.add(article)
        await session.flush()
        session.add(
            ArticleCompany(
                article_id=article.id, ticker=TICKER, method="headline", confidence=Decimal("0.9")
            )
        )
        extraction = StatementExtraction(
            article_id=article.id,
            cluster_id=article.id,
            ticker=TICKER,
            model="fake",
            prompt_version="v1",
            input_hash="x",
            processing_version="1",
            claims=[],
            dropped=[],
            input_tokens=0,
            output_tokens=0,
            cost=Decimal(0),
        )
        session.add(extraction)
        await session.flush()
        session.add(
            Statement(
                extraction_id=extraction.id,
                article_id=article.id,
                cluster_id=article.id,
                ticker=TICKER,
                quote="We will ship it",
                char_start=0,
                char_end=15,
                attribution="named_executive",
                executive_name="Jane Roe",
                management=True,
                is_direct_quote=True,
                venue="interview",
                statement_date=date(2026, 1, 1),
                date_from_article=False,
                claim_type="plan",
                claim="ship it",
                basis="unspecified",
                measure="level",
                unit="other",
                item_key="s1",
                model="fake",
                prompt_version="v1",
            )
        )
        # Jane Roe: five resolved claims (rates), one bold. John Doe: one open claim.
        names = ["delivered", "delivered", "not_delivered", "delivered_late", "partly_delivered"]
        rows = [promise(n + 1, article.id, bold=n == 0) for n in range(5)]
        rows.append(
            promise(6, article.id, speaker="John Doe", due_date=date(2027, 6, 30), bold=False)
        )
        rows.append(promise(7, article.id, claim_type="scheduled_action", subject="dividend"))
        rows.append(
            promise(8, article.id, specificity="vague", due_date=None, due_basis="open_ended")
        )
        session.add_all(rows)
        await session.flush()
        for row, name in zip(rows, names, strict=False):
            session.add(verdict(row.id, name, revised_before_due=name == "delivered_late"))
        session.add(verdict(rows[5].id, "too_early"))
        session.add(verdict(rows[6].id, "delivered"))
        session.add(
            PromiseReview(
                claim_key=rows[2].claim_key,
                ticker=TICKER,
                verdict="walked_back",
                note="lowered in March",
                summary="reviewed",
            )
        )
    yield factory
    await _clean(factory)


def settings_for(tmp_path: Path, **changes: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "api_keys": [KEY],
        "llm_provider": "fake",
        "llm_input_price": "0.10",
        "llm_output_price": "0.50",
        "llm_cache_dir": tmp_path / "llm",
    }
    return Settings(**{**base, **changes})


@asynccontextmanager
async def running(
    settings: Settings, model: ChatModel | None = None
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(
        settings,
        Overrides(
            model=model or fake_chat(UNIVERSE),
            universe=UNIVERSE,
            embedder=FakeEmbedder,
            reranker=FakeReranker,
            cache=ResponseCache(settings.llm_cache_dir),
        ),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": KEY}
        ) as client:
            yield app, client


@pytest.fixture
async def api(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    async with running(settings_for(tmp_path)) as found:
        yield found


def services(app: FastAPI) -> Services:
    found: Services = app.state.services
    return found


def parse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE events as (name, data), keep-alive comments left out."""
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if lines:
            name = lines[0].removeprefix("event: ")
            events.append((name, json.loads(lines[1].removeprefix("data: "))))
    return events


async def ask(
    client: httpx.AsyncClient, question: str = QUESTION, thread: str | None = None
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:
    body: dict[str, Any] = {"question": question}
    if thread:
        body["thread_id"] = thread
    response = await client.post("/v1/chat", json=body)
    return response, parse(response.text)


async def spend_rows(sessions: async_sessionmaker[AsyncSession]) -> list[LlmSpend]:
    async with sessions() as session:
        return list((await session.scalars(select(LlmSpend).order_by(LlmSpend.id))).all())


async def log_row(sessions: async_sessionmaker[AsyncSession], request_id: str) -> RequestLog:
    async with sessions() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == request_id))
    assert row is not None
    return row


@pytest.fixture(autouse=True)
async def fresh_ledger(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(LlmSpend))


# Health and docs.


async def test_health_readiness_and_docs_need_no_key(
    api: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    _, client = api
    anonymous = {"X-API-Key": ""}
    assert (await client.get("/healthz", headers=anonymous)).json() == {"status": "ok"}
    ready = await client.get("/readyz", headers=anonymous)
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready" and body["database"] and body["migration"] == body["head"]
    assert (await client.get("/docs", headers=anonymous)).status_code == 200
    schema = (await client.get("/openapi.json", headers=anonymous)).json()
    assert "/v1/chat" in schema["paths"] and "/v1/companies/{ticker}/promises" in schema["paths"]
    for model in ("DoneEvent", "ErrorEvent", "CitationEvent", "PromisePage", "TrackRecordPage"):
        assert model in schema["components"]["schemas"]


async def test_readiness_fails_when_the_migration_is_behind(
    api: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, client = api
    services(app).head = "9999"
    ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready" and "expects 9999" in ready.json()["detail"]


# Data endpoints.


async def test_companies_with_coverage_and_pagination(
    api: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    _, client = api
    first = (await client.get("/v1/companies", params={"limit": 1})).json()
    rows = list(first["items"])
    cursor = first["next_cursor"]
    while cursor:
        following = (
            await client.get("/v1/companies", params={"limit": 1, "cursor": cursor})
        ).json()
        rows += following["items"]
        cursor = following["next_cursor"]
    ours = {r["ticker"]: r for r in rows if r["ticker"] in (TICKER, OTHER)}
    assert ours[TICKER] == {
        "ticker": TICKER,
        "cik": CIK,
        "name": "APIXCO INC",
        "fiscal_year_end": "1231",
        "filings": 2,
        "latest_quarter": "FY2026 Q2",
        "guidance_rows": 3,
        "linked_articles": 1,
        "management_claims": 1,
        # Due 2026-06-30, with the 10-Q of that quarter filed: not the claim due in 2027,
        # and never the vague one.
        "due_claims": 6,
    }
    assert ours[OTHER]["latest_quarter"] is None and ours[OTHER]["guidance_rows"] == 0
    assert (await client.get("/v1/companies", params={"cursor": "bm9wZQ"})).status_code == 400
    assert (await client.get("/v1/companies", params={"limit": 201})).status_code == 422


async def test_promises_filters_and_fields(api: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, client = api
    url = f"/v1/companies/{TICKER.lower()}/promises"
    everything = (await client.get(url, params={"limit": 200})).json()["items"]
    assert len(everything) == 7  # the scheduled action is left out
    newest = everything[0]
    assert newest["speaker"] == "Jane Roe" and newest["specificity"] == "vague"
    assert newest["scored"] is False
    first = next(p for p in everything if p["subject"] == "product 1 shipping")
    assert first["role"] == "Chief Executive Officer" and first["bold"] is True
    assert first["quote"].endswith("…") and len(first["quote"].split()) == 41
    assert first["url"] == f"{NEWS}/interview" and first["publisher"] == "Test Wire"
    assert first["rule"].startswith("True if Apixco ships product 1")
    assert first["verdict"] == "delivered" and first["reviewed"] is False
    assert first["evidence"][0]["url"] == doc_url(Q1_10Q, "apix-q1.htm")
    assert first["evidence"][0]["date"] == "2026-05-01"
    assert first["target"] is None and first["measured"] is None  # an event
    reviewed = next(p for p in everything if p["subject"] == "product 3 shipping")
    assert reviewed["verdict"] == "walked_back" and reviewed["reviewed"] is True
    late = next(p for p in everything if p["subject"] == "product 4 shipping")
    assert late["revised_before_due"] is True

    async def subjects(**params: Any) -> list[str]:
        found = (await client.get(url, params=params)).json()["items"]
        return sorted(p["subject"] for p in found)

    assert await subjects(verdict="walked_back") == ["product 3 shipping"]
    assert await subjects(speaker="John Doe") == ["product 6 shipping"]
    assert len(await subjects(speaker="Janie Roe")) == 6  # an alias of Jane Roe
    assert await subjects(open=True) == ["product 6 shipping", "product 8 shipping"]
    assert await subjects(bold=True) == ["product 1 shipping"]
    assert await subjects(due_after="2027-01-01") == ["product 6 shipping"]
    assert len(await subjects(due_before="2026-12-31")) == 5
    assert "dividend" in await subjects(include_scheduled=True)
    page = (await client.get(url, params={"limit": 3})).json()
    assert len(page["items"]) == 3 and page["next_cursor"]
    rest = (await client.get(url, params={"limit": 3, "cursor": page["next_cursor"]})).json()
    assert {p["id"] for p in rest["items"]}.isdisjoint({p["id"] for p in page["items"]})
    assert (await client.get("/v1/companies/NOPE/promises")).status_code == 404
    assert (await client.get(url, params={"verdict": "maybe"})).status_code == 422


async def test_promise_detail_like_promises_show(api: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, client = api
    listed = (await client.get(f"/v1/companies/{TICKER}/promises")).json()["items"]
    reviewed = next(p for p in listed if p["subject"] == "product 3 shipping")
    detail = (await client.get(f"/v1/promises/{reviewed['id']}")).json()
    assert detail["version"] == "v3+5" and detail["current"] is True
    assert detail["claim"].startswith("We will ship product 3 by June")
    resolution = detail["resolution"]
    assert resolution["verdict"] == "not_delivered" and resolution["rule_version"] == "v3+5"
    assert resolution["findings"] == [
        {"evidence_id": "E1", "finding": "satisfies", "note": "shipped"}
    ]  # the irrelevant item is left out, as `promises show` does
    assert resolution["checks"][0] == {"check": "evidence_exist", "passed": True, "detail": None}
    assert resolution["evidence"][0]["description"] == "Product shipped in April."
    assert resolution["gathered"] == 2
    assert detail["review"]["verdict"] == "walked_back"
    assert detail["review"]["note"] == "lowered in March"
    assert detail["verdict"] == "walked_back"  # the review counts
    missing = await client.get("/v1/promises/999999999")
    assert missing.status_code == 404 and missing.json() == {"detail": "no promise 999999999"}


async def test_track_records(api: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, client = api
    found = (await client.get("/v1/track-record", params={"ticker": TICKER})).json()
    assert found["min_resolved"] == 5
    records = {r["key"]: r for r in found["items"]}
    jane = records["Jane Roe (APIX)"]
    assert jane["resolved"] == 5 and jane["too_few_to_judge"] is False
    assert jane["by_verdict"] == {
        "delivered": 2,
        "walked_back": 1,
        "delivered_late": 1,
        "partly_delivered": 1,
    }
    assert jane["rates"]["on_time_rate"] == pytest.approx(0.4)
    assert jane["rates"]["walked_back_share"] == pytest.approx(0.2)
    assert jane["vague"] == 1
    john = records["John Doe (APIX)"]
    assert john["too_few_to_judge"] is True and john["rates"] is None and john["resolved"] == 0
    by_company = (
        await client.get("/v1/track-record", params={"ticker": TICKER, "by": "company"})
    ).json()["items"]
    assert [r["key"] for r in by_company] == [TICKER] and by_company[0]["claims"] == 6
    specific = (
        await client.get("/v1/track-record", params={"ticker": TICKER, "specific_only": True})
    ).json()["items"]
    assert specific
    assert (await client.get("/v1/track-record", params={"ticker": "NOPE"})).status_code == 404
    assert (await client.get("/v1/track-record", params={"by": "team"})).status_code == 422


async def test_guidance_marks_flagged_and_wrong_rows(
    api: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    _, client = api
    rows = (await client.get(f"/v1/companies/{TICKER}/guidance")).json()["items"]
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["revenue"]["mark"] is None and by_metric["revenue"]["counts_in_verdicts"]
    assert by_metric["revenue"]["verdict"] == "met" and by_metric["revenue"]["issued_period"] == (
        "FY2026Q1"
    )
    assert by_metric["revenue"]["target_period"] == "FY2026Q2"
    flagged = by_metric["eps"]
    assert flagged["mark"] == "REVIEW" and flagged["review_status"] == "needs review"
    assert flagged["counts_in_verdicts"] is False and flagged["verdict"] == "beat"
    wrong = by_metric["capex"]
    assert wrong["mark"] == "WRONG" and wrong["verdict"] == "met"  # shown, not counted
    assert wrong["review_status"] == "reviewed: wrong" and not wrong["counts_in_verdicts"]
    only = (await client.get(f"/v1/companies/{TICKER}/guidance", params={"flagged": True})).json()
    assert [r["metric"] for r in only["items"]] == ["eps"]
    eps = (await client.get(f"/v1/companies/{TICKER}/guidance", params={"metric": "eps"})).json()
    assert [r["metric"] for r in eps["items"]] == ["eps"]
    assert (await client.get("/v1/companies/NOPE/guidance")).status_code == 404


async def test_financials_vintages(api: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, client = api
    url = f"/v1/companies/{TICKER}/financials"
    first = (await client.get(url)).json()
    assert first["vintage"] == "first" and [q["label"] for q in first["items"]] == [
        "FY2026 Q2",
        "FY2026 Q1",
    ]
    q2 = first["items"][0]
    assert Decimal(q2["gross_margin"]) == Decimal("0.5")
    assert Decimal(q2["metrics"]["revenue"]["value"]) == 11 * B
    assert first["items"][1]["metrics"]["revenue"]["accession_no"] == Q1_10Q
    latest = (await client.get(url, params={"vintage": "latest"})).json()
    assert Decimal(latest["items"][1]["metrics"]["revenue"]["value"]) == Decimal("10.2") * B
    one = (await client.get(url, params={"quarters": 1})).json()
    assert len(one["items"]) == 1 and one["next_cursor"] is None
    paged = (await client.get(url, params={"limit": 1})).json()
    assert len(paged["items"]) == 1 and paged["next_cursor"]
    assert (await client.get(url, params={"vintage": "best"})).status_code == 422
    assert (await client.get("/v1/companies/NOPE/financials")).status_code == 404


# Chat.


async def test_chat_streams_status_tools_tokens_citations_and_done(
    api: tuple[FastAPI, httpx.AsyncClient], sessions: async_sessionmaker[AsyncSession]
) -> None:
    _, client = api
    response, events = await ask(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    thread = response.headers["x-thread-id"]
    names = [name for name, _ in events]
    steps = [data["node"] for name, data in events if name == "status"]
    assert steps == ["router", "executor", "tools", "executor", "verifier", "answer"]
    first_token = names.index("token")
    assert names[:first_token] == [
        "status",
        "status",
        "status",
        "tool_start",
        "tool_end",
        "status",
        "status",
        "status",
    ]
    assert set(names[first_token:-2]) == {"token"}
    assert names[-2:] == ["citation", "done"]
    start = dict(events)["tool_start"]
    assert start["name"] == "get_actuals"
    assert start["arguments"] == {"ticker": TICKER, "metrics": ["revenue"], "periods": ["latest"]}
    end = dict(events)["tool_end"]
    assert end["call_id"] == start["call_id"] and end["evidence"] == ["E1"]
    assert end["summary"] == "1 evidence item: E1"
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert answer == "APIX's revenue in FY2026Q2 was $11,000M [1]."
    citation = dict(events)["citation"]
    assert citation == {
        "id": 1,
        "url": doc_url(Q2_10Q, "apix-q2.htm"),
        "title": "APIX 10-Q filed 2026-08-01",
        "provenance": "sec_filing",
        "evidence": ["E1"],
    }
    done = dict(events)["done"]
    request_id = response.headers["x-request-id"]
    assert done["request_id"] == request_id and done["thread_id"] == thread
    assert done["question_type"] == "lookup" and done["verified"] is True
    assert done["revisions"] == 0 and done["tool_calls"] == 1 and done["cache_hit"] is False
    assert done["input_tokens"] > 0 and Decimal(done["cost"]) > 0

    # The ledger records every paid call under the key's id; the run is logged and linked.
    rows = await spend_rows(sessions)
    assert {(r.command, r.api_key) for r in rows} == {(CHAT_COMMAND, key_id(KEY))}
    # The ledger keeps 6 decimals.
    assert float(sum(r.cost for r in rows)) == pytest.approx(float(done["cost"]), abs=1e-5)
    logged = await log_row(sessions, request_id)
    assert logged.outcome == "completed" and logged.thread_id == thread
    assert logged.agent_run_id is not None and logged.tool_calls == 1
    assert logged.cost == pytest.approx(Decimal(done["cost"]), abs=Decimal("0.000001"))
    assert logged.model == "scripted@fake"
    assert logged.key_id == key_id(KEY) and logged.status == 200
    async with sessions() as session:
        run = await session.get(AgentRun, logged.agent_run_id)
    assert run is not None and run.thread_id == thread and run.verified

    # The same question on a new thread comes from the response cache, at no cost.
    _, again = await ask(client)
    assert dict(again)["done"]["cache_hit"] is True and Decimal(dict(again)["done"]["cost"]) == 0


async def test_follow_ups_on_one_thread_and_its_history(
    api: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    _, client = api
    response, _ = await ask(client)
    thread = response.headers["x-thread-id"]
    follow_up, events = await ask(client, "And what about Apixco's revenue now?", thread)
    assert follow_up.headers["x-thread-id"] == thread
    assert dict(events)["done"]["thread_id"] == thread
    # Citation ids continue across the thread: this turn's evidence is E2.
    assert dict(events)["tool_end"]["evidence"] == ["E2"]
    history = (await client.get(f"/v1/threads/{thread}")).json()
    assert history["thread_id"] == thread
    assert [t["question"] for t in history["turns"]] == [
        QUESTION,
        "And what about Apixco's revenue now?",
    ]
    assert history["turns"][1]["answer"] == "APIX's revenue in FY2026Q2 was $11,000M [1]."
    assert history["turns"][1]["sources"][0]["url"] == doc_url(Q2_10Q, "apix-q2.htm")
    assert (await client.get("/v1/threads/no-such-thread")).status_code == 404
    assert (await client.get("/v1/threads/bad thread")).status_code == 422


async def test_question_size_and_thread_length_limits(
    api: tuple[FastAPI, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from talk_is_cheap.api import chat

    _, client = api
    too_long = await client.post("/v1/chat", json={"question": "x" * 2001})
    assert too_long.status_code == 422
    monkeypatch.setattr(chat, "MAX_TURNS", 1)
    response, _ = await ask(client)
    _, events = await ask(client, QUESTION, response.headers["x-thread-id"])
    assert [name for name, _ in events] == ["error"]
    assert dict(events)["error"]["code"] == "thread_full"
    assert dict(events)["error"]["status"] == 409


class Gated:
    """A model whose `call`-th response (1-based) waits for `gate`; `waiting` is set
    when it starts waiting."""

    def __init__(self, inner: ChatModel, call: int) -> None:
        self._inner = inner
        self._call = call
        self.calls = 0
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    @property
    def name(self) -> str:
        return self._inner.name

    async def respond(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str | None,
        max_tokens: int,
        node: str,
    ) -> tuple[AIMessage, Usage]:
        self.calls += 1
        if self.calls == self._call:
            self.waiting.set()
            await self.gate.wait()
        return await self._inner.respond(messages, tools, tool_choice, max_tokens, node)


async def test_a_slow_question_times_out_with_an_error_event(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    model = Gated(fake_chat(UNIVERSE), call=2)
    settings = settings_for(tmp_path, chat_timeout_seconds=0.5)
    async with running(settings, model) as (app, client):
        started = asyncio.get_running_loop().time()
        response, events = await ask(client)
        assert asyncio.get_running_loop().time() - started < 5  # never hangs
        assert [name for name, _ in events][-1] == "error"
        error = dict(events)["error"]
        assert error["code"] == "timeout" and error["status"] == 504
        assert error["request_id"] == response.headers["x-request-id"]
        model.gate.set()  # the call in flight finishes, and its cost is recorded
        await services(app).settle()
        # The thread's turn is closed, so the thread takes another question.
        history = (await client.get(f"/v1/threads/{response.headers['x-thread-id']}")).json()
        assert history["turns"][0]["answer"] == STOPPED_ANSWER
    rows = await spend_rows(sessions)
    assert len(rows) == 2  # the first executor call, and the one stopped in flight
    logged = await log_row(sessions, response.headers["x-request-id"])
    assert logged.outcome == "timeout"
    assert float(logged.cost or 0) == pytest.approx(float(sum(r.cost for r in rows)), abs=1e-5)


async def test_a_slow_tool_call_is_an_error_the_model_reads_and_the_run_goes_on(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(ctx: ToolContext, args: GetActualsInput) -> ToolOutput:
        await asyncio.sleep(30)
        raise AssertionError("not reached")

    monkeypatch.setitem(agent_tools.TOOLS, "get_actuals", (slow, GetActualsInput))
    model = fake_chat(UNIVERSE)
    settings = settings_for(tmp_path, tool_timeout_seconds=0.2)
    async with running(settings, model) as (_, client):
        started = asyncio.get_running_loop().time()
        response, events = await ask(client)
        assert asyncio.get_running_loop().time() - started < 5
    names = [name for name, _ in events]
    assert "error" not in names and names[-1] == "done"
    message = "get_actuals timed out after 0.2 seconds; try a narrower query or another tool"
    end = dict(events)["tool_end"]
    assert end["error"] == message and end["summary"] == f"error: {message}"
    # The model read the error as the tool's result and answered without the figure.
    executor_calls = [m for node, m in model.calls if node == "executor"]
    assert len(executor_calls) == 2 and message in str(executor_calls[1][-1].content)
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert answer == "I could not find a reported revenue for that company."
    logged = await log_row(sessions, response.headers["x-request-id"])
    assert logged.outcome == "completed" and logged.tool_calls == 1
    async with sessions() as session:
        run = await session.get(AgentRun, logged.agent_run_id)
    assert run is not None and run.tool_calls[0]["error"] == message


async def test_a_client_that_goes_away_cancels_the_run_and_its_cost_is_recorded(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    model = Gated(fake_chat(UNIVERSE), call=2)
    async with running(settings_for(tmp_path), model) as (app, client):
        gone = asyncio.Event()
        sent: list[bytes] = []
        request_id = ""
        thread = ""

        async def receive() -> dict[str, Any]:
            if not sent:
                sent.append(b"")
                body = json.dumps({"question": QUESTION}).encode()
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal request_id, thread
            if message["type"] == "http.response.start":
                headers = dict(message["headers"])
                request_id = headers[b"x-request-id"].decode()
                thread = headers[b"x-thread-id"].decode()
            elif message["type"] == "http.response.body":
                sent.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat",
            "raw_path": b"/v1/chat",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"x-api-key", KEY.encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
            "state": {},
        }
        call = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(model.waiting.wait(), 5)  # the second model call is in flight
        gone.set()
        await asyncio.wait_for(call, 5)  # the response ends at once
        streamed = parse(b"".join(sent).decode())
        assert "done" not in [name for name, _ in streamed]
        model.gate.set()
        await asyncio.wait_for(services(app).settle(), 5)
        # The first executor call, and the one in flight when the client went away.
        rows = await spend_rows(sessions)
        assert len(rows) == 2
        logged = await log_row(sessions, request_id)
        assert logged.outcome == "disconnected" and logged.agent_run_id is not None
        async with sessions() as session:
            run = await session.get(AgentRun, logged.agent_run_id)
        assert run is not None and run.answer == "" and "stopped" in run.usage
        assert run.llm_calls == 2 and run.cost == logged.cost
        assert float(run.cost) == pytest.approx(float(sum(r.cost for r in rows)), abs=1e-5)
        # The thread's turn was closed: a question on it runs normally afterwards.
        _, events = await ask(client, "And Apixco's revenue?", thread)
        assert dict(events)["done"]["thread_id"] == thread


@pytest.mark.parametrize(
    ("changes", "spent", "cap"),
    [
        ({"daily_llm_budget_usd": Decimal("0.01")}, Decimal("0.01"), "this key"),
        ({"daily_llm_budget_total_usd": Decimal("0.01")}, Decimal("0.01"), "all keys"),
        ({"llm_budget_usd": Decimal("0.01")}, Decimal("0.01"), "LLM_BUDGET_USD"),
    ],
)
async def test_over_a_budget_no_model_is_called(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    changes: dict[str, Any],
    spent: Decimal,
    cap: str,
) -> None:
    async with sessions() as session, session.begin():
        api_key = key_id(KEY) if cap == "this key" else key_id(OTHER_KEY)
        session.add(
            LlmSpend(
                command=CHAT_COMMAND,
                model="scripted@fake",
                input_tokens=1,
                output_tokens=1,
                cost=spent,
                counted=True,
                api_key=api_key,
            )
        )
    model = Gated(fake_chat(UNIVERSE), call=0)
    async with running(settings_for(tmp_path, **changes), model) as (_, client):
        _, events = await ask(client)
    assert [name for name, _ in events] == ["error"]
    error = dict(events)["error"]
    assert error["code"] == "budget_exceeded" and error["status"] == 402 and cap in error["message"]
    assert model.calls == 0
    assert len(await spend_rows(sessions)) == 1


async def test_a_run_stops_before_a_call_that_could_exceed_the_daily_budget(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    model = Gated(fake_chat(UNIVERSE), call=0)
    settings = settings_for(tmp_path, daily_llm_budget_usd=Decimal("0.000001"))
    async with running(settings, model) as (_, client):
        _, events = await ask(client)
    error = dict(events)["error"]
    assert error["code"] == "budget_exceeded" and "this key" in error["message"]
    assert model.calls == 0


async def test_chat_without_a_model_is_unavailable(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = settings_for(
        tmp_path, llm_provider="anthropic", llm_api_key="", anthropic_api_key=""
    )
    app = create_app(
        settings,
        Overrides(universe=UNIVERSE, embedder=FakeEmbedder, reranker=FakeReranker),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": KEY}
        ) as client:
            _, events = await ask(client)
            assert dict(events)["error"]["code"] == "model_unavailable"
            # Data endpoints work without a model.
            assert (await client.get(f"/v1/companies/{TICKER}/financials")).status_code == 200


async def test_feedback_on_an_answer(
    api: tuple[FastAPI, httpx.AsyncClient], sessions: async_sessionmaker[AsyncSession]
) -> None:
    _, client = api
    _, events = await ask(client)
    request_id = dict(events)["done"]["request_id"]
    saved = await client.post(
        "/v1/feedback", json={"request_id": request_id, "rating": "down", "comment": "too short"}
    )
    assert saved.status_code == 201
    assert saved.json()["request_id"] == request_id and saved.json()["rating"] == "down"
    async with sessions() as session:
        row = await session.get(Feedback, saved.json()["id"])
    assert row is not None and row.comment == "too short" and row.key_id == key_id(KEY)
    unknown = await client.post("/v1/feedback", json={"request_id": "0" * 32, "rating": "up"})
    assert unknown.status_code == 404
    bad = await client.post("/v1/feedback", json={"request_id": request_id, "rating": "meh"})
    assert bad.status_code == 422


# Keys, limits and the request log.


async def test_a_missing_or_wrong_key_is_refused(
    api: tuple[FastAPI, httpx.AsyncClient], sessions: async_sessionmaker[AsyncSession]
) -> None:
    _, client = api
    missing = await client.get("/v1/companies", headers={"X-API-Key": ""})
    assert missing.status_code == 401
    wrong = await client.get("/v1/companies", headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401
    chat = await client.post("/v1/chat", json={"question": QUESTION}, headers={"X-API-Key": "x"})
    assert chat.status_code == 401
    logged = await log_row(sessions, wrong.headers["x-request-id"])
    assert logged.status == 401 and logged.key_id is None


async def test_rate_limits_answer_429_with_retry_after(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = settings_for(
        tmp_path, rate_limit_per_minute=2, rate_limit_chat_per_minute=1, api_keys=[KEY, OTHER_KEY]
    )
    async with running(settings) as (_, client):
        for _ in range(2):
            assert (await client.get("/v1/track-record")).status_code == 200
        limited = await client.get("/v1/track-record")
        assert limited.status_code == 429 and 1 <= int(limited.headers["retry-after"]) <= 60
        # Another key has its own allowance; chat questions are counted separately.
        other = await client.get("/v1/track-record", headers={"X-API-Key": OTHER_KEY})
        assert other.status_code == 200
        assert (await client.post("/v1/chat", json={"question": QUESTION})).status_code == 200
        second = await client.post("/v1/chat", json={"question": QUESTION})
        assert second.status_code == 429 and "retry-after" in second.headers
        # Health checks are never limited.
        assert (await client.get("/healthz")).status_code == 200


async def test_production_refuses_to_start_without_keys(tmp_path: Path) -> None:
    with pytest.raises(ApiConfigError, match="API_KEYS"):
        create_app(settings_for(tmp_path, environment="production", api_keys=[]))
    with pytest.raises(ApiConfigError, match="fake"):
        create_app(settings_for(tmp_path, environment="production"))


async def test_every_request_is_logged_with_its_id_and_never_the_key(
    api: tuple[FastAPI, httpx.AsyncClient], sessions: async_sessionmaker[AsyncSession]
) -> None:
    _, client = api
    response = await client.get(f"/v1/companies/{TICKER}/financials", params={"quarters": 2})
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    logged = await log_row(sessions, request_id)
    assert (logged.method, logged.path, logged.status) == (
        "GET",
        f"/v1/companies/{TICKER}/financials",
        200,
    )
    assert logged.key_id == key_id(KEY) and KEY not in (logged.key_id or "")
    assert logged.latency_ms >= 0 and logged.outcome is None
    not_found = await client.get("/v1/companies/NOPE/guidance")
    assert (await log_row(sessions, not_found.headers["x-request-id"])).status == 404
    health = await client.get("/healthz")
    async with sessions() as session:
        probes = await session.scalar(
            select(func.count())
            .select_from(RequestLog)
            .where(RequestLog.request_id == health.headers["x-request-id"])
        )
    assert probes == 0


# Migration.


def test_the_request_log_migration_round_trips_on_a_throwaway_database(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(migrated_database).set(database="talkischeap_roundtrip_test")
    server = make_url(migrated_database).set(database="postgres")
    dsn = server.render_as_string(hide_password=False).replace("+psycopg", "")
    name = sql.Identifier(str(url.database))
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(name))
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
        command.check(config)  # the models match the migrations
        command.downgrade(config, "0015")
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))


async def test_the_free_fake_model_is_kept_out_of_the_ledger(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, llm_input_price="", llm_output_price="")
    async with running(settings) as (_, client):
        _, events = await ask(client)
    assert Decimal(dict(events)["done"]["cost"]) == 0
    assert await spend_rows(sessions) == []
