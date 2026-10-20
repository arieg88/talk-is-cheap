"""`talk-is-cheap eval agent` (`make smoke-agent`): the smoke set in
`evals/agent/smoke.jsonl`, scored by code against the database.

Each question lists facts code can check, not wording:

- `actual`: a reported value (ticker, metric, period, vintage), read from
  `financial_values` when scoring; the answer must state it, within the rounding it is
  written with or `tolerance_pct`;
- `derived`: a margin or percent change of two reported values, computed when scoring;
- `guidance`: a guidance row (ticker, metric, issued and target period); the answer must
  state its figures and its verdict, and that it was flagged when it was;
- `no_guidance`: the answer must say the company gives no guidance;
- `text`: the answer must use one of the given phrases and cite a filing with the section;
- `statement`: a management claim reported in the news (ticker, metric, target period);
  the answer must state its figure (of any matching claim), its verdict (unless
  `outcome` is false: the question does not ask how the result compared), how it compares
  with formal guidance when `comparison` is set, and "reported by", citing an article;
- `no_management_claims`: the answer must say management made no such claim; when
  `analyst_figure` is set, every sentence stating that figure must attribute it to an
  analyst, not to the company;
- `refuse`: the agent must decline without tool calls, and say "not investment advice"
  when `advice_note` is set.

Every question is scored on facts correct, citations resolve (every marker has a source
whose URL is a stored filing, release or article), numbers grounded (the final answer
passes the verifier's code checks) and refusal correct. `--dry-run` runs everything with a
scripted model (`dry_run_script`) that calls the tools the facts need and writes a draft
from their results: it checks the harness, tools, verifier and scoring at no cost.
"""

import json
import re
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.agent.checks import code_checks, mentions, sentences
from talk_is_cheap.agent.commands import (
    AgentSetupError,
    chat_model,
    open_agent,
    print_estimate,
)
from talk_is_cheap.agent.llm import ScriptedChat, text_of
from talk_is_cheap.agent.router import ADVICE_NOTE, QuestionType
from talk_is_cheap.agent.run import RunResult, ask, log_run
from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_engine, create_session_factory
from talk_is_cheap.db.models import Article, Company, Document, Filing, Guidance, Section
from talk_is_cheap.financials import (
    MetricValue,
    annual_financials,
    find_company,
    quarterly_financials,
)
from talk_is_cheap.guidance.llm import (
    Budget,
    CostCapReached,
    LlmError,
    Pricing,
    ResponseCache,
    model_label,
    pricing_for,
)
from talk_is_cheap.spend import budget_line, command_budget

