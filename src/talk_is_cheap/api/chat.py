"""`POST /v1/chat`: one question on a thread, streamed as Server-Sent Events.

The graph runs in a task of its own that puts events on a queue; the response reads the
queue until the run ends, the request's time limit passes (an `error` event, then the
stream closes) or the client goes away (the run is cancelled). Events:

- `status`: a node starting, or a tool being called, as a readable step;
- `tool_start`, `tool_end`: each tool call, its arguments and a short result summary;
- `token`: the answer, in pieces, once the verifier has passed it (text that has not been
  verified is never sent);
- `citation`: each source the answer's [n] markers point at;
- `error`: a code, the HTTP status it stands for and a message; the last event;
- `done`: request id, tokens, cost, seconds, revisions and whether it came from the cache.

Before any model call, the question's spending is checked against the key's and all
keys' DAILY_LLM_BUDGET_USD (per UTC day) and the ledger's LLM_BUDGET_USD; the run's cap is
what is left of the smallest. Every paid call is recorded in the ledger under
`api chat` with the key's id. A run that stops early is cancelled, a model call in flight
is left to finish so its cost is known and recorded, and the thread's turn is closed so
the next question starts cleanly.
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from datetime import time as day_time
from decimal import Decimal
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import BaseModel

from talk_is_cheap.agent.evidence import EvidenceLog
from talk_is_cheap.agent.graph import AgentDeps, history
from talk_is_cheap.agent.llm import ChatModel, prompt_version, text_of
from talk_is_cheap.agent.run import (
    RunResult,
    log_run,
    run_config,
    run_inputs,
    run_result,
)
from talk_is_cheap.api.app import FREE, Services
from talk_is_cheap.api.observability import log, store_request
from talk_is_cheap.api.schemas import (
    CHAT_EVENTS,
    ChatIn,
    CitationEvent,
    DoneEvent,
    ErrorEvent,
    StatusEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from talk_is_cheap.api.security import limit
from talk_is_cheap.guidance.llm import (
    Budget,
    BudgetExhausted,
    CostCapReached,
    LlmError,
    Usage,
)
from talk_is_cheap.spend import DatabaseLedger, counted_spend, spend_since

CHAT_COMMAND = "api chat"
MAX_TURNS = 30
# A comment line sent while nothing else is, so proxies keep the stream open.
KEEPALIVE_SECONDS = 10.0
# How long a model call in flight may take to finish after its run was stopped.
FINISH_SECONDS = 60.0
STOPPED_ANSWER = "The answer was stopped before it was finished."

NODE_STEPS = {
    "router": "Reading the question…",
    "planner": "Planning the steps…",
    "executor": "Deciding what to look up…",
    "verifier": "Checking the draft against the evidence…",
    "answer": "Writing the answer…",
}
TOOL_STEPS = {
    "resolve_company": "Looking up the company…",
    "get_actuals": "Reading reported financials…",
    "get_guidance": "Reading guidance…",
    "get_statements": "Reading what executives said in the news…",
    "search_filings": "Searching filings…",
    "search_news": "Searching the news…",
    "get_promises": "Reading promises and their verdicts…",
    "get_track_record": "Reading track records…",
    "cite_value": "Citing a figure…",
    "compute": "Computing…",
    "run_sql": "Querying the database…",
}

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post(
    "/chat",
    response_model=None,
    dependencies=[Depends(limit("chat"))],
    responses={
        200: {
            "description": "Server-Sent Events: `status`, `tool_start`, `tool_end`, "
            "`token`, `citation`, then `done`, or `error` as the last event. Each event's "
            "`data` is JSON: "
            + ", ".join(f"`{name}` is {model.__name__}" for name, model in CHAT_EVENTS.items())
            + ".",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        401: {"description": "missing or invalid API key"},
        429: {"description": "rate limit, with Retry-After"},
    },
)
async def chat(body: ChatIn, request: Request) -> EventSourceResponse:
    """Ask a question, optionally on an existing thread. The answer streams as
    Server-Sent Events; `X-Thread-ID` names the thread for follow-ups."""
    services: Services = request.app.state.services
    thread_id = body.thread_id or _new_thread_id()
    request.state.thread_id = thread_id
    run = ChatRun(
        services=services,
        question=body.question,
        thread_id=thread_id,
        request_id=request.state.request_id,
        key_id=request.state.key_id,
        state=request.state,
    )
    return EventSourceResponse(
        run.stream(),
        headers={
            "X-Thread-ID": thread_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _new_thread_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def sse(name: str, event: BaseModel) -> str:
    return f"event: {name}\ndata: {event.model_dump_json()}\n\n"


class Refused(Exception):
    """The question is not run: the error event says why."""

    def __init__(self, event: ErrorEvent) -> None:
        super().__init__(event.message)
        self.event = event


class FinishingChat:
    """The model of one request. When the request is stopped, a call in flight is not
    cancelled but left to finish, so what it cost can be recorded (`finish`)."""

    def __init__(self, model: ChatModel) -> None:
        self._model = model
        self.unfinished: list[tuple[str, asyncio.Future[tuple[AIMessage, Usage]]]] = []

    @property
    def name(self) -> str:
        return self._model.name

    async def respond(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str | None,
        max_tokens: int,
        node: str,
    ) -> tuple[AIMessage, Usage]:
        call = asyncio.ensure_future(
            self._model.respond(messages, tools, tool_choice, max_tokens, node)
        )
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            self.unfinished.append((node, call))
            raise

    async def finish(self, budget: Budget) -> None:
        """Wait for calls left in flight, and count and record what each cost."""
        for node, call in self.unfinished:
            try:
                _, usage = await asyncio.wait_for(call, FINISH_SECONDS)
            except LlmError as exc:
                usage = exc.usage  # type: ignore[assignment]
            except Exception:
                continue
            if usage is None:
                continue
            cost = budget.settle(Decimal(0), usage)
            if budget.ledger is not None:
                await budget.ledger.record(self.name, prompt_version(node), usage, cost)
        self.unfinished.clear()


_END = object()


@dataclass
class ChatRun:
    services: Services
    question: str
    thread_id: str
    request_id: str
    key_id: str
    state: Any  # the request's state: what the request log reads
    queue: asyncio.Queue[object] = field(default_factory=asyncio.Queue)
    outcome: str | None = None
    result: RunResult | None = None
    deps: AgentDeps | None = None
    model: FinishingChat | None = None
    cap_name: str = ""  # the budget the run's cap comes from
    holds_thread: bool = False
    started: float = field(default_factory=time.monotonic)

    async def stream(self) -> AsyncIterator[str]:
        settings = self.services.settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.chat_timeout_seconds
        producer: asyncio.Task[None] | None = None
        try:
            try:
                await self._admit()
            except Refused as refused:
                self._set_outcome(refused.event.code)
                yield sse("error", refused.event)
                return
            producer = asyncio.create_task(self._produce())
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    self._set_outcome("timeout")
                    producer.cancel()
                    yield sse(
                        "error",
                        self._error(
                            "timeout",
                            504,
                            f"the question took longer than {settings.chat_timeout_seconds:g} "
                            "seconds and was stopped",
                        ),
                    )
                    return
                try:
                    item = await asyncio.wait_for(self.queue.get(), min(left, KEEPALIVE_SECONDS))
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is _END:
                    return
                name, event = cast(tuple[str, BaseModel], item)
                yield sse(name, event)
        finally:
            if self.outcome is None:
                self._set_outcome("disconnected")
            if producer is None:
                self._release_thread()
            else:
                producer.cancel()
                # Recording a stopped run may wait for a model call in flight: the
                # stream closes now, the record follows.
                self.services.track(asyncio.create_task(self._finish(producer)))

    def _release_thread(self) -> None:
        if self.holds_thread:
            self.services.busy_threads.discard(self.thread_id)
            self.holds_thread = False

    def _set_outcome(self, outcome: str) -> None:
        self.outcome = outcome
        self.state.outcome = outcome

    def _error(self, code: str, status: int, message: str) -> ErrorEvent:
        return ErrorEvent(
            code=code,  # type: ignore[arg-type]
            status=status,
            message=message,
            request_id=self.request_id,
        )

    async def _admit(self) -> None:
        """Refuse the question before any model call when it cannot run: no model, the
        thread busy or full, or a budget spent. Otherwise prepare its dependencies."""
        services = self.services
        if services.model is None:
            raise Refused(
                self._error(
                    "model_unavailable", 503, f"chat is unavailable: {services.model_problem}"
                )
            )
        if self.thread_id in services.busy_threads:
            raise Refused(
                self._error("thread_busy", 409, "this thread is answering another question; retry")
            )
        services.busy_threads.add(self.thread_id)
        self.holds_thread = True
        snapshot = await services.graph.aget_state(run_config(self.thread_id))
        turns = len(history(snapshot.values.get("messages") or []))
        if turns >= MAX_TURNS:
            raise Refused(
                self._error(
                    "thread_full",
                    409,
                    f"a thread holds at most {MAX_TURNS} questions; start a new thread",
                )
            )
        budget = await self._budget()
        self.model = FinishingChat(services.model)
        base = services.agent
        self.deps = AgentDeps(
            model=self.model,
            cache=base.cache,
            budget=budget,
            tools=replace(base.tools, evidence=EvidenceLog(), calls=[]),
            today=date.today(),
        )

    async def _budget(self) -> Budget:
        settings = self.services.settings
        day = datetime.combine(datetime.now(UTC).date(), day_time(0), tzinfo=UTC)
        async with self.services.sessions() as session:
            ledger_total = await counted_spend(session)
            key_today = await spend_since(session, day, command=CHAT_COMMAND, api_key=self.key_id)
            all_today = await spend_since(session, day, command=CHAT_COMMAND)
        left = {
            f"the daily LLM budget of this key (${settings.daily_llm_budget_usd})": (
                settings.daily_llm_budget_usd - key_today
            ),
            f"the daily LLM budget of all keys (${settings.daily_llm_budget_total_usd})": (
                settings.daily_llm_budget_total_usd - all_today
            ),
            f"LLM_BUDGET_USD (${settings.llm_budget_usd})": (
                settings.llm_budget_usd - ledger_total
            ),
        }
        cap_name, smallest = min(left.items(), key=lambda item: item[1])
        if smallest <= 0:
            raise Refused(
                self._error("budget_exceeded", 402, f"{cap_name} is spent; no model was called")
            )
        self.cap_name = cap_name
        return Budget(
            min(settings.llm_cost_cap, *left.values()),
            self.services.pricing,
            limit=settings.llm_budget_usd,
            spent_before=ledger_total,
            # The fake model costs nothing and is kept out of the ledger.
            ledger=(
                None
                if self.services.pricing == FREE
                else DatabaseLedger(self.services.sessions, CHAT_COMMAND, api_key=self.key_id)
            ),
        )

    async def _produce(self) -> None:
        """Run the graph, putting events on the queue; then the answer, its citations
        and `done`, or an `error`."""
        assert self.deps is not None
        graph = self.services.graph
        put = self.queue.put
        try:
            async for event in graph.astream_events(
                run_inputs(self.question),
                run_config(self.thread_id),
                version="v2",
                context=self.deps,
            ):
                for item in _events(event):
                    await put(item)
            result = await run_result(
                graph, self.question, self.thread_id, self.deps.tools.calls, self.started
            )
            self.result = result
            for piece in _pieces(_body(result)):
                await put(("token", TokenEvent(text=piece)))
            for source in result.sources:
                await put(
                    (
                        "citation",
                        CitationEvent(
                            id=source["number"],
                            url=source["url"],
                            title=source["label"],
                            provenance=source["provenance"],
                            evidence=source["evidence"],
                        ),
                    )
                )
            self._set_outcome("completed")
            # Recorded before `done`, so feedback on the answer finds it at once.
            await asyncio.shield(self._record(result))
            await put(("done", self._done(result)))
        except (BudgetExhausted, CostCapReached) as exc:
            self._set_outcome("budget_exceeded")
            message = (
                str(exc)
                if isinstance(exc, BudgetExhausted)
                else f"the next model call could exceed {self.cap_name}; it was not made"
            )
            await put(("error", self._error("budget_exceeded", 402, message)))
        except LlmError as exc:
            self._set_outcome("model_error")
            await put(("error", self._error("model_error", 502, str(exc))))
        except Exception as exc:
            log.exception("chat run failed", thread_id=self.thread_id)
            self._set_outcome("internal_error")
            await put(("error", self._error("internal_error", 500, f"{type(exc).__name__}: {exc}")))
        finally:
            self.queue.put_nowait(_END)

    def _done(self, result: RunResult) -> DoneEvent:
        return DoneEvent(
            request_id=self.request_id,
            thread_id=self.thread_id,
            question_type=result.question_type,
            input_tokens=result.total("input_tokens"),
            output_tokens=result.total("output_tokens"),
            cost=result.cost,
            seconds=round(result.seconds, 3),
            revisions=result.revisions,
            cache_hit=result.served_from_cache,
            verified=result.verified,
            tool_calls=len(result.tool_calls),
        )

    async def _record(self, result: RunResult) -> None:
        """Log the run to `agent_runs`, and add it to the request's `request_log` row."""
        assert self.model is not None
        run_id = await log_run(self.services.sessions, result, self.model.name)
        await store_request(
            self.services.sessions,
            self.request_id,
            {
                "outcome": self.outcome,
                "thread_id": self.thread_id,
                "key_id": self.key_id,
                "agent_run_id": run_id,
                "model": self.model.name,
                "input_tokens": result.total("input_tokens"),
                "output_tokens": result.total("output_tokens"),
                "cost": result.cost,
                "tool_calls": len(result.tool_calls),
            },
            defaults={"method": "POST", "path": "/v1/chat", "status": 200, "latency_ms": 0},
        )

    async def _finish(self, producer: "asyncio.Task[None]") -> None:
        """Once the run has unwound: for a run stopped early, record what its calls
        cost, close the thread's turn and log what was done. Then free the thread."""
        try:
            await asyncio.gather(producer, return_exceptions=True)
            if self.result is not None or self.deps is None or self.model is None:
                return
            budget = self.deps.budget
            await self.model.finish(budget)
            await self._close_turn()
            stopped = RunResult(
                question=self.question,
                thread_id=self.thread_id,
                answer="",
                final="",
                sources=[],
                question_type="",
                tool_calls=list(self.deps.tools.calls),
                usage={
                    "stopped": {
                        "calls": budget.calls,
                        "cached": 0,
                        "input_tokens": budget.usage.input_tokens,
                        "output_tokens": budget.usage.output_tokens,
                        "cost": float(budget.spent),
                    }
                },
                seconds=time.monotonic() - self.started,
                revisions=0,
                failures=[],
                verified=False,
                evidence={},
            )
            await self._record(stopped)
        except Exception:
            log.exception("stopped chat run not recorded", thread_id=self.thread_id)
        finally:
            self._release_thread()

    async def _close_turn(self) -> None:
        """End the thread's unanswered turn with a note, so its next question is read as
        a question of its own."""
        graph = self.services.graph
        config = run_config(self.thread_id)
        messages = (await graph.aget_state(config)).values.get("messages") or []
        if not messages or (isinstance(messages[-1], AIMessage) and messages[-1].name == "answer"):
            return
        await graph.aupdate_state(
            config,
            {
                "messages": [AIMessage(STOPPED_ANSWER, name="answer")],
                "final": STOPPED_ANSWER,
                "answer": STOPPED_ANSWER,
                "sources": [],
            },
            as_node="answer",
        )


