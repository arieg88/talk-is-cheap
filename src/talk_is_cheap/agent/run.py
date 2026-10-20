"""Answer one question on a thread, and log the run to `agent_runs`."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.agent.evidence import Evidence
from talk_is_cheap.agent.graph import AgentDeps, trace_lines
from talk_is_cheap.agent.llm import PROMPT_VERSIONS
from talk_is_cheap.agent.tools import ToolCall
from talk_is_cheap.db.models import AgentRun

# Each node's prompt version, as stored with every run.
PROMPT_VERSION = " ".join(f"{node}/{version}" for node, version in PROMPT_VERSIONS.items())
# Executor and tool steps run up to 9 times per pass, and a question has up to 3 passes.
RECURSION_LIMIT = 100


@dataclass(frozen=True, slots=True)
class RunResult:
    question: str
    thread_id: str
    answer: str  # rendered, with the source list
    final: str  # with evidence ids, as verified
    sources: list[dict[str, Any]]
    question_type: str
    tool_calls: list[ToolCall]
    usage: dict[str, dict[str, Any]]
    seconds: float
    revisions: int
    failures: list[dict[str, Any]]
    verified: bool
    evidence: dict[str, Evidence]

    def total(self, field: str) -> int:
        return sum(int(line.get(field, 0)) for line in self.usage.values())

    @property
    def cost(self) -> Decimal:
        return sum((Decimal(str(line.get("cost", 0))) for line in self.usage.values()), Decimal(0))

    @property
    def served_from_cache(self) -> bool | None:
        """True when every model call came from the cache, None when there was none."""
        calls = self.total("calls")
        return None if calls == 0 else self.total("cached") == calls

    @property
    def failure_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for failure in self.failures:
            counts[failure["check"]] = counts.get(failure["check"], 0) + 1
        return counts


async def ask(
    graph: Any,
    deps: AgentDeps,
    question: str,
    thread_id: str,
    on_trace: Callable[[str], None] | None = None,
) -> RunResult:
    """Run the graph on one question. `on_trace` receives a line per node step and tool
    call as they happen."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    calls_before = len(deps.tools.calls)
    started = time.monotonic()
    inputs = {"question": question, "messages": [HumanMessage(question)]}
    async for chunk in graph.astream(inputs, config, stream_mode="updates"):
        for node, update in chunk.items():
            if on_trace is not None:
                for line in trace_lines(node, update):
                    on_trace(line)
    seconds = time.monotonic() - started
    state: Mapping[str, Any] = (await graph.aget_state(config)).values
    return RunResult(
        question=question,
        thread_id=thread_id,
        answer=state.get("answer", ""),
        final=state.get("final", ""),
        sources=list(state.get("sources") or []),
        question_type=state.get("question_type", ""),
        tool_calls=deps.tools.calls[calls_before:],
        usage=dict(state.get("usage") or {}),
        seconds=seconds,
        revisions=int(state.get("revision_count", 0)),
        failures=list(state.get("failures") or []),
        verified=bool(state.get("verified")) or state.get("question_type") == "out_of_scope",
        evidence=dict(state.get("evidence") or {}),
    )


async def log_run(sessions: async_sessionmaker[AsyncSession], result: RunResult, model: str) -> int:
    async with sessions() as session, session.begin():
        run = AgentRun(
            thread_id=result.thread_id,
            question=result.question,
            prompt_version=PROMPT_VERSION,
            model=model,
            question_type=result.question_type or None,
            tool_calls=[tool_call_record(c) for c in result.tool_calls],
            llm_calls=result.total("calls"),
            cached_calls=result.total("cached"),
            input_tokens=result.total("input_tokens"),
            output_tokens=result.total("output_tokens"),
            cost=result.cost,
            seconds=Decimal(str(round(result.seconds, 3))),
            revisions=result.revisions,
            verifier_failures=result.failure_counts,
            usage=result.usage,
            verified=result.verified,
            answer=result.answer,
        )
        session.add(run)
        await session.flush()
        return run.id


def tool_call_record(call: ToolCall) -> dict[str, object]:
    return {
        "name": call.name,
        "arguments": json.loads(json.dumps(call.arguments, default=str)),
        "seconds": round(call.seconds, 3),
        "evidence": call.evidence_ids,
        "error": call.error,
    }


def summary_line(result: RunResult) -> str:
    cached = result.served_from_cache
    cache = (
        "no model calls"
        if cached is None
        else (
            "served from cache"
            if cached
            else f"{result.total('cached')} of {result.total('calls')} model calls cached"
        )
    )
    failures = ", ".join(f"{k} {v}" for k, v in sorted(result.failure_counts.items()))
    return (
        f"{len(result.tool_calls)} tool calls, {result.total('input_tokens'):,} input and "
        f"{result.total('output_tokens'):,} output tokens, ${result.cost:.4f}, "
        f"{result.seconds:.1f}s, {result.revisions} revisions"
        + (f" (verifier: {failures})" if failures else "")
        + f", {cache}"
    )