NO_GUIDANCE = re.compile(
    r"\b(no guidance|not (?:give|provide|issue|offer)\w* (?:any )?(?:financial )?guidance|"
    r"(?:doesn't|does not|did not|didn't|do not) (?:give|provide|issue|offer)\w* "
    r"(?:any )?(?:\w+ )?(?:guidance|outlook)|no (?:\w+ )?(?:guidance|outlook))\b",
    re.IGNORECASE,
)
REFUSAL = re.compile(
    r"\b(can't help|cannot help|can only answer|not one of the companies|"
    r"don't make predictions|only answer questions)\b",
    re.IGNORECASE,
)
VERDICT_WORDS = {
    "beat": r"\b(beat\w*|exceed\w*|above|surpass\w*|topp\w*|outperform\w*)\b",
    "met": r"\b(met|within|in line|meet\w*)\b",
    "missed": r"\b(miss\w*|below|short|fell short)\b",
    "pending": r"\b(pending|not (?:yet )?reported)\b",
}
FLAG_WORDS = re.compile(r"\b(flag\w*|review\w*|plausibility)\b", re.IGNORECASE)
STATEMENT_VERDICT_WORDS = {
    **VERDICT_WORDS,
    "pending": r"\b(pending|not (?:yet )?(?:been )?reported|yet to (?:be )?report\w*|"
    r"not (?:yet )?available|has not (?:yet )?ended)\b",
    "confirmed": r"\b(confirm\w*|did (?:rise|grow|increase|fall|decline)|rose|grew)\b",
    "contradicted": r"\b(contradict\w*|did not|instead)\b",
}
COMPARISON_WORDS = {
    "consistent": r"\b(consistent|in line|within|match\w*|same as|agree\w*)\b",
    "differs": r"\b(differ\w*|outside|above|below|contradict\w*)\b",
    "no_formal_guidance": (
        r"\b(no (?:formal |matching |corresponding )?(?:\w+ ){0,3}guidance|not (?:part of|in) "
        r"(?:its |the )?(?:formal )?guidance|not (?:been )?(?:formally )?guided|"
        r"without formal guidance)\b"
    ),
}
NO_MANAGEMENT = re.compile(
    r"\b(no (?:\w+ ){0,3}(?:management|executive|company)|not (?:from|by) (?:management|the "
    r"company|an? executive)|(?:management|executives?|the company) (?:has|have|did) not|"
    r"(?:could not|couldn't|did not|didn't) find (?:any )?(?:\w+ ){0,2}(?:executive|management)|"
    r"(?:didn't|did not|has not|have not) (?:said|say|made|given|provided|commented))\b",
    re.IGNORECASE,
)
ANALYST_WORDS = re.compile(
    r"\b(analysts?|IDC|Counterpoint|estimates?|consensus|Wall Street|research firm|forecasters?)\b",
    re.IGNORECASE,
)


class Fact(BaseModel):
    kind: (
        str  # actual, derived, guidance, no_guidance, text, statement, no_management_claims, refuse
    )
    ticker: str | None = None
    metric: str | None = None
    period: str | None = None
    vintage: str = "latest"
    tolerance_pct: float = 0.5
    # derived: an operation over two reported values, a and b
    operation: str | None = None  # margin (a / b * 100) or pct_change ((b - a) / |a|)
    a: dict[str, str] | None = None
    b: dict[str, str] | None = None
    tolerance_points: float = 0.5
    # guidance
    issued: str | None = None
    target: str | None = None
    basis: str | None = None
    measure: str = "level"
    # text: the section the answer must cite, and the fiscal periods to search
    section: str | None = None
    periods: str | None = None
    any: list[str] = Field(default_factory=list)
    # statement: whether the answer must give the claim's verdict (when the question asks
    # how the result compared), and how the claim compares with formal guidance
    outcome: bool = True
    comparison: str | None = None
    # no_management_claims: a figure only an analyst gave
    analyst_figure: float | None = None
    # refuse
    advice_note: bool = False


class SmokeQuestion(BaseModel):
    id: str
    type: QuestionType
    question: str
    facts: list[Fact]


def load_questions(path: Path) -> list[SmokeQuestion]:
    questions = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                questions.append(SmokeQuestion.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"line {number}: {exc}") from exc
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("question ids must be unique")
    return questions


@dataclass
class Score:
    id: str
    type: str
    question: str
    routed_as: str = ""
    facts: float = 0.0  # share of facts correct
    citations: bool = False
    grounded: bool = False
    refusal: bool = False
    notes: list[str] = field(default_factory=list)
    tool_calls: int = 0
    llm_calls: int = 0
    cached_calls: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    revisions: int = 0
    verifier_failures: dict[str, int] = field(default_factory=dict)
    # Each verifier failure: the revision, the check and what it found.
    failure_details: list[str] = field(default_factory=list)
    answer: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.facts == 1.0 and self.citations and self.grounded and self.refusal


# Expected values, read from the database when scoring.


async def _value(
    session: AsyncSession, ticker: str, metric: str, period: str, vintage: str
) -> MetricValue | None:
    company = await find_company(session, ticker)
    if company is None:
        return None
    compact = period.replace(" ", "").upper()
    if len(compact) == 6:
        years = await annual_financials(
            session,
            company.cik,
            vintage="first" if vintage == "first" else "latest",
            metrics=[metric],
        )
        return next((y.metrics.get(metric) for y in years if y.label == compact), None)
    quarters = await quarterly_financials(
        session,
        company.cik,
        vintage="first" if vintage == "first" else "latest",
        quarters=None,
        metrics=[metric],
    )
    return next(
        (
            q.metrics.get(metric)
            for q in quarters
            if f"FY{q.fiscal_year}Q{q.fiscal_quarter}" == compact
        ),
        None,
    )


