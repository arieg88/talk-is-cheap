"""A fake API client and sample responses for the UI's page tests: the API's own
response models, filled with small data."""

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from talk_is_cheap.api.schemas import (
    Check,
    CitationEvent,
    CompanyCoverageOut,
    DoneEvent,
    EvidenceLink,
    FeedbackOut,
    Finding,
    GuidanceOut,
    GuidanceSourceOut,
    PromiseDetail,
    PromiseOut,
    Rates,
    Resolution,
    StatusEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    TrackRecordOut,
    TrackRecordPage,
)
from talk_is_cheap.ui.client import ApiError, ChatEvent, ChatStarted

ARTICLE = "https://news.example.com/interview"
FILING = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/apix-q1.htm"
RELEASE = "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/ex99.htm"
SECRET_KEY = "sk-ui-test-0123456789"


def company(ticker: str = "APIX", **values: Any) -> CompanyCoverageOut:
    base: dict[str, Any] = {
        "ticker": ticker,
        "cik": 1,
        "name": "APIXCO CORP",
        "fiscal_year_end": "1231",
        "filings": 12,
        "latest_quarter": "FY2026 Q2",
        "guidance_rows": 3,
        "linked_articles": 1200,
        "management_claims": 4,
        "due_claims": 1,
    }
    return CompanyCoverageOut(**(base | values))


def promise(promise_id: int, **values: Any) -> PromiseOut:
    base: dict[str, Any] = {
        "id": promise_id,
        "ticker": "APIX",
        "quote": f"We will ship product {promise_id} by June.",
        "speaker": "Jane Roe",
        "role": "Chief Executive Officer",
        "date": date(2026, 1, 10),
        "publisher": "Test Wire",
        "url": ARTICLE,
        "subject": f"product {promise_id} shipping",
        "claim_type": "promise",
        "specificity": "specific",
        "bold": False,
        "rule": f"True if Apixco ships product {promise_id} by 2026-06-30.",
        "due_date": date(2026, 6, 30),
        "due_basis": "explicit",
        "verdict": "delivered",
        "reviewed": False,
        "scored": True,
        "guidance_id": None,
        "measured": None,
        "target": None,
        "revised_before_due": False,
        "evidence": [],
    }
    return PromiseOut(**(base | values))


def evidence(evidence_id: str = "E1", url: str = FILING) -> EvidenceLink:
    return EvidenceLink(
        id=evidence_id,
        date=date(2026, 5, 1),
        tool="search_filings",
        provenance="sec_filing",
        url=url,
        description="## Products **Product 1** shipped in April.",
    )


def detail(promise_id: int = 11, **values: Any) -> PromiseDetail:
    listed = promise(promise_id, **values).model_dump()
    return PromiseDetail(
        **listed,
        version="v3+5",
        current=True,
        claim=listed["quote"] + " That is the plan.",
        deadline_text="by June",
        bold_basis=None,
        baseline=None,
        notes=[],
        review=None,
        resolution=Resolution(
            verdict=listed["verdict"] or "delivered",
            model_verdict=listed["verdict"] or "delivered",
            rule_version="v3+5",
            resolved_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
            reasoning="The 10-Q says the product shipped in April.",
            proof_date=date(2026, 5, 1),
            proof_date_source="published",
            grace_end=date(2026, 8, 10),
            days_late=None,
            revised_before_due=False,
            resolved_with=None,
            measured_from=[],
            findings=[Finding(evidence_id="E1", finding="satisfies", note="shipped")],
            checks=[
                Check(check="evidence_exist", passed=True),
                Check(check="key_terms", passed=False, detail="Widget"),
                Check(check="on_time", passed=None, detail="on time until 2026-08-10"),
            ],
            evidence=[evidence()],
            gathered=4,
        ),
    )


def record(key: str, resolved: int, **values: Any) -> TrackRecordOut:
    enough = resolved >= 5
    base: dict[str, Any] = {
        "key": key,
        "claims": resolved + 2,
        "resolved": resolved,
        "by_verdict": {"delivered": max(resolved - 2, 0), "delivered_late": min(resolved, 1),
                       "walked_back": min(resolved, 1), "too_early": 2},
        "expired": 0,
        "vague": 1,
        "too_few_to_judge": not enough,
        "rates": Rates(
            on_time_rate=0.6, walked_back_share=0.2, bold_share=0.4, bold_hit_rate=0.5,
            median_days_late=12.0,
        )
        if enough
        else None,
        "promise_ids": [],
    }  # fmt: skip
    return TrackRecordOut(**(base | values))


