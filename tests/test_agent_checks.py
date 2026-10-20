"""The verifier's code checks: numbers normalized and found in evidence, citations that
exist, and sentences with numbers that carry one."""

from decimal import Decimal
from typing import Any

import pytest

from talk_is_cheap.agent.checks import (
    cited_ids,
    code_checks,
    evidence_values,
    grounded,
    mentions,
    sentences,
)
from talk_is_cheap.agent.evidence import Evidence, EvidenceLog, sources_of


def item(
    evidence_id: str, tool: str = "get_actuals", url: str = "https://sec.gov/x", **data: Any
) -> Evidence:
    return {"id": evidence_id, "provenance": "sec_filing", "url": url, "tool": tool, "data": data}


EVIDENCE = {
    "E1": item("E1", metric="revenue", period="FY2027Q2", value=96221000000.0, unit="USD"),
    "E2": item("E2", metric="revenue", period="FY2026Q2", value=46743000000.0, unit="USD"),
    "E3": item(
        "E3",
        tool="compute",
        operation="pct_change",
        inputs=["E2", "E1"],
        result=105.853711,
        unit="percent",
    ),
    "E4": item(
        "E4",
        tool="get_guidance",
        low=89180000000.0,
        high=92820000000.0,
        point=91e9,
        unit="USD",
        quote="Revenue is expected to be $91.0 billion, plus or minus 2%.",
    ),
    "E5": item("E5", metric="eps_diluted", value=-0.21, unit="USD/shares"),
    "E6": item("E6", tool="run_sql", url="", rows=[[1, 2]]),
}


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        ("$96.2 billion", Decimal("96.2e9"), "usd"),
        ("$96,221 million", Decimal("96221e6"), "usd"),
        ("$96,221M", Decimal("96221e6"), "usd"),
        ("96.2B", Decimal("96.2e9"), "plain"),
        ("105.9%", Decimal("105.9"), "pct"),
        ("50 basis points", Decimal("0.5"), "pct"),
        ("$(0.21)", Decimal("-0.21"), "usd"),
        ("-$0.21", Decimal("-0.21"), "usd"),
        ("1.2 trillion", Decimal("1.2e12"), "plain"),
        ("17 percent", Decimal("17"), "pct"),
    ],
)
def test_numbers_are_normalized(text: str, value: Decimal, kind: str) -> None:
    [found] = mentions(f"It was {text} in the quarter.")
    assert (found.value, found.kind) == (value, kind)


def test_periods_dates_forms_and_citations_are_not_numbers() -> None:
    text = (
        "In FY2027 Q2 (fiscal 2027, the quarter ended July 26, 2026, filed 2026-08-26 in "
        "its 10-Q, Item 7, Q3 2026) [E1, E12] the 2nd and 3rd quarters"
    )
    assert mentions(text) == []


def test_small_counts_are_marked() -> None:
    [count] = mentions("over the last 4 quarters")
    assert count.small
    [large] = mentions("13 quarters")
    assert not large.small


def test_rounding_decides_grounding() -> None:
    values = evidence_values(EVIDENCE.values())
    assert grounded(mentions("$96.2 billion")[0], values)
    assert grounded(mentions("$96 billion")[0], values)
    assert grounded(mentions("106%")[0], values)  # compute result, rounded
    assert grounded(mentions("$91.0 billion")[0], values)  # from the guidance quote
    assert not grounded(mentions("$96.3 billion")[0], values)
    assert not grounded(mentions("$97 billion")[0], values)
    assert grounded(mentions("a loss of $0.21 per share")[0], values)  # sign in the words


def test_a_grounded_cited_draft_passes() -> None:
    draft = (
        "Revenue was $96.2 billion in fiscal Q2 2027 [E1], up 105.9% from $46.7 billion a "
        "year earlier [E3, E2]. Guidance was $91.0 billion [E4]."
    )
    assert code_checks(draft, EVIDENCE) == []


def test_each_check_names_its_failure() -> None:
    checks = {f.check for f in code_checks("Revenue was $99.9 billion [E1].", EVIDENCE)}
    assert checks == {"numbers_grounded"}
    checks = {f.check for f in code_checks("Revenue was $96.2 billion.", EVIDENCE)}
    assert checks == {"numbers_cited"}
    checks = {f.check for f in code_checks("Revenue rose [E9].", EVIDENCE)}
    assert checks == {"citations_exist"}
    # run_sql rows have no URL: a citation of them does not resolve
    checks = {f.check for f in code_checks("There are 2 rows [E6].", EVIDENCE)}
    assert checks == {"citations_exist"}


def test_numbers_in_the_question_are_allowed() -> None:
    draft = "Nothing was found for the $5 billion figure you asked about."
    assert {f.check for f in code_checks(draft, EVIDENCE)} == {
        "numbers_grounded",
        "numbers_cited",
    }
    assert [f.check for f in code_checks(draft, EVIDENCE, "Was it $5 billion?")] == [
        "numbers_cited"
    ]


def test_sentences_and_cited_ids() -> None:
    text = "Revenue was $96.2 billion [E1]. Margin was 74.9% [E4, E2].\n- EPS was $0.21 [E5]"
    assert sentences(text) == [
        "Revenue was $96.2 billion [E1].",
        "Margin was 74.9% [E4, E2].",
        "- EPS was $0.21 [E5]",
    ]
    assert cited_ids(text) == ["E1", "E4", "E2", "E5"]


def test_evidence_ids_continue_and_computed_items_cite_their_inputs() -> None:
    log = EvidenceLog({"E1": EVIDENCE["E1"], "E2": EVIDENCE["E2"]})
    added = log.add("sec_filing", "u", "compute", {"inputs": ["E2", "E1"], "result": 1.0})
    assert added["id"] == "E3"
    assert [s["id"] for s in sources_of(added, log.items)] == ["E2", "E1"]