def _states(answer: str, value: Decimal, tolerance: Decimal) -> bool:
    for mention in mentions(answer):
        allowed = max(mention.rounding, abs(value) * tolerance)
        candidates = [abs(value)]
        if mention.kind == "pct":
            candidates.append(abs(value) * 100)
        if any(abs(abs(mention.value) - c) <= allowed for c in candidates):
            return True
    return False


async def check_fact(session: AsyncSession, fact: Fact, result: RunResult) -> tuple[bool, str]:
    answer = result.answer.split("\n\nSources:\n")[0]
    tolerance = Decimal(str(fact.tolerance_pct)) / 100
    if fact.kind == "refuse":
        ok = result.question_type == "out_of_scope" and not result.tool_calls
        if ok and fact.advice_note and ADVICE_NOTE.lower() not in answer.lower():
            return False, "no 'not investment advice' note"
        return ok, "" if ok else "did not decline"
    assert fact.ticker is not None
    if fact.kind == "actual":
        assert fact.metric and fact.period
        value = await _value(session, fact.ticker, fact.metric, fact.period, fact.vintage)
        if value is None:
            return False, f"no stored {fact.metric} {fact.period} for {fact.ticker}"
        ok = _states(answer, value.value, tolerance)
        label = f"{fact.ticker} {fact.metric} {fact.period} ({fact.vintage}) {value.value:,.2f}"
        return ok, "" if ok else f"missing {label}"
    if fact.kind == "derived":
        assert fact.a and fact.b
        a = await _value(session, fact.ticker, fact.a["metric"], fact.a["period"], fact.vintage)
        b = await _value(session, fact.ticker, fact.b["metric"], fact.b["period"], fact.vintage)
        if a is None or b is None:
            return False, "inputs not stored"
        if fact.operation == "margin":
            expected = a.value / b.value * 100
        else:
            expected = (b.value - a.value) / abs(a.value) * 100
        points = Decimal(str(fact.tolerance_points))
        ok = any(
            m.kind == "pct" and abs(abs(m.value) - abs(expected)) <= max(points, m.rounding)
            for m in mentions(answer)
        )
        return ok, "" if ok else f"missing {fact.operation} {expected:.2f}%"
    if fact.kind == "guidance":
        row = await _guidance_row(session, fact)
        if row is None:
            return False, "guidance row not found"
        stated = [
            v
            for v in (row.low, row.high, row.point)
            if v is not None and _states(answer, v, Decimal("0.005"))
        ]
        if row.low is not None and row.high is not None:
            figures_ok = (row.low in stated and row.high in stated) or row.point in stated
        else:
            figures_ok = bool(stated)
        problems = []
        if not figures_ok:
            problems.append("guided figures not stated")
        pattern = VERDICT_WORDS.get(row.verdict)
        if pattern and not re.search(pattern, answer, re.IGNORECASE):
            problems.append(f"verdict {row.verdict} not stated")
        if row.plausibility == "flagged" and not FLAG_WORDS.search(answer):
            problems.append("flag not mentioned")
        return not problems, "; ".join(problems)
    if fact.kind == "no_guidance":
        ok = bool(NO_GUIDANCE.search(answer))
        return ok, "" if ok else "does not say there is no guidance"
    if fact.kind == "text":
        used = any(p.lower() in answer.lower() for p in fact.any)
        cited = await _cites_section(session, result, fact.ticker, fact.section)
        problems = []
        if not used:
            problems.append(f"none of {fact.any}")
        if not cited:
            problems.append(f"no cited {fact.section} section")
        return not problems, "; ".join(problems)
    if fact.kind == "statement":
        return await _check_statement(session, fact, result, answer)
    if fact.kind == "no_management_claims":
        problems = []
        if not NO_MANAGEMENT.search(answer):
            problems.append("does not say management made no such claim")
        if fact.analyst_figure is not None:
            figure = Decimal(str(fact.analyst_figure))
            for sentence in sentences(answer):
                if _states(sentence, figure, Decimal("0.005")) and not ANALYST_WORDS.search(
                    sentence
                ):
                    problems.append(f"analyst figure {figure} not attributed to an analyst")
        return not problems, "; ".join(problems)
    return False, f"unknown fact kind {fact.kind}"


