"""The agent's tools: plain async functions over the stored data, each with a Pydantic
input schema and a docstring the model reads.

Every tool returns evidence items (`agent/evidence.py`), each with a citation id, a
provenance tag, a URL and its data, and keeps them small: the fields the model needs,
passages cut to PASSAGE_CHARS. A tool that cannot answer says why instead of raising, so
the model can try something else.
"""

import difflib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.agent import sql
from talk_is_cheap.agent.evidence import (
    Evidence,
    EvidenceLog,
    Provenance,
    compact,
    evidence_date,
    provenance_of,
)
from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    Chunk,
    Company,
    Document,
    Filing,
    FinancialValue,
)
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.financials import (
    MetricValue,
    QuarterFinancials,
    Vintage,
    YearFinancials,
    annual_financials,
    find_company,
    quarterly_financials,
)
from talk_is_cheap.guidance.matching import counts_in_verdicts, needs_review
from talk_is_cheap.guidance.queries import company_guidance
from talk_is_cheap.universe import Universe, UniverseCompany

if TYPE_CHECKING:
    from talk_is_cheap.db.models import Guidance, GuidanceSource
    from talk_is_cheap.retrieval.encoders import Embedder, Reranker
    from talk_is_cheap.retrieval.search import SearchResult
    from talk_is_cheap.statements.queries import StatementRow

PASSAGE_CHARS = 700
QUOTE_CHARS = 300
SUMMARY_CHARS = 300
MAX_PERIODS = 12

Metric = Literal[
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "operating_expenses",
    "net_income",
    "eps_diluted",
    "research_and_development",
    "operating_cash_flow",
    "capex",
    "finance_lease_principal_payments",
]
GuidanceMetric = Literal[
    "revenue",
    "gross_margin",
    "operating_margin",
    "operating_expenses",
    "operating_income",
    "eps",
    "capex",
    "tax_rate",
    "other",
]
DocType = Literal["press_release", "earnings_exhibit", "deliveries_report", "10q", "10k"]
SectionType = Literal["outlook", "results", "commentary", "mdna", "risk_factors", "other"]
Operation = Literal["pct_change", "difference", "ratio", "sum", "margin"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    seconds: float
    evidence_ids: list[str]
    error: str | None


@dataclass
class ToolContext:
    """What the tools run against. `embedder` and `reranker` load the search models on
    first use; `evidence` assigns citation ids; `calls` logs every call."""

    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
    universe: Universe
    embedder: Callable[[], "Embedder"]
    reranker: Callable[[], "Reranker"]
    evidence: EvidenceLog = field(default_factory=EvidenceLog)
    calls: list[ToolCall] = field(default_factory=list)
    sql_timeout_seconds: float = sql.STATEMENT_TIMEOUT_SECONDS
    # Hybrid results the cross-encoder re-orders in `search_filings` and `search_news`;
    # None for the retrieval default.
    rerank_depth: int | None = None
    _models: dict[str, Any] = field(default_factory=dict)

    def loaded_embedder(self) -> "Embedder":
        if "embedder" not in self._models:
            self._models["embedder"] = self.embedder()
        embedder: Embedder = self._models["embedder"]
        return embedder

    def loaded_reranker(self) -> "Reranker":
        if "reranker" not in self._models:
            self._models["reranker"] = self.reranker()
        reranker: Reranker = self._models["reranker"]
        return reranker


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """What the model reads (`content`, JSON-ready) and the evidence it added."""

    content: dict[str, Any]
    evidence: list[Evidence]


class ToolFailed(Exception):
    """The tool could not answer; the message tells the model why."""


# Inputs. Field descriptions are part of what the model reads.


class ResolveCompanyInput(BaseModel):
    query: str = Field(description="company name or ticker, e.g. 'Nvidia' or 'NVDA'")


class GetActualsInput(BaseModel):
    ticker: str
    metrics: list[Metric] = Field(min_length=1, max_length=6)
    periods: list[str] = Field(
        min_length=1,
        max_length=MAX_PERIODS,
        description="fiscal quarters 'FY2027Q2', fiscal years 'FY2026', or 'latest' and "
        "'latest-1', 'latest-2', ... for the company's most recent reported quarters",
    )
    vintage: Literal["first", "latest"] = Field(
        default="latest",
        description="'first': as first reported; 'latest': as most recently reported",
    )


class GetGuidanceInput(BaseModel):
    ticker: str
    metric: GuidanceMetric | None = None
    issued_period: str | None = Field(
        default=None, description="the quarter whose earnings release issued it, 'FY2026Q2'"
    )
    target_period: str | None = Field(
        default=None, description="the period guided: a quarter 'FY2026Q3' or a year 'FY2026'"
    )
    limit: int = Field(default=10, ge=1, le=20)


class SearchFilingsInput(BaseModel):
    query: str
    ticker: str | None = None
    periods: str | None = Field(
        default=None, description="fiscal quarters or ranges: 'FY2027Q2', 'FY2026Q1..FY2027Q2'"
    )
    doc_types: list[DocType] | None = None
    section_types: list[SectionType] | None = None
    date_from: date | None = Field(default=None, description="filed on or after this day")
    date_to: date | None = Field(default=None, description="filed on or before this day")
    k: int = Field(default=6, ge=1, le=10)


class SearchNewsInput(BaseModel):
    query: str
    ticker: str | None = None
    date_from: date | None = Field(default=None, description="published on or after this day")
    date_to: date | None = Field(default=None, description="published on or before this day")
    k: int = Field(default=6, ge=1, le=10)


class GetStatementsInput(BaseModel):
    ticker: str
    metric: GuidanceMetric | None = None
    period: str | None = Field(
        default=None,
        description="the target period 'FY2025' or 'FY2026Q3' (a year includes its "
        "quarters), or a calendar year '2025' of the statement or its target",
    )
    speaker: str | None = Field(default=None, description="a name, e.g. 'Jensen Huang' or 'Huang'")
    management_only: bool = Field(
        default=True,
        description="true: only claims by executives verified against the roster, "
        "spokespeople and the company; false: also analysts, unnamed sources and reporters",
    )
    checkable_only: bool = Field(
        default=False, description="only numeric forecasts and directional claims"
    )
    date_from: date | None = Field(default=None, description="made on or after this day")
    date_to: date | None = Field(default=None, description="made on or before this day")
    limit: int = Field(default=10, ge=1, le=20)


PromiseVerdict = Literal[
    "delivered",
    "delivered_late",
    "partly_delivered",
    "not_delivered",
    "walked_back",
    "too_early",
    "expired_unproven",
    "cannot_determine",
]


class GetPromisesInput(BaseModel):
    ticker: str | None = None
    query: str | None = Field(
        default=None,
        description="words the claim's subject or quote must contain, e.g. 'Blackwell' or "
        "'dividend'",
    )
    speaker: str | None = Field(default=None, description="a name, e.g. 'Elon Musk' or 'Musk'")
    verdict: PromiseVerdict | None = None
    due_before: date | None = None
    due_after: date | None = None
    limit: int = Field(default=8, ge=1, le=15)


class GetTrackRecordInput(BaseModel):
    ticker: str | None = None
    speaker: str | None = None
    by: Literal["executive", "company"] = "executive"


class CiteValueInput(BaseModel):
    evidence_id: str = Field(description="the evidence id of a passage, headline or quote")
    span: str = Field(
        description="the exact text of that item, copied character for character, that "
        "contains the one number to cite: 'produced 410,831 vehicles'"
    )


class ComputeInput(BaseModel):
    operation: Operation = Field(
        description="pct_change: (b - a) / |a| * 100 from operands [a, b]; difference: "
        "a - b; ratio: a / b; sum: all operands; margin: a / b * 100"
    )
    operands: list[str] = Field(
        min_length=1,
        max_length=12,
        description="evidence ids, 'E3' for its value or 'E3.point' for one field",
    )


class RunSqlInput(BaseModel):
    sql: str = Field(description="one SELECT statement")


# Tools.


async def resolve_company(ctx: ToolContext, args: ResolveCompanyInput) -> ToolOutput:
    """Find a company by name or ticker. Returns its CIK, ticker, name, fiscal year end
    (MMDD) and latest reported quarter, or the closest matches."""
    query = args.query.strip()
    async with ctx.sessions() as session:
        companies = {c.ticker: c for c in (await session.scalars(select(Company))).all()}
        match = _match_company(query, ctx.universe, companies)
        if match is None:
            names = {
                **{t: t for t in companies},
                **{n.lower(): c.ticker for c in ctx.universe.companies for n in c.names},
                **{c.name.lower(): t for t, c in companies.items()},
            }
            close = difflib.get_close_matches(query.lower(), list(names), n=3, cutoff=0.6)
            return ToolOutput(
                {
                    "found": False,
                    "closest": sorted({names[c] for c in close}),
                    "covered": sorted(companies),
                },
                [],
            )
        company = companies[match]
        latest = await session.scalar(
            select(func.max(FinancialValue.fiscal_year * 10 + FinancialValue.fiscal_quarter)).where(
                FinancialValue.cik == company.cik, FinancialValue.period_type == "quarter"
            )
        )
    universe_company = _universe_company(ctx.universe, company.ticker)
    item = ctx.evidence.add(
        "sec_filing",
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company.cik:010d}",
        "resolve_company",
        {
            "ticker": company.ticker,
            "cik": company.cik,
            "name": universe_company.name if universe_company else company.name,
            "sec_name": company.name,
            "fiscal_year_end": company.fiscal_year_end,
            "latest_reported_quarter": f"FY{latest // 10}Q{latest % 10}" if latest else None,
        },
    )
    return ToolOutput({"found": True, "evidence": [item]}, [item])


