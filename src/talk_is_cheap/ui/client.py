"""The UI's one client of the HTTP API (docs/api.md).

Responses are read into the API's own response models (`api/schemas.py`), so the UI and
the API cannot drift apart. The base URL, the API key and the timeouts are set here and
nowhere else; the key is sent in `X-API-Key` from the Streamlit server and never reaches
the browser.

Every failure is an `ApiError` whose message can be shown as it is: 401, 402, 404, 429
and 5xx have their own classes, as do a connection that fails or times out. A chat
question streams Server-Sent Events (`chat`); an `error` event raises the same classes.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Self

import httpx
from httpx_sse import SSEError, connect_sse
from pydantic import BaseModel, ValidationError

from talk_is_cheap.api.schemas import (
    CHAT_EVENTS,
    ChatIn,
    CitationEvent,
    CompanyCoverageOut,
    CompanyPage,
    DoneEvent,
    ErrorEvent,
    FeedbackIn,
    FeedbackOut,
    GuidanceMetricName,
    GuidanceOut,
    GuidancePage,
    Page,
    PromiseDetail,
    PromiseOut,
    PromisePage,
    StatusEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    TrackRecordPage,
)
from talk_is_cheap.ui.config import UiSettings

PAGE_SIZE = 200
# Data requests: the API reads whole lists from the database, so allow a few seconds.
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# A chat question ends within CHAT_TIMEOUT_SECONDS (60) on the API; keep-alive comments
# come every 10 seconds, so a read that waits longer than this means the API is gone.
CHAT_TIMEOUT = httpx.Timeout(90.0, connect=5.0)

TrackBy = Literal["executive", "company", "all"]


class ApiError(Exception):
    """A request the API refused or could not answer. `str()` is fit to show."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class Unauthorized(ApiError):
    """401: the UI's key is missing or not one of the API's keys."""


class BudgetExceeded(ApiError):
    """402: a daily or project LLM budget is spent."""


class NotFound(ApiError):
    """404: no such company, promise or chat request."""


