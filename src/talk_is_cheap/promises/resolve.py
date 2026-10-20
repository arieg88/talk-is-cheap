"""Resolve framed promises once they are due, from evidence dated after the statement.

A small LangGraph graph per claim, over the agent's tools:

    plan -> gather -> judge -> check

- plan: the model reads the resolution rule and chooses what to gather: the metric's
  periods, searches of later filings (releases, deliveries reports, 10-Qs, 10-Ks) and
  news, and whether to read the company's later statements.
- gather: code turns the plan into at most MAX_TOOL_CALLS calls of `get_actuals`,
  `get_statements`, `search_filings` and `search_news`, every one limited to evidence
  dated after the statement.
- judge: the model gives a verdict with structured output, citing evidence ids, and
  says where a measured number is (an evidence id and the exact span). Code reads the
  number with `cite_value` and `compute`; the model never states it.
- check: `checks.py`, before anything is stored.

Which claims: specific and partial ones with a checkable target (vague claims are never
scored; a deadline without a target has nothing to judge), by default
only those due: the due date and its grace period have passed (`PROMISE_GRACE_DAYS`;
by default until the first 10-Q or 10-K covering the due period, whose XBRL values are
the reported results). Open-ended claims are checked for delivery once a 10-Q or 10-K
has been filed since the statement. `too_early` verdicts are
checked again by later runs. Claims that make the same promise (one statement reported
by several outlets) are resolved once and share the verdict.

Every call goes through the response cache, so a run with no new evidence makes no paid
call; a verdict also records an evidence key (the rule version, the evidence and the
prompt versions), so such a run changes nothing.
"""

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.agent.evidence import Evidence, EvidenceLog, compact, evidence_date
from talk_is_cheap.agent.tools import ToolContext, call_tool
from talk_is_cheap.db.models import (
    Article,
    Company,
    Document,
    Promise,
    PromiseVerdict,
    StockSplit,
)
from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.llm import (
    Budget,
    ExtractionModel,
    Pricing,
    ResponseCache,
    Usage,
    bounded_usage,
    complete,
    load_prompt_file,
)
from talk_is_cheap.promises.checks import Checked, Measured, PromiseFacts, check, item_date
from talk_is_cheap.promises.framing import NUMERIC_KINDS, covering_period
from talk_is_cheap.promises.schema import EvidencePlan, Judgment
from talk_is_cheap.universe import Universe

PLAN_PROMPT = ("promise_plan", "v1")
JUDGE_PROMPT = ("promise_judge", "v3")
MAX_TOOL_CALLS = 6
# Claims resolved at once: the model calls and the searches of one claim overlap with
# another's (the search models run in worker threads).
CONCURRENCY = 4
# Related companies whose documents are searched too, at most, and the results of each.
MAX_RELATED = 2
RELATED_K = 5
# Results per search: enough to find a figure, few enough to keep the judge's input small.
SEARCH_K = 3
LATER_STATEMENTS = 6
# What the judge reads of each item's text.
TEXT_CHARS = 500
# Output limits, and what a call reserves against the budget.
PLAN_OUTPUT_TOKENS = 800
JUDGE_OUTPUT_TOKENS = 2_500
PLAN_OVERHEAD_TOKENS = 1_000
JUDGE_OVERHEAD_TOKENS = 1_300
# What a typical claim costs in output, for the estimate a run prints (docs/promises.md).
TYPICAL_OUTPUT_TOKENS = 450
# Evidence for a claim is input of this size at most, for the estimate.
JUDGE_EVIDENCE_CHARS = MAX_TOOL_CALLS * SEARCH_K * (TEXT_CHARS + 200)
# Filings that cover a fiscal period: the 10-Q or 10-K, whose XBRL values are the
# reported results. Earnings releases come a few days earlier, so evidence from a release
# is always on time.
COVERING_DOCS = ("10q", "10k")
# Reported metrics behind each promise metric, for `get_actuals`.
ACTUAL_METRICS: Mapping[str, list[str]] = {
    "revenue": ["revenue"],
    "operating_income": ["operating_income"],
    "operating_expenses": ["operating_expenses"],
    "capex": ["capex"],
    "eps": ["eps_diluted"],
    "gross_margin": ["gross_profit", "revenue"],
    "operating_margin": ["operating_income", "revenue"],
}