async def get_actuals(ctx: ToolContext, args: GetActualsInput) -> ToolOutput:
    """Reported values from XBRL in 10-Qs and 10-Ks, per metric and fiscal period, as
    first or as latest reported, with unit, accession number and filing URL. Quarters
    derived from the annual total or year-to-date values are marked `derived`. Margins
    and growth are not stored: get the values and use `compute`."""
    async with ctx.sessions() as session:
        company = await _company(session, args.ticker)
        vintage: Vintage = args.vintage
        quarters = await quarterly_financials(
            session, company.cik, vintage=vintage, quarters=None, metrics=args.metrics
        )
        wanted_years = [p for p in args.periods if _is_year(p)]
        years = (
            await annual_financials(session, company.cik, vintage=vintage, metrics=args.metrics)
            if wanted_years
            else []
        )
        rows: list[tuple[str, date, str, MetricValue]] = []
        missing: list[str] = []
        for period in args.periods:
            found = _period_values(period, quarters, years)
            if found is None:
                missing.append(f"{period}: no such period reported")
                continue
            for metric in args.metrics:
                value = found.metrics.get(metric)
                if value is None:
                    missing.append(f"{metric} {found.label}: not reported")
                else:
                    rows.append((found.label, found.period_end, metric, value))
        urls = await _filing_urls(session, company.cik, [v.accession_no for *_, v in rows])
        accepted = dict(
            (
                await session.execute(
                    select(Filing.accession_no, Filing.acceptance_datetime).where(
                        Filing.accession_no.in_({v.accession_no for *_, v in rows})
                    )
                )
            ).all()
        )
    items = [
        ctx.evidence.add(
            "sec_filing",
            urls.get(value.accession_no, ""),
            "get_actuals",
            {
                "ticker": company.ticker,
                "metric": metric,
                "period": label,
                "period_end": period_end.isoformat(),
                "value": float(value.value),
                "unit": value.unit,
                "display": display(value.value, value.unit),
                "vintage": vintage,
                "form": value.form,
                "filed": value.filed.isoformat(),
                **(
                    {"accepted": at.isoformat()}
                    if (at := accepted.get(value.accession_no)) is not None
                    else {}
                ),
                "accession_no": value.accession_no,
                **({"derived": value.derivation or True} if value.derived else {}),
            },
        )
        for label, period_end, metric, value in rows
    ]
    content: dict[str, Any] = {"evidence": items}
    if missing:
        content["not_found"] = missing
    return ToolOutput(content, items)


