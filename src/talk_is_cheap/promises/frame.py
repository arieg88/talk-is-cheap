"""Frame management claims: one model call per claim, then `framing.py` in code.

The claims framed are every management claim in `statements` that is not already a
numeric forecast with a verdict (beat, met or missed against the reported result), and
not reviewed as an extraction error. The model reads only the quote, its paragraph, the
article and statement dates, the speaker and the company's fiscal calendar; articles
are not extracted again.

Runs are incremental: a claim that already has a promise of the current version (prompt
and post-processing) is up to date; responses are cached, so a new post-processing
version frames every claim again from the cache, as a new version, at no cost. Rows of
older versions are kept, no longer current, with their verdicts.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import (
    Article,
    ArticleText,
    Company,
    Document,
    Guidance,
    Promise,
    Statement,
    StatementOutcome,
)
from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
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
from talk_is_cheap.guidance.matching import Actuals, load_actuals
from talk_is_cheap.news.queries import ARTICLE_PUBLISHER
from talk_is_cheap.promises.framing import (
    PROCESSING_VERSION,
    ClaimInput,
    Framed,
    GuidedFigure,
    claim_key,
    frame,
)
from talk_is_cheap.promises.schema import FramingAnswer
from talk_is_cheap.universe import UniverseCompany

PROMPT_NAME = "promise_framing"
PROMPT_VERSION = "v3"
CONCURRENCY = 4
# Output limit of one call, in tokens: also what a call reserves against the budget.
MAX_OUTPUT_TOKENS = 1_600
# Output tokens of a typical call, for the estimate a run prints (docs/promises.md).
TYPICAL_OUTPUT_TOKENS = 320
# Tokens of instructions and the output schema beyond the prompt text, rounded up.
INPUT_OVERHEAD_TOKENS = 1_600
# The paragraph around a quote: the quote's own paragraph, and the one before it while
# the text is shorter than this.
PARAGRAPH_CHARS = 700
# Outcomes that make a numeric forecast already scored.
SCORED = ("beat", "met", "missed")


def version_label(prompt_version: str = PROMPT_VERSION) -> str:
    """The version of a promise row: its prompt and post-processing versions."""
    return f"{prompt_version}+{PROCESSING_VERSION}"


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return load_prompt_file(PROMPT_NAME, version)


def paragraph_of(text: str, start: int, end: int) -> str:
    """The paragraph holding text[start:end], with the paragraph before it when the
    quote's own is short."""
    first = text.rfind("\n", 0, start) + 1
    last = text.find("\n", end)
    last = len(text) if last < 0 else last
    if last - first < PARAGRAPH_CHARS // 2 and first > 0:
        first = text.rfind("\n", 0, first - 1) + 1
    piece = text[first:last].strip()
    if len(piece) <= PARAGRAPH_CHARS:
        return piece
    middle = (start + end) // 2 - first
    left = max(0, middle - PARAGRAPH_CHARS // 2)
    return piece[left : left + PARAGRAPH_CHARS].strip()


def speaker_of(row: Statement) -> str:
    if row.executive_name:
        return row.executive_name
    return row.attribution.replace("_", " ")


def fiscal_note(fiscal: FiscalCalendar | None, said: date) -> str:
    if fiscal is None:
        return "unknown"
    under_way = fiscal.quarter_ended_before(said)
    quarter = (
        (under_way.fiscal_year + 1, 1)
        if under_way.fiscal_quarter == 4
        else (under_way.fiscal_year, under_way.fiscal_quarter + 1)
    )
    end = fiscal.year_end_in(fiscal.fiscal_year(said))
    return (
        f"fiscal years end on {end.strftime('%B')} {end.day} (fiscal {end.year} ends "
        f"{end.isoformat()}); on the statement date fiscal Q{quarter[1]} {quarter[0]} is "
        "under way"
    )


def render_prompt(
    template: str, claim: ClaimInput, role: str | None, fiscal: FiscalCalendar | None
) -> str:
    values = {
        "company": claim.company,
        "ticker": claim.ticker,
        "speaker": claim.speaker,
        "role": role or "unknown",
        "statement_date": claim.statement_date.isoformat(),
        "article_date": claim.article_date.isoformat(),
        "fiscal": fiscal_note(fiscal, claim.statement_date),
        "quote": claim.quote,
        "paragraph": claim.paragraph,
    }
    prompt = template
    for name, value in values.items():
        prompt = prompt.replace("{" + name + "}", value)
    if claim.focus:
        # Several claims share this quote; each gets its own prompt (and cache entry).
        prompt += f"\nThe quote holds several claims. Frame only this one: {claim.focus}\n"
    return prompt


def estimate_usage(prompt: str) -> Usage:
    return bounded_usage(prompt, INPUT_OVERHEAD_TOKENS, MAX_OUTPUT_TOKENS)


@dataclass(frozen=True, slots=True)
class PlannedClaim:
    claim: ClaimInput
    prompt: str
    # "up_to_date", "cached" or "call".
    status: str
    prompt_version: str = PROMPT_VERSION


@dataclass(slots=True)
class FramePlan:
    ticker: str
    claims: int  # management claims to frame
    planned: list[PlannedClaim]
    stale: list[str]  # claim keys with a current promise and no current claim

    def count(self, status: str) -> int:
        return sum(p.status == status for p in self.planned)

    @property
    def estimated(self) -> Usage:
        total = Usage(0, 0)
        for p in self.planned:
            if p.status == "call":
                total += estimate_usage(p.prompt)
        return total


async def claims_to_frame(
    session: AsyncSession, company: UniverseCompany
) -> list[tuple[ClaimInput, str | None]]:
    """The company's management claims to frame, oldest first, with each speaker's
    role."""
    rows = (
        await session.execute(
            select(Statement, StatementOutcome, Article.published_at, ARTICLE_PUBLISHER)
            .join(Article, Article.id == Statement.article_id)
            .outerjoin(StatementOutcome, StatementOutcome.statement_id == Statement.id)
            .where(Statement.ticker == company.ticker, Statement.management)
            .order_by(Statement.statement_date, Statement.id)
        )
    ).all()
    texts = {
        t.article_id: t.text
        for t in (
            await session.scalars(
                select(ArticleText).where(
                    ArticleText.article_id.in_({r.article_id for r, *_ in rows})
                )
            )
        ).all()
        if t.text
    }
    filed = await reported_through(session, company.ticker)
    shared: dict[tuple[int, str], int] = {}
    for row, *_ in rows:
        shared[(row.article_id, row.quote)] = shared.get((row.article_id, row.quote), 0) + 1
    found = []
    for row, outcome, published, publisher in rows:
        if outcome is not None and (outcome.review == "wrong" or outcome.verdict in SCORED):
            continue
        text = texts.get(row.article_id, row.quote)
        start, end = (row.char_start, row.char_end) if row.article_id in texts else (0, len(text))
        found.append(
            (
                ClaimInput(
                    claim_key=claim_key(row.article_id, row.ticker, row.item_key),
                    statement_id=row.id,
                    article_id=row.article_id,
                    ticker=row.ticker,
                    company=company.name or company.ticker,
                    speaker=speaker_of(row),
                    statement_date=row.statement_date,
                    article_date=published.date(),
                    quote=row.quote,
                    paragraph=paragraph_of(text, start, end),
                    publisher=publisher,
                    focus=row.claim if shared[(row.article_id, row.quote)] > 1 else None,
                    reported_through=max(
                        (q for day, q in filed if day < row.statement_date), default=None
                    ),
                ),
                row.executive_role or row.speaker_role,
            )
        )
    return found


async def plan_framing(
    session: AsyncSession,
    companies: Sequence[UniverseCompany],
    model_name: str,
    cache: ResponseCache,
    calendars: Mapping[str, FiscalCalendar],
    *,
    template: str,
    prompt_version: str = PROMPT_VERSION,
    missing_only: bool = False,
    limit: int | None = None,
    only: frozenset[str] | None = None,
    upgrade: bool = False,
) -> list[FramePlan]:
    """What a run would do with each claim. `limit` keeps, per company, the first
    `limit` claims that need work; `missing_only` frames only claims with no promise of
    any version; `only` frames only the claims with these claim keys, with the current
    prompt (the others keep the promise they have); `upgrade` frames every claim with the
    current prompt. Otherwise a claim keeps the prompt version it was framed with, and a
    new post-processing version re-processes its cached answer at no cost; new claims
    get the current prompt."""
    templates = {prompt_version: template}
    plans = []
    for company in companies:
        stored = (
            await session.execute(
                select(Promise.claim_key, Promise.version, Promise.current).where(
                    Promise.ticker == company.ticker
                )
            )
        ).all()
        framed = {(k, v) for k, v, _ in stored}
        any_version = {k for k, _, _ in stored}
        # The prompt version each claim's current promise was framed with.
        prompted = {k: v.split("+", 1)[0] for k, v, is_current in stored if is_current}
        claims = await claims_to_frame(session, company)
        planned: list[PlannedClaim] = []
        to_do = 0
        for claim, role in claims:
            if only is not None and claim.claim_key not in only:
                continue
            # A claim keeps the prompt it was framed with (a new post-processing version
            # re-processes its cached answer), unless it is picked or upgraded.
            keep = claim.claim_key in prompted and only is None and not upgrade
            version = prompted[claim.claim_key] if keep else prompt_version
            if version not in templates:
                templates[version] = load_prompt(version)
            prompt = render_prompt(templates[version], claim, role, calendars.get(company.ticker))
            if (claim.claim_key, version_label(version)) in framed or (
                missing_only and claim.claim_key in any_version
            ):
                status = "up_to_date"
            elif cache.get(model_name, version, prompt, FramingAnswer) is not None:
                status = "cached"
            else:
                status = "call"
            if status != "up_to_date":
                if limit is not None and to_do >= limit:
                    continue
                to_do += 1
            planned.append(PlannedClaim(claim, prompt, status, version))
        current_keys = {c.claim_key for c, _ in claims}
        stale = (
            []
            if limit is not None or only is not None
            else sorted({k for k, _, is_current in stored if is_current} - current_keys)
        )
        plans.append(FramePlan(company.ticker, len(claims), planned, stale))
    return plans


@dataclass(slots=True)
class FrameResult:
    ticker: str
    claims: int = 0
    up_to_date: int = 0
    cached: int = 0
    called: int = 0
    by_specificity: dict[str, int] = field(default_factory=dict)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    cost: Decimal = Decimal(0)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def frame_claims(
    sessions: async_sessionmaker[AsyncSession],
    model: ExtractionModel[FramingAnswer],
    cache: ResponseCache,
    budget: Budget,
    *,
    companies: Sequence[UniverseCompany],
    calendars: Mapping[str, FiscalCalendar],
    prompt_version: str = PROMPT_VERSION,
    missing_only: bool = False,
    limit: int | None = None,
    only: frozenset[str] | None = None,
    upgrade: bool = False,
    on_result: Callable[[FrameResult], None] | None = None,
) -> list[FrameResult]:
    """Frame every company's claims that need it and store the promises. Stops at the
    cost cap, keeping what was framed before it."""
    template = load_prompt(prompt_version)
    async with sessions() as session:
        plans = await plan_framing(
            session,
            companies,
            model.name,
            cache,
            calendars,
            template=template,
            prompt_version=prompt_version,
            missing_only=missing_only,
            limit=limit,
            only=only,
            upgrade=upgrade,
        )
    results = []
    stop = False
    for plan in plans:
        result = FrameResult(plan.ticker, claims=plan.claims, up_to_date=plan.count("up_to_date"))
        to_run = [] if stop else [p for p in plan.planned if p.status != "up_to_date"]
        if stop:
            result.errors.append("not run: the cost cap or budget was reached")
        async with sessions() as session:
            actuals = await _actuals(session, plan.ticker)
            guidance = await guided_figures(session, plan.ticker)
        outcomes = await _run(
            to_run,
            model,
            cache,
            budget,
            prompt_version,
            calendars.get(plan.ticker),
            actuals,
            guidance,
        )
        async with sessions() as session, session.begin():
            for planned, outcome in zip(to_run, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    stop = stop or isinstance(outcome, CostCapReached)
                    result.errors.append(f"statement {planned.claim.statement_id}: {outcome}")
                    continue
                response, framed = outcome
                if response.cached:
                    result.cached += 1
                else:
                    result.called += 1
                    result.cost += response.cost
                    result.usage += response.usage
                await store_promise(
                    session,
                    planned.claim,
                    framed,
                    response,
                    model.name,
                    version_label(planned.prompt_version),
                )
            await _relink(session, [p.claim for p in plan.planned])
            if plan.stale:
                await session.execute(
                    update(Promise)
                    .where(Promise.claim_key.in_(plan.stale))
                    .values(current=False, statement_id=None)
                )
            counts = await session.execute(
                select(Promise.specificity, Promise.id).where(
                    Promise.ticker == plan.ticker, Promise.current
                )
            )
            for specificity, _ in counts:
                result.by_specificity[specificity] = result.by_specificity.get(specificity, 0) + 1
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


async def guided_figures(session: AsyncSession, ticker: str) -> list[GuidedFigure]:
    """The company's formal guidance figures that count in verdicts."""
    from talk_is_cheap.guidance.matching import counts_in_verdicts

    rows = (
        await session.scalars(
            select(Guidance)
            .join(Company, Company.cik == Guidance.cik)
            .where(Company.ticker == ticker)
        )
    ).all()
    return [
        GuidedFigure(
            g.id,
            g.metric,
            (g.target_fiscal_year, g.target_fiscal_quarter),
            g.issued_at,
            tuple(v for v in (g.low, g.high, g.point) if v is not None),
        )
        for g in rows
        if g.target_fiscal_year is not None and g.measure == "level" and counts_in_verdicts(g)
    ]


async def reported_through(session: AsyncSession, ticker: str) -> list[tuple[date, FiscalQuarter]]:
    """When each fiscal quarter's results were first filed (release, 10-Q or 10-K)."""
    rows = await session.execute(
        select(Document.filed_at, Document.fiscal_year, Document.fiscal_quarter)
        .join(Company, Company.cik == Document.cik)
        .where(Company.ticker == ticker, Document.doc_type.in_(("press_release", "10q", "10k")))
    )
    return sorted((filed, FiscalQuarter(y, q)) for filed, y, q in rows)


async def _actuals(session: AsyncSession, ticker: str) -> Actuals | None:
    cik = await session.scalar(select(Company.cik).where(Company.ticker == ticker))
    return None if cik is None else await load_actuals(session, cik)


_Outcome = tuple[Response[FramingAnswer], Framed] | BaseException


async def _run(
    planned: Sequence[PlannedClaim],
    model: ExtractionModel[FramingAnswer],
    cache: ResponseCache,
    budget: Budget,
    prompt_version: str,
    fiscal: FiscalCalendar | None,
    actuals: Actuals | None,
    guidance: Sequence[GuidedFigure] = (),
) -> list[_Outcome]:
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(item: PlannedClaim) -> _Outcome:
        async with limit:
            try:
                answering: ExtractionModel[FramingAnswer] = (
                    CacheOnlyModel(model.name, FramingAnswer) if item.status == "cached" else model
                )
                response = await complete(
                    answering,
                    item.prompt,
                    item.prompt_version,
                    cache,
                    budget,
                    estimate_usage(item.prompt),
                )
            except (LlmError, CostCapReached) as exc:
                return exc
            except Exception as exc:  # a provider error must not lose the other claims
                return LlmError(f"{type(exc).__name__}: {exc}")
        framed = frame(item.claim, response.extraction.framing(), fiscal, actuals, guidance)
        return response, framed

    return list(await asyncio.gather(*(one(p) for p in planned)))


async def store_promise(
    session: AsyncSession,
    claim: ClaimInput,
    framed: Framed,
    response: Response[FramingAnswer],
    model: str,
    version: str,
) -> Promise:
    """Add the promise of this version, and make it the claim's current one."""
    await session.execute(
        update(Promise).where(Promise.claim_key == claim.claim_key).values(current=False)
    )
    baseline = framed.baseline
    row = Promise(
        claim_key=claim.claim_key,
        version=version,
        current=True,
        statement_id=claim.statement_id,
        article_id=claim.article_id,
        ticker=claim.ticker,
        speaker=claim.speaker,
        statement_date=claim.statement_date,
        quote=claim.quote,
        subject=framed.subject,
        # Management claims are about their company (`outcomes.py`).
        subject_kind="company",
        subject_key=claim.ticker,
        metric=framed.metric,
        measure_period=framed.measure_period,
        target_kind=framed.target_kind,
        direction=framed.direction,
        target_value=framed.target_value,
        unit=framed.unit,
        target_text=framed.target_text,
        required_value=framed.required_value,
        event=framed.event,
        baseline_value=baseline.value if baseline else None,
        baseline_period=baseline.period if baseline else None,
        baseline_source=framed.baseline_source,
        baseline_accession_no=baseline.accession_no if baseline else None,
        due_date=framed.due_date,
        due_basis=framed.due_basis,
        deadline_text=framed.deadline_text,
        resolution_rule=framed.resolution_rule,
        specificity=framed.specificity,
        bold=framed.bold,
        bold_basis=framed.bold_basis,
        claim_type=framed.claim_type,
        guidance_id=framed.guidance_id,
        related_companies=framed.related_companies,
        comparison_basis=framed.comparison_basis,
        group_key=framed.group_key,
        framing=response.extraction.model_dump(mode="json"),
        notes=framed.notes,
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost=response.cost,
    )
    session.add(row)
    await session.flush()
    return row


async def _relink(session: AsyncSession, claims: Sequence[ClaimInput]) -> None:
    """Point each claim's promises at its current statement row (re-extraction gives
    statements new ids)."""
    for claim in claims:
        await session.execute(
            update(Promise)
            .where(Promise.claim_key == claim.claim_key)
            .values(statement_id=claim.statement_id)
        )


@dataclass(frozen=True, slots=True)
class DryRun:
    plans: list[FramePlan]
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

    @property
    def typical_cost(self) -> Decimal | None:
        if self.pricing is None:
            return None
        usage = Usage(self.estimated.input_tokens, TYPICAL_OUTPUT_TOKENS * self.calls)
        return self.pricing.cost(usage)
