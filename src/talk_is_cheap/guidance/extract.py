"""Extract guidance from the outlook sections of earnings releases and store it.

Per document (a press release, or an earnings exhibit such as Nvidia's CFO commentary)
with outlook sections that give guidance: one model call over all those sections, then
deterministic post-processing (`items.py`). Per filing, the items of its documents are
merged into `guidance` rows with their sources, and every row of the company gets a
verdict (`matching.py`).

Runs are incremental: a document already extracted with the same model, prompt version
and prompt text is skipped. Responses are cached, so extracting again after deleting rows
costs nothing, and a new post-processing version re-processes stored extractions from
their cached responses without calling a model.
"""

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from importlib.resources import files
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import (
    Company,
    Document,
    Guidance,
    GuidanceExtraction,
    GuidanceSource,
    Section,
)
from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.filings.sections import has_guidance
from talk_is_cheap.guidance.items import (
    PROCESSING_VERSION,
    Dropped,
    GuidanceItem,
    SourceDocument,
    SourceSection,
    merge,
    process,
)
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
    complete,
    estimate_usage,
)
from talk_is_cheap.guidance.matching import item_key, refresh_verdicts

PROMPT_NAME = "guidance_extraction"
PROMPT_VERSION = "v1"
# Documents whose outlook is extracted; the order is the order of sources in a row.
DOC_TYPES = ("press_release", "earnings_exhibit")
# Model calls in flight at once.
CONCURRENCY = 4


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (
        files("talk_is_cheap")
        .joinpath("prompts", PROMPT_NAME, f"{version}.md")
        .read_text(encoding="utf-8")
    )


@dataclass(frozen=True, slots=True)
class OutlookDocument:
    """A document with outlook sections that give guidance, and what the prompt needs."""

    cik: int
    ticker: str
    company: str
    calendar: FiscalCalendar
    period_end: date
    filed_at: date
    source: SourceDocument

    @property
    def key(self) -> tuple[str, str]:
        return self.source.accession_no, self.source.document_name


def render_prompt(template: str, document: OutlookDocument) -> str:
    source = document.source
    sections = "\n\n".join(
        f'<section title="{s.title}">\n{s.text.strip()}\n</section>' for s in source.sections
    )
    values = {
        "company": document.company,
        "ticker": document.ticker,
        "document": f"{source.doc_type.replace('_', ' ')} {source.document_name} "
        f"(filing {source.accession_no})",
        "issued_label": f"{source.issued.fiscal_year} Q{source.issued.fiscal_quarter}",
        "period_end": document.period_end.isoformat(),
        "filed": document.filed_at.isoformat(),
        "sections": sections,
    }
    prompt = template
    for name, value in values.items():
        prompt = prompt.replace("{" + name + "}", value)
    return prompt


def input_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


async def company_documents(
    session: AsyncSession, cik: int, names: Mapping[str, str] | None = None
) -> list[OutlookDocument]:
    """The company's releases and earnings exhibits with outlook sections that give
    guidance, oldest first. Outlooks that only point to the earnings call are left out."""
    company = await session.get(Company, cik)
    if company is None or company.fiscal_year_end is None:
        return []
    calendar = FiscalCalendar.parse(company.fiscal_year_end)
    statement = (
        select(Document, Section)
        .join(Section, Section.document_id == Document.id)
        .where(
            Document.cik == cik,
            Document.doc_type.in_(DOC_TYPES),
            Section.section_type == "outlook",
        )
        .order_by(
            Document.filed_at, Document.accession_no, Document.document_name, Section.position
        )
    )
    grouped: dict[int, tuple[Document, list[Section]]] = {}
    for document, section in (await session.execute(statement)).all():
        if has_guidance(section.text):
            grouped.setdefault(document.id, (document, []))[1].append(section)
    name = (names or {}).get(company.ticker, company.name)
    return [
        OutlookDocument(
            cik=cik,
            ticker=company.ticker,
            company=name,
            calendar=calendar,
            period_end=document.period_end,
            filed_at=document.filed_at,
            source=SourceDocument(
                accession_no=document.accession_no,
                document_name=document.document_name,
                doc_type=document.doc_type,
                url=document.url,
                issued=FiscalQuarter(document.fiscal_year, document.fiscal_quarter),
                sections=[SourceSection(s.title, s.text, s.char_start) for s in sections],
            ),
        )
        for document, sections in grouped.values()
    ]


