"""Routing rules, decided without a model where they can be."""

from datetime import date

import pytest

from talk_is_cheap.agent.router import (
    ADVICE_NOTE,
    companies_in,
    out_of_scope_answer,
    route_by_rules,
)
from talk_is_cheap.universe import Universe, UniverseCompany

UNIVERSE = Universe(
    history_start=date(2024, 1, 1),
    forms=["10-Q"],
    companies=[
        UniverseCompany(ticker="NVDA", name="Nvidia"),
        UniverseCompany(ticker="AMD", name="AMD"),
        UniverseCompany(ticker="META", name="Meta"),
        UniverseCompany(ticker="GOOGL", name="Alphabet", aliases=["Google"]),
        UniverseCompany(ticker="AVGO", name="Broadcom", news_only=True),
    ],
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What was Nvidia's revenue in fiscal Q2 2027?", "lookup"),
        ("What was NVDA net income as first reported for FY2026 Q1?", "lookup"),
        ("What does Nvidia's 10-Q MD&A say drove the growth?", "lookup"),
        ("Did nvidia beat its revenue guidance last quarter?", "guidance_check"),
        ("What revenue does Google expect next quarter?", "guidance_check"),
        ("How did Meta's capex change over the last four quarters?", "trend"),
        ("Netflix-style: by how much did Nvidia revenue grow from Q2 2025 to Q2 2026?", "trend"),
        ("Compare Nvidia and AMD revenue in their latest quarters", "comparison"),
        ("Who had more revenue, Alphabet or Meta?", "comparison"),
        ("Should I buy Nvidia stock?", "out_of_scope"),
        ("Is AMD a good investment?", "out_of_scope"),
        ("Will Nvidia's stock go up after earnings?", "out_of_scope"),
        ("Will Meta beat estimates next quarter?", "out_of_scope"),
    ],
)
def test_rules(question: str, expected: str) -> None:
    route = route_by_rules(question, UNIVERSE)
    assert route is not None and route.question_type == expected


@pytest.mark.parametrize(
    "question",
    [
        "What was Broadcom's revenue last quarter?",  # news only: not a covered company
        "Which company has the biggest data centers?",
        "Tell me about Nvidia",  # a company, nothing to look up
    ],
)
def test_undecided_questions_go_to_the_model(question: str) -> None:
    assert route_by_rules(question, UNIVERSE) is None


def test_follow_ups_keep_the_previous_type() -> None:
    route = route_by_rules("And the quarter before?", UNIVERSE, "lookup")
    assert route is not None and route.question_type == "lookup"
    assert route_by_rules("And the quarter before?", UNIVERSE, "out_of_scope") is None


def test_word_tickers_match_only_in_upper_case() -> None:
    assert [c.ticker for c in companies_in("amd and meta", UNIVERSE)] == ["AMD", "META"]
    assert companies_in("the metadata of a crm", UNIVERSE) == []
    assert companies_in("buybacks at nvidia's peers", UNIVERSE)[0].ticker == "NVDA"


def test_buybacks_are_not_advice() -> None:
    route = route_by_rules("How much did Nvidia spend on buybacks in FY2026?", UNIVERSE)
    assert route is not None and route.question_type == "lookup"


def test_out_of_scope_answer_says_what_the_agent_does() -> None:
    advice = out_of_scope_answer("Should I buy Nvidia?", UNIVERSE, trading=True)
    assert ADVICE_NOTE in advice and "NVDA" in advice and "AVGO" not in advice
    other = out_of_scope_answer("What is Broadcom's revenue?", UNIVERSE, trading=False)
    assert ADVICE_NOTE not in other and "guidance" in other
