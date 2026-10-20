"""Chat model calls of the agent's nodes, through the guidance code's response cache,
budget and ledger (`guidance/llm.py`).

Every call is keyed by model, node prompt version and a hash of the messages, tools and
tool choice, so an identical question re-run is answered from the cache at no cost.
A call that is not cached first reserves its largest possible cost (its input estimated
generously, plus the node's output limit, MAX_OUTPUT_TOKENS) against the run's cap and
the total budget, and is recorded in the ledger when it returns.
"""

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from typing import Any, Protocol

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    message_to_dict,
    messages_from_dict,
)

from talk_is_cheap.guidance.llm import (
    CHARS_PER_TOKEN,
    Budget,
    LlmError,
    ResponseCache,
    Usage,
    record_paid,
)

# Output limit per node, in tokens: also what a call reserves against the budget.
MAX_OUTPUT_TOKENS: Mapping[str, int] = {
    "router": 200,
    "planner": 400,
    "executor": 1_200,
    "verifier": 1_500,
    "answer": 900,
}
# Tokens the provider adds for tool definitions and message framing, rounded up.
INPUT_OVERHEAD_TOKENS = 600

PROMPT_VERSIONS: Mapping[str, str] = {
    "router": "v3",
    "planner": "v3",
    "executor": "v3",
    "verifier": "v2",
    "answer": "v3",
}


def load_prompt(node: str, version: str | None = None) -> str:
    name = f"{version or PROMPT_VERSIONS[node]}.md"
    return (
        files("talk_is_cheap").joinpath("prompts", "agent", node, name).read_text(encoding="utf-8")
    )


def prompt_version(node: str) -> str:
    """The version recorded with each call in the cache and the ledger: "executor/v1"."""
    return f"{node}/{PROMPT_VERSIONS[node]}"


class ChatModel(Protocol):
    """Answers a conversation, optionally with tools. `name` identifies the model and
    its settings in the cache and the ledger."""

    @property
    def name(self) -> str: ...

    async def respond(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str | None,
        max_tokens: int,
        node: str,
    ) -> tuple[AIMessage, Usage]: ...


class LangChainChat:
    """A LangChain chat model with tools bound per call."""

    def __init__(self, build: Callable[[int], Any], name: str) -> None:
        self._build = build  # max_tokens -> chat model
        self._name = name
        self._models: dict[int, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    async def respond(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str | None,
        max_tokens: int,
        node: str,
    ) -> tuple[AIMessage, Usage]:
        if max_tokens not in self._models:
            self._models[max_tokens] = self._build(max_tokens)
        model = self._models[max_tokens]
        if tools:
            model = model.bind_tools(list(tools), tool_choice=tool_choice)
        message = await model.ainvoke(list(messages))
        if not isinstance(message, AIMessage):
            raise LlmError(f"the model returned {type(message).__name__}, not a message")
        metadata: Mapping[str, Any] = message.usage_metadata or {}
        return message, Usage(
            int(metadata.get("input_tokens", 0)), int(metadata.get("output_tokens", 0))
        )


Script = Callable[[str, Sequence[BaseMessage], Sequence[Mapping[str, Any]]], AIMessage]


class ScriptedChat:
    """A stand-in model that answers from a script: `script(node, messages, tools)`.
    Used by tests and by `eval agent --dry-run`; it costs nothing."""

    def __init__(self, script: Script, name: str = "scripted@test") -> None:
        self._script = script
        self._name = name
        self.calls: list[tuple[str, list[BaseMessage]]] = []

    @property
    def name(self) -> str:
        return self._name

    async def respond(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: str | None,
        max_tokens: int,
        node: str,
    ) -> tuple[AIMessage, Usage]:
        self.calls.append((node, list(messages)))
        reply = self._script(node, messages, tools)
        size = sum(len(str(m.content)) for m in messages)
        return reply, Usage(math.ceil(size / 4), math.ceil(len(str(reply.content)) / 4) + 20)


@dataclass(frozen=True, slots=True)
class Reply:
    message: AIMessage
    usage: Usage
    cost: Decimal
    cached: bool


def request_key(
    messages: Sequence[BaseMessage], tools: Sequence[Mapping[str, Any]], tool_choice: str | None
) -> str:
    """What identifies a request in the cache: the messages (their type, name, content and
    tool calls, not the ids the graph gives them), the tools and the tool choice."""
    return json.dumps(
        {
            "messages": [_message_key(m) for m in messages],
            "tools": list(tools),
            "tool_choice": tool_choice,
        },
        sort_keys=True,
        default=str,
    )


def _message_key(message: BaseMessage) -> dict[str, Any]:
    key: dict[str, Any] = {"type": message.type, "content": message.content}
    if message.name:
        key["name"] = message.name
    if isinstance(message, AIMessage) and message.tool_calls:
        key["tool_calls"] = [
            {"id": c["id"], "name": c["name"], "args": c["args"]} for c in message.tool_calls
        ]
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        key["tool_call_id"] = tool_call_id
    return key


def estimate_usage(key: str, node: str) -> Usage:
    """The largest usage a call can have: its input, estimated generously from the
    request's length, and the node's output limit."""
    return Usage(
        INPUT_OVERHEAD_TOKENS + math.ceil(len(key) / CHARS_PER_TOKEN), MAX_OUTPUT_TOKENS[node]
    )


async def chat(
    model: ChatModel,
    node: str,
    messages: Sequence[BaseMessage],
    cache: ResponseCache,
    budget: Budget,
    *,
    tools: Sequence[Mapping[str, Any]] = (),
    tool_choice: str | None = None,
) -> Reply:
    """The cached reply to this request, or a new call within the budget."""
    version = prompt_version(node)
    key = request_key(messages, tools, tool_choice)
    found = cache.read(model.name, version, key)
    if found is not None:
        message = messages_from_dict([found["message"]])[0]
        assert isinstance(message, AIMessage)
        usage = Usage(found["input_tokens"], found["output_tokens"])
        return Reply(message, usage, Decimal(found["cost"]), cached=True)
    reservation = budget.reserve_usage(estimate_usage(key, node))
    try:
        message, usage = await model.respond(
            messages, tools, tool_choice, MAX_OUTPUT_TOKENS[node], node
        )
    except LlmError as exc:
        await record_paid(budget, model.name, version, reservation, exc.usage)
        raise
    except BaseException:
        budget.settle(reservation, None)
        raise
    cost = await record_paid(budget, model.name, version, reservation, usage)
    cache.write(
        model.name,
        version,
        key,
        {
            "model": model.name,
            "prompt_version": version,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost": str(cost),
            "message": message_to_dict(message),
        },
    )
    return Reply(message, usage, cost, cached=False)


def text_of(message: BaseMessage) -> str:
    """A message's text, without tool-use blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)