def prompt_version() -> str:
    return f"{PLAN_PROMPT[0]}/{PLAN_PROMPT[1]} {JUDGE_PROMPT[0]}/{JUDGE_PROMPT[1]}"


def rule_hash(promise: Promise) -> str:
    return hashlib.sha256(promise.resolution_rule.encode()).hexdigest()


# An open-ended claim not shown delivered this long after the statement is
# `expired_unproven`.
OPEN_ENDED_LIFETIME = timedelta(days=548)  # 18 months
# Per-share targets, adjusted for stock splits made after the statement.
_PER_SHARE = re.compile(r"\b(per share|a share|dividend|eps|earnings per share)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClaimContext:
    """What resolving a claim reads besides the promise: when its statement's article was
    published, and the stock splits made since."""

    statement_at: datetime | None = None
    split_ratio: Decimal = Decimal(1)


def is_per_share(promise: Promise) -> bool:
    if promise.unit == "usd_per_share" or promise.metric == "eps":
        return True
    return promise.target_kind in NUMERIC_KINDS and bool(
        _PER_SHARE.search(f"{promise.subject} {promise.target_text or ''}")
    )


async def claim_context(session: AsyncSession, promise: Promise, today: date) -> ClaimContext:
    published = await session.scalar(
        select(Article.published_at).where(Article.id == promise.article_id)
    )
    cik = await session.scalar(select(Company.cik).where(Company.ticker == promise.ticker))
    ratio = Decimal(1)
    if cik is not None:
        until = promise.due_date or today
        for split in (await session.scalars(select(StockSplit).where(StockSplit.cik == cik))).all():
            if promise.statement_date < split.effective_date <= until:
                ratio *= split.ratio
    return ClaimContext(published, ratio)


def facts_of(
    promise: Promise, company_names: Sequence[str] = (), context: ClaimContext | None = None
) -> PromiseFacts:
    context = context or ClaimContext()
    return PromiseFacts(
        statement_date=promise.statement_date,
        due_date=promise.due_date,
        target_kind=promise.target_kind,
        direction=promise.direction,
        required_value=promise.required_value,
        baseline_value=promise.baseline_value,
        resolution_rule=promise.resolution_rule,
        quote=promise.quote,
        company_names=tuple(company_names),
        statement_at=context.statement_at,
        split_ratio=context.split_ratio,
        per_share=is_per_share(promise),
        expires=promise.statement_date + OPEN_ENDED_LIFETIME if promise.due_date is None else None,
    )


# Grace periods and which claims are due.


def covering_quarter(fiscal: FiscalCalendar, due: date) -> FiscalQuarter:
    """The fiscal quarter that holds `due`: the one after the last quarter ending before
    it. A due date within DRIFT after a quarter's nominal end belongs to that quarter, so
    December 31 is the fourth quarter of a 52/53-week year ending December 26."""
    year, quarter = covering_period(fiscal, due, "quarter")
    assert quarter is not None
    return FiscalQuarter(year, quarter)


@dataclass(frozen=True, slots=True)
class Filed:
    quarter: FiscalQuarter
    filed: date


async def company_filings(session: AsyncSession, cik: int) -> list[Filed]:
    rows = await session.execute(
        select(Document.fiscal_year, Document.fiscal_quarter, Document.filed_at).where(
            Document.cik == cik, Document.doc_type.in_(COVERING_DOCS)
        )
    )
    return sorted((Filed(FiscalQuarter(y, q), f) for y, q, f in rows), key=lambda f: f.filed)


def grace_end(
    due: date | None,
    filings: Sequence[Filed],
    fiscal: FiscalCalendar | None,
    grace_days: int | None,
) -> date | None:
    """When delivery stops counting as on time: `grace_days` after the due date, or the
    filing date of the first 10-Q or 10-K covering the due period. None while no such
    filing exists."""
    if due is None:
        return None
    if grace_days is not None:
        return due + timedelta(days=grace_days)
    if fiscal is None:
        return None
    wanted = covering_quarter(fiscal, due)
    for f in filings:
        if f.quarter >= wanted:
            return f.filed
    return None


def is_due(promise: Promise, end: date | None, filings: Sequence[Filed], today: date) -> bool:
    """Due: the grace period has ended; an open-ended claim, once anything was filed
    after it."""
    if promise.due_date is None:
        return any(f.filed > promise.statement_date for f in filings)
    return end is not None and end <= today


# The graph.


class ResolveState(TypedDict, total=False):
    promise: dict[str, Any]
    plan: dict[str, Any]
    calls: list[dict[str, Any]]
    evidence: dict[str, Evidence]
    judgment: dict[str, Any]
    usage: dict[str, Any]
    outcome: dict[str, Any]


@dataclass
class ResolveDeps:
    planner: ExtractionModel[EvidencePlan]
    judge: ExtractionModel[Judgment]
    cache: ResponseCache
    budget: Budget
    tools: ToolContext
    today: date


@dataclass(slots=True)
class Resolution:
    """One claim's verdict, before it is stored."""

    checked: Checked
    judgment: Judgment
    measured: Measured
    evidence: dict[str, Evidence]
    calls: list[dict[str, Any]]
    evidence_key: str
    usage: Usage
    cost: Decimal
    cached: bool
    seconds: float = 0.0


def evidence_fingerprint(items: Mapping[str, Evidence]) -> list[str]:
    """What makes the gathered evidence what it is, independent of its ids."""
    keys = []
    for item in items.values():
        data = {k: v for k, v in item["data"].items() if k not in ("id",)}
        keys.append(f"{item['tool']}|{item['url']}|{json.dumps(data, sort_keys=True, default=str)}")
    return sorted(keys)


def evidence_key(promise: Promise, items: Mapping[str, Evidence]) -> str:
    prompts = load_prompt_file(*PLAN_PROMPT) + load_prompt_file(*JUDGE_PROMPT)
    parts = [
        promise.version,
        rule_hash(promise),
        prompt_version(),
        hashlib.sha256(prompts.encode()).hexdigest(),
        *evidence_fingerprint(items),
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def plan_prompt(promise: Promise, fiscal_note: str, today: date) -> str:
    values = {
        "ticker": promise.ticker,
        "speaker": promise.speaker,
        "statement_date": promise.statement_date.isoformat(),
        "quote": promise.quote,
        "subject": promise.subject,
        "metric": promise.metric or "none",
        "rule": promise.resolution_rule,
        "due": promise.due_date.isoformat() if promise.due_date else "none (open-ended)",
        "fiscal": fiscal_note,
        "today": today.isoformat(),
    }
    return _fill(load_prompt_file(*PLAN_PROMPT), values)


def judge_prompt(
    promise: Promise, items: Mapping[str, Evidence], grace: date | None, today: date
) -> str:
    values = {
        "ticker": promise.ticker,
        "speaker": promise.speaker,
        "statement_date": promise.statement_date.isoformat(),
        "quote": promise.quote,
        "rule": promise.resolution_rule,
        "target": _target_line(promise),
        "basis": promise.comparison_basis or "not stated",
        "related": ", ".join(promise.related_companies) or "none",
        "due": promise.due_date.isoformat() if promise.due_date else "none (open-ended)",
        "grace": grace.isoformat() if grace else "no filing covering the due period yet",
        "today": today.isoformat(),
        "evidence": json.dumps(_for_judge(items), default=str, separators=(",", ":")),
    }
    return _fill(load_prompt_file(*JUDGE_PROMPT), values)


def _fill(template: str, values: Mapping[str, str]) -> str:
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


def _target_line(promise: Promise) -> str:
    if promise.target_kind is None:
        return "none"
    parts = [promise.target_kind]
    if promise.target_value is not None:
        parts.append(f"target {promise.target_value.normalize():f} {promise.unit or ''}".strip())
    if promise.required_value is not None:
        parts.append(f"required {promise.required_value.normalize():f}")
    if promise.baseline_value is not None:
        parts.append(f"baseline {promise.baseline_value.normalize():f} ({promise.baseline_period})")
    return "; ".join(parts)


def _for_judge(items: Mapping[str, Evidence]) -> list[dict[str, Any]]:
    """Items as the judge reads them: id, tool, date and data, texts cut."""
    out = []
    for item in compact(items.values()):
        entry = {
            k: (v[:TEXT_CHARS] + " …" if isinstance(v, str) and len(v) > TEXT_CHARS else v)
            for k, v in item.items()
            if k not in ("url", "provenance", "provenance_note", "accession_no", "chars")
        }
        source = items[item["id"]]
        entry["tool"] = source["tool"]
        when = evidence_date(source)
        entry["date"] = when.isoformat() if when else None
        out.append(entry)
    return out


def tool_calls(
    promise: Promise,
    plan: EvidencePlan,
    universe: Universe | None = None,
    fiscal: FiscalCalendar | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """The plan as at most MAX_TOOL_CALLS calls, each limited to evidence dated after the
    statement: the metric's periods (always with the period the due date closes), the
    company's later filings, news and later statements; then one search per related
    company it names (its filings when it is covered, else news about it), with its own
    RELATED_K results."""
    after = (promise.statement_date + timedelta(days=1)).isoformat()
    calls: list[tuple[str, dict[str, Any]]] = []
    if promise.metric is not None and promise.metric in ACTUAL_METRICS:
        periods = list(plan.actuals_periods[:4])
        if promise.due_date is not None and fiscal is not None:
            year, quarter = covering_period(fiscal, promise.due_date, promise.measure_period)
            target = f"FY{year}" if quarter is None else f"FY{year}Q{quarter}"
            if target not in periods:
                periods = [target, *periods][:4]
        if periods:
            calls.append(
                (
                    "get_actuals",
                    {
                        "ticker": promise.ticker,
                        "metrics": ACTUAL_METRICS[promise.metric],
                        "periods": periods,
                        "vintage": "first",
                    },
                )
            )

    def filings(query: str, ticker: str | None) -> tuple[str, dict[str, Any]]:
        arguments: dict[str, Any] = {"query": query, "date_from": after, "k": SEARCH_K}
        if ticker:
            arguments["ticker"] = ticker
        return "search_filings", arguments

    def news(query: str, ticker: str | None) -> tuple[str, dict[str, Any]]:
        arguments: dict[str, Any] = {"query": query, "date_from": after, "k": SEARCH_K}
        if ticker:
            arguments["ticker"] = ticker
        return "search_news", arguments

    own = [filings(q, promise.ticker) for q in plan.filing_queries[:3]]
    own_news = [news(q, promise.ticker) for q in plan.news_queries[:2]]
    # Each related company is searched on its own, with its own top results, beyond the
    # cap: a partner's release is often the only document that reports the outcome.
    related = []
    topic = promise.event or promise.subject
    for name in promise.related_companies[:MAX_RELATED]:
        ticker = _covered_ticker(name, universe)
        if ticker is not None and ticker != promise.ticker:
            related.append(("search_filings", {**filings(topic, ticker)[1], "k": RELATED_K}))
        elif ticker is None:
            related.append(("search_news", {**news(f"{name} {topic}", None)[1], "k": RELATED_K}))
    statements = (
        [
            (
                "get_statements",
                {"ticker": promise.ticker, "date_from": after, "limit": LATER_STATEMENTS},
            )
        ]
        if plan.check_later_statements
        else []
    )
    calls += own[:1] + own_news[:1] + statements + own[1:] + own_news[1:]
    return calls[:MAX_TOOL_CALLS] + related


def _covered_ticker(name: str, universe: Universe | None) -> str | None:
    """The ticker of a company of the universe the name refers to (its ticker, name or
    an alias), if any."""
    if universe is None:
        return None
    wanted = name.strip().casefold()
    for company in universe.companies:
        if wanted == company.ticker.casefold() or wanted in (n.casefold() for n in company.names):
            return None if company.news_only else company.ticker
    return None


async def measure(ctx: ToolContext, judgment: Judgment) -> Measured:
    """The measured value, read by code: each source through `cite_value` (spans) or as
    reported, then `compute` (a single value is a sum of one), so it is always a
    derived item that cites its inputs."""
    if not judgment.measured:
        return Measured(None, None, None, "no measured value given")
    ids = []
    for source in judgment.measured:
        if source.span:
            output = await call_tool(
                ctx, "cite_value", {"evidence_id": source.evidence_id, "span": source.span}
            )
            if "error" in output.content:
                return Measured(None, None, None, f"cite_value: {output.content['error']}")
            ids.append(output.evidence[0]["id"])
        else:
            ids.append(source.evidence_id)
    operation = {"value": "sum", "sum": "sum", "ratio": "ratio"}[judgment.measured_operation]
    if operation == "sum" and judgment.measured_operation == "value":
        ids = ids[:1]
    output = await call_tool(ctx, "compute", {"operation": operation, "operands": ids})
    if "error" in output.content:
        return Measured(None, None, None, f"compute: {output.content['error']}")
    item = output.evidence[0]
    unit = str(item["data"]["unit"])
    return Measured(Decimal(str(item["data"]["result"])), unit, item)


def build_resolver(deps: ResolveDeps, fiscal_notes: Mapping[str, str]) -> Any:
    """The per-claim graph. State carries the promise's id; the promise rows and grace
    periods are looked up in `context`."""

    async def plan(state: ResolveState) -> dict[str, Any]:
        promise: Promise = state["promise"]["row"]
        prompt = plan_prompt(promise, fiscal_notes.get(promise.ticker, "unknown"), deps.today)
        response = await complete(
            deps.planner,
            prompt,
            f"{PLAN_PROMPT[0]}/{PLAN_PROMPT[1]}",
            deps.cache,
            deps.budget,
            bounded_usage(prompt, PLAN_OVERHEAD_TOKENS, PLAN_OUTPUT_TOKENS),
        )
        return {
            "plan": response.extraction.model_dump(),
            "usage": _usage(state, response.usage, response.cost, response.cached),
        }

    async def gather(state: ResolveState) -> dict[str, Any]:
        promise: Promise = state["promise"]["row"]
        ctx: ToolContext = state["promise"]["ctx"]
        calls = []
        plan = EvidencePlan.model_validate(state["plan"])
        fiscal = state["promise"].get("fiscal")
        for name, arguments in tool_calls(promise, plan, ctx.universe, fiscal):
            output = await call_tool(ctx, name, arguments)
            calls.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "evidence": [i["id"] for i in output.evidence],
                    "error": output.content.get("error"),
                }
            )
        return {"calls": calls, "evidence": dict(ctx.evidence.items)}

    async def judge(state: ResolveState) -> dict[str, Any]:
        promise: Promise = state["promise"]["row"]
        items = state.get("evidence") or {}
        prompt = judge_prompt(promise, items, state["promise"]["grace"], deps.today)
        response = await complete(
            deps.judge,
            prompt,
            f"{JUDGE_PROMPT[0]}/{JUDGE_PROMPT[1]}",
            deps.cache,
            deps.budget,
            bounded_usage(prompt, JUDGE_OVERHEAD_TOKENS, JUDGE_OUTPUT_TOKENS),
        )
        return {
            "judgment": response.extraction.model_dump(),
            "usage": _usage(state, response.usage, response.cost, response.cached),
        }

    async def checks(state: ResolveState) -> dict[str, Any]:
        promise: Promise = state["promise"]["row"]
        ctx: ToolContext = state["promise"]["ctx"]
        judgment = Judgment.model_validate(state["judgment"])
        measured = await measure(ctx, judgment)
        items = dict(ctx.evidence.items)
        names = _company_names(promise.ticker, ctx.universe)
        context = state["promise"].get("context")
        checked = check(
            facts_of(promise, names, context), judgment, items, measured,
            state["promise"]["grace"], deps.today,
        )  # fmt: skip
        return {"outcome": {"checked": checked, "measured": measured}, "evidence": items}

    graph = StateGraph(ResolveState)
    graph.add_node("plan", plan)
    graph.add_node("gather", gather)
    graph.add_node("judge", judge)
    graph.add_node("check", checks)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "gather")
    graph.add_edge("gather", "judge")
    graph.add_edge("judge", "check")
    graph.add_edge("check", END)
    return graph.compile()


