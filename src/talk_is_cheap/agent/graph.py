"""The agent as a LangGraph graph:

    router -> (planner ->) executor <-> tools -> verifier -> answer
                                ^                    |
                                +---- revision ------+

- router: deterministic rules (`agent/router.py`), the model only when they cannot
  decide. Out-of-scope questions go straight to the answer, with no tool call.
- planner: a few steps, for trend and comparison questions only.
- executor: a tool-calling loop (LangGraph's ToolNode) that adds every result to the
  evidence and stops after MAX_TOOL_CALLS calls per question.
- verifier: the code checks (`agent/checks.py`), then the model, only if they pass. A
  failure goes back to the executor with feedback, at most MAX_REVISIONS times; then the
  answer keeps the verified sentences and says what could not be verified. Every failure
  records the check that caught it.
- answer: the final text with [1]-style markers and a numbered source list. The answer
  model sees only the evidence the draft cites.

State is checkpointed per thread, so a follow-up ("and the quarter before?") sees the
earlier questions, answers and evidence.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from talk_is_cheap.agent.checks import Failure, cited_ids, code_checks, sentences
from talk_is_cheap.agent.evidence import Evidence, compact, sources_of
from talk_is_cheap.agent.llm import ChatModel, Reply, chat, load_prompt, text_of
from talk_is_cheap.agent.router import (
    QUESTION_TYPES,
    QuestionType,
    describe,
    out_of_scope_answer,
    route_by_rules,
)
from talk_is_cheap.agent.tools import TOOLS, ToolContext, call_tool
from talk_is_cheap.guidance.llm import Budget, ResponseCache

MAX_TOOL_CALLS = 8
MAX_REVISIONS = 2
# What a tool result shows of itself in a trace.
TRACE_CHARS = 300


def merge_evidence(old: Mapping[str, Evidence], new: Mapping[str, Evidence]) -> dict[str, Evidence]:
    return {**old, **new}


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    question_type: QuestionType
    route_reason: str
    trading: bool
    plan: list[str]
    evidence: Annotated[dict[str, Evidence], merge_evidence]
    draft: str
    verifier_feedback: str
    revision_count: int
    verified: bool
    next_step: str  # where the verifier sends the draft: "executor" or "answer"
    failures: list[dict[str, Any]]  # this question's verifier failures, with their check
    unverified: list[str]  # problems left when the revisions ran out
    tool_calls: int  # this question's
    usage: dict[str, dict[str, Any]]  # this question's, per node
    final: str  # the answer with evidence ids, before rendering
    answer: str
    sources: list[dict[str, Any]]


class RouteDecision(BaseModel):
    """Route the question."""

    question_type: Literal["lookup", "guidance_check", "trend", "comparison", "out_of_scope"]


class PlanSteps(BaseModel):
    """The plan."""

    steps: list[str] = Field(max_length=6)


class ClaimCheck(BaseModel):
    claim: str
    verdict: Literal["supported", "unsupported", "prefer_primary"]
    reason: str = ""


class VerifierVerdict(BaseModel):
    """The verdict on each claim of the draft."""

    claims: list[ClaimCheck] = Field(default_factory=list)


def _schema(model: type[BaseModel], name: str) -> dict[str, Any]:
    tool = convert_to_openai_tool(model)
    tool["function"]["name"] = name
    return tool


ROUTE_TOOL = _schema(RouteDecision, "route")
PLAN_TOOL = _schema(PlanSteps, "plan")
VERDICT_TOOL = _schema(VerifierVerdict, "verdict")


@dataclass
class AgentDeps:
    """What one run of the graph uses: the model, the cache and budget every call goes
    through, the tools' context and today's date."""

    model: ChatModel
    cache: ResponseCache
    budget: Budget
    tools: ToolContext
    today: date


def build_tools(ctx: ToolContext) -> list[StructuredTool]:
    """The tools as LangChain tools: what the model reads is the JSON content, the
    evidence items travel as the tool message's artifact."""

    def make(name: str) -> StructuredTool:
        function, schema = TOOLS[name]

        async def run(**arguments: Any) -> tuple[str, list[Evidence]]:
            output = await call_tool(ctx, name, arguments)
            content = json.dumps(output.content, default=str, separators=(",", ":"))
            return content, output.evidence

        return StructuredTool.from_function(
            coroutine=run,
            name=name,
            description=" ".join((function.__doc__ or "").split()),
            args_schema=schema,
            response_format="content_and_artifact",
        )

    return [make(name) for name in TOOLS]