async def get_guidance(ctx: ToolContext, args: GetGuidanceInput) -> ToolOutput:
    """Management's guidance extracted from earnings releases, newest issue first:
    low/high/point and unit, basis, conditions, the verdict against the result as first
    reported (beat, met, missed, pending, not_comparable), the quoted sentence and its
    source URL. `review` says whether the plausibility check flagged the row and what a
    reviewer decided; rows that need review or were reviewed as wrong do not count."""
    async with ctx.sessions() as session:
        company = await _company(session, args.ticker)
        rows = await company_guidance(session, company.cik, args.metric)
        if not rows:
            return ToolOutput(*await _no_guidance(ctx, session, company, args.metric))
    issued = _quarter(args.issued_period) if args.issued_period else None
    target = args.target_period.replace(" ", "").upper() if args.target_period else None
    selected = [
        r
        for r in rows
        if (issued is None or (r.guidance.fiscal_year, r.guidance.fiscal_quarter) == issued)
        and (target is None or _target_label(r.guidance) == target)
    ]
    if not selected:
        periods = sorted({_target_label(r.guidance) for r in rows}, reverse=True)[:8]
        return ToolOutput(
            {"evidence": [], "not_found": "no guidance row matches", "targets_guided": periods},
            [],
        )
    items = [
        ctx.evidence.add(
            provenance_of(r.sources[0].doc_type) if r.sources else "company_press_release",
            r.sources[0].url if r.sources else "",
            "get_guidance",
            _guidance_data(company.ticker, r.guidance, r.sources),
        )
        for r in selected[: args.limit]
    ]
    content: dict[str, Any] = {"evidence": items}
    if len(selected) > args.limit:
        content["more_rows"] = len(selected) - args.limit
    return ToolOutput(content, items)


async def get_statements(ctx: ToolContext, args: GetStatementsInput) -> ToolOutput:
    """Forward-looking claims about a company reported in the news (interviews,
    conferences, earnings calls quoted by reporters, statements, posts), newest first:
    who said it (speaker, role, attribution), venue, date, publisher and article link, a
    short quote, the claim's figures and target period; for management's claims the
    outcome against the result as first reported (beat, met, missed, pending,
    not_comparable; directional: confirmed, contradicted; plans: not_checkable, with a
    filing passage as context) and how it compares with formal guidance issued within 45
    days (consistent, differs, no_formal_guidance). News is secondary: say "reported by
    <publisher>", and prefer filings and guidance for figures they also give."""
    from talk_is_cheap.statements.queries import company_statements

    async with ctx.sessions() as session:
        company = await _company(session, args.ticker)
        rows = await company_statements(
            session,
            company.ticker,
            metric=args.metric,
            period=args.period,
            speaker=args.speaker,
            management_only=args.management_only,
            checkable_only=args.checkable_only,
        )
        rows = [
            r
            for r in rows
            if (args.date_from is None or r.statement.statement_date >= args.date_from)
            and (args.date_to is None or r.statement.statement_date <= args.date_to)
        ]
        if not rows:
            others = 0
            if args.management_only:
                others = len(
                    await company_statements(
                        session,
                        company.ticker,
                        metric=args.metric,
                        period=args.period,
                        speaker=args.speaker,
                        management_only=False,
                    )
                )
            content: dict[str, Any] = {
                "evidence": [],
                "not_found": f"no {'management ' if args.management_only else ''}claims "
                "in stored news match",
            }
            if others:
                content["claims_by_others"] = (
                    f"{others} matching claims by analysts, unnamed sources or reporters; "
                    "they are not management's (management_only=false lists them)"
                )
            return ToolOutput(content, [])
    items = [
        ctx.evidence.add("news_report", r.url, "get_statements", _statement_data(r))
        for r in rows[: args.limit]
    ]
    content = {"evidence": items}
    if len(rows) > args.limit:
        content["more_claims"] = len(rows) - args.limit
    return ToolOutput(content, items)


async def search_filings(ctx: ToolContext, args: SearchFilingsInput) -> ToolOutput:
    """Search the text of earnings releases, CFO commentaries, 10-Qs and 10-Ks (hybrid
    keyword and vector search, reranked). Use for explanations (MD&A, section `mdna`),
    risk factors (`risk_factors`), outlook text (`outlook`), and operating figures
    (deliveries reports, shareholder letters). Pass the ticker and the periods or filing
    dates when known. Returns passages with filing date, document URL and character
    range."""
    results = await _search(
        ctx,
        args.query,
        tickers=[args.ticker.upper()] if args.ticker else None,
        periods=args.periods,
        sources=["filing"],
        doc_types=list(args.doc_types) if args.doc_types else None,
        section_types=list(args.section_types) if args.section_types else None,
        since=args.date_from,
        until=args.date_to,
        k=args.k,
    )
    items = [
        ctx.evidence.add(
            provenance_of(r.doc_type),
            r.url,
            "search_filings",
            {
                "ticker": ", ".join(r.tickers),
                "period": r.period.label if r.period else None,
                "filed": r.published_at.date().isoformat(),
                **({"period_end": r.period_end.isoformat()} if r.period_end else {}),
                **({"accepted": r.accepted_at.isoformat()} if r.accepted_at else {}),
                "doc_type": r.doc_type,
                "section": r.section_type,
                "section_title": r.section_title,
                "chars": f"{r.char_start}-{r.char_end}",
                "text": _cut(" ".join(r.text.split()), PASSAGE_CHARS),
            },
        )
        for r in results
    ]
    return ToolOutput({"evidence": items}, items)