def _usage(state: ResolveState, usage: Usage, cost: Decimal, cached: bool) -> dict[str, Any]:
    line = dict(state.get("usage") or {"input": 0, "output": 0, "cost": "0", "cached": True})
    if not cached:
        line["input"] += usage.input_tokens
        line["output"] += usage.output_tokens
        line["cost"] = str(Decimal(line["cost"]) + cost)
        line["cached"] = False
    return line


async def resolve_one(
    graph: Any,
    deps: ResolveDeps,
    promise: Promise,
    grace: date | None,
    fiscal: FiscalCalendar | None = None,
) -> Resolution:
    ctx = replace(deps.tools, evidence=EvidenceLog(), calls=[])
    started = time.monotonic()
    async with ctx.sessions() as session:
        context = await claim_context(session, promise, deps.today)
    state = await graph.ainvoke(
        {
            "promise": {
                "row": promise,
                "ctx": ctx,
                "grace": grace,
                "fiscal": fiscal,
                "context": context,
            }
        },
    )
    outcome = state["outcome"]
    usage_line = state.get("usage") or {}
    gathered = {
        k: v for k, v in state["evidence"].items() if v["tool"] not in ("cite_value", "compute")
    }
    return Resolution(
        checked=outcome["checked"],
        judgment=Judgment.model_validate(state["judgment"]),
        measured=outcome["measured"],
        evidence=state["evidence"],
        calls=state.get("calls") or [],
        evidence_key=evidence_key(promise, gathered),
        usage=Usage(int(usage_line.get("input", 0)), int(usage_line.get("output", 0))),
        cost=Decimal(str(usage_line.get("cost", "0"))),
        cached=bool(usage_line.get("cached", True)),
        seconds=time.monotonic() - started,
    )


