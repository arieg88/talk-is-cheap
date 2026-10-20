"""The answer: [1]-style markers and a numbered source list, notes the answer must
carry, and what is kept when verification gives up."""

from typing import Any

from talk_is_cheap.agent.evidence import Evidence
from talk_is_cheap.agent.graph import (
    ClaimCheck,
    VerifierVerdict,
    _model_failures,
    _verified_part,
    _with_notes,
    render,
)

FILING = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/q.htm"
RELEASE = "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/release.htm"
ARTICLE = "https://news.example.test/agentco-outlook"


def item(
    evidence_id: str, tool: str, url: str, provenance: str = "sec_filing", **data: Any
) -> Evidence:
    return {"id": evidence_id, "provenance": provenance, "url": url, "tool": tool, "data": data}


EVIDENCE = {
    "E1": item(
        "E1",
        "get_actuals",
        FILING,
        ticker="AGNT",
        metric="revenue",
        period="FY2026Q2",
        value=11e9,
        unit="USD",
        form="10-Q",
        filed="2026-08-01",
    ),
    "E2": item(
        "E2",
        "get_actuals",
        FILING,
        ticker="AGNT",
        metric="revenue",
        period="FY2026Q1",
        value=10e9,
        unit="USD",
        form="10-Q",
        filed="2026-08-01",
    ),
    "E3": item(
        "E3",
        "compute",
        FILING,
        operation="pct_change",
        inputs=["E2", "E1"],
        result=10.0,
        unit="percent",
    ),
    "E4": item(
        "E4",
        "get_guidance",
        RELEASE,
        "company_press_release",
        ticker="AGNT",
        metric="eps",
        target="FY2026Q2",
        issued="FY2026Q1",
        issued_on="2026-04-28",
        point=0.5,
        unit="USD/shares",
        verdict="beat",
        review="flagged by the plausibility check (x); reviewed as ok",
    ),
    "E5": item(
        "E5",
        "search_news",
        ARTICLE,
        "news_report",
        headline="Agentco raises outlook",
        publisher="Test Wire",
        published="2026-09-01",
        summary="Sees $12 billion.",
    ),
}


def test_markers_are_numbered_and_sources_listed() -> None:
    text, sources = render("Revenue was $11.0 billion [E1], up 10.0% [E3].", EVIDENCE)
    # E1 and E2 come from the same filing and share a number; E3 cites its inputs
    assert text.startswith("Revenue was $11.0 billion [1], up 10.0% [1].")
    assert [s["url"] for s in sources] == [FILING]
    assert text.endswith(f"Sources:\n[1] AGNT 10-Q filed 2026-08-01: {FILING}")


def test_adjacent_markers_are_merged() -> None:
    text, _ = render("Revenue rose [E1][E5] [E5, E1][E4].", EVIDENCE)
    assert text.startswith("Revenue rose [1][2][3].")


def test_news_sources_say_who_reported_them() -> None:
    text, sources = render("Agentco sees $12 billion [E5]. EPS guidance was $0.50 [E4].", EVIDENCE)
    assert "[1]" in text and "[2]" in text
    assert sources[0]["label"].endswith("(reported by Test Wire)")
    assert sources[1]["label"] == "AGNT earnings release for FY2026Q1, filed 2026-04-28"


def test_flagged_guidance_and_news_only_guidance_get_their_notes() -> None:
    noted = _with_notes("EPS guidance was $0.50 [E4]. Agentco expects $12 billion [E5].", EVIDENCE)
    assert "Agentco expects $12 billion (reported by Test Wire) [E5]." in noted
    assert "was flagged by the plausibility check (x); reviewed as ok [E4]." in noted
    # an answer that already says so is left alone
    said = "EPS guidance of $0.50 was flagged for review and reviewed as ok [E4]."
    assert _with_notes(said, EVIDENCE) == said


def test_giving_up_keeps_the_verified_sentences() -> None:
    draft = "Revenue was $11.0 billion [E1]. Revenue was $99 billion [E2]. It grew a lot [E3]."
    failures = [
        {
            "check": "numbers_grounded",
            "detail": "'$99 billion' is in no evidence: …",
            "revision": 2,
        },
        {"check": "llm_unsupported", "detail": "It grew a lot: vague", "revision": 2},
    ]
    kept = _verified_part(draft, EVIDENCE, "q", failures)
    assert kept == (
        "Revenue was $11.0 billion [E1].\n\nLeft out because they could not be verified (a "
        "figure not in the evidence, not supported by the cited evidence): 2 statements."
    )


def test_preferring_a_primary_source_only_counts_when_news_is_cited() -> None:
    verdict = VerifierVerdict(
        claims=[
            ClaimCheck(claim="revenue was $11.0 billion", verdict="supported"),
            ClaimCheck(claim="sees $12 billion", verdict="prefer_primary", reason="news"),
            ClaimCheck(claim="grew 10%", verdict="unsupported", reason="wrong period"),
        ]
    )
    with_news = _model_failures(verdict, [EVIDENCE["E1"], EVIDENCE["E5"]])
    assert [f.check for f in with_news] == ["llm_prefer_primary", "llm_unsupported"]
    without = _model_failures(verdict, [EVIDENCE["E1"]])
    assert [(f.check, f.detail) for f in without] == [("llm_unsupported", "grew 10%: wrong period")]