def tool_schemas(tools: Sequence[StructuredTool]) -> list[dict[str, Any]]:
    return [convert_to_openai_tool(t) for t in tools]


def build_graph(deps: AgentDeps, checkpointer: Any = None) -> Any:
    tools = build_tools(deps.tools)
    schemas = tool_schemas(tools)
    universe = deps.tools.universe
    covered = [c for c in universe.companies if not c.news_only]
    companies = describe(covered)

    async def call(
        state: AgentState, node: str, messages: Sequence[BaseMessage], **options: Any
    ) -> tuple[Reply, dict[str, dict[str, Any]]]:
        reply = await chat(deps.model, node, messages, deps.cache, deps.budget, **options)
        usage = {k: dict(v) for k, v in (state.get("usage") or {}).items()}
        line = usage.setdefault(
            node, {"calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        )
        line["calls"] += 1
        line["cached"] += int(reply.cached)
        if not reply.cached:
            line["input_tokens"] += reply.usage.input_tokens
            line["output_tokens"] += reply.usage.output_tokens
            line["cost"] += float(reply.cost)
        return reply, usage

    async def router(state: AgentState) -> dict[str, Any]:
        question = state["question"]
        deps.tools.evidence.reset(state.get("evidence") or {})
        history = _history(state.get("messages") or [])
        previous: QuestionType | None = state.get("question_type") if history else None
        route = route_by_rules(question, universe, previous)
        update: dict[str, Any] = {
            "plan": [],
            "draft": "",
            "verifier_feedback": "",
            "revision_count": 0,
            "verified": False,
            "failures": [],
            "unverified": [],
            "tool_calls": 0,
            "usage": {},
            "final": "",
            "answer": "",
            "sources": [],
            "next_step": "",
        }
        if route is not None:
            return update | {
                "question_type": route.question_type,
                "route_reason": f"rules: {route.reason}",
                "trading": route.trading,
            }
        prompt = load_prompt("router").format(
            companies=companies, history=_history_text(history) or "nothing"
        )
        reply, usage = await call(
            update,  # type: ignore[arg-type]
            "router",
            [SystemMessage(prompt), HumanMessage(question)],
            tools=[ROUTE_TOOL],
            tool_choice="route",
        )
        chosen = _tool_args(reply.message, "route").get("question_type")
        question_type: QuestionType = chosen if chosen in QUESTION_TYPES else "lookup"
        return update | {
            "question_type": question_type,
            "route_reason": "model",
            "trading": False,
            "usage": usage,
        }

    async def planner(state: AgentState) -> dict[str, Any]:
        prompt = load_prompt("planner").format(
            question_type=state["question_type"], today=deps.today.isoformat()
        )
        history = _history_text(_history(state.get("messages") or []))
        question = state["question"] + (
            f"\n\nEarlier in this conversation:\n{history}" if history else ""
        )
        reply, usage = await call(
            state,
            "planner",
            [SystemMessage(prompt), HumanMessage(question)],
            tools=[PLAN_TOOL],
            tool_choice="plan",
        )
        steps = [str(s) for s in _tool_args(reply.message, "plan").get("steps", [])][:6]
        return {"plan": steps, "usage": usage}

    async def executor(state: AgentState) -> dict[str, Any]:
        messages = state.get("messages") or []
        harvested = _harvest(messages)
        deps.tools.evidence.sync({**(state.get("evidence") or {}), **harvested})
        used = state.get("tool_calls", 0)
        remaining = MAX_TOOL_CALLS - used
        prompt = load_prompt("executor").format(companies=companies, today=deps.today.isoformat())
        conversation = _executor_messages(messages, state.get("plan") or [])
        if remaining <= 0:
            conversation.append(
                HumanMessage(
                    f"You have used all {MAX_TOOL_CALLS} tool calls. Write the answer now "
                    "from the evidence you have, and say what you could not find."
                )
            )
        reply, usage = await call(
            state, "executor", [SystemMessage(prompt), *conversation], tools=schemas
        )
        message = reply.message
        update: dict[str, Any] = {"evidence": harvested, "usage": usage}
        if message.tool_calls and remaining > 0:
            keep = message.tool_calls[:remaining]
            if len(keep) < len(message.tool_calls):
                message = _with_tool_calls(message, keep)
            return update | {"messages": [message], "tool_calls": used + len(keep)}
        if message.tool_calls:
            message = AIMessage(content=text_of(message))
        draft = text_of(message).strip() or "I could not find an answer in the stored data."
        return update | {"messages": [message], "draft": draft}

    async def verifier(state: AgentState) -> dict[str, Any]:
        draft = state.get("draft", "")
        evidence = state.get("evidence") or {}
        revision = state.get("revision_count", 0)
        failures = code_checks(draft, evidence, state["question"])
        usage = state.get("usage") or {}
        if not failures:
            cited = [evidence[i] for i in cited_ids(draft) if i in evidence]
            prompt = load_prompt("verifier").format(
                question=state["question"],
                draft=draft,
                evidence=json.dumps(compact(cited), default=str, indent=None),
            )
            reply, usage = await call(
                state,
                "verifier",
                [SystemMessage(prompt), HumanMessage("Check the draft.")],
                tools=[VERDICT_TOOL],
                tool_choice="verdict",
            )
            failures = _model_failures(_verdict(reply.message), cited)
        recorded = list(state.get("failures") or []) + [
            {"check": f.check, "detail": f.detail, "revision": revision} for f in failures
        ]
        if not failures:
            return {"verified": True, "next_step": "answer", "failures": recorded, "usage": usage}
        if revision < MAX_REVISIONS:
            feedback = _feedback(failures)
            return {
                "verified": False,
                "next_step": "executor",
                "verifier_feedback": feedback,
                "revision_count": revision + 1,
                "failures": recorded,
                "messages": [HumanMessage(feedback)],
                "usage": usage,
            }
        return {
            "verified": False,
            "next_step": "answer",
            "verifier_feedback": _feedback(failures),
            "failures": recorded,
            "unverified": [f.detail for f in failures],
            "usage": usage,
        }

    async def answer(state: AgentState) -> dict[str, Any]:
        question = state["question"]
        if state["question_type"] == "out_of_scope":
            text = out_of_scope_answer(question, universe, trading=state.get("trading", False))
            return {
                "final": text,
                "answer": text,
                "sources": [],
                "messages": [AIMessage(text, name="answer")],
            }
        evidence = state.get("evidence") or {}
        draft = state.get("draft", "")
        usage = state.get("usage") or {}
        if state.get("verified"):
            final = draft
            cited = [evidence[i] for i in cited_ids(draft) if i in evidence]
            if cited:
                prompt = load_prompt("answer").format(
                    question=question,
                    draft=draft,
                    evidence=json.dumps(compact(cited), default=str),
                )
                reply, usage = await call(
                    state, "answer", [SystemMessage(prompt), HumanMessage("Write the answer.")]
                )
                edited = text_of(reply.message).strip()
                if edited and _keeps_grounding(edited, draft, evidence, question):
                    final = edited
        else:
            final = _verified_part(draft, evidence, question, state.get("failures") or [])
        final = _with_notes(final, evidence)
        text, sources = render(final, evidence)
        return {
            "final": final,
            "answer": text,
            "sources": sources,
            "messages": [AIMessage(final, name="answer")],
            "usage": usage,
        }

    def after_router(state: AgentState) -> str:
        kind = state["question_type"]
        if kind == "out_of_scope":
            return "answer"
        return "planner" if kind in ("trend", "comparison") else "executor"

    def after_executor(state: AgentState) -> str:
        last = (state.get("messages") or [])[-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "verifier"

    def after_verifier(state: AgentState) -> str:
        return state.get("next_step") or "answer"

    graph = StateGraph(AgentState)
    graph.add_node("router", router)
    graph.add_node("planner", planner)
    graph.add_node("executor", executor)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("verifier", verifier)
    graph.add_node("answer", answer)
    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router", after_router, {"answer": "answer", "planner": "planner", "executor": "executor"}
    )
    graph.add_edge("planner", "executor")
    graph.add_conditional_edges(
        "executor", after_executor, {"tools": "tools", "verifier": "verifier"}
    )
    graph.add_edge("tools", "executor")
    graph.add_conditional_edges(
        "verifier", after_verifier, {"executor": "executor", "answer": "answer"}
    )
    graph.add_edge("answer", END)
    return graph.compile(checkpointer=checkpointer)


# Conversation.


def _current_turn_start(messages: Sequence[BaseMessage]) -> int:
    """Index of the current question: the last human message that follows an answer
    (or the first message)."""
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.name == "answer":
            start = index + 1
    return start


def _history(messages: Sequence[BaseMessage]) -> list[tuple[str, str]]:
    """Earlier questions and answers of the conversation."""
    pairs = []
    question = None
    for message in messages[: _current_turn_start(messages)]:
        if isinstance(message, HumanMessage) and question is None:
            question = text_of(message)
        elif isinstance(message, AIMessage) and message.name == "answer" and question:
            pairs.append((question, text_of(message)))
            question = None
    return pairs


def _history_text(history: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"Q: {q}\nA: {a}" for q, a in history[-3:])


def _executor_messages(messages: Sequence[BaseMessage], plan: Sequence[str]) -> list[BaseMessage]:
    """Earlier questions and answers (without their tool calls), then the current turn,
    with the plan added to its question."""
    conversation: list[BaseMessage] = []
    for question, answer in _history(messages)[-3:]:
        conversation += [HumanMessage(question), AIMessage(answer)]
    current = list(messages[_current_turn_start(messages) :])
    if current and isinstance(current[0], HumanMessage) and plan:
        steps = "\n".join(f"{n}. {s}" for n, s in enumerate(plan, start=1))
        current[0] = HumanMessage(f"{text_of(current[0])}\n\nPlan:\n{steps}")
    return conversation + current


def _harvest(messages: Sequence[BaseMessage]) -> dict[str, Evidence]:
    """Evidence carried by this turn's tool messages."""
    found: dict[str, Evidence] = {}
    for message in messages[_current_turn_start(messages) :]:
        if isinstance(message, ToolMessage) and isinstance(message.artifact, list):
            for item in message.artifact:
                if isinstance(item, dict) and "id" in item:
                    found[item["id"]] = item
    return found


def _with_tool_calls(message: AIMessage, keep: Sequence[Any]) -> AIMessage:
    ids = {call["id"] for call in keep}
    content: str | list[Any] = message.content
    if isinstance(content, list):
        content = [
            b
            for b in content
            if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in ids)
        ]
    return AIMessage(content=content, tool_calls=list(keep), id=message.id)


def _tool_args(message: AIMessage, name: str) -> dict[str, Any]:
    for call in message.tool_calls:
        if call["name"] == name:
            return dict(call["args"])
    return {}


def _verdict(message: AIMessage) -> VerifierVerdict:
    try:
        return VerifierVerdict.model_validate(_tool_args(message, "verdict"))
    except ValueError:
        return VerifierVerdict(
            claims=[ClaimCheck(claim="(draft)", verdict="unsupported", reason="no usable verdict")]
        )


def _model_failures(verdict: VerifierVerdict, cited: Sequence[Evidence]) -> list[Failure]:
    """The claims the verifier model did not find supported. Preferring a primary source
    only applies when the draft cites news at all."""
    cites_news = any(item["provenance"] == "news_report" for item in cited)
    return [
        Failure(f"llm_{c.verdict}", f"{c.claim}: {c.reason}".rstrip(": "))
        for c in verdict.claims
        if c.verdict == "unsupported" or (c.verdict == "prefer_primary" and cites_news)
    ]


def _feedback(failures: Sequence[Failure]) -> str:
    lines = "\n".join(f"- [{f.check}] {f.detail}" for f in failures[:8])
    return (
        "The verifier rejected the draft:\n"
        f"{lines}\n"
        "Fix these: get missing figures with a tool, use compute for arithmetic, cite every "
        "sentence with a number, drop claims the evidence does not support. Then write the "
        "full answer again."
    )


# Answer.


def _keeps_grounding(
    edited: str, draft: str, evidence: Mapping[str, Evidence], question: str
) -> bool:
    """Whether an edited answer still passes the code checks and cites nothing the
    draft did not."""
    if code_checks(edited, evidence, question):
        return False
    return set(cited_ids(edited)) <= set(cited_ids(draft))


def _verified_part(
    draft: str,
    evidence: Mapping[str, Evidence],
    question: str,
    failures: Sequence[Mapping[str, Any]],
) -> str:
    """The sentences of a draft that passed every check, and what could not be verified."""
    last = max((f["revision"] for f in failures), default=0)
    problems = [f for f in failures if f["revision"] == last]
    claims = [
        _plain_words(f["detail"].split(":", 1)[0])
        for f in problems
        if f["check"].startswith("llm_")
    ]
    kept, dropped = [], 0
    for sentence in sentences(draft):
        words = _plain_words(sentence)
        if code_checks(sentence, evidence, question) or any(
            claim and SequenceMatcher(None, claim, words).ratio() >= CLAIM_MATCH for claim in claims
        ):
            dropped += 1
            continue
        kept.append(sentence)
    reasons = ", ".join(sorted({_REASONS.get(f["check"], "unsupported") for f in problems}))
    text = " ".join(kept) if kept else "I could not verify an answer from the stored data."
    if dropped:
        statements = "one statement" if dropped == 1 else f"{dropped} statements"
        text += f"\n\nLeft out because they could not be verified ({reasons}): {statements}."
    return text


# How closely a sentence must match a claim the verifier model named to be left out.
CLAIM_MATCH = 0.6
# Why a statement was left out, without the figures that could not be verified.
_REASONS = {
    "numbers_grounded": "a figure not in the evidence",
    "numbers_cited": "a figure without a source",
    "citations_exist": "a citation that does not resolve",
    "llm_unsupported": "not supported by the cited evidence",
    "llm_prefer_primary": "cited only from news",
}


def _plain_words(text: str) -> str:
    return " ".join(re.sub(r"\[E\d+(?:\s*,\s*E\d+)*\]", " ", text).lower().split())


_GUIDANCE_WORDS = re.compile(r"\b(guid\w*|outlook|expect\w*|forecast\w*)\b", re.IGNORECASE)


def _with_notes(text: str, evidence: Mapping[str, Evidence]) -> str:
    """Notes the answer must carry: guidance rows flagged for review, and guidance or
    statements that only news reports."""
    cited = [evidence[i] for i in cited_ids(text) if i in evidence]
    notes = []
    for item in cited:
        review = item["data"].get("review") if item["tool"] == "get_guidance" else None
        if review and "review" not in text.lower() and "flagged" not in text.lower():
            data = item["data"]
            notes.append(
                f"Note: the {data['ticker']} {data['metric']} guidance for {data['target']} "
                f"was {review} [{item['id']}]."
            )
    lines = []
    for sentence in sentences(text):
        ids = cited_ids(sentence)
        items = [evidence[i] for i in ids if i in evidence]
        news = [i for i in items if i["provenance"] == "news_report"]
        statements = any(i["tool"] in ("get_statements", "get_promises") for i in news)
        if items and len(news) == len(items) and (statements or _GUIDANCE_WORDS.search(sentence)):
            publishers = ", ".join(
                sorted({str(i["data"].get("publisher") or "a news outlet") for i in news})
            )
            if "reported by" not in sentence.lower():
                sentence = _before_citations(sentence, f" (reported by {publishers})")
        lines.append(sentence)
    joined = _rejoin(text, lines)
    return "\n\n".join([joined, *notes]) if notes else joined


def _before_citations(sentence: str, words: str) -> str:
    """`words` put before the sentence's closing citations ("... $12 billion (reported by
    Reuters) [E5].")."""
    end = re.search(r"(?:\s*\[E\d+(?:\s*,\s*E\d+)*\])+\s*[.!?]?\s*$", sentence)
    if end is None:
        return sentence.rstrip(".") + words + "."
    return sentence[: end.start()] + words + sentence[end.start() :]


def _rejoin(original: str, lines: Sequence[str]) -> str:
    """Sentences put back together, keeping the original's line breaks between them."""
    if not lines:
        return original
    if "\n" not in original:
        return " ".join(lines)
    out: list[str] = []
    position = 0
    for line in lines:
        head = line.split(" (reported by ")[0][:40]
        found = original.find(head, position)
        if out and found > position and "\n" in original[position:found]:
            out.append("\n" + line)
        elif out:
            out.append(" " + line)
        else:
            out.append(line)
        if found >= 0:
            position = found + len(head)
    return "".join(out)


def render(text: str, evidence: Mapping[str, Evidence]) -> tuple[str, list[dict[str, Any]]]:
    """[E3]-style citations as [1]-style markers, and the numbered source list. A
    computed result is cited by the items it was computed from; items from the same
    document and place share a number."""
    numbers: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], int] = {}

    def number_of(item: Evidence) -> int:
        if item["id"] in numbers:
            return numbers[item["id"]]
        label = source_label(item)
        key = (item["url"], label)
        if key not in by_key:
            by_key[key] = len(sources) + 1
            sources.append(
                {
                    "number": by_key[key],
                    "label": label,
                    "url": item["url"],
                    "provenance": item["provenance"],
                    "evidence": [],
                }
            )
        numbers[item["id"]] = by_key[key]
        sources[by_key[key] - 1]["evidence"].append(item["id"])
        return by_key[key]

    def replace(match: re.Match[str]) -> str:
        marks: list[int] = []
        for evidence_id in re.findall(r"E\d+", match.group(1)):
            item = evidence.get(evidence_id)
            if item is None:
                continue
            for source in sources_of(item, evidence):
                n = number_of(source)
                if n not in marks:
                    marks.append(n)
        return "".join(f"[{n}]" for n in sorted(marks))

    rendered = re.sub(r"\[(E\d+(?:\s*,\s*E\d+)*)\]", replace, text)
    # Adjacent markers ("[1][2] [2][3]") become one run, each number once, in order.
    rendered = re.sub(
        r"\[\d+\](?:\s*\[\d+\])+",
        lambda m: "".join(f"[{n}]" for n in sorted({int(x) for x in re.findall(r"\d+", m[0])})),
        rendered,
    )
    rendered = re.sub(r"\s+([.,;])", r"\1", rendered.replace(" []", ""))
    if sources:
        lines = [f"[{s['number']}] {s['label']}: {s['url']}" for s in sources]
        rendered += "\n\nSources:\n" + "\n".join(lines)
    return rendered, sources