def _company_names(ticker: str, universe: Universe | None) -> list[str]:
    """A company's own names (ticker, name, aliases): never key terms of its rules."""
    names = [ticker]
    if universe is not None:
        for company in universe.companies:
            if company.ticker == ticker:
                names += company.names
    return names


# Runs.


@dataclass(frozen=True, slots=True)
class Candidate:
    promise: Promise  # the group's representative: its earliest statement
    members: list[Promise]
    grace: date | None
    status: str  # "new", "recheck" (has a verdict of this rule version)


@dataclass(slots=True)
class ResolvePlan:
    ticker: str
    promises: int  # current, scorable promises
    due: int
    candidates: list[Candidate]

    def count(self, status: str) -> int:
        return sum(c.status == status for c in self.candidates)


async def plan_resolution(
    session: AsyncSession,
    tickers: Sequence[str],
    calendars: Mapping[str, FiscalCalendar],
    today: date,
    *,
    due_only: bool = True,
    grace_days: int | None = None,
    limit: int | None = None,
) -> list[ResolvePlan]:
    """Which promise groups a run would resolve, per company. `limit`: at most that many
    groups per company, those without a verdict first."""
    plans = []
    for ticker in tickers:
        cik = await session.scalar(select(Company.cik).where(Company.ticker == ticker))
        filings = await company_filings(session, cik) if cik is not None else []
        rows = (
            await session.scalars(
                select(Promise)
                .where(
                    Promise.ticker == ticker,
                    Promise.current,
                    Promise.specificity.in_(("specific", "partial")),
                    Promise.target_kind.is_not(None),
                    Promise.guidance_id.is_(None),  # scored with formal guidance
                )
                .order_by(Promise.statement_date, Promise.id)
            )
        ).all()
        verdicts = {
            v.promise_id: v
            for v in (
                await session.scalars(
                    select(PromiseVerdict).where(
                        PromiseVerdict.promise_id.in_([r.id for r in rows])
                    )
                )
            ).all()
        }
        groups: dict[str, list[Promise]] = defaultdict(list)
        for row in rows:
            groups[row.group_key].append(row)
        fiscal = calendars.get(ticker)
        candidates = []
        due = 0
        for members in groups.values():
            head = members[0]
            end = grace_end(head.due_date, filings, fiscal, grace_days)
            if is_due(head, end, filings, today):
                due += 1
            elif due_only:
                continue
            stored = verdicts.get(head.id)
            fresh = stored is not None and stored.rule_hash == rule_hash(head)
            candidates.append(Candidate(head, members, end, "recheck" if fresh else "new"))
        candidates.sort(key=lambda c: (c.status != "new", c.promise.statement_date))
        if limit is not None:
            candidates = candidates[:limit]
        plans.append(ResolvePlan(ticker, len(rows), due, candidates))
    return plans