async def _check_statement(
    session: AsyncSession, fact: Fact, result: RunResult, answer: str
) -> tuple[bool, str]:
    from talk_is_cheap.db.models import Statement, StatementOutcome

    assert fact.ticker and fact.metric and fact.target
    target = fact.target.upper().removeprefix("FY")
    statement = (
        select(Statement, StatementOutcome)
        .join(StatementOutcome, StatementOutcome.statement_id == Statement.id)
        .where(
            Statement.ticker == fact.ticker,
            Statement.metric == fact.metric,
            Statement.management,
            Statement.target_fiscal_year == int(target[:4]),
        )
    )
    statement = statement.where(
        Statement.target_fiscal_quarter == int(target[5])
        if len(target) > 4
        else Statement.target_fiscal_quarter.is_(None)
    )
    rows = [(s, o) for s, o in (await session.execute(statement)).all() if o.review != "wrong"]
    if not rows:
        return False, f"no stored management claim for {fact.ticker} {fact.metric} {fact.target}"
    problems = []
    figures = [v for s, _ in rows for v in (s.low, s.high, s.point) if v is not None]
    if figures and not any(_states(answer, v, Decimal("0.005")) for v in figures):
        problems.append("claimed figure not stated")
    verdicts = {o.verdict for _, o in rows}
    if (
        fact.outcome
        and verdicts & set(STATEMENT_VERDICT_WORDS)
        and not any(
            re.search(STATEMENT_VERDICT_WORDS[v], answer, re.IGNORECASE)
            for v in verdicts
            if v in STATEMENT_VERDICT_WORDS
        )
    ):
        problems.append(f"verdict ({', '.join(sorted(verdicts))}) not stated")
    if fact.comparison and not re.search(COMPARISON_WORDS[fact.comparison], answer, re.I):
        problems.append(f"comparison with guidance ({fact.comparison}) not stated")
    if "reported by" not in result.answer.lower():
        problems.append('no "reported by"')
    cited = {src["url"] for src in result.sources}
    articles = await session.scalar(select(Article.id).where(Article.url.in_(cited)).limit(1))
    if articles is None:
        problems.append("no news article cited")
    return not problems, "; ".join(problems)


async def _guidance_row(session: AsyncSession, fact: Fact) -> Guidance | None:
    assert fact.ticker and fact.issued and fact.target
    company = await find_company(session, fact.ticker)
    if company is None:
        return None
    issued = fact.issued.upper().removeprefix("FY")
    target = fact.target.upper().removeprefix("FY")
    statement = select(Guidance).where(
        Guidance.cik == company.cik,
        Guidance.metric == fact.metric,
        Guidance.measure == fact.measure,
        Guidance.fiscal_year == int(issued[:4]),
        Guidance.fiscal_quarter == int(issued[5]),
        Guidance.target_fiscal_year == int(target[:4]),
    )
    statement = statement.where(
        Guidance.target_fiscal_quarter == int(target[5])
        if len(target) > 4
        else Guidance.target_fiscal_quarter.is_(None)
    )
    if fact.basis:
        statement = statement.where(Guidance.basis == fact.basis)
    return (await session.scalars(statement.order_by(Guidance.id))).first()