async def search_news(ctx: ToolContext, args: SearchNewsInput) -> ToolOutput:
    """Search news headlines and feed summaries (not full articles). Returns headline,
    publisher, date, summary, link, and how confidently the article is linked to the
    ticker. News is secondary: prefer filings and guidance for figures."""
    tickers = [args.ticker.upper()] if args.ticker else None
    results = await _search(
        ctx,
        args.query,
        tickers=tickers,
        since=args.date_from,
        until=args.date_to,
        sources=["news"],
        k=args.k,
    )
    async with ctx.sessions() as session:
        found = await session.execute(
            select(Chunk.id, Article)
            .join(Article, Article.id == Chunk.article_id)
            .where(Chunk.id.in_([r.chunk_id for r in results]))
        )
        articles = {chunk_id: article for chunk_id, article in found}
        links: dict[int, Decimal] = {}
        if tickers:
            linked = await session.execute(
                select(ArticleCompany.article_id, ArticleCompany.confidence).where(
                    ArticleCompany.article_id.in_([a.id for a in articles.values()]),
                    ArticleCompany.ticker == tickers[0],
                )
            )
            links = {article_id: confidence for article_id, confidence in linked}
    items = []
    for r in results:
        article = articles.get(r.chunk_id)
        if article is None:
            continue
        data: dict[str, Any] = {
            "headline": article.title,
            "publisher": article.publisher,
            "published": article.published_at.date().isoformat(),
            "summary": _cut(article.summary or "", SUMMARY_CHARS),
        }
        if article.id in links:
            data["link_confidence"] = float(links[article.id])
        items.append(ctx.evidence.add("news_report", article.url, "search_news", data))
    return ToolOutput({"evidence": items}, items)


async def compute(ctx: ToolContext, args: ComputeInput) -> ToolOutput:
    """Arithmetic over evidence values. All arithmetic in an answer comes from here:
    pct_change [a, b] is (b - a) / |a| * 100; difference [a, b] is a - b; ratio [a, b] is
    a / b; sum adds all operands; margin [a, b] is a / b * 100 (gross profit over revenue).
    Operands are evidence ids ('E3') or an id and a field ('E5.point'). Returns the result
    as new evidence that cites its operands."""
    values = [_operand(ctx.evidence, ref) for ref in args.operands]
    numbers = [v for v, _, _ in values]
    units = {u for _, u, _ in values}
    op = args.operation
    expected = {"pct_change": 2, "difference": 2, "ratio": 2, "margin": 2}
    if op in expected and len(numbers) != expected[op]:
        raise ToolFailed(f"{op} takes exactly 2 operands, got {len(numbers)}")
    if op in ("pct_change", "difference", "sum") and len(units) > 1:
        raise ToolFailed(f"{op} needs operands in one unit, got {', '.join(sorted(units))}")
    try:
        if op == "pct_change":
            result, unit = (numbers[1] - numbers[0]) / abs(numbers[0]) * 100, "percent"
        elif op == "difference":
            result, unit = numbers[0] - numbers[1], units.pop()
        elif op == "sum":
            result, unit = sum(numbers, Decimal(0)), units.pop()
        elif op == "ratio":
            result, unit = numbers[0] / numbers[1], "ratio"
        else:
            result, unit = numbers[0] / numbers[1] * 100, "percent"
    except (ZeroDivisionError, InvalidOperation) as exc:
        raise ToolFailed(f"{op} divides by zero") from exc
    inputs = list(dict.fromkeys(item_id for _, _, item_id in values))
    first = ctx.evidence.items[inputs[0]]
    provenances = {ctx.evidence.items[i]["provenance"] for i in inputs}
    provenance: Provenance = first["provenance"] if len(provenances) == 1 else "sec_filing"
    rounded = round(result, 6)
    item = ctx.evidence.add(
        provenance,
        first["url"],
        "compute",
        {
            "operation": op,
            "operands": list(args.operands),
            "inputs": inputs,
            "result": float(rounded),
            "unit": unit,
            "display": display(rounded, unit),
        },
    )
    return ToolOutput({"evidence": [item]}, [item])


async def get_promises(ctx: ToolContext, args: GetPromisesInput) -> ToolOutput:
    """Promises management made in public (reported in the news), framed into a fixed
    resolution rule and judged from evidence dated after the statement: quote, speaker,
    date, publisher and article link, the rule, due date, verdict (delivered,
    delivered_late, partly_delivered, not_delivered, walked_back, too_early,
    cannot_determine, or null when not resolved yet), measured value against the target,
    and the outcome evidence as items of their own. Vague claims are listed with
    `scored: false`: they are never judged."""
    from talk_is_cheap.promises.queries import promise_rows

    async with ctx.sessions() as session:
        ticker = (await _company(session, args.ticker)).ticker if args.ticker else None
        rows = await promise_rows(
            session,
            ticker,
            verdicts=[args.verdict] if args.verdict else None,
            speaker=args.speaker,
            due_before=args.due_before,
            due_after=args.due_after,
        )
    if args.query:
        words = args.query.casefold().split()
        rows = [
            r
            for r in rows
            if all(w in f"{r.promise.subject} {r.promise.quote}".casefold() for w in words)
        ]
    if not rows:
        return ToolOutput({"evidence": [], "not_found": "no promises in stored news match"}, [])
    items: list[Evidence] = []
    for row in rows[: args.limit]:
        outcome = [_outcome_item(ctx, e) for e in _outcome_evidence(row)]
        items.append(_promise_item(ctx, row, [o["id"] for o in outcome]))
        items.extend(outcome)
    content: dict[str, Any] = {"evidence": items}
    if len(rows) > args.limit:
        content["more_promises"] = len(rows) - args.limit
    return ToolOutput(content, items)