def source_label(item: Evidence) -> str:
    data = item["data"]
    tool = item["tool"]
    if tool == "get_actuals":
        return f"{data['ticker']} {data['form']} filed {data['filed']}"
    if tool == "get_guidance":
        if "guidance_rows" in data:
            return f"{data['ticker']} earnings release {data.get('latest_release', '')}".strip()
        return f"{data['ticker']} earnings release for {data['issued']}, filed {data['issued_on']}"
    if tool == "search_filings":
        kind = {
            "press_release": "earnings release",
            "earnings_exhibit": "earnings exhibit",
            "10q": "10-Q",
            "10k": "10-K",
        }.get(data["doc_type"], data["doc_type"])
        title = f", {data['section_title']}" if data.get("section_title") else ""
        return f"{data['ticker']} {kind} {data['period']}{title}, characters {data['chars']}"
    if tool == "search_news":
        publisher = data.get("publisher") or "news"
        return f'{publisher}, {data["published"]}: "{data["headline"]}" (reported by {publisher})'
    if tool == "get_statements":
        publisher = data.get("publisher") or "news"
        who = data.get("speaker") or str(data.get("attribution", "")).replace("_", " ")
        return (
            f"{publisher}, {data['published']}: {who} on {data['date']} (reported by {publisher})"
        )
    if tool == "get_promises":
        publisher = data.get("publisher") or "news"
        return f"{publisher}: {data['speaker']} on {data['date']} (reported by {publisher})"
    if tool == "promise_evidence":
        return f"{data['kind']}, {data.get('date') or 'undated'}"
    if tool == "cite_value":
        return f"cited from {data['source']}"
    if tool == "resolve_company":
        return f"{data['ticker']} filings on SEC EDGAR"
    return str(tool)