async def _cites_section(
    session: AsyncSession, result: RunResult, ticker: str, section: str | None
) -> bool:
    urls = [s["url"] for s in result.sources]
    if not urls:
        return False
    company = await find_company(session, ticker)
    if company is None:
        return False
    found = await session.scalar(
        select(Document.id)
        .join(Section, Section.document_id == Document.id)
        .where(Document.cik == company.cik, Document.url.in_(urls), Section.section_type == section)
        .limit(1)
    )
    return found is not None


async def citations_resolve(session: AsyncSession, result: RunResult) -> tuple[bool, str]:
    """Every marker has a source, and every source URL is a stored filing document,
    filing, article or EDGAR company page."""
    body = result.answer.split("\n\nSources:\n")[0]
    numbers = {s["number"] for s in result.sources}
    markers = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
    if markers - numbers:
        return False, f"markers without a source: {sorted(markers - numbers)}"
    for source in result.sources:
        if not await _url_resolves(session, source["url"]):
            return False, f"unknown source URL {source['url']}"
    return True, ""


async def _url_resolves(session: AsyncSession, url: str) -> bool:
    if not url:
        return False
    if await session.scalar(select(Document.id).where(Document.url == url).limit(1)):
        return True
    if await session.scalar(select(Article.id).where(Article.url == url).limit(1)):
        return True
    company_page = re.search(r"browse-edgar\?action=getcompany&CIK=(\d+)$", url)
    if company_page:
        return bool(await session.get(Company, int(company_page.group(1))))
    archive = re.search(r"/Archives/edgar/data/(\d+)/(\d{18})/", url)
    if archive:
        folder = archive.group(2)
        accession = f"{folder[:10]}-{folder[10:12]}-{folder[12:]}"
        return bool(await session.get(Filing, accession))
    return False


async def score(
    sessions: async_sessionmaker[AsyncSession], question: SmokeQuestion, result: RunResult
) -> Score:
    s = Score(id=question.id, type=question.type, question=question.question)
    s.routed_as = result.question_type
    s.tool_calls = len(result.tool_calls)
    s.llm_calls = result.total("calls")
    s.cached_calls = result.total("cached")
    s.cost = float(result.cost)
    s.seconds = round(result.seconds, 1)
    s.revisions = result.revisions
    s.verifier_failures = result.failure_counts
    s.failure_details = [
        f"{f['revision']} {f['check']}: {f['detail'][:200]}" for f in result.failures
    ]
    s.answer = result.answer
    async with sessions() as session:
        outcomes = [await check_fact(session, fact, result) for fact in question.facts]
        s.facts = sum(ok for ok, _ in outcomes) / max(len(outcomes), 1)
        s.notes += [note for ok, note in outcomes if not ok and note]
        s.citations, note = await citations_resolve(session, result)
        if note:
            s.notes.append(note)
        if question.type != "out_of_scope" and not result.sources:
            s.citations = False
            s.notes.append("no sources")
    s.grounded = not code_checks(result.final, result.evidence, question.question)
    if not s.grounded:
        s.notes.append("final answer fails the code checks")
    refused = result.question_type == "out_of_scope"
    s.refusal = refused == (question.type == "out_of_scope")
    if not s.refusal:
        s.notes.append("declined an in-scope question" if refused else "answered out of scope")
    return s


# The scripted model for --dry-run.