def _events(event: Mapping[str, Any]) -> list[tuple[str, BaseModel]]:
    """The stream events for one of LangGraph's `astream_events`."""
    kind, name = event["event"], event.get("name", "")
    metadata = event.get("metadata") or {}
    if kind == "on_chain_start" and name in NODE_STEPS and metadata.get("langgraph_node") == name:
        return [("status", StatusEvent(node=name, step=NODE_STEPS[name]))]
    if kind == "on_tool_start":
        arguments = event["data"].get("input") or {}
        return [
            ("status", StatusEvent(node="tools", step=TOOL_STEPS.get(name, f"Calling {name}…"))),
            (
                "tool_start",
                ToolStartEvent(
                    call_id=event["run_id"],
                    name=name,
                    arguments=json.loads(json.dumps(arguments, default=str)),
                ),
            ),
        ]
    if kind == "on_tool_end":
        output = event["data"].get("output")
        return [("tool_end", _tool_end(event["run_id"], name, output))]
    return []


def _tool_end(call_id: str, name: str, output: Any) -> ToolEndEvent:
    evidence = []
    error = None
    if isinstance(output, ToolMessage):
        if isinstance(output.artifact, list):
            evidence = [str(item["id"]) for item in output.artifact if isinstance(item, dict)]
        try:
            content = json.loads(text_of(output))
        except ValueError:
            content = {}
        if isinstance(content, dict) and content.get("error"):
            error = str(content["error"])
    if error is not None:
        summary = f"error: {error}"
    elif evidence:
        summary = (
            f"{len(evidence)} evidence item{'s' * (len(evidence) != 1)}: "
            + ", ".join(evidence[:8])
            + ("…" if len(evidence) > 8 else "")
        )
    else:
        summary = "nothing found"
    return ToolEndEvent(call_id=call_id, name=name, summary=summary, evidence=evidence, error=error)


def _body(result: RunResult) -> str:
    """The rendered answer without its source list, which citations carry."""
    marker = "\n\nSources:\n"
    text = result.answer
    return text[: text.rindex(marker)] if result.sources and marker in text else text


def _pieces(text: str) -> list[str]:
    """The answer as token events: a word and the space after it."""
    return re.findall(r"\S+\s*|\s+", text)