async def get_track_record(ctx: ToolContext, args: GetTrackRecordInput) -> ToolOutput:
    """How executives' (or companies') public promises held up: resolved claims by
    verdict, the on-time rate, median days late, the share walked back, the share of
    bold claims and their hit rate. Only specific and partial claims count, each once;
    vague claims are counted apart and never scored. A group with fewer than 5 resolved
    claims is "too few to judge": say so, and give no rate for it."""
    from talk_is_cheap.promises.queries import promise_rows
    from talk_is_cheap.promises.track import TOO_FEW, company_track_records

    async with ctx.sessions() as session:
        ticker = (await _company(session, args.ticker)).ticker if args.ticker else None
        records = await company_track_records(
            session, by=args.by, ticker=ticker, speaker=args.speaker
        )
        rows = {r.promise.id: r for r in await promise_rows(session, ticker, speaker=args.speaker)}
    if not records:
        return ToolOutput({"evidence": [], "not_found": "no promises in stored news match"}, [])
    items: list[Evidence] = []
    for record in records[:10]:
        examples = [rows[i] for i in record.promise_ids if i in rows and rows[i].verdict][:3]
        inputs = [_promise_item(ctx, r, []) for r in examples]
        items.extend(inputs)
        data: dict[str, Any] = {
            "group": record.key,
            "claims": record.claims,
            "resolved": record.resolved,
            "by_verdict": record.by_verdict,
            "vague_not_scored": record.vague,
            "expired_unproven": record.expired,
            "inputs": [i["id"] for i in inputs],
        }
        if record.enough:
            data |= {
                k: round(v * 100, 1) if v is not None else None
                for k, v in (
                    ("on_time_rate_pct", record.on_time_rate),
                    ("walked_back_pct", record.walked_back_share),
                    ("bold_pct", record.bold_share),
                    ("bold_hit_rate_pct", record.bold_hit_rate),
                )
            }
            data["median_days_late"] = record.median_days_late
        else:
            data["judgment"] = TOO_FEW
        items.append(ctx.evidence.add("news_report", "", "get_track_record", data))
    return ToolOutput({"evidence": items}, items)


def _promise_item(ctx: ToolContext, row: Any, outcome_ids: Sequence[str]) -> Evidence:
    from talk_is_cheap.statements.commands import short_quote

    promise, verdict = row.promise, row.verdict
    data: dict[str, Any] = {
        "promise_id": promise.id,
        "ticker": promise.ticker,
        "speaker": promise.speaker,
        "date": promise.statement_date.isoformat(),
        "publisher": row.publisher,
        "quote": short_quote(promise.quote),
        "subject": promise.subject,
        "rule": promise.resolution_rule,
        "due": promise.due_date.isoformat() if promise.due_date else "open-ended",
        "specificity": promise.specificity,
        "bold": promise.bold,
        "provenance_note": f"reported by {row.publisher or 'a news outlet'}",
    }
    if promise.guidance_id is not None:
        data["verdict_note"] = (
            "restates formal guidance: scored with the guidance (get_guidance), not here"
        )
        return ctx.evidence.add("news_report", row.url, "get_promises", data)
    if promise.claim_type == "scheduled_action":
        data["claim_type"] = "scheduled action: resolved, but not counted in track records"
    if promise.specificity == "vague":
        data["scored"] = False
        data["verdict_note"] = "vague: no checkable target or deadline, never scored"
        return ctx.evidence.add("news_report", row.url, "get_promises", data)
    data["verdict"] = row.effective
    if row.effective is None:
        data["verdict_note"] = "not resolved yet"
    elif row.effective == "too_early":
        data["verdict_note"] = "too early to judge"
    elif row.effective == "expired_unproven":
        data["verdict_note"] = "open-ended and not shown delivered 18 months after the statement"
    if row.review is not None:
        data["review"] = f"reviewed as {row.review.verdict}" + (
            f": {row.review.note}" if row.review.note else ""
        )
    if promise.target_value is not None:
        data["target"] = float(promise.target_value)
        data["unit"] = promise.unit
    if promise.required_value is not None:
        data["required_value"] = float(promise.required_value)
    if verdict is not None:
        if verdict.measured_value is not None:
            data["measured_value"] = float(verdict.measured_value)
            data["measured_unit"] = verdict.measured_unit
        data["reasoning"] = verdict.reasoning
        if verdict.days_late is not None:
            data["days_late"] = verdict.days_late
        if verdict.revised_before_due:
            data["revised_before_due"] = True
        if verdict.failed_checks:
            data["failed_checks"] = verdict.failed_checks
    if outcome_ids:
        data["outcome_evidence"] = list(outcome_ids)
    return ctx.evidence.add("news_report", row.url, "get_promises", data)


def _outcome_evidence(row: Any) -> list[dict[str, Any]]:
    """The outcome evidence a verdict cites that has a URL, at most 3."""
    if row.verdict is None or row.promise.specificity == "vague":
        return []
    return [e for e in row.verdict.evidence if e.get("url")][:3]


def _outcome_item(ctx: ToolContext, record: Mapping[str, Any]) -> Evidence:
    data = record.get("data") or {}
    kept = {
        k: (_cut(v, 300) if isinstance(v, str) else v)
        for k, v in data.items()
        if k in ("ticker", "metric", "period", "value", "unit", "display", "doc_type",
                 "section", "headline", "publisher", "claim", "quote", "text", "span")
    }  # fmt: skip
    kept["kind"] = f"outcome evidence ({record.get('tool')})"
    kept["date"] = record.get("date")
    provenance: Provenance = record.get("provenance") or "sec_filing"
    return ctx.evidence.add(provenance, str(record["url"]), "promise_evidence", kept)