def _evidence_in(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    items = []
    for message in messages:
        if isinstance(message, ToolMessage):
            try:
                items.extend(json.loads(text_of(message)).get("evidence", []))
            except ValueError:
                continue
    return items


def dry_run_script(question: SmokeQuestion) -> Any:
    """A script that answers like a careful model would: the tool calls the facts need,
    a compute call for derived figures, and a draft made from the results."""

    def calls_for() -> list[dict[str, Any]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        for fact in question.facts:
            if fact.kind == "actual":
                calls.append(
                    (
                        "get_actuals",
                        {
                            "ticker": fact.ticker,
                            "metrics": [fact.metric],
                            "periods": [fact.period],
                            "vintage": fact.vintage,
                        },
                    )
                )
            elif fact.kind == "derived" and fact.a and fact.b:
                metrics = sorted({fact.a["metric"], fact.b["metric"]})
                periods = sorted({fact.a["period"], fact.b["period"]})
                calls.append(
                    (
                        "get_actuals",
                        {
                            "ticker": fact.ticker,
                            "metrics": metrics,
                            "periods": periods,
                            "vintage": fact.vintage,
                        },
                    )
                )
            elif fact.kind == "guidance":
                calls.append(
                    (
                        "get_guidance",
                        {
                            "ticker": fact.ticker,
                            "metric": fact.metric,
                            "issued_period": fact.issued,
                            "target_period": fact.target,
                        },
                    )
                )
            elif fact.kind == "no_guidance":
                calls.append(("get_guidance", {"ticker": fact.ticker}))
            elif fact.kind == "statement":
                calls.append(
                    (
                        "get_statements",
                        {"ticker": fact.ticker, "metric": fact.metric, "period": fact.target},
                    )
                )
            elif fact.kind == "no_management_claims":
                calls.append(("get_statements", {"ticker": fact.ticker, "management_only": False}))
            elif fact.kind == "text":
                calls.append(
                    (
                        "search_filings",
                        {
                            "query": question.question,
                            "ticker": fact.ticker,
                            "section_types": [fact.section],
                            "periods": fact.periods,
                        },
                    )
                )
        return [{"name": n, "args": a, "id": f"call_{i}"} for i, (n, a) in enumerate(calls)]

    def draft(items: Sequence[dict[str, Any]]) -> str:
        lines = []
        if any(f.kind == "no_management_claims" for f in question.facts):
            lines.append("Management made no such claim in the stored news.")
        for item in items:
            tool_lines = _draft_line(item, question)
            if tool_lines:
                lines.append(tool_lines)
        return " ".join(lines) or "I could not find an answer in the stored data."

    def script(node: str, messages: Sequence[BaseMessage], tools: Any) -> AIMessage:
        system = text_of(messages[0]) if messages and isinstance(messages[0], SystemMessage) else ""
        if node == "router":
            route: dict[str, Any] = {"question_type": question.type}
            return AIMessage("", tool_calls=[{"name": "route", "args": route, "id": "r"}])
        if node == "planner":
            plan: dict[str, Any] = {"steps": ["fetch the values", "compute", "answer"]}
            return AIMessage("", tool_calls=[{"name": "plan", "args": plan, "id": "p"}])
        if node == "verifier":
            verdict: dict[str, Any] = {"claims": []}
            return AIMessage("", tool_calls=[{"name": "verdict", "args": verdict, "id": "v"}])
        if node == "answer":
            return AIMessage(system.split("Draft:\n", 1)[1].split("\n\nEvidence cited:", 1)[0])
        # Each smoke question runs on a thread of its own, so every tool result is this
        # question's.
        items = _evidence_in(messages)
        if not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage("", tool_calls=calls_for())
        derived = [f for f in question.facts if f.kind == "derived"]
        if derived and not any(i.get("operation") for i in items):
            calls = []
            for n, fact in enumerate(derived):
                assert fact.a and fact.b
                ids = [_find(items, fact.ticker, fact.a), _find(items, fact.ticker, fact.b)]
                if all(ids):
                    operation = "margin" if fact.operation == "margin" else "pct_change"
                    calls.append(
                        {
                            "name": "compute",
                            "id": f"c{n}",
                            "args": {"operation": operation, "operands": ids},
                        }
                    )
            if calls:
                return AIMessage("", tool_calls=calls)
        return AIMessage(draft(items))

    return script


def _find(items: Sequence[dict[str, Any]], ticker: str | None, ref: Mapping[str, str]) -> str:
    for item in items:
        if (item.get("ticker"), item.get("metric"), item.get("period")) == (
            ticker,
            ref["metric"],
            ref["period"],
        ):
            return str(item["id"])
    return ""


def _draft_line(item: Mapping[str, Any], question: SmokeQuestion) -> str:
    marker = f"[{item['id']}]"
    if "claim_type" in item:
        return _statement_line(item, marker)
    if "operation" in item:
        return f"The {item['operation'].replace('_', ' ')} is {item['display']} {marker}."
    if "metric" in item and "value" in item:
        return (
            f"{item['ticker']} {item['metric'].replace('_', ' ')} for {item['period']} "
            f"({item['vintage']} reported) was {item['display']} {marker}."
        )
    if "guidance_rows" in item:
        return f"{item['ticker']} gives no guidance in its earnings releases {marker}."
    if "verdict" in item:
        figures = " to ".join(
            _figure(item[k], item["unit"]) for k in ("low", "high") if k in item
        ) or _figure(item.get("point"), item["unit"])
        review = f" The row was {item['review']} {marker}." if item.get("review") else ""
        return (
            f"{item['ticker']} guided {item['metric']} for {item['target']} at {figures}; "
            f"the result {item['verdict']} the guidance {marker}.{review}"
        )
    if "text" in item:
        quoted = [s.rstrip(" .…") for s in sentences(item["text"]) if "|" not in s][:3]
        return " ".join(f"{s} {marker}." for s in quoted)
    return ""


def _statement_line(item: Mapping[str, Any], marker: str) -> str:
    """A statement as a careful draft writes it: who, what, when, the outcome and the
    comparison with guidance, reported by the publisher."""
    figure = item.get("as_written") or ""
    if not item["management"]:
        who = item.get("role") or item.get("speaker") or "an analyst"
        return (
            f"The only claim is by {who}, an analyst, not the company: {figure} "
            f"(reported by {item['publisher']}) {marker}."
        )
    who = item.get("speaker") or "the company"
    line = (
        f"On {item['date']}, {who} said {item['ticker']} expected {item.get('metric', '')} "
        f"of {figure} for {item.get('target', 'the period')} (reported by "
        f"{item['publisher']}) {marker}."
    )
    verdict = item.get("verdict")
    if verdict in ("beat", "met", "missed"):
        line += f" The result {verdict} that figure {marker}."
    elif verdict == "pending":
        line += f" The period has not been reported yet, so it is pending {marker}."
    comparison = item.get("guidance_comparison")
    if comparison == "consistent":
        line += f" It is consistent with formal guidance {marker}."
    elif comparison == "differs":
        line += f" It differs from formal guidance {marker}."
    elif comparison == "no_formal_guidance":
        line += f" There is no formal guidance for that period {marker}."
    return line


def _figure(value: Any, unit: str) -> str:
    if value is None:
        return "?"
    if unit == "USD":
        return f"${value / 1e9:.2f} billion"
    if unit == "USD/shares":
        return f"${value:.2f}"
    return f"{value}%"


# The command.


async def smoke_command(
    path: Path,
    only: Sequence[str] | None,
    model: str | None,
    dry_run: bool,
    max_cost: Decimal | None,
    results_dir: Path,
) -> int:
    try:
        questions = load_questions(path)
    except (OSError, ValueError) as exc:
        print(f"cannot use {path}: {exc}")
        return 1
    if only:
        questions = [q for q in questions if q.id in only]
    settings = get_settings()
    label = "scripted@dry-run" if dry_run else model_label(settings, model)
    engine = create_engine(settings)
    holder: dict[str, SmokeQuestion] = {}
    scores: list[Score] = []
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    try:
        sessions = create_session_factory(engine)
        if dry_run:
            chat: Any = ScriptedChat(
                lambda node, messages, tools: dry_run_script(holder["q"])(node, messages, tools),
                name=label,
            )
            budget = Budget(Decimal(1), Pricing(Decimal(0), Decimal(0)))
            print(
                f"dry run: {len(questions)} questions with a scripted model, no calls made",
                flush=True,
            )
            cache_dir = tempfile.TemporaryDirectory()
            cache = ResponseCache(Path(cache_dir.name))
        else:
            pricing = pricing_for(label, settings)
            if pricing is None:
                print(f"no price known for {label}")
                return 1
            try:
                chat = chat_model(settings, model)
            except LlmError as exc:
                print(exc)
                return 1
            budget = await command_budget(sessions, settings, pricing, "eval agent", cap=max_cost)
            await print_estimate(sessions, budget, label, len(questions))
            cache = ResponseCache(settings.llm_cache_dir)
        async with open_agent(settings, chat, budget, engine, cache=cache) as agent:
            print(HEADER, flush=True)
            for question in questions:
                holder["q"] = question
                thread = f"smoke-{question.id}-{uuid.uuid4().hex[:6]}"
                try:
                    result = await ask(agent.graph, agent.deps, question.question, thread)
                except CostCapReached as exc:
                    print(f"stopped: {exc}", flush=True)
                    break
                except LlmError as exc:
                    s = Score(
                        id=question.id,
                        type=question.type,
                        question=question.question,
                        error=str(exc),
                    )
                    scores.append(s)
                    print(f"{question.id:<24}ERROR {exc}", flush=True)
                    continue
                if not dry_run:
                    await log_run(agent.sessions, result, agent.model_name)
                s = await score(sessions, question, result)
                scores.append(s)
                print(_row(s), flush=True)
    except AgentSetupError as exc:
        print(exc)
        return 1
    finally:
        await engine.dispose()
    _print_summary(scores, budget, dry_run, time.monotonic() - started)
    out = results_dir / f"{stamp}{'-dry-run' if dry_run else ''}.json"
    write_results(
        out,
        {
            "run_at": stamp,
            "model": label,
            "dry_run": dry_run,
            "questions": len(questions),
            "cost": float(budget.spent),
            "scores": [asdict(s) | {"passed": s.passed} for s in scores],
        },
    )
    print(f"wrote {out}")
    return 0


def write_results(path: Path, results: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=1), encoding="utf-8")


HEADER = (
    f"\n{'ID':<24}{'TYPE':<15}{'ROUTED':<15}{'FACTS':>6}{'CITES':>6}{'GROUND':>7}"
    f"{'REFUSE':>7}{'TOOLS':>6}{'REV':>4}{'COST':>9}{'SEC':>6}"
)


def _mark(ok: bool) -> str:
    return "ok" if ok else "FAIL"


def _row(s: Score) -> str:
    return (
        f"{s.id[:23]:<24}{s.type:<15}{s.routed_as:<15}{s.facts:>6.2f}{_mark(s.citations):>6}"
        f"{_mark(s.grounded):>7}{_mark(s.refusal):>7}{s.tool_calls:>6}{s.revisions:>4}"
        f"{s.cost:>9.4f}{s.seconds:>6.1f}" + (f"\n{'':<24}{'; '.join(s.notes)}" if s.notes else "")
    )


def _print_summary(scores: Sequence[Score], budget: Budget, dry_run: bool, seconds: float) -> None:
    done = [s for s in scores if s.error is None]
    n = max(len(done), 1)
    print(
        f"\n{len(done)} questions: facts {sum(s.facts for s in done) / n:.2f}, citations "
        f"{sum(s.citations for s in done)}/{len(done)}, grounded "
        f"{sum(s.grounded for s in done)}/{len(done)}, refusals "
        f"{sum(s.refusal for s in done)}/{len(done)}; {sum(s.passed for s in done)} passed "
        f"all four; {sum(s.tool_calls for s in done)} tool calls; {seconds:.0f}s"
    )
    failures: dict[str, int] = {}
    for s in done:
        for check, count in s.verifier_failures.items():
            failures[check] = failures.get(check, 0) + count
    if failures:
        print(
            "verifier failures by check: "
            + ", ".join(f"{k} {v}" for k, v in sorted(failures.items()))
        )
    if dry_run:
        print("total cost: $0 (dry run)")
    else:
        print(f"total cost: ${budget.spent:.4f} ({budget.calls} paid calls)")
        print(budget_line(budget))