@dataclass(slots=True)
class ResolveResult:
    ticker: str
    resolved: int = 0
    up_to_date: int = 0
    cached: int = 0
    called: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    failed_checks: dict[str, int] = field(default_factory=dict)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    cost: Decimal = Decimal(0)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0  # wall time of the company's claims
    claim_seconds: list[float] = field(default_factory=list)  # each claim's own time

    @property
    def ok(self) -> bool:
        return not self.errors


async def resolve_promises(
    sessions: async_sessionmaker[AsyncSession],
    deps: ResolveDeps,
    plans: Sequence[ResolvePlan],
    fiscal_notes: Mapping[str, str],
    *,
    calendars: Mapping[str, FiscalCalendar] | None = None,
    concurrency: int = CONCURRENCY,
    on_claim: Callable[[Candidate, str], None] | None = None,
    on_result: Callable[[ResolveResult], None] | None = None,
) -> list[ResolveResult]:
    """Resolve every planned group, `concurrency` claims at a time, and store its verdict
    on each member. Stops at the cost cap, keeping what was resolved before it."""
    from talk_is_cheap.guidance.llm import CostCapReached, LlmError

    graph = build_resolver(deps, fiscal_notes)
    results = []
    stop = False
    limit = asyncio.Semaphore(concurrency)

    async def one(
        candidate: Candidate, result: ResolveResult, fiscal: FiscalCalendar | None
    ) -> None:
        nonlocal stop
        async with limit:
            if stop:
                return
            async with sessions() as session:
                promise = await session.get(Promise, candidate.promise.id)
                assert promise is not None
                stored = await session.get(PromiseVerdict, promise.id)
            try:
                resolution = await resolve_one(graph, deps, promise, candidate.grace, fiscal)
            except CostCapReached as exc:
                stop = True
                result.errors.append(f"promise {promise.id}: {exc}")
                return
            except LlmError as exc:
                result.errors.append(f"promise {promise.id}: {exc}")
                return
            if resolution.cached:
                result.cached += 1
            else:
                result.called += 1
                result.usage += resolution.usage
                result.cost += resolution.cost
            if stored is not None and stored.evidence_key == resolution.evidence_key:
                result.up_to_date += 1
            async with sessions() as session, session.begin():
                await store_verdict(session, candidate, resolution, deps.judge.name)
            verdict = resolution.checked.verdict
            result.resolved += 1
            result.claim_seconds.append(resolution.seconds)
            result.verdicts[verdict] = result.verdicts.get(verdict, 0) + 1
            for name in resolution.checked.failed:
                result.failed_checks[name] = result.failed_checks.get(name, 0) + 1
            if on_claim is not None:
                on_claim(candidate, verdict)

    for plan in plans:
        result = ResolveResult(plan.ticker)
        fiscal = (calendars or {}).get(plan.ticker)
        started = time.monotonic()

        await asyncio.gather(*(one(c, result, fiscal) for c in plan.candidates))
        if stop:
            result.errors.append("stopped: the cost cap or budget was reached")
        result.seconds = time.monotonic() - started
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def _item_record(item: Evidence, items: Mapping[str, Evidence]) -> dict[str, Any]:
    when = item_date(item, items)
    return {
        "id": item["id"],
        "tool": item["tool"],
        "provenance": item["provenance"],
        "url": item["url"],
        "date": when.isoformat() if when else None,
        "data": item["data"],
    }


