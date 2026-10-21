"""The UI's API client against a fake HTTP transport: each method, the error mapping,
and the chat stream's events."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from talk_is_cheap.api.schemas import (
    CitationEvent,
    DoneEvent,
    StatusEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from talk_is_cheap.ui.client import (
    ApiClient,
    ApiError,
    BudgetExceeded,
    ChatStarted,
    NotFound,
    RateLimited,
    ServerError,
    Unauthorized,
    Unreachable,
)
from talk_is_cheap.ui.config import UiSettings
from tests.ui_fakes import SECRET_KEY, company, detail, guidance, promise, record

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, key: str | None = SECRET_KEY) -> ApiClient:
    return ApiClient("http://api.test/", key, transport=httpx.MockTransport(handler))


def page(items: list[Any], cursor: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"items": [i.model_dump(mode="json") for i in items], "next_cursor": cursor} | extra


def test_companies_follow_every_page_and_send_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("cursor") == "next":
            return httpx.Response(200, json=page([company("APIY")]))
        return httpx.Response(200, json=page([company("APIX")], "next"))

    found = client(handler).companies()
    assert [c.ticker for c in found] == ["APIX", "APIY"]
    assert all(r.headers["X-API-Key"] == SECRET_KEY for r in seen)
    assert seen[0].url.path == "/v1/companies" and seen[0].url.params["limit"] == "200"
    assert "cursor" not in seen[0].url.params


def test_no_key_header_without_a_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "X-API-Key" not in request.headers
        return httpx.Response(200, json=page([]))

    assert client(handler, key=None).companies() == []


def test_settings_give_url_and_key_and_the_key_is_never_in_repr() -> None:
    settings = UiSettings(api_url="http://api:8080/", api_key=SECRET_KEY)  # type: ignore[arg-type]
    api = ApiClient.from_settings(settings)
    assert api.base_url == "http://api:8080" and api.scope == "http://api:8080"
    assert SECRET_KEY not in repr(api) and SECRET_KEY not in repr(settings)
    assert UiSettings(api_key="").api_key is None  # type: ignore[arg-type]


def test_promises_pass_filters_as_query_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/companies/APIX/promises"
        assert dict(request.url.params) == {
            "include_scheduled": "true",
            "verdict": "delivered",
            "speaker": "Jane Roe",
            "due_after": "2026-01-01",
            "bold": "true",
            "limit": "200",
        }
        return httpx.Response(200, json=page([promise(1)]))

    from datetime import date

    found = client(handler).promises(
        "APIX",
        include_scheduled=True,
        verdict="delivered",
        speaker="Jane Roe",
        due_after=date(2026, 1, 1),
        bold=True,
    )
    assert found[0].subject == "product 1 shipping"


def test_promise_detail_track_record_and_guidance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/promises/11":
            return httpx.Response(200, json=detail(11).model_dump(mode="json"))
        if path == "/v1/track-record":
            assert request.url.params["by"] == "all" and "ticker" not in request.url.params
            return httpx.Response(200, json=page([record("all", 6)], min_resolved=5))
        assert path == "/v1/companies/APIX/guidance"
        assert request.url.params["metric"] == "revenue"
        return httpx.Response(200, json=page([guidance(1)]))

    api = client(handler)
    found = api.promise(11)
    assert found.resolution is not None and found.resolution.evidence[0].provenance == "sec_filing"
    records = api.track_record("all")
    assert records.min_resolved == 5 and records.items[0].key == "all"
    assert records.next_cursor is None
    assert api.guidance("APIX", metric="revenue")[0].verdict == "beat"


def test_feedback_posts_the_rating() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/feedback"
        assert json.loads(request.content) == {"request_id": "a" * 32, "rating": "down"}
        return httpx.Response(201, json={"id": 3, "request_id": "a" * 32, "rating": "down"})

    assert client(handler).feedback("a" * 32, "down", "  ".strip()).id == 3


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind", "words"),
    [
        (401, {"detail": "invalid API key"}, {}, Unauthorized, "UI_API_KEY"),
        (402, {"detail": "spent"}, {}, BudgetExceeded, "midnight UTC"),
        (404, {"detail": "NOPE is not loaded"}, {}, NotFound, "NOPE is not loaded"),
        (429, {"detail": "rate limit"}, {"Retry-After": "17"}, RateLimited, "17 seconds"),
        (500, {"detail": "boom"}, {}, ServerError, "HTTP 500"),
        (503, "not json", {}, ServerError, "HTTP 503"),
        (422, {"detail": [{"msg": "bad"}]}, {}, ApiError, "invalid request"),
    ],
)
def test_errors_are_friendly_exceptions(
    status: int, body: Any, headers: dict[str, str], kind: type[ApiError], words: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    with pytest.raises(kind) as caught:
        client(handler).companies()
    assert type(caught.value) is kind
    assert words in str(caught.value) and caught.value.status == status
    assert "Traceback" not in str(caught.value)
    if kind is RateLimited:
        assert isinstance(caught.value, RateLimited) and caught.value.retry_after == 17


@pytest.mark.parametrize(
    ("raised", "words"),
    [
        (httpx.ConnectError("refused"), "Cannot reach the API at http://api.test"),
        (httpx.ReadTimeout("slow"), "did not answer in time"),
    ],
)
def test_an_unreachable_api(raised: Exception, words: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    with pytest.raises(Unreachable, match=words):
        client(handler).companies()


# Chat.


def sse(*events: tuple[str, dict[str, Any]], comment: bool = True) -> bytes:
    text = ": keep-alive\n\n" if comment else ""
    for name, data in events:
        text += f"event: {name}\ndata: {json.dumps(data)}\n\n"
    return text.encode()


DONE = {
    "request_id": "a" * 32,
    "thread_id": "t1",
    "question_type": "lookup",
    "input_tokens": 10,
    "output_tokens": 5,
    "cost": "0.0016",
    "seconds": 4.6,
    "revisions": 0,
    "cache_hit": False,
    "verified": True,
    "tool_calls": 1,
}
EVERY_EVENT = (
    ("status", {"node": "tools", "step": "Searching filings…"}),
    ("tool_start", {"call_id": "c1", "name": "search_filings", "arguments": {"q": "x"}}),
    ("tool_end", {"call_id": "c1", "name": "search_filings", "summary": "1 evidence item: E1",
                  "evidence": ["E1"], "error": None}),
    ("token", {"text": "Revenue was $96.2 billion "}),
    ("token", {"text": "[1]."}),
    ("citation", {"id": 1, "url": "https://www.sec.gov/x.htm", "title": "NVDA 10-Q",
                  "provenance": "sec_filing", "evidence": ["E1"]}),
    ("something_new", {"ignored": True}),
    ("done", DONE),
)  # fmt: skip


def stream(body: bytes, check: Callable[[httpx.Request], None] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if check:
            check(request)
        return httpx.Response(
            200,
            content=body,
            headers={
                "Content-Type": "text/event-stream",
                "X-Request-ID": "a" * 32,
                "X-Thread-ID": "t1",
            },
        )

    return handler


def test_chat_parses_every_event_type() -> None:
    def check(request: httpx.Request) -> None:
        assert request.url.path == "/v1/chat"
        assert request.headers["X-API-Key"] == SECRET_KEY
        assert json.loads(request.content) == {"question": "Revenue?", "thread_id": "t1"}

    events = list(client(stream(sse(*EVERY_EVENT), check)).chat("Revenue?", "t1"))
    assert events[0] == ChatStarted(request_id="a" * 32, thread_id="t1")
    kinds = [type(e) for e in events[1:]]
    assert kinds == [
        StatusEvent,
        ToolStartEvent,
        ToolEndEvent,
        TokenEvent,
        TokenEvent,
        CitationEvent,
        DoneEvent,
    ]
    status, start, end = events[1], events[2], events[3]
    assert isinstance(status, StatusEvent) and status.step == "Searching filings…"
    assert isinstance(start, ToolStartEvent) and start.arguments == {"q": "x"}
    assert isinstance(end, ToolEndEvent) and end.evidence == ["E1"]
    citation, done = events[6], events[7]
    assert isinstance(citation, CitationEvent) and citation.provenance == "sec_filing"
    assert isinstance(done, DoneEvent) and str(done.cost) == "0.0016" and done.tool_calls == 1


def test_a_new_conversation_sends_no_thread() -> None:
    def check(request: httpx.Request) -> None:
        assert json.loads(request.content) == {"question": "Hi"}

    assert list(client(stream(sse(("done", DONE)), check)).chat("Hi"))[-1] == DoneEvent(**DONE)


@pytest.mark.parametrize(
    ("code", "status", "kind", "words"),
    [
        ("budget_exceeded", 402, BudgetExceeded, "budget"),
        ("timeout", 504, ServerError, "longer than a minute"),
        ("model_unavailable", 503, ServerError, "no model configured"),
        ("model_error", 502, ServerError, "model failed"),
        ("thread_full", 409, ApiError, "30 questions"),
        ("thread_busy", 409, ApiError, "still answering"),
        ("internal_error", 500, ServerError, "failed while answering"),
    ],
)
def test_an_error_event_raises_with_a_plain_message(
    code: str, status: int, kind: type[ApiError], words: str
) -> None:
    body = sse(
        ("status", {"node": "router", "step": "Reading the question…"}),
        ("error", {"code": code, "status": status, "message": "raw", "request_id": "a" * 32}),
    )
    events: list[object] = []
    with pytest.raises(kind) as caught:
        for event in client(stream(body)).chat("Q"):
            events.append(event)
    assert words in str(caught.value) and caught.value.code == code
    assert caught.value.detail == "raw" and len(events) == 2


def test_chat_errors_before_the_stream_are_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limit"}, headers={"Retry-After": "5"})

    with pytest.raises(RateLimited, match="5 seconds"):
        list(client(handler).chat("Q"))


def test_a_stream_that_stops_before_done() -> None:
    body = sse(("token", {"text": "Half "}))
    with pytest.raises(ServerError, match="ended before it was finished"):
        list(client(stream(body)).chat("Q"))


def test_an_unreadable_event_is_an_api_error() -> None:
    body = sse(("done", {"request_id": "x"}))
    with pytest.raises(ApiError, match="could not read"):
        list(client(stream(body)).chat("Q"))


def test_chat_with_the_api_gone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(Unreachable, match="Cannot reach"):
        list(client(handler).chat("Q"))