# Traces.


def trace_lines(node: str, update: Mapping[str, Any] | None) -> list[str]:
    """What a node did, for --show-trace: its decision, and each tool call with its
    arguments and a truncated result."""
    if not update:
        return [node]
    lines = []
    if node == "router":
        lines.append(f"router: {update.get('question_type')} ({update.get('route_reason')})")
    elif node == "planner":
        lines.append("planner: " + " | ".join(update.get("plan") or []))
    elif node == "executor":
        for message in update.get("messages") or []:
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    arguments = json.dumps(call["args"], default=str)
                    lines.append(f"executor -> {call['name']}({arguments})")
            elif isinstance(message, AIMessage):
                lines.append(f"executor: draft ({len(text_of(message))} characters)")
    elif node == "tools":
        for message in update.get("messages") or []:
            if isinstance(message, ToolMessage):
                content = text_of(message)
                cut = content[:TRACE_CHARS] + ("…" if len(content) > TRACE_CHARS else "")
                lines.append(f"tools <- {message.name}: {cut}")
    elif node == "verifier":
        failures = [
            f
            for f in update.get("failures") or []
            if f["revision"] == update.get("revision_count", 1) - 1
            or update.get("next_step") == "answer"
        ]
        if update.get("verified"):
            lines.append("verifier: passed")
        else:
            checks = ", ".join(sorted({f["check"] for f in failures})) or "?"
            lines.append(f"verifier: failed ({checks}) -> {update.get('next_step')}")
    elif node == "answer":
        lines.append("answer: written")
    return lines or [node]
