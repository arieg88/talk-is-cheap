"""Request, response and stream-event models of the HTTP API (Pydantic v2), as the
OpenAPI document at /docs shows them."""

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from talk_is_cheap.agent.evidence import Provenance

# The promise tracker's verdicts (db/models.py: PROMISE_VERDICTS).
PromiseVerdictName = Literal[
    "delivered",
    "delivered_late",
    "partly_delivered",
    "not_delivered",
    "walked_back",
    "too_early",
    "expired_unproven",
    "cannot_determine",
]
GuidanceMetricName = Literal[
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
MAX_QUESTION_CHARS = 2_000
MAX_COMMENT_CHARS = 2_000
ThreadId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]


class Page(BaseModel):
    """A page of a list. Pass `next_cursor` as `cursor` for the next page; null at the
    end."""

    next_cursor: str | None


# Health.


class Health(BaseModel):
    status: Literal["ok"]


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    database: bool
    migration: str | None = Field(description="the database's Alembic revision")
    head: str | None = Field(description="the newest revision this build knows")
    detail: str | None = None


# Companies.


class CompanyCoverageOut(BaseModel):
    ticker: str
    cik: int
    name: str
    fiscal_year_end: str | None = Field(description="MMDD")
    filings: int
    latest_quarter: str | None = Field(description="the latest fiscal quarter with values")
    guidance_rows: int
    linked_articles: int = Field(description="articles linked with confidence 0.5 or more")
    management_claims: int
    due_claims: int = Field(description="promise groups whose due date and grace period passed")


class CompanyPage(Page):
    items: list[CompanyCoverageOut]


# Promises.


class Amount(BaseModel):
    value: Decimal
    unit: str | None
    display: str


class EvidenceLink(BaseModel):
    id: str
    date: dt.date | None
    tool: str
    provenance: Provenance | None = Field(
        description="SEC filing, company press release or news report"
    )
    url: str | None
    description: str


class PromiseOut(BaseModel):
    id: int
    ticker: str
    quote: str = Field(description="at most 40 words of the reported quote")
    speaker: str
    role: str | None = Field(description="the speaker's role on the statement date")
    date: dt.date
    publisher: str | None
    url: str = Field(description="the article the claim was read from")
    subject: str
    claim_type: Literal["promise", "scheduled_action"]
    specificity: Literal["specific", "partial", "vague"]
    bold: bool
    rule: str = Field(description="the resolution rule, fixed when the claim was framed")
    due_date: dt.date | None
    due_basis: str
    verdict: PromiseVerdictName | None = Field(
        description="the verdict that counts: a review's, else the resolver's; null if not resolved"
    )
    reviewed: bool
    scored: bool = Field(
        description="false for vague claims and claims scored with formal guidance"
    )
    guidance_id: int | None = Field(description="the formal guidance row it restates")
    measured: Amount | None
    target: Amount | None
    revised_before_due: bool
    evidence: list[EvidenceLink]


class PromisePage(Page):
    items: list[PromiseOut]


class Baseline(BaseModel):
    value: Decimal | None
    period: str | None
    source: str | None


class Finding(BaseModel):
    evidence_id: str
    finding: str
    note: str | None = None


class Check(BaseModel):
    check: str
    passed: bool | None = Field(description="null for a note")
    detail: str | None = None


class Resolution(BaseModel):
    verdict: PromiseVerdictName
    model_verdict: PromiseVerdictName = Field(description="the judge's, before the checks")
    rule_version: str
    resolved_at: dt.datetime
    reasoning: str
    proof_date: dt.date | None
    proof_date_source: str | None
    grace_end: dt.date | None
    days_late: int | None
    revised_before_due: bool
    resolved_with: int | None
    measured_from: list[str] = Field(description="evidence ids the measured value cites")
    findings: list[Finding]
    checks: list[Check]
    evidence: list[EvidenceLink]
    gathered: int


class Review(BaseModel):
    verdict: PromiseVerdictName
    note: str | None
    decided_at: dt.datetime


class PromiseDetail(PromiseOut):
    version: str
    current: bool
    claim: str = Field(description="the full quote")
    deadline_text: str | None
    bold_basis: str | None
    baseline: Baseline | None
    notes: list[str] = Field(description="what framing code changed or rejected")
    resolution: Resolution | None
    review: Review | None


# Track records.


class Rates(BaseModel):
    on_time_rate: float | None
    walked_back_share: float | None
    bold_share: float | None
    bold_hit_rate: float | None
    median_days_late: float | None


class TrackRecordOut(BaseModel):
    key: str = Field(
        description='an executive ("Jen-Hsun Huang (NVDA)"), a ticker, or "all" with `by=all`'
    )
    claims: int
    resolved: int
    by_verdict: dict[str, int]
    expired: int
    vague: int
    too_few_to_judge: bool
    rates: Rates | None = Field(description="null when too few claims are resolved")
    promise_ids: list[int]