def guidance(row_id: int, verdict: str = "beat", **values: Any) -> GuidanceOut:
    base: dict[str, Any] = {
        "id": row_id,
        "issued_period": "FY2026Q1",
        "issued_at": date(2026, 4, 30),
        "target_period": "FY2026Q2",
        "metric": "revenue",
        "segment": None,
        "basis": "gaap",
        "measure": "level",
        "constant_currency": False,
        "unit": "usd",
        "low": Decimal("105840000000"),
        "high": Decimal("110160000000"),
        "point": Decimal("108000000000"),
        "value_text": "$108.0 billion, plus or minus 2%",
        "qualitative": None,
        "conditions": None,
        "actual": Decimal("112000000000") if verdict in ("beat", "met", "missed") else None,
        "difference": None,
        "percent_difference": None,
        "verdict": verdict,
        "verdict_reason": "non-GAAP guidance; reported values are GAAP"
        if verdict == "not_comparable"
        else None,
        "basis_assumed": False,
        "mark": None,
        "counts_in_verdicts": True,
        "review_status": None,
        "plausibility": "passed",
        "plausibility_rule": None,
        "plausibility_note": None,
        "sources": [
            GuidanceSourceOut(
                url=RELEASE,
                document_name="ex99.htm",
                section_title="Outlook",
                char_start=10,
                char_end=90,
            )
        ],
    }
    return GuidanceOut(**(base | values))


def answer_events(request_id: str = "a" * 32) -> list[ChatEvent]:
    return [
        ChatStarted(request_id=request_id, thread_id="thread-1"),
        StatusEvent(node="router", step="Reading the question…"),
        StatusEvent(node="tools", step="Reading reported financials…"),
        ToolStartEvent(call_id="c1", name="get_actuals", arguments={"ticker": "APIX"}),
        ToolEndEvent(
            call_id="c1", name="get_actuals", summary="1 evidence item: E1", evidence=["E1"]
        ),
        TokenEvent(text="Apixco's revenue was "),
        TokenEvent(text="$96.2 billion [1]."),
        CitationEvent(
            id=1, url=FILING, title="APIX 10-Q filed 2026-08-26", provenance="sec_filing",
            evidence=["E1"],
        ),
        DoneEvent(
            request_id=request_id, thread_id="thread-1", question_type="lookup",
            input_tokens=100, output_tokens=20, cost=Decimal("0.0016"), seconds=4.6,
            revisions=0, cache_hit=False, verified=True, tool_calls=1,
        ),
    ]  # fmt: skip


class FakeApi:
    """What the pages call on `ApiClient`, from fixed data; `error` makes every call
    fail with it."""

    def __init__(
        self,
        *,
        companies: Sequence[CompanyCoverageOut] = (),
        promises: dict[str, list[PromiseOut]] | None = None,
        details: dict[int, PromiseDetail] | None = None,
        records: dict[tuple[str, str | None], list[TrackRecordOut]] | None = None,
        guidance: dict[str, list[GuidanceOut]] | None = None,
        chat: Sequence[ChatEvent] = (),
        chat_error: ApiError | None = None,
        error: ApiError | None = None,
    ) -> None:
        self.scope = f"fake-{uuid.uuid4().hex}"
        self.base_url = "http://api.test"
        self._companies = list(companies)
        self._promises = promises or {}
        self._details = details or {}
        self._records = records or {}
        self._guidance = guidance or {}
        self._chat = list(chat)
        self._chat_error = chat_error
        self._error = error
        self.asked: list[tuple[str, str | None]] = []
        self.feedback_sent: list[tuple[str, str, str | None]] = []

    def _check(self) -> None:
        if self._error is not None:
            raise self._error

    def companies(self) -> list[CompanyCoverageOut]:
        self._check()
        return self._companies

    def promises(self, ticker: str, *, include_scheduled: bool = False) -> list[PromiseOut]:
        self._check()
        found = self._promises.get(ticker, [])
        return [p for p in found if include_scheduled or p.claim_type != "scheduled_action"]

    def promise(self, promise_id: int) -> PromiseDetail:
        self._check()
        from talk_is_cheap.ui.client import NotFound

        if promise_id not in self._details:
            raise NotFound(f"no promise {promise_id}", status=404)
        return self._details[promise_id]

    def track_record(self, by: str = "executive", ticker: str | None = None) -> TrackRecordPage:
        self._check()
        items = self._records.get((by, ticker), [])
        return TrackRecordPage(min_resolved=5, items=items, next_cursor=None)

    def guidance(self, ticker: str) -> list[GuidanceOut]:
        self._check()
        return self._guidance.get(ticker, [])

    def chat(self, question: str, thread_id: str | None = None) -> Iterator[ChatEvent]:
        self.asked.append((question, thread_id))
        yield ChatStarted(request_id="b" * 32, thread_id="thread-1")
        if self._chat_error is not None:
            raise self._chat_error
        yield from self._chat

    def feedback(self, request_id: str, rating: str, comment: str | None = None) -> FeedbackOut:
        self.feedback_sent.append((request_id, rating, comment))
        return FeedbackOut(id=1, request_id=request_id, rating=rating)  # type: ignore[arg-type]
