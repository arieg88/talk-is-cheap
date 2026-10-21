"""Health checks and the typed data endpoints: a thin layer over the project's query
functions (financials, guidance, promises, track records, coverage, the agent's
threads), with pagination and 404s.

Lists are read whole from their query function and cut into pages here: `cursor` is an
opaque offset into the list, so a page read while a data job writes may repeat or skip a
row at the edge.
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.agent.graph import history, render
from talk_is_cheap.agent.run import run_config
from talk_is_cheap.api.app import Services
from talk_is_cheap.api.schemas import (
    Amount,
    Baseline,
    Check,
    CompanyCoverageOut,
    CompanyPage,
    EvidenceLink,
    FeedbackIn,
    FeedbackOut,
    FinancialsPage,
    Finding,
    GuidanceMetricName,
    GuidanceOut,
    GuidancePage,
    GuidanceSourceOut,
    Health,
    MetricOut,
    Problem,
    PromiseDetail,
    PromiseOut,
    PromisePage,
    PromiseVerdictName,
    QuarterOut,
    Rates,
    Readiness,
    Resolution,
    Review,
    Source,
    ThreadId,
    ThreadOut,
    TrackRecordOut,
    TrackRecordPage,
    Turn,
)
from talk_is_cheap.api.security import limit
from talk_is_cheap.db.health import check_database
from talk_is_cheap.db.models import Company, Feedback, RequestLog
from talk_is_cheap.financials import find_company, quarterly_financials
from talk_is_cheap.guidance.matching import counts_in_verdicts, needs_review
from talk_is_cheap.guidance.queries import GuidanceRow, company_guidance
from talk_is_cheap.promises.commands import describe_evidence, measured_amount, target_amount
from talk_is_cheap.promises.framing import show
from talk_is_cheap.promises.queries import PromiseRow, promise_row, promise_rows
from talk_is_cheap.promises.track import MIN_RESOLVED, TrackRecord, company_track_records
from talk_is_cheap.statements.commands import short_quote
from talk_is_cheap.statements.roster import match_executive, roster_name
from talk_is_cheap.universe import Universe

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
READY_TIMEOUT_SECONDS = 3.0
MARGIN_PLACES = Decimal("0.000001")
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": Problem, "description": "not found"}}

health = APIRouter(tags=["health"])
data = APIRouter(prefix="/v1", tags=["data"], dependencies=[Depends(limit("default"))])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="items per page")]
Cursor = Annotated[str | None, Query(description="`next_cursor` of the previous page")]
Ticker = Annotated[str, Path(pattern=r"^[A-Za-z.\-]{1,16}$", examples=["NVDA"])]


def services_of(request: Request) -> Services:
    found: Services = request.app.state.services
    return found


async def session_of(request: Request) -> AsyncIterator[AsyncSession]:
    async with services_of(request).sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(session_of)]


def page[T](items: Sequence[T], limit: int, cursor: str | None) -> tuple[list[T], str | None]:
    """One page of `items` and the cursor of the next, None at the end."""
    offset = 0
    if cursor:
        try:
            offset = int(json.loads(base64.urlsafe_b64decode(cursor.encode()))["offset"])
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc
        if offset < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor")
    end = offset + limit
    following = None
    if end < len(items):
        following = base64.urlsafe_b64encode(json.dumps({"offset": end}).encode()).decode()
    return list(items[offset:end]), following


async def company_or_404(session: AsyncSession, ticker: str) -> Company:
    company = await find_company(session, ticker)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{ticker.upper()} is not loaded")
    return company


# Health.


@health.get("/healthz", response_model=Health)
async def healthz() -> Health:
    """Liveness: the process answers."""
    return Health(status="ok")


@health.get("/readyz", response_model=Readiness, responses={503: {"model": Readiness}})
async def readyz(request: Request) -> Any:
    """Readiness: the database answers, has pgvector, and is migrated to this build's
    newest migration."""
    services = services_of(request)
    try:
        found = await asyncio.wait_for(check_database(services.engine), READY_TIMEOUT_SECONDS)
    except Exception as exc:
        body = Readiness(
            status="not_ready",
            database=False,
            migration=None,
            head=services.head,
            detail=f"database unreachable: {type(exc).__name__}",
        )
        return JSONResponse(body.model_dump(), status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    detail = None
    if not found.ready:
        detail = "pgvector or migrations missing"
    elif services.head is None:
        detail = "alembic.ini not found: the newest migration is unknown"
    elif found.migration_revision != services.head:
        detail = f"database at {found.migration_revision}, this build expects {services.head}"
    body = Readiness(
        status="not_ready" if detail else "ready",
        database=True,
        migration=found.migration_revision,
        head=services.head,
        detail=detail,
    )
    if detail:
        return JSONResponse(body.model_dump(), status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return body


# Companies.


@data.get("/companies", response_model=CompanyPage)
async def companies(
    request: Request, session: Session, limit: Limit = DEFAULT_LIMIT, cursor: Cursor = None
) -> CompanyPage:
    """Loaded companies and what is stored about each: latest quarter, guidance rows,
    linked articles, management claims and claims due to be judged."""
    from talk_is_cheap.companies import company_coverage
    from talk_is_cheap.statements.extract import calendars

    settings = services_of(request).settings
    rows = await company_coverage(
        session,
        await calendars(session),
        date.today(),
        grace_days=settings.promise_grace_days,
    )
    items, following = page(rows, limit, cursor)
    return CompanyPage(
        items=[CompanyCoverageOut.model_validate(r, from_attributes=True) for r in items],
        next_cursor=following,
    )


# Promises.


@data.get("/companies/{ticker}/promises", response_model=PromisePage, responses=NOT_FOUND)
async def company_promises(
    request: Request,
    session: Session,
    ticker: Ticker,
    verdict: PromiseVerdictName | None = None,
    speaker: Annotated[str | None, Query(max_length=100, examples=["Jensen Huang"])] = None,
    due_before: date | None = None,
    due_after: date | None = None,
    open: Annotated[bool, Query(description="only claims not settled yet")] = False,
    bold: Annotated[bool, Query(description="only bold claims")] = False,
    include_scheduled: Annotated[
        bool, Query(description="include scheduled actions (splits, dividends)")
    ] = False,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> PromisePage:
    """A company's promises, newest statement first, with their verdicts. `speaker` takes
    the roster's name or any alias of it ("Jensen Huang")."""
    company = await company_or_404(session, ticker)
    universe = services_of(request).universe
    listed = next((c for c in universe.companies if c.ticker == company.ticker), None)
    if speaker and listed is not None:
        speaker = roster_name(listed, speaker)
    rows = await promise_rows(
        session,
        company.ticker,
        verdicts=[verdict] if verdict else None,
        open_only=open,
        bold_only=bold,
        speaker=speaker,
        due_before=due_before,
        due_after=due_after,
        scheduled=include_scheduled,
    )
    items, following = page(rows, limit, cursor)
    return PromisePage(
        items=[PromiseOut(**promise_fields(r, universe)) for r in items], next_cursor=following
    )