async def cite_value(ctx: ToolContext, args: CiteValueInput) -> ToolOutput:
    """A number from text, grounded like a reported value: give the evidence id of a
    passage, headline or quote and the exact span that contains the number. The span
    must be in that item's text and hold exactly one number; returns it as new evidence
    with its value, unit and the source's date and URL, for `compute`."""
    source = ctx.evidence.get(args.evidence_id.strip())
    if source is None:
        raise ToolFailed(f"{args.evidence_id} is not an evidence id")
    found = cited_number(source, args.span)
    item = ctx.evidence.add(
        source["provenance"],
        source["url"],
        "cite_value",
        {
            "source": source["id"],
            "span": found.span,
            "value": float(found.value),
            "unit": found.unit,
            "display": display(found.value, found.unit),
            **({"date": d.isoformat()} if (d := evidence_date(source)) else {}),
        },
    )
    return ToolOutput({"evidence": [item]}, [item])


@dataclass(frozen=True, slots=True)
class CitedNumber:
    span: str  # as the source writes it
    value: Decimal
    unit: str  # USD, percent, or the counted word ("vehicles")


# Fields of an evidence item a cited span may come from.
TEXT_FIELDS = ("text", "quote", "headline", "summary", "claim")


def cited_number(source: Evidence, span: str) -> CitedNumber:
    """The one number in `span`, which must be in one of the source's text fields.
    Raises ToolFailed otherwise."""
    from talk_is_cheap.agent.checks import mentions, strip_non_figures
    from talk_is_cheap.guidance.grounding import locate

    for name in TEXT_FIELDS:
        text = source["data"].get(name)
        if not isinstance(text, str):
            continue
        place = locate(span, text)
        if place is None:
            continue
        written = text[place[0] : place[1]]
        numbers = [m for m in mentions(written)]
        if len(numbers) != 1:
            raise ToolFailed(f"the span holds {len(numbers)} numbers; give a span with exactly one")
        number = numbers[0]
        unit = {"usd": "USD", "pct": "percent"}.get(number.kind)
        if unit is None:
            after = strip_non_figures(written).split(number.text, 1)[-1].split()
            unit = after[0].strip(".,;:()").lower() if after else "count"
        return CitedNumber(written, number.value, unit)
    raise ToolFailed(f"the span is not in the text of {source['id']}")


async def run_sql(ctx: ToolContext, args: RunSqlInput) -> ToolOutput:
    """One read-only SELECT over companies, filings, financial_values, documents,
    sections, guidance, guidance_sources, guidance_reviews, stock_splits, articles,
    article_companies, statements, statement_outcomes, promises, promise_verdicts and
    promise_reviews; at most 200 rows, 5 seconds.
    For questions the other tools cannot answer (counts, rankings). Rows have no citation
    URL: cite figures from get_actuals, get_guidance or get_statements."""
    try:
        async with ctx.engine.connect() as connection:
            result = await sql.run_sql(
                connection, args.sql, timeout_seconds=ctx.sql_timeout_seconds
            )
    except sql.SqlRejected as exc:
        raise ToolFailed(f"rejected: {exc}") from exc
    except Exception as exc:  # the database's error is the model's to read
        message = str(getattr(exc, "orig", exc)).splitlines()[0]
        raise ToolFailed(f"query failed: {message}") from exc
    item = ctx.evidence.add(
        "sec_filing",
        "",
        "run_sql",
        {
            "sql": result.sql,
            "columns": result.columns,
            "rows": result.rows,
            **({"truncated_at": sql.MAX_ROWS} if result.truncated else {}),
        },
    )
    return ToolOutput({"evidence": [item]}, [item])


ToolFunction = Callable[[ToolContext, Any], Awaitable[ToolOutput]]

TOOLS: Mapping[str, tuple[ToolFunction, type[BaseModel]]] = {
    "resolve_company": (resolve_company, ResolveCompanyInput),
    "get_actuals": (get_actuals, GetActualsInput),
    "get_guidance": (get_guidance, GetGuidanceInput),
    "get_statements": (get_statements, GetStatementsInput),
    "search_filings": (search_filings, SearchFilingsInput),
    "search_news": (search_news, SearchNewsInput),
    "compute": (compute, ComputeInput),
    "cite_value": (cite_value, CiteValueInput),
    "get_promises": (get_promises, GetPromisesInput),
    "get_track_record": (get_track_record, GetTrackRecordInput),
    "run_sql": (run_sql, RunSqlInput),
}


async def call_tool(ctx: ToolContext, name: str, arguments: Mapping[str, Any]) -> ToolOutput:
    """Validate the arguments, run the tool and log the call. A failure is returned as
    an `error` the model reads, never raised."""
    function, schema = TOOLS[name]
    started = time.monotonic()
    error = None
    try:
        output = await function(ctx, schema.model_validate(dict(arguments)))
    except ToolFailed as exc:
        error = str(exc)
    except ValueError as exc:  # pydantic's ValidationError among them
        error = f"invalid arguments: {exc}"
    if error is not None:
        output = ToolOutput({"error": error}, [])
    elif "evidence" in output.content:
        # The model reads each item flat: id, provenance, URL and its data.
        output = ToolOutput(
            {**output.content, "evidence": compact(output.evidence)}, output.evidence
        )
    ctx.calls.append(
        ToolCall(
            name,
            dict(arguments),
            time.monotonic() - started,
            [i["id"] for i in output.evidence],
            error,
        )
    )
    return output


# Helpers.


def display(value: Decimal | float, unit: str) -> str:
    """A figure as a person writes it: "$96,221M", "$1.87", "74.98%"."""
    number = Decimal(str(value))
    if unit == "USD":
        sign = "-" if number < 0 else ""
        return (
            f"{sign}${abs(number) / 10**6:,.0f}M"
            if abs(number) >= 10**6
            else f"{sign}${abs(number):,.0f}"
        )
    if unit == "USD/shares":
        return f"-${abs(number):.2f}" if number < 0 else f"${number:.2f}"
    if unit == "percent":
        return f"{number:.2f}%"
    if unit == "ratio":
        return f"{number:.4f}"
    return f"{number.normalize():,f}"


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def _is_year(period: str) -> bool:
    compact = period.replace(" ", "").upper()
    return compact.startswith("FY") and compact[2:].isdigit() and len(compact) == 6


