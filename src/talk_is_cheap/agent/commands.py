"""`talk-is-cheap ask` and `talk-is-cheap agent-graph`, and the setup they share with
`eval agent`: the model, its cache and budget, the tools, the graph and its memory."""

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.agent.graph import AgentDeps, build_graph
from talk_is_cheap.agent.llm import MAX_OUTPUT_TOKENS, ChatModel, LangChainChat
from talk_is_cheap.agent.memory import CheckpointerSchemaError, open_checkpointer
from talk_is_cheap.agent.run import RunResult, ask, log_run, summary_line
from talk_is_cheap.agent.tools import ToolContext
from talk_is_cheap.config import Settings, get_settings
from talk_is_cheap.db import create_engine, create_session_factory
from talk_is_cheap.db.models import AgentRun
from talk_is_cheap.guidance.llm import (
    Budget,
    LlmError,
    Pricing,
    ResponseCache,
    Usage,
    build_chat_model,
    model_label,
    pricing_for,
)
from talk_is_cheap.spend import budget_line, command_budget
from talk_is_cheap.universe import load_universe

# The most model calls one question can make: router, planner, the executor up to 9
# times in each of 3 passes, the verifier 3 times, the answer.
MAX_CALLS_PER_QUESTION = 1 + 1 + 9 * 3 + 3 + 1
# Input of one call at most, generously: the prompt, the conversation and tool results.
MAX_INPUT_TOKENS_PER_CALL = 12_000
# A typical question before any run is measured: 7 calls of 4,000 input and 300 output.
TYPICAL_USAGE = Usage(7 * 4_000, 7 * 300)


class AgentSetupError(RuntimeError):
    pass


@dataclass
class Agent:
    graph: Any
    deps: AgentDeps
    sessions: async_sessionmaker[AsyncSession]
    model_name: str


@asynccontextmanager
async def open_agent(
    settings: Settings,
    model: ChatModel,
    budget: Budget,
    engine: AsyncEngine,
    *,
    cache: ResponseCache | None = None,
) -> AsyncIterator[Agent]:
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker

    sessions = create_session_factory(engine)
    universe = load_universe(settings.universe_file)
    tools = ToolContext(
        sessions=sessions,
        engine=engine,
        universe=universe,
        embedder=lambda: load_embedder(settings),
        reranker=lambda: load_reranker(settings),
    )
    deps = AgentDeps(
        model=model,
        cache=cache or ResponseCache(settings.llm_cache_dir),
        budget=budget,
        tools=tools,
        today=date.today(),
    )
    try:
        async with open_checkpointer(engine) as checkpointer:
            yield Agent(build_graph(deps, checkpointer), deps, sessions, model.name)
    except CheckpointerSchemaError as exc:
        raise AgentSetupError(str(exc)) from exc


def chat_model(settings: Settings, model: str | None) -> LangChainChat:
    label = model_label(settings, model)

    def build(max_tokens: int) -> Any:
        return build_chat_model(settings, model, max_tokens=max_tokens)

    build_chat_model(settings, model)  # fail now on a missing key or package
    return LangChainChat(build, label)


def worst_case_per_question(pricing: Pricing) -> Decimal:
    largest_output = max(MAX_OUTPUT_TOKENS.values())
    return pricing.cost(
        Usage(
            MAX_CALLS_PER_QUESTION * MAX_INPUT_TOKENS_PER_CALL,
            MAX_CALLS_PER_QUESTION * largest_output,
        )
    )


async def typical_per_question(
    sessions: async_sessionmaker[AsyncSession], label: str, pricing: Pricing
) -> tuple[Decimal, str]:
    """The average cost of a question that made paid calls with this model, from
    `agent_runs`, or a rough default before any run."""
    async with sessions() as session:
        row = (
            await session.execute(
                select(func.avg(AgentRun.cost), func.count()).where(
                    AgentRun.model == label, AgentRun.cached_calls < AgentRun.llm_calls
                )
            )
        ).one()
    if row[1]:
        return Decimal(row[0]), f"average of {row[1]} measured runs"
    return pricing.cost(TYPICAL_USAGE), "rough estimate, no run measured yet"


