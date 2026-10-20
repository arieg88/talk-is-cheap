"""Extract forward-looking claims from candidate news stories and store them.

Per candidate story and company (`candidates.py`): one model call over the story's text,
then deterministic post-processing (`items.py`): grounding, figures, periods and speaker
attribution. The claims of each story replace its stored ones, and the company's outcomes
are rebuilt (`outcomes.py`).

Runs are incremental, like guidance extraction: a story already extracted with the same
model, prompt version and prompt text is skipped; responses are cached; a new
post-processing version re-processes stored extractions from the cache without a call.
"""

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Company, Statement
from talk_is_cheap.db.models import StatementExtraction as ExtractionRow
from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.guidance.llm import (
    Budget,
    CacheOnlyModel,
    CostCapReached,
    ExtractionModel,
    LlmError,
    Pricing,
    Response,
    ResponseCache,
    Usage,
    bounded_usage,
    complete,
    load_prompt_file,
)
from talk_is_cheap.statements.candidates import Candidate, candidates
from talk_is_cheap.statements.items import PROCESSING_VERSION, Claim, Dropped, SourceText, process
from talk_is_cheap.statements.outcomes import FindContext, rebuild_outcomes
from talk_is_cheap.statements.schema import StatementExtraction
from talk_is_cheap.universe import UniverseCompany

PROMPT_NAME = "statement_extraction"
PROMPT_VERSION = "v1"
CONCURRENCY = 4
# Output limit of one call, in tokens: also what a call reserves against the budget.
MAX_OUTPUT_TOKENS = 6_000
# Output tokens of a typical call, for the estimate a run prints: measured on the first
# extraction runs (docs/statements.md).
TYPICAL_OUTPUT_TOKENS = 1_350
# Tokens of instructions and the output schema beyond the prompt text, rounded up.
INPUT_OVERHEAD_TOKENS = 1_200
# Article text sent to the model, cut at a paragraph boundary: long pages are mostly
# related-story lists and boilerplate after this.
MAX_TEXT_CHARS = 12_000


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return load_prompt_file(PROMPT_NAME, version)