def _quarter(period: str) -> tuple[int, int]:
    try:
        quarter = FiscalQuarter.parse(period)
    except ValueError as exc:
        raise ToolFailed(str(exc)) from exc
    return quarter.fiscal_year, quarter.fiscal_quarter


@dataclass(frozen=True, slots=True)
class _Period:
    label: str
    period_end: date
    metrics: Mapping[str, MetricValue]


def _period_values(
    period: str, quarters: Sequence[QuarterFinancials], years: Sequence[YearFinancials]
) -> _Period | None:
    compact = period.replace(" ", "").lower()
    if compact.startswith("latest"):
        back = compact.removeprefix("latest")
        if back and not (back.startswith("-") and back[1:].isdigit()):
            raise ToolFailed(f"period {period!r}: use 'latest' or 'latest-1'")
        offset = int(back[1:]) if back else 0
        if offset >= len(quarters):
            return None
        row = quarters[offset]
        return _Period(f"FY{row.fiscal_year}Q{row.fiscal_quarter}", row.period_end, row.metrics)
    if _is_year(period):
        year = int(compact[2:])
        for year_row in years:
            if year_row.fiscal_year == year:
                return _Period(f"FY{year}", year_row.period_end, year_row.metrics)
        return None
    fiscal_year, fiscal_quarter = _quarter(period)
    for row in quarters:
        if (row.fiscal_year, row.fiscal_quarter) == (fiscal_year, fiscal_quarter):
            return _Period(f"FY{fiscal_year}Q{fiscal_quarter}", row.period_end, row.metrics)
    return None


async def _company(session: AsyncSession, ticker: str) -> Company:
    company = await find_company(session, ticker.strip())
    if company is None:
        raise ToolFailed(f"{ticker!r} is not a loaded company; use resolve_company")
    return company


def _universe_company(universe: Universe, ticker: str) -> UniverseCompany | None:
    return next((c for c in universe.companies if c.ticker == ticker), None)


def _match_company(query: str, universe: Universe, companies: Mapping[str, Company]) -> str | None:
    wanted = query.lower().removesuffix("'s")
    if wanted.upper() in companies:
        return wanted.upper()
    for company in universe.companies:
        if company.ticker in companies and wanted in (n.lower() for n in company.names):
            return company.ticker
    for ticker, company_row in companies.items():
        sec_name = company_row.name.lower()
        if wanted == sec_name or sec_name.startswith(wanted + " "):
            return ticker
    return None


async def _filing_urls(
    session: AsyncSession, cik: int, accession_numbers: Sequence[str]
) -> dict[str, str]:
    """The sec.gov URL of each filing's primary document (its index page without one)."""
    if not accession_numbers:
        return {}
    found = await session.execute(
        select(Filing.accession_no, Filing.primary_document).where(
            Filing.accession_no.in_(set(accession_numbers))
        )
    )
    urls = {}
    for accession_no, primary in found:
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no.replace('-', '')}"
        urls[accession_no] = (
            f"{folder}/{primary}" if primary else f"{folder}/{accession_no}-index.htm"
        )
    return urls


def _target_label(row: "Guidance") -> str:
    if row.target_fiscal_year is None:
        return "-"
    if row.target_fiscal_quarter is None:
        return f"FY{row.target_fiscal_year}"
    return f"FY{row.target_fiscal_year}Q{row.target_fiscal_quarter}"


def _review_status(row: "Guidance") -> str | None:
    if row.review == "wrong":
        return "reviewed as wrong: not valid guidance, left out of verdicts"
    if needs_review(row):
        return f"flagged for review ({row.plausibility_note}); verdict not counted until reviewed"
    if row.plausibility == "flagged" and row.review == "ok":
        return f"flagged by the plausibility check ({row.plausibility_note}); reviewed as ok"
    return None


def _guidance_data(
    ticker: str, row: "Guidance", sources: Sequence["GuidanceSource"]
) -> dict[str, Any]:
    unit = {"usd": "USD", "usd_per_share": "USD/shares", "pct": "percent"}.get(row.unit, row.unit)
    figures = {
        name: float(value)
        for name, value in (("low", row.low), ("high", row.high), ("point", row.point))
        if value is not None
    }
    data: dict[str, Any] = {
        "ticker": ticker,
        "metric": row.metric,
        "basis": row.basis,
        "measure": row.measure,
        "issued": f"FY{row.fiscal_year}Q{row.fiscal_quarter}",
        "issued_on": row.issued_at.isoformat(),
        "target": _target_label(row),
        **figures,
        "unit": unit,
        "as_written": row.value_text or row.qualitative,
        "verdict": row.verdict if row.review != "wrong" else None,
        "counts_in_verdicts": counts_in_verdicts(row),
    }
    if row.segment:
        data["segment"] = row.segment
    if row.constant_currency:
        data["constant_currency"] = True
    if row.verdict_reason:
        data["verdict_reason"] = row.verdict_reason
    if row.basis_assumed:
        data["basis_note"] = "basis not stated; compared with the GAAP actual"
    if row.actual is not None:
        data["actual_first_reported"] = float(row.actual)
    if row.difference is not None:
        data["difference"] = float(row.difference)
    if row.percent_difference is not None:
        data["percent_difference"] = float(row.percent_difference)
    if row.conditions:
        data["conditions"] = _cut(row.conditions, 200)
    review = _review_status(row)
    if review:
        data["review"] = review
    if sources:
        data["quote"] = _cut(sources[0].quote, QUOTE_CHARS)
    return data