async def release_count(session: AsyncSession, cik: int) -> int:
    rows = await session.scalars(
        select(Document.id).where(Document.cik == cik, Document.doc_type == "press_release")
    )
    return len(rows.all())


@dataclass(frozen=True, slots=True)
class PlannedDocument:
    document: OutlookDocument
    prompt: str
    input_hash: str
    # "up_to_date"; "reprocess" (stored with an older post-processing version, processed
    # again from its cached response); "cached"; or "call".
    status: str
    model: str  # whose response is used: the run's model, or the stored one to reprocess


@dataclass(slots=True)
class CompanyPlan:
    cik: int
    ticker: str
    releases: int
    documents: list[PlannedDocument]
    # Stored extractions of documents that no longer have guidance (re-parsed or gone).
    stale: list[tuple[str, str]]

    def count(self, status: str) -> int:
        return sum(p.status == status for p in self.documents)

    @property
    def estimated(self) -> Usage:
        total = Usage(0, 0)
        for planned in self.documents:
            if planned.status == "call":
                total += estimate_usage(planned.prompt)
        return total


async def plan_company(
    session: AsyncSession,
    cik: int,
    model_name: str,
    cache: ResponseCache,
    *,
    template: str,
    prompt_version: str = PROMPT_VERSION,
    names: Mapping[str, str] | None = None,
    missing_only: bool = False,
) -> CompanyPlan:
    """What a run would do with each document. A document is up to date when extracted
    with this model, prompt version and prompt text, or with any model when
    `missing_only`."""
    documents = await company_documents(session, cik, names)
    stored = {
        (e.accession_no, e.document_name): e
        for e in (
            await session.scalars(select(GuidanceExtraction).where(GuidanceExtraction.cik == cik))
        ).all()
    }
    planned = []
    for document in documents:
        prompt = render_prompt(template, document)
        digest = input_hash(prompt)
        existing = stored.get(document.key)
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
        elif cache.get(model_name, prompt_version, prompt) is not None:
            status = "cached"
        else:
            status = "call"
        planned.append(PlannedDocument(document, prompt, digest, status, model))
    current = {d.key for d in documents}
    ticker = await session.scalar(select(Company.ticker).where(Company.cik == cik))
    return CompanyPlan(
        cik=cik,
        ticker=ticker or str(cik),
        releases=await release_count(session, cik),
        documents=planned,
        stale=sorted(set(stored) - current),
    )


