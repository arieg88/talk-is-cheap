"""What the UI's pages say: names, amounts, periods, thin-data sentences, the
scorecard's counts; and that the CLI starts the UI without importing Streamlit."""

import subprocess
import sys
from datetime import date
from decimal import Decimal

import pytest

from talk_is_cheap.ui import format as fmt
from tests.ui_fakes import guidance, promise, record

TODAY = date(2026, 10, 21)


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("NVIDIA CORP", "Nvidia"),
        ("ADVANCED MICRO DEVICES INC", "Advanced Micro Devices"),
        ("AMAZON COM INC", "Amazon"),
        ("QUALCOMM INC/DE", "Qualcomm"),
        ("CISCO SYSTEMS, INC.", "Cisco Systems"),
        ("Meta Platforms, Inc.", "Meta Platforms"),
        ("Apple Inc.", "Apple"),
    ],
)
def test_company_names(name: str, shown: str) -> None:
    assert fmt.company_name(name) == shown


def test_speakers_as_track_records_group_them() -> None:
    assert fmt.speaker_key("NVDA", "company spokesperson") == "NVDA (company)"
    assert fmt.speaker_key("NVDA", "Jen-Hsun Huang") == "Jen-Hsun Huang (NVDA)"
    assert fmt.speaker_label("Jen-Hsun Huang (NVDA)") == "Jen-Hsun Huang · NVDA"
    assert fmt.speaker_label("NVDA (company)") == "NVDA company statements"


@pytest.mark.parametrize(
    ("value", "unit", "measure", "shown"),
    [
        ("108000000000", "usd", "level", "$108.0B"),
        ("105840000000", "usd", "level", "$105.8B"),
        ("9200000", "usd", "level", "$9.2M"),
        ("74.5", "pct", "level", "74.5%"),
        ("9", "pct", "yoy_growth", "+9.0% y/y"),
        ("-2", "pct", "sequential_growth", "-2.0% q/q"),
        ("30", "pct", "share_of_revenue", "30.0% of revenue"),
        ("1.234", "usd_per_share", "level", "$1.23 a share"),
    ],
)
def test_amounts_have_units(value: str, unit: str, measure: str, shown: str) -> None:
    assert fmt.amount(Decimal(value), unit, measure) == shown


def test_guided_ranges_points_and_words() -> None:
    assert fmt.guided(guidance(1)) == "$105.8B to $110.2B"
    assert fmt.guided(guidance(1, low=None, high=None)) == "$108.0B"
    assert fmt.guided(guidance(1, high=None, point=None)) == "at least $105.8B"
    words = guidance(1, low=None, high=None, point=None, value_text=None, qualitative="strong")
    assert fmt.guided(words) == "strong"
    assert fmt.period("FY2027Q3") == "FY2027 Q3" and fmt.period("FY2027") == "FY2027"


def test_excerpts_lose_their_markdown() -> None:
    assert (
        fmt.excerpt("## Edge Computing - **Revenue** | $7.2B") == "Edge Computing - Revenue $7.2B"
    )


def test_claims_summaries_for_thin_data() -> None:
    assert fmt.claims_summary([], TODAY, "Meta") == "No management claims are stored for Meta yet."
    vague = [promise(1, scored=False, specificity="vague", verdict=None)]
    assert "1 claim, none of them scorable" in fmt.claims_summary(vague, TODAY, "Meta")
    later = [
        promise(1, verdict=None, due_date=date(2027, 5, 1)),
        promise(2, verdict=None, due_date=date(2027, 3, 9)),
        promise(3, verdict="too_early", due_date=None),
        promise(4, verdict=None, due_date=date(2028, 1, 1)),
    ]
    assert (
        fmt.claims_summary(later, TODAY, "Meta")
        == "4 claims, none due yet. The earliest is due in March 2027."
    )
    open_ended = [promise(1, verdict=None, due_date=None)]
    assert "none has a deadline" in fmt.claims_summary(open_ended, TODAY, "Meta")
    mixed = [promise(1), promise(2, verdict=None, due_date=date(2027, 1, 1))]
    assert fmt.claims_summary(mixed, TODAY, "X") == (
        "2 claims, 1 past their due date, 1 with a verdict."
    )


def test_record_summaries_never_give_a_rate_under_the_minimum() -> None:
    assert fmt.record_summary(record("X", 6), 5) == ""
    assert fmt.record_summary(record("X", 1), 5) == (
        "Too few to judge: 1 resolved claim of 3 scorable claims; rates need 5."
    )
    assert "no track record" in fmt.record_summary(None, 5)


def test_scorecard_counts_only_comparable_rows() -> None:
    card = fmt.scorecard(
        [
            guidance(1, "beat"),
            guidance(2, "missed"),
            guidance(3, "not_comparable"),
            guidance(4, "not_comparable"),
            guidance(5, "pending"),
            guidance(6, "beat", mark="REVIEW", counts_in_verdicts=False),
            guidance(7, "met", mark="WRONG", counts_in_verdicts=False),
        ]
    )
    assert card.comparable == 2 and card.excluded == 5
    assert card.counted == {"beat": 1, "missed": 1}
    assert card.exclusions() == [
        "2 not comparable (non-GAAP guidance; reported values are GAAP)",
        "1 pending (target period not reported yet)",
        "1 flagged for review",
        "1 reviewed as extraction errors",
    ]


def test_every_verdict_has_a_tone_and_the_states_of_unjudged_claims() -> None:
    assert {v.tone for v in fmt.PROMISE_VERDICTS.values()} <= set(fmt.TONE_COLOURS)
    assert fmt.PROMISE_VERDICTS["delivered"].tone == fmt.GUIDANCE_VERDICTS["beat"].tone == "green"
    assert fmt.PROMISE_VERDICTS["delivered_late"].tone == fmt.GUIDANCE_VERDICTS["met"].tone
    assert fmt.PROMISE_VERDICTS["walked_back"].tone == fmt.GUIDANCE_VERDICTS["missed"].tone
    assert fmt.PROMISE_VERDICTS["expired_unproven"].tone == "outline"
    assert fmt.promise_state(promise(1, verdict=None)) == "pending"
    assert fmt.promise_state(promise(1, scored=False, verdict=None)) == "not_scored"


def test_importing_the_cli_does_not_import_streamlit() -> None:
    code = "import sys, talk_is_cheap.cli; sys.exit('streamlit' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_the_ui_never_imports_the_database_or_the_agent_graph() -> None:
    code = (
        "import sys, talk_is_cheap.ui.client, talk_is_cheap.ui.data;"
        "import talk_is_cheap.ui.views.promises, talk_is_cheap.ui.views.ask;"
        "bad = [m for m in ('sqlalchemy', 'langgraph', 'langchain_core', 'talk_is_cheap.db',"
        " 'talk_is_cheap.agent.graph') if m in sys.modules]; print(bad); sys.exit(bool(bad))"
    )
    found = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )
    assert found.returncode == 0, found.stdout


def test_the_ui_command_runs_streamlit_on_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from talk_is_cheap import cli
    from talk_is_cheap.ui import server

    ran: list[list[str]] = []
    monkeypatch.setattr(server.os, "execv", lambda path, args: ran.append(args))
    cli.main(["ui", "--host", "0.0.0.0", "--port", "8600"])
    assert ran[0][1:5] == ["-m", "streamlit", "run", str(server.APP)]
    assert ran[0][5:] == ["--server.address", "0.0.0.0", "--server.port", "8600"]