async def store_verdict(
    session: AsyncSession, candidate: Candidate, resolution: Resolution, model: str
) -> None:
    head = candidate.promise
    items = resolution.evidence
    judgment = resolution.judgment
    cited = [_item_record(items[i], items) for i in resolution.checked.cited if i in items]
    measured = resolution.measured
    checked = resolution.checked
    for member in candidate.members:
        await session.execute(delete(PromiseVerdict).where(PromiseVerdict.promise_id == member.id))
        session.add(
            PromiseVerdict(
                promise_id=member.id,
                verdict=checked.verdict,
                model_verdict=checked.derived,
                assessments=[a.model_dump() for a in judgment.assessments],
                failed_checks=checked.failed,
                checks=checked.checks,
                measured_value=measured.value,
                measured_unit=measured.unit,
                measured=_item_record(measured.item, items) if measured.item else None,
                evidence=cited,
                gathered=[
                    _item_record(i, items)
                    for i in items.values()
                    if i["tool"] not in ("cite_value", "compute")
                ],
                reasoning=judgment.reasoning,
                confidence=Decimal(str(round(judgment.confidence, 3))),
                proof_date=checked.proof_date,
                proof_date_source=checked.proof_date_source,
                revised_before_due=checked.revised_before_due,
                grace_end=candidate.grace,
                days_late=checked.days_late,
                rule_version=head.version,
                rule_hash=rule_hash(head),
                evidence_key=resolution.evidence_key,
                resolved_with=None if member.id == head.id else head.id,
                model=model,
                prompt_version=prompt_version(),
                input_tokens=resolution.usage.input_tokens if member.id == head.id else 0,
                output_tokens=resolution.usage.output_tokens if member.id == head.id else 0,
                cost=resolution.cost if member.id == head.id else Decimal(0),
            )
        )
    await session.flush()


@dataclass(frozen=True, slots=True)
class DryRun:
    plans: list[ResolvePlan]
    pricing: Pricing | None

    @property
    def claims(self) -> int:
        return sum(len(p.candidates) for p in self.plans)

    @property
    def worst(self) -> Usage:
        """Two calls per claim at their largest: the plan, and the judge reading the most
        evidence gathering can return."""
        per_claim = Usage(
            PLAN_OVERHEAD_TOKENS + 800 + JUDGE_OVERHEAD_TOKENS + 1_200
            + int(JUDGE_EVIDENCE_CHARS / 2.4),
            PLAN_OUTPUT_TOKENS + JUDGE_OUTPUT_TOKENS,
        )  # fmt: skip
        return Usage(per_claim.input_tokens * self.claims, per_claim.output_tokens * self.claims)

    @property
    def worst_cost(self) -> Decimal | None:
        return None if self.pricing is None else self.pricing.cost(self.worst)

    @property
    def typical_cost(self) -> Decimal | None:
        if self.pricing is None:
            return None
        usage = Usage(self.worst.input_tokens // 2, TYPICAL_OUTPUT_TOKENS * self.claims)
        return self.pricing.cost(usage)
