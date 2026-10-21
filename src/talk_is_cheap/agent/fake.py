"""A scripted stand-in for the model, chosen with LLM_PROVIDER=fake: it costs nothing and
needs no key, so the API and the graph can be run against real data without a provider.

It answers every question the same way: the latest quarter's revenue of the first
company the question names, from `get_actuals`, cited. The tools, the verifier's code
checks, citations and memory all run as with a real model.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from talk_is_cheap.agent.llm import ScriptedChat, text_of
from talk_is_cheap.agent.router import companies_in
from talk_is_cheap.universe import Universe

FAKE_MODEL = "scripted@fake"


def fake_chat(universe: Universe) -> ScriptedChat:
    return ScriptedChat(FakeScript(universe), name=FAKE_MODEL)


class FakeScript:
    def __init__(self, universe: Universe) -> None:
        self._universe = universe

    def __call__(
        self, node: str, messages: Sequence[BaseMessage], tools: Sequence[Mapping[str, Any]]
    ) -> AIMessage:
        if node == "router":
            return _call("route", {"question_type": "lookup"})
        if node == "planner":
            return _call("plan", {"steps": ["get the latest quarter's revenue"]})
        if node == "verifier":
            return _call("verdict", {"claims": []})
        if node == "answer":
            draft = text_of(messages[0]).split("Draft:\n", 1)[-1]
            return AIMessage(draft.split("\n\nEvidence cited:")[0].strip())
        return self._executor(messages)

    def _executor(self, messages: Sequence[BaseMessage]) -> AIMessage:
        results = _turn_results(messages)
        if results:
            items = [e for r in results for e in r.get("evidence", []) if "display" in e]
            if not items:
                return AIMessage("I could not find a reported revenue for that company.")
            item = items[-1]
            return AIMessage(
                f"{item['ticker']}'s revenue in {item['period']} was {item['display']} "
                f"[{item['id']}]."
            )
        question = next((text_of(m) for m in reversed(messages) if m.type == "human"), "").split(
            "\n\nPlan:", 1
        )[0]
        named = companies_in(question, self._universe)
        if not named:
            return AIMessage("I could not tell which covered company the question is about.")
        arguments = {"ticker": named[0].ticker, "metrics": ["revenue"], "periods": ["latest"]}
        return _call("get_actuals", arguments)


def _turn_results(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """The tool results since the question."""
    results: list[dict[str, Any]] = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            if results or message.type == "human":
                break
            continue
        results.insert(0, json.loads(text_of(message)))
    return results


def _call(name: str, arguments: dict[str, Any]) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": arguments, "id": f"call_{name}"}])
