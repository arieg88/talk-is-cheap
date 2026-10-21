"""Request ids, logs and the request log.

Every request gets a `request_id`, returned in `X-Request-ID`. structlog writes one line
per request (method, path, status, latency, key id; never the key itself), as JSON in
production and readable text elsewhere, and a `request_log` row is stored for every
request but health checks and the API docs. A chat question adds its run to the same row
(`chat.py`).
"""

import logging
import sys
import time
import uuid
from collections.abc import Mapping
from typing import Any

import anyio
import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from talk_is_cheap.config import Environment, Settings
from talk_is_cheap.db.models import RequestLog

REQUEST_ID_HEADER = "X-Request-ID"
UNLOGGED_PATHS = frozenset(
    {"/healthz", "/readyz", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)

log = structlog.get_logger("talk_is_cheap.api")


def configure_logging(settings: Settings) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.environment == Environment.PRODUCTION
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=False,
    )


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestLogMiddleware:
    """Gives each request its id (`request.state.request_id`), adds X-Request-ID to the
    response, and logs and stores the request once it is answered."""

    def __init__(self, app: ASGIApp, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = time.monotonic()
        status = 500  # unless a response starts

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                latency_ms = round((time.monotonic() - started) * 1000)
                key = state.get("key_id")
                path = scope["path"]
                # Health checks and docs at debug level: Docker probes every 15 seconds.
                write = log.debug if path in UNLOGGED_PATHS else log.info
                write(
                    "request",
                    method=scope["method"],
                    path=path,
                    status=status,
                    latency_ms=latency_ms,
                    key_id=key,
                    outcome=state.get("outcome"),
                )
                if path not in UNLOGGED_PATHS:
                    # Stored even when the client went away and the request was cancelled.
                    with anyio.CancelScope(shield=True):
                        await store_request(
                            self.sessions,
                            request_id,
                            {
                                "method": scope["method"],
                                "path": path,
                                "status": status,
                                "latency_ms": latency_ms,
                                "key_id": key,
                                "outcome": state.get("outcome"),
                                "thread_id": state.get("thread_id"),
                            },
                        )


async def store_request(
    sessions: async_sessionmaker[AsyncSession],
    request_id: str,
    values: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any] | None = None,
) -> None:
    """Insert the request's `request_log` row, or add `values` to it: the middleware and
    a chat run each write their part, in either order. `defaults` fill the columns a
    first insert needs and `values` lack. A failure is logged, never raised."""
    try:
        async with sessions() as session, session.begin():
            update = {k: v for k, v in values.items() if v is not None}
            statement = (
                insert(RequestLog)
                .values(request_id=request_id, **{**(defaults or {}), **values})
                .on_conflict_do_update(index_elements=[RequestLog.request_id], set_=update)
            )
            await session.execute(statement)
    except Exception:
        log.exception("request_log not stored", request_id=request_id)