async def print_estimate(
    sessions: async_sessionmaker[AsyncSession],
    budget: Budget,
    label: str,
    questions: int,
) -> None:
    typical, basis = await typical_per_question(sessions, label, budget.pricing)
    worst = worst_case_per_question(budget.pricing)
    print(
        f"estimate for {questions} question{'s' * (questions != 1)} with {label}: about "
        f"${typical * questions:.4f} ({basis}), at most ${worst * questions:.2f}; cached "
        "calls are free; run cap "
        f"${budget.cap}",
        flush=True,
    )
    print(budget_line(budget), flush=True)


def print_result(result: RunResult) -> None:
    print(result.answer)
    print()
    print(summary_line(result), flush=True)


async def ask_command(
    question: str | None,
    thread: str | None,
    model: str | None,
    show_trace: bool,
    max_cost: Decimal | None,
) -> int:
    settings = get_settings()
    label = model_label(settings, model)
    pricing = pricing_for(label, settings)
    if pricing is None:
        print(
            f"no price known for {label}: set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE", file=sys.stderr
        )
        return 1
    try:
        chat = chat_model(settings, model)
    except LlmError as exc:
        print(exc, file=sys.stderr)
        return 1
    engine = create_engine(settings)
    thread_id = thread or uuid.uuid4().hex[:12]
    trace: Callable[[str], None] | None = (
        (lambda line: print(f"  · {line}", flush=True)) if show_trace else None
    )
    try:
        sessions = create_session_factory(engine)
        budget = await command_budget(sessions, settings, pricing, "ask", cap=max_cost)
        await print_estimate(sessions, budget, label, 1)
        async with open_agent(settings, chat, budget, engine) as agent:
            print(f"thread {thread_id}\n", flush=True)
            if question is not None:
                return await _answer(agent, question, thread_id, trace)
            print("Ask a question; an empty line or Ctrl-D ends the chat.\n", flush=True)
            while True:
                try:
                    line = (await asyncio.to_thread(input, "> ")).strip()
                except EOFError:
                    print()
                    break
                if not line:
                    break
                status = await _answer(agent, line, thread_id, trace)
                if status:
                    return status
                print()
            print(budget_line(budget))
            return 0
    except AgentSetupError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


async def _answer(
    agent: Agent, question: str, thread_id: str, trace: Callable[[str], None] | None
) -> int:
    try:
        result = await ask(agent.graph, agent.deps, question, thread_id, trace)
    except LlmError as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    await log_run(agent.sessions, result, agent.model_name)
    return 0


def agent_graph_command(path: Path) -> int:
    """Write the graph as Mermaid. Building it needs no database or model."""
    from langchain_core.messages import AIMessage

    from talk_is_cheap.agent.llm import ScriptedChat

    settings = get_settings()
    unused: Any = None  # nothing runs: the graph is only drawn
    deps = AgentDeps(
        model=ScriptedChat(lambda node, messages, tools: AIMessage("")),
        cache=ResponseCache(settings.llm_cache_dir),
        budget=Budget(Decimal(0), Pricing(Decimal(0), Decimal(0))),
        tools=ToolContext(
            sessions=unused,
            engine=unused,
            universe=load_universe(settings.universe_file),
            embedder=unused,
            reranker=unused,
        ),
        today=date.today(),
    )
    mermaid = build_graph(deps).get_graph().draw_mermaid()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Agent graph\n\n"
        "The graph `talk-is-cheap ask` runs, written by `talk-is-cheap agent-graph`. "
        "Dotted edges are conditional. How each node works is in "
        "[agent.md](agent.md).\n\n"
        f"```mermaid\n{mermaid.strip()}\n```\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0