@data.get("/promises/{promise_id}", response_model=PromiseDetail, responses=NOT_FOUND)
async def promise_detail(request: Request, session: Session, promise_id: int) -> PromiseDetail:
    """One promise as `promises show` prints it: the claim, its rule and version, every
    piece of evidence with dates and links, the checks and any review."""
    row = await promise_row(session, promise_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no promise {promise_id}")
    p, v = row.promise, row.verdict
    resolution = None
    if v is not None:
        measured: dict[str, Any] = dict(v.measured or {})
        resolution = Resolution(
            verdict=v.verdict,  # type: ignore[arg-type]
            model_verdict=v.model_verdict,  # type: ignore[arg-type]
            rule_version=v.rule_version,
            resolved_at=v.resolved_at,
            reasoning=v.reasoning,
            proof_date=v.proof_date,
            proof_date_source=v.proof_date_source,
            grace_end=v.grace_end,
            days_late=v.days_late,
            revised_before_due=v.revised_before_due,
            resolved_with=v.resolved_with,
            measured_from=list((measured.get("data") or {}).get("inputs", [])),
            findings=[
                Finding(
                    evidence_id=str(f["evidence_id"]),
                    finding=str(f["finding"]),
                    note=str(f["note"]) if f.get("note") else None,
                )
                for f in v.assessments
                if f.get("finding") != "irrelevant"
            ],
            checks=[
                Check(
                    check=str(c["check"]),
                    passed=c["passed"],  # type: ignore[arg-type]
                    detail=str(c["detail"]) if c.get("detail") else None,
                )
                for c in v.checks
            ],
            evidence=evidence_links(v.evidence),
            gathered=len(v.gathered),
        )
    review = None
    if row.review is not None:
        review = Review(
            verdict=row.review.verdict,  # type: ignore[arg-type]
            note=row.review.note,
            decided_at=row.review.decided_at,
        )
    return PromiseDetail(
        **promise_fields(row, services_of(request).universe),
        version=p.version,
        current=p.current,
        claim=p.quote,
        deadline_text=p.deadline_text,
        bold_basis=p.bold_basis,
        baseline=(
            Baseline(value=p.baseline_value, period=p.baseline_period, source=p.baseline_source)
            if p.baseline_source
            else None
        ),
        notes=list(p.notes),
        resolution=resolution,
        review=review,
    )


def promise_fields(row: PromiseRow, universe: Universe) -> dict[str, Any]:
    p, v = row.promise, row.verdict
    company = next((c for c in universe.companies if c.ticker == p.ticker), None)
    executive = match_executive(p.speaker, None, company, p.statement_date) if company else None
    return {
        "id": p.id,
        "ticker": p.ticker,
        "quote": short_quote(p.quote),
        "speaker": p.speaker,
        "role": executive.role if executive else None,
        "date": p.statement_date,
        "publisher": row.publisher,
        "url": row.url,
        "subject": p.subject,
        "claim_type": p.claim_type,
        "specificity": p.specificity,
        "bold": p.bold,
        "rule": p.resolution_rule,
        "due_date": p.due_date,
        "due_basis": p.due_basis,
        "verdict": row.effective,
        "reviewed": row.review is not None,
        "scored": p.specificity != "vague" and p.guidance_id is None,
        "guidance_id": p.guidance_id,
        "measured": _amount(measured_amount(row)),
        "target": _amount(target_amount(row)),
        "revised_before_due": bool(v and v.revised_before_due),
        "evidence": evidence_links(v.evidence) if v else [],
    }


def _amount(found: tuple[Any, str | None] | None) -> Amount | None:
    if found is None:
        return None
    value, unit = found
    return Amount(value=value, unit=unit, display=show(value, unit))


def evidence_links(items: Sequence[dict[str, Any]]) -> list[EvidenceLink]:
    return [
        EvidenceLink(
            id=str(e["id"]),
            date=e.get("date"),
            tool=str(e.get("tool", "")),
            url=e.get("url") or None,
            description=describe_evidence(e),
        )
        for e in items
    ]


# Track records.


@data.get("/track-record", response_model=TrackRecordPage)
async def track_record(
    session: Session,
    by: Literal["executive", "company"] = "executive",
    ticker: Annotated[str | None, Query(pattern=r"^[A-Za-z.\-]{1,16}$")] = None,
    specific_only: bool = False,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> TrackRecordPage:
    """Track records per executive or per company, most resolved claims first. A group
    with fewer than `min_resolved` resolved claims is `too_few_to_judge`, without rates."""
    if ticker is not None:
        await company_or_404(session, ticker)
    records = await company_track_records(
        session, by=by, ticker=ticker, specific_only=specific_only
    )
    items, following = page(records, limit, cursor)
    return TrackRecordPage(
        min_resolved=MIN_RESOLVED, items=[_record(r) for r in items], next_cursor=following
    )


def _record(r: TrackRecord) -> TrackRecordOut:
    return TrackRecordOut(
        key=r.key,
        claims=r.claims,
        resolved=r.resolved,
        by_verdict=dict(r.by_verdict),
        expired=r.expired,
        vague=r.vague,
        too_few_to_judge=not r.enough,
        rates=(
            Rates(
                on_time_rate=r.on_time_rate,
                walked_back_share=r.walked_back_share,
                bold_share=r.bold_share,
                bold_hit_rate=r.bold_hit_rate,
                median_days_late=r.median_days_late,
            )
            if r.enough
            else None
        ),
        promise_ids=list(r.promise_ids),
    )


# Guidance.


@data.get("/companies/{ticker}/guidance", response_model=GuidancePage, responses=NOT_FOUND)
async def guidance(
    session: Session,
    ticker: Ticker,
    metric: GuidanceMetricName | None = None,
    flagged: Annotated[
        bool, Query(description="only rows flagged by the plausibility check")
    ] = False,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> GuidancePage:
    """A company's guidance, newest issue first, with actuals as first reported, verdicts
    and review status. Rows flagged and not reviewed (REVIEW) and rows reviewed as wrong
    (WRONG) do not count in verdicts, as in `talk-is-cheap guidance`."""
    company = await company_or_404(session, ticker)
    rows = await company_guidance(session, company.cik, metric, flagged_only=flagged)
    items, following = page(rows, limit, cursor)
    return GuidancePage(items=[_guidance(r) for r in items], next_cursor=following)


def _guidance(found: GuidanceRow) -> GuidanceOut:
    row = found.guidance
    target = None
    if row.target_fiscal_year is not None:
        target = f"FY{row.target_fiscal_year}" + (
            f"Q{row.target_fiscal_quarter}" if row.target_fiscal_quarter else ""
        )
    review_status = None
    if row.plausibility == "flagged":
        review_status = {None: "needs review", "ok": "reviewed: ok", "wrong": "reviewed: wrong"}[
            row.review
        ]
    elif row.review == "wrong":
        review_status = "reviewed: wrong"
    return GuidanceOut(
        id=row.id,
        issued_period=f"FY{row.fiscal_year}Q{row.fiscal_quarter}",
        issued_at=row.issued_at,
        target_period=target,
        metric=row.metric,  # type: ignore[arg-type]
        segment=row.segment,
        basis=row.basis,
        measure=row.measure,
        constant_currency=row.constant_currency,
        unit=row.unit,
        low=row.low,
        high=row.high,
        point=row.point,
        value_text=row.value_text,
        qualitative=row.qualitative,
        conditions=row.conditions,
        actual=row.actual,
        difference=row.difference,
        percent_difference=row.percent_difference,
        verdict=row.verdict,  # type: ignore[arg-type]
        verdict_reason=row.verdict_reason,
        basis_assumed=row.basis_assumed,
        mark="REVIEW" if needs_review(row) else "WRONG" if row.review == "wrong" else None,
        counts_in_verdicts=counts_in_verdicts(row),
        review_status=review_status,
        plausibility=row.plausibility,
        plausibility_rule=row.plausibility_rule,
        plausibility_note=row.plausibility_note,
        sources=[
            GuidanceSourceOut(
                url=s.url,
                document_name=s.document_name,
                section_title=s.section_title,
                char_start=s.char_start,
                char_end=s.char_end,
            )
            for s in found.sources
        ],
    )


# Financials.


@data.get("/companies/{ticker}/financials", response_model=FinancialsPage, responses=NOT_FOUND)
async def financials(
    session: Session,
    ticker: Ticker,
    vintage: Annotated[
        Literal["first", "latest"],
        Query(description="values as first reported, or as most recently reported"),
    ] = "first",
    quarters: Annotated[int, Query(ge=1, le=80, description="the latest N quarters")] = 8,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> FinancialsPage:
    """Quarterly financials, newest first. Derived quarters (from year-to-date totals)
    say how they were derived."""
    company = await company_or_404(session, ticker)
    rows = await quarterly_financials(session, company.cik, vintage=vintage, quarters=quarters)
    items, following = page(rows, limit, cursor)
    return FinancialsPage(
        ticker=company.ticker,
        vintage=vintage,
        items=[
            QuarterOut(
                fiscal_year=q.fiscal_year,
                fiscal_quarter=q.fiscal_quarter,
                label=q.label,
                period_end=q.period_end,
                gross_margin=_fraction(q.gross_margin),
                operating_margin=_fraction(q.operating_margin),
                metrics={
                    name: MetricOut.model_validate(m, from_attributes=True)
                    for name, m in q.metrics.items()
                },
            )
            for q in items
        ],
        next_cursor=following,
    )


def _fraction(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(MARGIN_PLACES)


# Threads.


@data.get("/threads/{thread_id}", response_model=ThreadOut, responses=NOT_FOUND)
async def thread(request: Request, thread_id: Annotated[ThreadId, Path()]) -> ThreadOut:
    """A conversation's questions and answers, from the agent's checkpointer, each answer
    with its sources."""
    graph = services_of(request).graph
    snapshot = await graph.aget_state(run_config(thread_id))
    values = snapshot.values or {}
    turns = history(values.get("messages") or [])
    if not turns:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no thread {thread_id}")
    evidence = values.get("evidence") or {}
    out = []
    for question, final in turns:
        text, sources = render(final, evidence)
        body = text.split("\n\nSources:\n", 1)[0] if sources else text
        out.append(
            Turn(
                question=question,
                answer=body,
                sources=[
                    Source(
                        number=s["number"],
                        label=s["label"],
                        url=s["url"],
                        provenance=s["provenance"],
                    )
                    for s in sources
                ],
            )
        )
    return ThreadOut(thread_id=thread_id, turns=out)


# Feedback.


@data.post(
    "/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    responses=NOT_FOUND,
)
async def feedback(request: Request, session: Session, body: FeedbackIn) -> FeedbackOut:
    """A thumbs up or down on a chat answer, by the `request_id` of its `done` event, with
    an optional comment. Saved for evals."""
    asked = await session.scalar(
        select(RequestLog.id).where(
            RequestLog.request_id == body.request_id, RequestLog.path == "/v1/chat"
        )
    )
    if asked is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no chat request {body.request_id}")
    row = Feedback(
        request_id=body.request_id,
        rating=body.rating,
        comment=body.comment,
        key_id=request.state.key_id,
    )
    session.add(row)
    await session.commit()
    return FeedbackOut(id=row.id, request_id=row.request_id, rating=body.rating)