def prompt_text(text: str) -> str:
    """The text sent to the model: all of it, or its first MAX_TEXT_CHARS cut at a
    paragraph boundary. Quotes are grounded in the full text either way."""
    if len(text) <= MAX_TEXT_CHARS:
        return text
    cut = text.rfind("\n", 0, MAX_TEXT_CHARS)
    return text[: cut if cut > MAX_TEXT_CHARS // 2 else MAX_TEXT_CHARS]


def render_prompt(template: str, candidate: Candidate, company: UniverseCompany) -> str:
    story = candidate.story
    values = {
        "company": company.name or company.ticker,
        "ticker": company.ticker,
        "title": story.title,
        "publisher": story.publisher or "unknown",
        "published": story.published_at.date().isoformat(),
        "text": prompt_text(story.text),
    }
    prompt = template
    for name, value in values.items():
        prompt = prompt.replace("{" + name + "}", value)
    return prompt


def estimate_usage(prompt: str) -> Usage:
    """The largest usage a call can have: its input, estimated generously, and the
    output limit."""
    return bounded_usage(prompt, INPUT_OVERHEAD_TOKENS, MAX_OUTPUT_TOKENS)


def input_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PlannedStory:
    candidate: Candidate
    prompt: str
    input_hash: str
    # "up_to_date", "reprocess" (older post-processing; the cached response is processed
    # again), "cached" or "call".
    status: str
    model: str


@dataclass(slots=True)
class CompanyPlan:
    ticker: str
    stories: int  # linked stories
    planned: list[PlannedStory]
    stale: list[int]  # stored extractions (article ids) no longer among the candidates

    def count(self, status: str) -> int:
        return sum(p.status == status for p in self.planned)

    @property
    def estimated(self) -> Usage:
        total = Usage(0, 0)
        for p in self.planned:
            if p.status == "call":
                total += estimate_usage(p.prompt)
        return total


async def plan_companies(
    session: AsyncSession,
    companies: Sequence[UniverseCompany],
    model_name: str,
    cache: ResponseCache,
    *,
    template: str,
    prompt_version: str = PROMPT_VERSION,
    missing_only: bool = False,
    limit: int | None = None,
) -> list[CompanyPlan]:
    """What a run would do with each candidate story. `limit` keeps, per company, the
    first `limit` stories that need work (oldest first); up-to-date stories are listed
    as well, and nothing is removed as stale."""
    found, totals = await candidates(session, companies)
    plans = []
    for company in companies:
        stored = {
            e.article_id: e
            for e in (
                await session.scalars(
                    select(ExtractionRow).where(ExtractionRow.ticker == company.ticker)
                )
            ).all()
        }
        planned: list[PlannedStory] = []
        to_do = 0
        for candidate in found[company.ticker]:
            prompt = render_prompt(template, candidate, company)
            digest = input_hash(prompt)
            existing = stored.get(candidate.story.article_id)
            model = model_name
            if (
                existing is not None
                and (existing.model == model_name or missing_only)
                and existing.prompt_version == prompt_version
                and existing.input_hash == digest
            ):
                model = existing.model
                processed = existing.processing_version == PROCESSING_VERSION
                status = "up_to_date" if processed else "reprocess"
            elif cache.get(model_name, prompt_version, prompt, StatementExtraction) is not None:
                status = "cached"
            else:
                status = "call"
            if status != "up_to_date":
                if limit is not None and to_do >= limit:
                    continue
                to_do += 1
            planned.append(PlannedStory(candidate, prompt, digest, status, model))
        current = {c.story.article_id for c in found[company.ticker]}
        stale = [] if limit is not None else sorted(set(stored) - current)
        plans.append(CompanyPlan(company.ticker, totals[company.ticker], planned, stale))
    return plans


@dataclass(slots=True)
class ExtractResult:
    ticker: str
    stories: int = 0
    candidates: int = 0
    up_to_date: int = 0
    reprocessed: int = 0
    cached: int = 0
    called: int = 0
    claims: int = 0
    management: int = 0
    dropped: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    cost: Decimal = Decimal(0)
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    dropped_quotes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def extract_statements(
    sessions: async_sessionmaker[AsyncSession],
    model: ExtractionModel[StatementExtraction],
    cache: ResponseCache,
    budget: Budget,
    *,
    companies: Sequence[UniverseCompany],
    calendars: Mapping[str, FiscalCalendar],
    prompt_version: str = PROMPT_VERSION,
    missing_only: bool = False,
    limit: int | None = None,
    find_context: FindContext | None = None,
    on_result: Callable[[ExtractResult], None] | None = None,
) -> list[ExtractResult]:
    """Extract every company's new or changed candidate stories, store the claims, and
    rebuild the company's outcomes. Stops at the cost cap, keeping what was extracted
    before it."""
    template = load_prompt(prompt_version)
    async with sessions() as session:
        plans = await plan_companies(
            session,
            companies,
            model.name,
            cache,
            template=template,
            prompt_version=prompt_version,
            missing_only=missing_only,
            limit=limit,
        )
    by_ticker = {c.ticker: c for c in companies}
    results = []
    stop = False
    for plan in plans:
        started = time.monotonic()
        company = by_ticker[plan.ticker]
        result = ExtractResult(
            plan.ticker,
            stories=plan.stories,
            candidates=len(plan.planned),
            up_to_date=plan.count("up_to_date"),
        )
        to_run = [] if stop else [p for p in plan.planned if p.status != "up_to_date"]
        if stop:
            result.errors.append("not run: the cost cap or budget was reached")
        outcomes = await _run(
            to_run, model, cache, budget, prompt_version, company, calendars.get(plan.ticker)
        )
        async with sessions() as session, session.begin():
            for planned, outcome in zip(to_run, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    stop = stop or isinstance(outcome, CostCapReached)
                    result.errors.append(f"article {planned.candidate.story.article_id}: {outcome}")
                    continue
                response, claims, dropped = outcome
                if planned.status == "reprocess":
                    result.reprocessed += 1
                elif response.cached:
                    result.cached += 1
                else:
                    result.called += 1
                    result.cost += response.cost
                    result.usage += response.usage
                result.dropped += len(dropped)
                result.dropped_quotes += [str(d.item.get("quote", ""))[:100] for d in dropped]
                await store_extraction(session, planned, prompt_version, response, claims, dropped)
            for article_id in plan.stale:
                await session.execute(
                    delete(ExtractionRow).where(
                        ExtractionRow.article_id == article_id,
                        ExtractionRow.ticker == plan.ticker,
                    )
                )
            await session.flush()
            rows = (
                await session.scalars(select(Statement).where(Statement.ticker == plan.ticker))
            ).all()
            result.claims = len(rows)
            result.management = sum(r.management for r in rows)
            types: dict[str, int] = {}
            for r in rows:
                if r.management:
                    types[r.claim_type] = types.get(r.claim_type, 0) + 1
            result.by_type = types
            result.outcomes = await rebuild_outcomes(session, plan.ticker, find_context)
        result.seconds = time.monotonic() - started
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


_Outcome = tuple[Response[StatementExtraction], list[Claim], list[Dropped]] | BaseException


async def _run(
    planned: Sequence[PlannedStory],
    model: ExtractionModel[StatementExtraction],
    cache: ResponseCache,
    budget: Budget,
    prompt_version: str,
    company: UniverseCompany,
    calendar: FiscalCalendar | None,
) -> list[_Outcome]:
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(item: PlannedStory) -> _Outcome:
        answering: ExtractionModel[StatementExtraction] = (
            CacheOnlyModel(item.model, StatementExtraction) if item.status == "reprocess" else model
        )
        async with limit:
            try:
                response = await complete(
                    answering,
                    item.prompt,
                    prompt_version,
                    cache,
                    budget,
                    estimate_usage(item.prompt),
                )
            except (LlmError, CostCapReached) as exc:
                return exc
            except Exception as exc:  # a provider error must not lose the other stories
                return LlmError(f"{type(exc).__name__}: {exc}")
        story = item.candidate.story
        source = SourceText(story.article_id, story.text, story.published_at.date())
        claims, dropped = process(source, response.extraction, company, calendar)
        return response, claims, dropped

    return list(await asyncio.gather(*(one(p) for p in planned)))


async def store_extraction(
    session: AsyncSession,
    planned: PlannedStory,
    prompt_version: str,
    response: Response[StatementExtraction],
    claims: Sequence[Claim],
    dropped: Sequence[Dropped],
) -> None:
    """Replace the story's stored extraction and statements."""
    story = planned.candidate.story
    ticker = planned.candidate.ticker
    await session.execute(
        delete(ExtractionRow).where(
            ExtractionRow.article_id == story.article_id, ExtractionRow.ticker == ticker
        )
    )
    row = ExtractionRow(
        article_id=story.article_id,
        cluster_id=story.cluster_id,
        ticker=ticker,
        model=planned.model,
        prompt_version=prompt_version,
        input_hash=planned.input_hash,
        processing_version=PROCESSING_VERSION,
        claims=[c.to_json() for c in claims],
        dropped=[{"reason": d.reason, "item": d.item} for d in dropped],
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost=response.cost,
    )
    session.add(row)
    await session.flush()
    for claim in claims:
        session.add(statement_row(claim, row, planned.model, prompt_version))
    await session.flush()


def statement_row(
    claim: Claim, extraction: ExtractionRow, model: str, prompt_version: str
) -> Statement:
    return Statement(
        extraction_id=extraction.id,
        article_id=extraction.article_id,
        cluster_id=extraction.cluster_id,
        ticker=extraction.ticker,
        quote=claim.quote,
        char_start=claim.char_start,
        char_end=claim.char_end,
        speaker_name=claim.speaker_name,
        speaker_role=claim.speaker_role,
        attribution=claim.attribution,
        executive_name=claim.executive_name,
        executive_role=claim.executive_role,
        management=claim.management,
        is_direct_quote=claim.is_direct_quote,
        venue=claim.venue,
        statement_date=claim.statement_date,
        date_from_article=claim.date_from_article,
        claim_type=claim.claim_type,
        claim=claim.claim,
        metric=claim.metric,
        metric_raw=claim.metric_raw,
        segment=claim.segment,
        basis=claim.basis,
        measure=claim.measure,
        constant_currency=False,
        period_text=claim.period_text,
        target_fiscal_year=claim.target.fiscal_year if claim.target else None,
        target_fiscal_quarter=claim.target.fiscal_quarter if claim.target else None,
        low=claim.low,
        high=claim.high,
        point=claim.point,
        unit=claim.unit,
        value_text=claim.value_text,
        direction=claim.direction,
        item_key=claim.key(),
        model=model,
        prompt_version=prompt_version,
    )


async def calendars(session: AsyncSession) -> dict[str, FiscalCalendar]:
    """Each loaded company's fiscal calendar, by ticker."""
    rows = await session.execute(select(Company.ticker, Company.fiscal_year_end))
    return {t: FiscalCalendar.parse(end) for t, end in rows if end}


@dataclass(frozen=True, slots=True)
class DryRun:
    plans: list[CompanyPlan]
    pricing: Pricing | None

    @property
    def calls(self) -> int:
        return sum(p.count("call") for p in self.plans)

    @property
    def estimated(self) -> Usage:
        total = Usage(0, 0)
        for plan in self.plans:
            total += plan.estimated
        return total

    @property
    def estimated_cost(self) -> Decimal | None:
        return None if self.pricing is None else self.pricing.cost(self.estimated)