@dataclass(slots=True)
class ExtractResult:
    ticker: str
    releases: int = 0
    documents: int = 0
    up_to_date: int = 0
    reprocessed: int = 0
    cached: int = 0
    called: int = 0
    items: int = 0
    dropped: int = 0
    rows: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    cost: Decimal = Decimal(0)
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    dropped_quotes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def extract_guidance(
    sessions: async_sessionmaker[AsyncSession],
    model: ExtractionModel,
    cache: ResponseCache,
    budget: Budget,
    *,
    ciks: Sequence[int],
    names: Mapping[str, str] | None = None,
    prompt_version: str = PROMPT_VERSION,
    missing_only: bool = False,
    on_result: Callable[[ExtractResult], None] | None = None,
) -> list[ExtractResult]:
    """Extract every company's new or changed outlook documents, store the items, rebuild
    the guidance rows of the filings they belong to, and refresh the verdicts. Stops at
    the cost cap, keeping what was extracted before it."""
    template = load_prompt(prompt_version)
    results = []
    stop = False
    for cik in ciks:
        started = time.monotonic()
        async with sessions() as session:
            plan = await plan_company(
                session,
                cik,
                model.name,
                cache,
                template=template,
                prompt_version=prompt_version,
                names=names,
                missing_only=missing_only,
            )
        result = ExtractResult(
            plan.ticker,
            releases=plan.releases,
            documents=len(plan.documents),
            up_to_date=plan.count("up_to_date"),
        )
        to_run = [] if stop else [p for p in plan.documents if p.status != "up_to_date"]
        if stop:
            result.errors.append("not run: the cost cap or budget was reached")
        outcomes = await _run(to_run, model, cache, budget, prompt_version)

        async with sessions() as session, session.begin():
            touched: set[str] = set()
            for planned, outcome in zip(to_run, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    stop = stop or isinstance(outcome, CostCapReached)
                    result.errors.append(f"{planned.document.source.document_name}: {outcome}")
                    continue
                response, items, dropped = outcome
                if planned.status == "reprocess":
                    result.reprocessed += 1
                elif response.cached:
                    result.cached += 1
                else:
                    result.called += 1
                    result.cost += response.cost
                    result.usage += response.usage
                result.items += len(items)
                result.dropped += len(dropped)
                result.dropped_quotes += [str(d.item.get("quote", ""))[:100] for d in dropped]
                await _store_extraction(
                    session, planned, planned.model, prompt_version, response, items, dropped
                )
                touched.add(planned.document.source.accession_no)
            for accession_no, document_name in plan.stale:
                await session.execute(
                    delete(GuidanceExtraction).where(
                        GuidanceExtraction.accession_no == accession_no,
                        GuidanceExtraction.document_name == document_name,
                    )
                )
                touched.add(accession_no)
            for accession_no in sorted(touched):
                await rebuild_filing(session, cik, accession_no)
            result.verdicts = await refresh_verdicts(session, cik)
            result.rows = sum(result.verdicts.values())
        result.seconds = time.monotonic() - started
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


_Outcome = tuple[Response, list[GuidanceItem], list[Dropped]] | BaseException


async def _run(
    planned: Sequence[PlannedDocument],
    model: ExtractionModel,
    cache: ResponseCache,
    budget: Budget,
    prompt_version: str,
) -> list[_Outcome]:
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(item: PlannedDocument) -> _Outcome:
        # Reprocessing reads the stored model's cached response and never calls a model.
        answering = CacheOnlyModel(item.model) if item.status == "reprocess" else model
        async with limit:
            try:
                response = await complete(answering, item.prompt, prompt_version, cache, budget)
            except (LlmError, CostCapReached) as exc:
                return exc
            except Exception as exc:  # a provider error must not lose the other documents
                return LlmError(f"{type(exc).__name__}: {exc}")
        document = item.document
        items, dropped = process(document.source, response.extraction, document.calendar)
        return response, items, dropped

    return list(await asyncio.gather(*(one(p) for p in planned)))


async def _store_extraction(
    session: AsyncSession,
    planned: PlannedDocument,
    model_name: str,
    prompt_version: str,
    response: Response,
    items: Sequence[GuidanceItem],
    dropped: Sequence[Dropped],
) -> None:
    source = planned.document.source
    await session.execute(
        delete(GuidanceExtraction).where(
            GuidanceExtraction.accession_no == source.accession_no,
            GuidanceExtraction.document_name == source.document_name,
        )
    )
    session.add(
        GuidanceExtraction(
            cik=planned.document.cik,
            accession_no=source.accession_no,
            document_name=source.document_name,
            model=model_name,
            prompt_version=prompt_version,
            input_hash=planned.input_hash,
            processing_version=PROCESSING_VERSION,
            items=[item.to_json() for item in items],
            dropped=[{"reason": d.reason, "item": d.item} for d in dropped],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost=response.cost,
        )
    )
    await session.flush()


async def rebuild_filing(session: AsyncSession, cik: int, accession_no: str) -> int:
    """Replace the guidance rows of one filing with the merged items of its documents'
    stored extractions. Returns the number of rows."""
    await session.execute(delete(Guidance).where(Guidance.accession_no == accession_no))
    extractions = (
        await session.scalars(
            select(GuidanceExtraction).where(GuidanceExtraction.accession_no == accession_no)
        )
    ).all()
    issuing = (
        await session.execute(
            select(Document.fiscal_year, Document.fiscal_quarter, Document.filed_at)
            .where(Document.accession_no == accession_no)
            .limit(1)
        )
    ).first()
    if not extractions or issuing is None:
        return 0
    order = {doc_type: index for index, doc_type in enumerate(DOC_TYPES)}
    ordered = sorted(
        extractions,
        key=lambda e: (
            min((order.get(_doc_type(i), 9) for i in e.items), default=9),
            e.document_name,
        ),
    )
    items = merge(GuidanceItem.from_json(i) for e in ordered for i in e.items)
    model = ordered[0].model
    prompt_version = ordered[0].prompt_version
    for item in items:
        row = Guidance(
            cik=cik,
            accession_no=accession_no,
            fiscal_year=issuing.fiscal_year,
            fiscal_quarter=issuing.fiscal_quarter,
            issued_at=issuing.filed_at,
            metric=item.metric,
            metric_raw=item.metric_raw,
            segment=item.segment,
            basis=item.basis,
            measure=item.measure,
            constant_currency=item.constant_currency,
            period_text=item.period_text,
            target_fiscal_year=item.target.fiscal_year if item.target else None,
            target_fiscal_quarter=item.target.fiscal_quarter if item.target else None,
            low=item.low,
            high=item.high,
            point=item.point,
            plus_minus=item.plus_minus,
            plus_minus_kind=item.plus_minus_kind,
            unit=item.unit,
            value_text=item.value_text,
            qualitative=item.qualitative,
            conditions=item.conditions,
            model=model,
            prompt_version=prompt_version,
            verdict="pending",
            basis_assumed=False,
        )
        row.item_key = item_key(row)
        session.add(row)
        await session.flush()
        for position, source in enumerate(item.sources):
            session.add(
                GuidanceSource(
                    guidance_id=row.id,
                    position=position,
                    accession_no=source.accession_no,
                    document_name=source.document_name,
                    doc_type=source.doc_type,
                    url=source.url,
                    section_title=source.section_title,
                    char_start=source.char_start,
                    char_end=source.char_end,
                    quote=source.quote,
                )
            )
    await session.flush()
    return len(items)


def _doc_type(item: Mapping[str, Any]) -> str:
    return str(item["sources"][0]["doc_type"])


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


async def dry_run(
    sessions: async_sessionmaker[AsyncSession],
    model_name: str,
    cache: ResponseCache,
    pricing: Pricing | None,
    *,
    ciks: Sequence[int],
    names: Mapping[str, str] | None = None,
    prompt_version: str = PROMPT_VERSION,
    missing_only: bool = False,
) -> DryRun:
    template = load_prompt(prompt_version)
    plans = []
    async with sessions() as session:
        for cik in ciks:
            plans.append(
                await plan_company(
                    session,
                    cik,
                    model_name,
                    cache,
                    template=template,
                    prompt_version=prompt_version,
                    names=names,
                    missing_only=missing_only,
                )
            )
    return DryRun(plans, pricing)


async def company_ciks(session: AsyncSession, tickers: Sequence[str] | None) -> dict[str, int]:
    """Ticker to CIK of loaded companies, all or the given ones. Raises ValueError naming
    tickers that are not loaded."""
    rows = dict((await session.execute(select(Company.ticker, Company.cik))).all())
    if not tickers:
        return dict(sorted(rows.items()))
    wanted = [t.strip().upper() for t in tickers]
    missing = [t for t in wanted if t not in rows]
    if missing:
        raise ValueError(f"not loaded: {', '.join(missing)}")
    return {t: rows[t] for t in wanted}