class TrackRecordPage(Page):
    min_resolved: int = Field(description="resolved claims a group needs for rates")
    items: list[TrackRecordOut]


# Guidance.


class GuidanceSourceOut(BaseModel):
    url: str
    document_name: str
    section_title: str | None
    char_start: int
    char_end: int


class GuidanceOut(BaseModel):
    id: int
    issued_period: str = Field(description="the quarter whose earnings release issued it")
    issued_at: dt.date
    target_period: str | None
    metric: GuidanceMetricName
    segment: str | None
    basis: str
    measure: str
    constant_currency: bool
    unit: str
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    value_text: str | None
    qualitative: str | None
    conditions: str | None
    actual: Decimal | None = Field(description="as first reported, in the guidance's unit")
    difference: Decimal | None
    percent_difference: Decimal | None
    verdict: Literal["beat", "met", "missed", "pending", "not_comparable"] = Field(
        description="as stored; it counts only when `counts_in_verdicts`"
    )
    verdict_reason: str | None
    basis_assumed: bool
    mark: Literal["REVIEW", "WRONG"] | None = Field(
        description="as `guidance` marks rows: REVIEW, flagged and not reviewed; WRONG, "
        "reviewed as wrong. Neither counts in verdicts"
    )
    counts_in_verdicts: bool
    review_status: str | None = Field(
        description="needs review, reviewed: ok or reviewed: wrong, for flagged or wrong rows"
    )
    plausibility: str | None
    plausibility_rule: str | None
    plausibility_note: str | None
    sources: list[GuidanceSourceOut]


class GuidancePage(Page):
    items: list[GuidanceOut]


# Financials.


class MetricOut(BaseModel):
    value: Decimal
    unit: str
    filed: dt.date
    accession_no: str
    form: str
    derived: bool
    derivation: str | None


class QuarterOut(BaseModel):
    fiscal_year: int
    fiscal_quarter: int
    label: str
    period_end: dt.date
    gross_margin: Decimal | None = Field(description="a fraction: 0.75 is 75%")
    operating_margin: Decimal | None
    metrics: dict[str, MetricOut]


class FinancialsPage(Page):
    ticker: str
    vintage: Literal["first", "latest"]
    items: list[QuarterOut]


# Threads.


class Source(BaseModel):
    number: int
    label: str
    url: str
    provenance: Provenance


class Turn(BaseModel):
    question: str
    answer: str = Field(description="with [1]-style citations")
    sources: list[Source]


class ThreadOut(BaseModel):
    thread_id: str
    turns: list[Turn]


# Feedback.


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="the chat's request id")
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)


class FeedbackOut(BaseModel):
    id: int
    request_id: str
    rating: Literal["up", "down"]


# Chat.


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    thread_id: ThreadId | None = Field(
        default=None, description="a thread to continue; a new one when omitted"
    )


class StatusEvent(BaseModel):
    """`event: status`: a step of the graph."""

    node: str
    step: str


class ToolStartEvent(BaseModel):
    """`event: tool_start`."""

    call_id: str
    name: str
    arguments: dict[str, object]


class ToolEndEvent(BaseModel):
    """`event: tool_end`."""

    call_id: str
    name: str
    summary: str
    evidence: list[str] = Field(description="the citation ids the call added")
    error: str | None = None


class TokenEvent(BaseModel):
    """`event: token`: the next piece of the verified answer."""

    text: str


class CitationEvent(BaseModel):
    """`event: citation`: a source the answer's [n] markers point at."""

    id: int
    url: str
    title: str
    provenance: Provenance
    evidence: list[str]


class ErrorEvent(BaseModel):
    """`event: error`: the last event of a question that did not finish."""

    code: Literal[
        "budget_exceeded",
        "thread_full",
        "thread_busy",
        "timeout",
        "model_unavailable",
        "model_error",
        "internal_error",
    ]
    status: int = Field(description="the HTTP status this error would have")
    message: str
    request_id: str


class DoneEvent(BaseModel):
    """`event: done`: the last event of an answered question."""

    request_id: str
    thread_id: str
    question_type: str
    input_tokens: int
    output_tokens: int
    cost: Decimal = Field(description="US dollars; cached calls cost nothing")
    seconds: float
    revisions: int
    cache_hit: bool | None = Field(
        description="every model call came from the cache; null when none was made"
    )
    verified: bool
    tool_calls: int


CHAT_EVENTS: dict[str, type[BaseModel]] = {
    "status": StatusEvent,
    "tool_start": ToolStartEvent,
    "tool_end": ToolEndEvent,
    "token": TokenEvent,
    "citation": CitationEvent,
    "error": ErrorEvent,
    "done": DoneEvent,
}


class Problem(BaseModel):
    """An error response."""

    detail: str
