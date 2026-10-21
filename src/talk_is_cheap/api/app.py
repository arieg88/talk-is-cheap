"""The HTTP API: `create_app(settings)`.

The lifespan handler opens the database pool and the agent's checkpointer, warms the
search models and compiles the agent's graph once; each chat request then runs that graph
with its own budget, evidence log and tool log (`chat.py`). Routes are a thin layer over
the project's query functions (`routes.py`).
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap import __version__
from talk_is_cheap.agent.graph import AgentDeps, build_graph
from talk_is_cheap.agent.llm import ChatModel
from talk_is_cheap.agent.memory import open_checkpointer
from talk_is_cheap.agent.tools import ToolContext
from talk_is_cheap.api.observability import (
    REQUEST_ID_HEADER,
    RequestLogMiddleware,
    configure_logging,
    log,
)
from talk_is_cheap.api.schemas import CHAT_EVENTS
from talk_is_cheap.api.security import Keys, RateLimiter
from talk_is_cheap.config import Environment, Settings
from talk_is_cheap.db import create_engine, create_session_factory
from talk_is_cheap.db.health import migration_head
from talk_is_cheap.guidance.llm import Budget, LlmError, Pricing, ResponseCache, pricing_for
from talk_is_cheap.universe import Universe, load_universe

if TYPE_CHECKING:
    from talk_is_cheap.retrieval.encoders import Embedder, Reranker

ALEMBIC_INI = Path("alembic.ini")
FREE = Pricing(Decimal(0), Decimal(0))


class ApiConfigError(RuntimeError):
    """The settings do not allow the API to start."""


@dataclass(frozen=True, slots=True)
class Overrides:
    """What tests (and nothing else) replace: the model, the universe, the search
    models and the response cache."""

    model: ChatModel | None = None
    universe: Universe | None = None
    embedder: "Callable[[], Embedder] | None" = None
    reranker: "Callable[[], Reranker] | None" = None
    cache: ResponseCache | None = None


@dataclass
class Services:
    """What the lifespan opens, shared by all requests."""

    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    graph: Any
    agent: AgentDeps  # the graph's own dependencies; each chat request gets a copy
    model: ChatModel | None  # None: no LLM key configured, chat is unavailable
    model_problem: str | None
    pricing: Pricing
    universe: Universe
    head: str | None
    # Threads with a question running (or finishing), so a thread runs one at a time.
    busy_threads: set[str] = field(default_factory=set)
    # Chat runs stopped early and still recording what they spent.
    finishing: set["asyncio.Task[None]"] = field(default_factory=set)

    def track(self, task: "asyncio.Task[None]") -> None:
        self.finishing.add(task)
        task.add_done_callback(self.finishing.discard)

    async def settle(self) -> None:
        """Wait for stopped runs to finish recording."""
        while self.finishing:
            await asyncio.gather(*list(self.finishing), return_exceptions=True)


def check_settings(settings: Settings) -> None:
    """Raise ApiConfigError when the settings cannot run the API: no API_KEYS, or the
    fake model, in production."""
    if settings.environment == Environment.PRODUCTION:
        if not settings.api_keys:
            raise ApiConfigError("API_KEYS must be set in production")
        if settings.llm_provider == "fake":
            raise ApiConfigError("LLM_PROVIDER=fake is not allowed in production")


def create_app(settings: Settings, overrides: Overrides | None = None) -> FastAPI:
    """The API for these settings (`check_settings` first)."""
    check_settings(settings)
    overrides = overrides or Overrides()
    keys = Keys(settings.api_keys)
    configure_logging(settings)
    engine = create_engine(settings)
    sessions = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with open_services(settings, overrides, engine, sessions) as services:
                app.state.services = services
                log.info(
                    "api started",
                    environment=settings.environment.value,
                    model=services.model.name if services.model else None,
                    keys=len(settings.api_keys),
                )
                try:
                    yield
                finally:
                    await services.settle()
        finally:
            await engine.dispose()

    app = FastAPI(
        title="talk-is-cheap",
        version=__version__,
        summary="What executives said in public, judged against what happened.",
        lifespan=lifespan,
    )
    app.state.keys = keys
    app.state.limiters = {
        "chat": RateLimiter(settings.rate_limit_chat_per_minute),
        "default": RateLimiter(settings.rate_limit_per_minute),
    }
    app.add_middleware(RequestLogMiddleware, sessions=sessions)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "Content-Type"],
            expose_headers=[REQUEST_ID_HEADER, "Retry-After", "X-Thread-ID"],
        )

    from talk_is_cheap.api import chat, routes

    app.include_router(routes.health)
    app.include_router(routes.data)
    app.include_router(chat.router)
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]
    return app


@asynccontextmanager
async def open_services(
    settings: Settings,
    overrides: Overrides,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Services]:
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker

    universe = overrides.universe or load_universe(settings.universe_file)
    model, problem = overrides.model, None
    if model is None:
        try:
            model = build_model(settings, universe)
        except LlmError as exc:
            problem = str(exc)
            log.warning("chat unavailable", reason=problem)
    pricing = FREE
    if model is not None:
        known = pricing_for(model.name, settings)
        if known is None and not model.name.startswith("scripted@"):
            raise ApiConfigError(
                f"no price known for {model.name}: set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE"
            )
        pricing = known or FREE
    tools = ToolContext(
        sessions=sessions,
        engine=engine,
        universe=universe,
        embedder=overrides.embedder or (lambda: load_embedder(settings)),
        reranker=overrides.reranker or (lambda: load_reranker(settings)),
        tool_timeout_seconds=settings.tool_timeout_seconds,
    )
    # Loaded now, so the first search does not spend its time limit loading them; if
    # they cannot be (no network for the first download), the first search tries again.
    try:
        await asyncio.to_thread(tools.loaded_embedder)
        await asyncio.to_thread(tools.loaded_reranker)
    except Exception as exc:
        log.warning("search models not loaded", reason=f"{type(exc).__name__}: {exc}")
    agent = AgentDeps(
        model=model or _unavailable(),
        cache=overrides.cache or ResponseCache(settings.llm_cache_dir),
        budget=Budget(Decimal(0), pricing),
        tools=tools,
        today=date.today(),
    )
    async with open_checkpointer(engine, pool_size=settings.database_pool_size) as checkpointer:
        yield Services(
            settings=settings,
            engine=engine,
            sessions=sessions,
            graph=build_graph(agent, checkpointer),
            agent=agent,
            model=model,
            model_problem=problem,
            pricing=pricing,
            universe=universe,
            head=migration_head(ALEMBIC_INI),
        )


def build_model(settings: Settings, universe: Universe) -> ChatModel:
    """The configured model: the scripted one for LLM_PROVIDER=fake. Raises LlmError
    without a key."""
    if settings.llm_provider == "fake":
        from talk_is_cheap.agent.fake import fake_chat

        return fake_chat(universe)
    from talk_is_cheap.agent.commands import chat_model

    return chat_model(settings, None)


def _unavailable() -> ChatModel:
    from langchain_core.messages import AIMessage

    from talk_is_cheap.agent.llm import ScriptedChat

    # Never called: chat requests are refused while no model is configured.
    return ScriptedChat(lambda node, messages, tools: AIMessage(""), name="unavailable")


def _openapi(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document, with the chat stream's event models among its schemas."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        routes=app.routes,
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in CHAT_EVENTS.values():
        found = TypeAdapter(model).json_schema(ref_template="#/components/schemas/{model}")
        for name, definition in found.pop("$defs", {}).items():
            components.setdefault(name, definition)
        components[model.__name__] = found
    app.openapi_schema = schema
    return schema
