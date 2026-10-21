"""Ask: questions to the agent over `POST /v1/chat`, streamed.

While the agent works, its `status`, `tool_start` and `tool_end` events show what it is
doing; the verified answer arrives as `token` events, then its citations, which link the
answer's [n] markers to the filing or article. Follow-ups stay on the conversation's
thread until "New conversation". Each answer can be rated, with an optional comment
(`/v1/feedback`). Nothing here is cached.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

import streamlit as st

from talk_is_cheap.api.schemas import (
    CitationEvent,
    DoneEvent,
    StatusEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from talk_is_cheap.ui import data, theme
from talk_is_cheap.ui.client import ApiClient, ApiError, ChatStarted
from talk_is_cheap.ui.format import count

EXAMPLES = (
    "Did Nvidia deliver Blackwell when it said it would?",
    "Which executives' promises have held up best?",
    "What has Lisa Su promised about AMD's AI chips, and has any of it come due?",
    "What was Nvidia's revenue last quarter, against its guidance?",
)
PROVENANCE = {
    "sec_filing": "SEC filing",
    "company_press_release": "company press release",
    "news_report": "news report",
}
TURNS_KEY = "ask_turns"
THREAD_KEY = "ask_thread"
_MARKER = re.compile(r"\[(\d+)\]")


@dataclass(slots=True)
class AskTurn:
    question: str
    answer: str = ""
    citations: list[CitationEvent] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    done: DoneEvent | None = None
    error: str | None = None
    feedback: Literal["up", "down"] | None = None


def render() -> None:
    api = data.client()
    turns: list[AskTurn] = st.session_state.setdefault(TURNS_KEY, [])
    intro, reset = st.columns([5, 1])
    intro.markdown(
        "Ask about what executives promised and whether it happened, about guidance "
        "against results, or about reported financials. Every figure in an answer is "
        "checked against the evidence, and every claim cites a filing or article."
    )
    if turns and reset.button(
        "New conversation", icon=":material/add_comment:", key="new-conversation"
    ):
        st.session_state[TURNS_KEY] = []
        st.session_state[THREAD_KEY] = None
        st.rerun()

    asked: str | None = None
    if not turns:
        st.caption("Try one of these:")
        columns = st.columns(len(EXAMPLES))
        for i, (column, example) in enumerate(zip(columns, EXAMPLES, strict=True)):
            if column.button(example, key=f"example-{i}", width="stretch"):
                asked = example
    for i, turn in enumerate(turns):
        _show_turn(api, turn, i)
    typed = st.chat_input("Ask a question, or a follow-up on this conversation")
    question = asked or typed
    if question:
        _ask(api, question.strip(), turns)


def _ask(api: ApiClient, question: str, turns: list[AskTurn]) -> None:
    turn = AskTurn(question)
    with st.chat_message("user"):
        st.markdown(theme.plain(question))
    with st.chat_message("assistant"):
        status = st.status("Sending the question…", expanded=False)
        body = st.empty()
        tokens: list[str] = []
        try:
            for event in api.chat(question, st.session_state.get(THREAD_KEY)):
                if isinstance(event, ChatStarted):
                    if event.thread_id:
                        st.session_state[THREAD_KEY] = event.thread_id
                elif isinstance(event, StatusEvent):
                    status.update(label=event.step)
                    turn.steps.append(event.step)
                elif isinstance(event, ToolStartEvent):
                    status.write(f"Calling `{event.name}`…")
                elif isinstance(event, ToolEndEvent):
                    outcome = f"error: {event.error}" if event.error else event.summary
                    status.write(f"`{event.name}`: {theme.plain(outcome)}")
                elif isinstance(event, TokenEvent):
                    tokens.append(event.text)
                    body.markdown(theme.plain("".join(tokens)) + "▌")
                elif isinstance(event, CitationEvent):
                    turn.citations.append(event)
                elif isinstance(event, DoneEvent):
                    turn.done = event
                    st.session_state[THREAD_KEY] = event.thread_id
            status.update(label="Answered", state="complete")
        except ApiError as exc:
            status.update(label="Stopped", state="error")
            turn.error = str(exc)
        turn.answer = "".join(tokens).strip()
    turns.append(turn)
    st.rerun()


def _show_turn(api: ApiClient, turn: AskTurn, index: int) -> None:
    with st.chat_message("user"):
        st.markdown(theme.plain(turn.question))
    with st.chat_message("assistant"):
        if turn.error:
            st.error(turn.error, icon=":material/error:")
            return
        st.markdown(linked(turn.answer, turn.citations))
        if turn.citations:
            st.markdown("**Sources**  \n" + "  \n".join(_source(c) for c in turn.citations))
        if turn.done is not None:
            st.caption(meta_line(turn.done))
            _feedback(api, turn, index)


def linked(answer: str, citations: list[CitationEvent]) -> str:
    """The answer as markdown, its [n] markers linked to their sources."""
    urls = {c.id: c.url for c in citations}
    parts: list[str] = []
    last = 0
    for marker in _MARKER.finditer(answer):
        parts.append(theme.plain(answer[last : marker.start()]))
        number = int(marker.group(1))
        url = urls.get(number)
        parts.append(f"[\\[{number}\\]](<{url}>)" if url else theme.plain(marker.group(0)))
        last = marker.end()
    parts.append(theme.plain(answer[last:]))
    return "".join(parts)


def _source(c: CitationEvent) -> str:
    kind = PROVENANCE.get(c.provenance, c.provenance)
    return f"\\[{c.id}\\] [{theme.plain(c.title)}](<{c.url}>) · {kind}"


def meta_line(done: DoneEvent) -> str:
    if done.cache_hit:
        cost = "from the cache, no cost"
    elif done.cost == 0:
        cost = "no cost"
    else:
        cost = f"${done.cost:.4f}"
    parts = [cost, f"{done.seconds:.1f} s", count(done.tool_calls, "tool call")]
    if done.revisions:
        parts.append(count(done.revisions, "revision"))
    if not done.verified:
        parts.append("not every sentence could be verified")
    return " · ".join(parts)


def _feedback(api: ApiClient, turn: AskTurn, index: int) -> None:
    done = turn.done
    assert done is not None
    if turn.feedback:
        thumb = "👍" if turn.feedback == "up" else "👎"
        st.caption(f"{thumb} Thanks, your feedback was saved.")
        return
    rating = st.feedback("thumbs", key=f"rating-{index}")
    if rating is None:
        return
    with st.form(key=f"feedback-{index}", border=False):
        comment = st.text_input("Anything to add? (optional)", key=f"comment-{index}")
        if st.form_submit_button("Send feedback"):
            chosen: Literal["up", "down"] = "up" if rating == 1 else "down"
            try:
                api.feedback(done.request_id, chosen, comment.strip() or None)
            except ApiError as exc:
                st.error(f"Feedback was not saved. {exc}")
                return
            turn.feedback = chosen
            st.rerun()