def _statement_data(found: "StatementRow") -> dict[str, Any]:
    from talk_is_cheap.statements.commands import short_quote
    from talk_is_cheap.statements.queries import target_label

    row, outcome = found.statement, found.outcome
    unit = {"usd": "USD", "usd_per_share": "USD/shares", "pct": "percent"}.get(row.unit, row.unit)
    data: dict[str, Any] = {
        "ticker": row.ticker,
        "date": row.statement_date.isoformat(),
        "speaker": row.executive_name or row.speaker_name,
        "role": row.executive_role or row.speaker_role,
        "attribution": row.attribution,
        "management": row.management,
        "venue": row.venue,
        "publisher": found.publisher,
        "published": found.published_at.date().isoformat(),
        "claim": row.claim,
        "claim_type": row.claim_type,
        "quote": short_quote(row.quote),
        "provenance_note": f"reported by {found.publisher or 'a news outlet'}",
    }
    if row.date_from_article:
        data["date_note"] = "the article's date; the article does not date the statement"
    if row.attribution == "unverified_executive":
        data["attribution_note"] = "named as an executive, but not on the roster on that date"
    for name, value in (
        ("metric", row.metric),
        ("segment", row.segment),
        ("target", target_label(row)),
        ("as_written", row.value_text),
        ("direction", row.direction),
    ):
        if value:
            data[name] = value
    if row.basis != "unspecified":
        data["basis"] = row.basis
    for figure, amount in (("low", row.low), ("high", row.high), ("point", row.point)):
        if amount is not None:
            data[figure] = float(amount)
    if any(v is not None for v in (row.low, row.high, row.point)):
        data["unit"] = unit
    if outcome is None:
        if not row.management:
            data["outcome"] = "not scored: not a management claim"
        return data
    data["verdict"] = outcome.verdict if outcome.review != "wrong" else None
    if outcome.verdict_reason:
        data["verdict_reason"] = outcome.verdict_reason
    if outcome.actual is not None:
        data["actual_first_reported"] = float(outcome.actual)
    if outcome.prior_actual is not None:
        data["prior_period_actual"] = float(outcome.prior_actual)
    if outcome.difference is not None and row.claim_type == "numeric_forecast":
        data["difference"] = float(outcome.difference)
    if outcome.plausibility == "flagged":
        state = {None: "needs review", "ok": "reviewed as ok", "wrong": "reviewed as wrong"}
        data["review"] = f"flagged ({outcome.plausibility_note}); {state[outcome.review]}"
    elif outcome.review == "wrong":
        data["review"] = "reviewed as wrong: left out of outcome counts"
    if outcome.review == "wrong":
        return data  # an extraction error: neither a verdict nor a comparison
    data["guidance_comparison"] = outcome.guidance_comparison
    if outcome.guidance_note:
        data["guidance_note"] = outcome.guidance_note
    guidance = found.guidance
    if guidance is not None:
        data["formal_guidance"] = {
            "issued_on": guidance.issued_at.isoformat(),
            "basis": guidance.basis,
            **{
                k: float(v)
                for k, v in (
                    ("low", guidance.low),
                    ("high", guidance.high),
                    ("point", guidance.point),
                )
                if v is not None
            },
        }
    if outcome.context_url:
        data["filing_context"] = {
            "label": outcome.context_label,
            "url": outcome.context_url,
            "text": _cut(outcome.context_text or "", 300),
        }
    return data


async def _no_guidance(
    ctx: ToolContext, session: AsyncSession, company: Company, metric: str | None
) -> tuple[dict[str, Any], list[Evidence]]:
    """Evidence that a company gives no guidance: its latest earnings release, which has
    no outlook with guidance."""
    release = (
        await session.execute(
            select(Document.url, Document.fiscal_year, Document.fiscal_quarter)
            .where(Document.cik == company.cik, Document.doc_type == "press_release")
            .order_by(Document.filed_at.desc())
            .limit(1)
        )
    ).first()
    releases = await session.scalar(
        select(func.count()).where(
            Document.cik == company.cik, Document.doc_type == "press_release"
        )
    )
    what = f"{metric} guidance" if metric else "guidance"
    data = {
        "ticker": company.ticker,
        "guidance_rows": 0,
        "earnings_releases_checked": releases or 0,
        "finding": f"no {what} in any stored earnings release of {company.ticker}",
    }
    if release is None:
        return {"evidence": [], "finding": data["finding"]}, []
    data["latest_release"] = f"FY{release.fiscal_year}Q{release.fiscal_quarter}"
    item = ctx.evidence.add("company_press_release", release.url, "get_guidance", data)
    return {"evidence": [item]}, [item]


def _operand(log: EvidenceLog, ref: str) -> tuple[Decimal, str, str]:
    item_id, _, field_name = ref.strip().partition(".")
    item = log.get(item_id)
    if item is None:
        raise ToolFailed(f"{item_id} is not an evidence id")
    data = item["data"]
    names = [field_name] if field_name else ["value", "result", "point"]
    for name in names:
        value = data.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return Decimal(str(value)), str(data.get("unit", "")), item_id
    numeric = [k for k, v in data.items() if isinstance(v, int | float) and not isinstance(v, bool)]
    raise ToolFailed(
        f"{ref}: no numeric field {field_name or 'value'}; fields: {', '.join(numeric) or 'none'}"
    )


async def _search(ctx: ToolContext, query: str, **filters: Any) -> list["SearchResult"]:
    from talk_is_cheap.retrieval.search import Searcher

    async with ctx.sessions() as session:
        searcher = Searcher(session, ctx.loaded_embedder, ctx.loaded_reranker)
        if ctx.rerank_depth is not None:
            filters["rerank_depth"] = ctx.rerank_depth
        try:
            return await searcher.search(query, **filters)
        except ValueError as exc:  # a malformed period
            raise ToolFailed(str(exc)) from exc