class RateLimited(ApiError):
    """429: too many requests; `retry_after` seconds until the next is let in."""

    def __init__(self, message: str, *, retry_after: int | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ServerError(ApiError):
    """5xx: the API failed, timed out on a question or has no model."""


class Unreachable(ApiError):
    """The API did not answer: refused connection, DNS, or a timeout."""


@dataclass(frozen=True, slots=True)
class ChatStarted:
    """The first item of a chat stream, from the response headers."""

    request_id: str | None
    thread_id: str | None


ChatEvent = (
    ChatStarted
    | StatusEvent
    | ToolStartEvent
    | ToolEndEvent
    | TokenEvent
    | CitationEvent
    | DoneEvent
)

CHAT_MESSAGES = {
    "budget_exceeded": "The question budget is used up for today; it resets at midnight UTC.",
    "thread_full": "This conversation holds 30 questions, the most it can. Start a new one.",
    "thread_busy": "This conversation is still answering another question. Wait a moment.",
    "timeout": "The question took longer than a minute and was stopped. Try a narrower question.",
    "model_unavailable": "Chat is not available: the API has no model configured.",
    "model_error": "The model failed to answer. Try again in a moment.",
    "internal_error": "The API failed while answering. Try again in a moment.",
}


def error_for(
    status: int,
    detail: str | None,
    *,
    code: str | None = None,
    retry_after: int | None = None,
) -> ApiError:
    """The ApiError for an HTTP status (or a chat `error` event's), with a message for
    people rather than the API's own wording."""
    message = CHAT_MESSAGES.get(code or "")
    if status == 401:
        return Unauthorized(
            message or "The API refused the UI's key: set UI_API_KEY to one of its API_KEYS.",
            status=status,
            code=code,
            detail=detail,
        )
    if status == 402:
        return BudgetExceeded(
            message or CHAT_MESSAGES["budget_exceeded"], status=status, code=code, detail=detail
        )
    if status == 404:
        return NotFound(detail or "Not found.", status=status, code=code, detail=detail)
    if status == 429:
        wait = f" Try again in {retry_after} seconds." if retry_after else " Try again shortly."
        return RateLimited(
            "Too many requests for now." + wait,
            retry_after=retry_after,
            status=status,
            code=code,
            detail=detail,
        )
    if status >= 500:
        return ServerError(
            message or f"The API failed to answer (HTTP {status}). Try again in a moment.",
            status=status,
            code=code,
            detail=detail,
        )
    return ApiError(
        message or f"The API refused the request: {detail or f'HTTP {status}'}",
        status=status,
        code=code,
        detail=detail,
    )


class ApiClient:
    """Synchronous, as Streamlit scripts are. One `httpx.Client` per instance."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout = TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._http = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    @classmethod
    def from_settings(cls, settings: UiSettings) -> Self:
        key = settings.api_key.get_secret_value() if settings.api_key else None
        return cls(settings.api_url, key)

    @property
    def scope(self) -> str:
        """What cached responses are keyed by besides their arguments."""
        return self.base_url

    def __repr__(self) -> str:
        return f"ApiClient({self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    # Data.

    def companies(self) -> list[CompanyCoverageOut]:
        return [c for p in self._pages("/v1/companies", {}, CompanyPage) for c in p.items]

    def promises(
        self,
        ticker: str,
        *,
        include_scheduled: bool = False,
        verdict: str | None = None,
        speaker: str | None = None,
        due_after: date | None = None,
        due_before: date | None = None,
        bold: bool = False,
    ) -> list[PromiseOut]:
        params = {
            "include_scheduled": include_scheduled,
            "verdict": verdict,
            "speaker": speaker,
            "due_after": due_after,
            "due_before": due_before,
            "bold": bold,
        }
        pages = self._pages(f"/v1/companies/{ticker}/promises", params, PromisePage)
        return [p for page in pages for p in page.items]

    def promise(self, promise_id: int) -> PromiseDetail:
        return PromiseDetail.model_validate(self._get(f"/v1/promises/{promise_id}", {}))

    def track_record(
        self, by: TrackBy = "executive", ticker: str | None = None, *, specific_only: bool = False
    ) -> TrackRecordPage:
        params = {"by": by, "ticker": ticker, "specific_only": specific_only}
        pages = self._pages("/v1/track-record", params, TrackRecordPage)
        return TrackRecordPage(
            min_resolved=pages[0].min_resolved,
            items=[r for p in pages for r in p.items],
            next_cursor=None,
        )

    def guidance(
        self, ticker: str, *, metric: GuidanceMetricName | None = None, flagged: bool = False
    ) -> list[GuidanceOut]:
        params = {"metric": metric, "flagged": flagged}
        pages = self._pages(f"/v1/companies/{ticker}/guidance", params, GuidancePage)
        return [g for p in pages for g in p.items]

    def feedback(
        self, request_id: str, rating: Literal["up", "down"], comment: str | None = None
    ) -> FeedbackOut:
        body = FeedbackIn(request_id=request_id, rating=rating, comment=comment or None)
        found = self._send("POST", "/v1/feedback", json=body.model_dump(exclude_none=True))
        return FeedbackOut.model_validate(found)

    # Chat.

    def chat(self, question: str, thread_id: str | None = None) -> Iterator[ChatEvent]:
        """Ask a question; yields `ChatStarted`, then the stream's events up to `done`.
        An `error` event, or an error before the stream, raises an ApiError."""
        body = ChatIn(question=question, thread_id=thread_id).model_dump(exclude_none=True)
        try:
            with connect_sse(
                self._http,
                "POST",
                "/v1/chat",
                json=body,
                headers={"Accept": "text/event-stream"},
                timeout=CHAT_TIMEOUT,
            ) as source:
                response = source.response
                if response.status_code != 200:
                    response.read()
                    raise self._error(response)
                yield ChatStarted(
                    request_id=response.headers.get("X-Request-ID"),
                    thread_id=response.headers.get("X-Thread-ID"),
                )
                for sent in source.iter_sse():
                    model = CHAT_EVENTS.get(sent.event)
                    if model is None:
                        continue
                    try:
                        event = model.model_validate_json(sent.data)
                    except ValidationError as exc:
                        raise ApiError(
                            "The API sent an answer the UI could not read.",
                            detail=f"{sent.event}: {exc.error_count()} errors",
                        ) from exc
                    if isinstance(event, ErrorEvent):
                        raise error_for(event.status, event.message, code=event.code)
                    yield _chat_event(event)
                    if isinstance(event, DoneEvent):
                        return
        except httpx.TimeoutException as exc:
            raise Unreachable(
                "The API stopped sending the answer; try again.", detail=str(exc)
            ) from exc
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from exc
        except SSEError as exc:
            raise ApiError("The API did not stream an answer.", detail=str(exc)) from exc
        raise ServerError("The answer ended before it was finished. Try again.")

    # Plumbing.

    def _pages[P: Page](self, path: str, params: Mapping[str, Any], model: type[P]) -> list[P]:
        """Every page of a list, following `next_cursor`."""
        pages: list[P] = []
        cursor: str | None = None
        while True:
            found = model.model_validate(
                self._get(path, {**params, "limit": PAGE_SIZE, "cursor": cursor})
            )
            pages.append(found)
            cursor = found.next_cursor
            if cursor is None:
                return pages

    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        return self._send("GET", path, params=params)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        query = {
            k: (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in (params or {}).items()
            if v is not None and v is not False
        }
        try:
            response = self._http.request(method, path, params=query, json=json)
        except httpx.TimeoutException as exc:
            raise Unreachable(
                f"The API at {self.base_url} did not answer in time.", detail=str(exc)
            ) from exc
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from exc
        if response.is_error:
            raise self._error(response)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("The API sent a response the UI could not read.") from exc

    def _unreachable(self, exc: Exception) -> Unreachable:
        return Unreachable(
            f"Cannot reach the API at {self.base_url}. Is it running?", detail=str(exc)
        )

    @staticmethod
    def _error(response: httpx.Response) -> ApiError:
        detail: str | None = None
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("detail") is not None:
                found = body["detail"]
                detail = found if isinstance(found, str) else "invalid request"
        except ValueError:
            detail = response.text[:200] or None
        retry = response.headers.get("Retry-After")
        return error_for(
            response.status_code,
            detail,
            retry_after=int(retry) if retry and retry.isdigit() else None,
        )


def _chat_event(event: BaseModel) -> ChatEvent:
    assert isinstance(
        event, StatusEvent | ToolStartEvent | ToolEndEvent | TokenEvent | CitationEvent | DoneEvent
    )
    return event
