"""The promise tracker's deterministic parts: due dates, framing (grounding, baselines,
specificity, boldness, the rule), the resolver's checks, cited numbers and track
records."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from talk_is_cheap.agent.evidence import EvidenceLog
from talk_is_cheap.agent.tools import ToolFailed, cited_number
from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.guidance.matching import Actuals, Reported
from talk_is_cheap.promises.checks import Measured, PromiseFacts, check
from talk_is_cheap.promises.deadlines import add_duration, due_date
from talk_is_cheap.promises.framing import ClaimInput, frame, show
from talk_is_cheap.promises.schema import Deadline, DeadlineAnswer, FramingAnswer, Judgment
from talk_is_cheap.promises.track import Scored, track_records

DECEMBER = FiscalCalendar(12, 31)
JANUARY = FiscalCalendar(1, 26)  # Nvidia
SAID = date(2025, 3, 10)


def deadline(**values: Any) -> Deadline:
    return Deadline(**values)


@pytest.mark.parametrize(
    ("values", "expected", "basis"),
    [
        ({"type": "duration", "amount": 1, "unit": "years"}, date(2026, 3, 10), "relative"),
        ({"type": "duration", "amount": 18, "unit": "months"}, date(2026, 9, 10), "relative"),
        ({"type": "calendar_year", "offset": 1}, date(2026, 12, 31), "explicit"),
        ({"type": "calendar_year", "year": 2027}, date(2027, 12, 31), "explicit"),
        ({"type": "calendar_half", "half": 1, "year": 2026}, date(2026, 6, 30), "explicit"),
        ({"type": "calendar_quarter", "quarter": 2}, date(2025, 6, 30), "explicit"),
        ({"type": "calendar_quarter", "quarter": 1}, date(2025, 3, 31), "explicit"),
        ({"type": "month", "month": 2}, date(2026, 2, 28), "explicit"),
        ({"type": "date", "month": 6, "day": 10, "year": 2025}, date(2025, 6, 10), "explicit"),
        ({"type": "season", "season": "summer", "offset": 1}, date(2026, 9, 22), "explicit"),
        ({"type": "season", "season": "summer"}, date(2025, 9, 22), "explicit"),
        ({"type": "fiscal_year", "offset": 1}, date(2026, 12, 31), "fiscal_period"),
        ({"type": "fiscal_quarter", "offset": 1}, date(2025, 6, 30), "fiscal_period"),
        ({"type": "none"}, None, "open_ended"),
        ({"type": "duration"}, None, "open_ended"),
    ],
)
def test_due_dates_are_computed_in_code(
    values: dict[str, Any], expected: date | None, basis: str
) -> None:
    due = due_date(deadline(**values), SAID, DECEMBER)
    assert (due.date, due.basis) == (expected, basis)


def test_fiscal_deadlines_follow_the_company_calendar() -> None:
    # Said in March 2025, Nvidia's fiscal 2026 (ending January 2026) is under way.
    assert due_date(deadline(type="fiscal_year", offset=1), SAID, JANUARY).date == date(2027, 1, 26)
    fourth = deadline(type="fiscal_quarter", quarter=4)
    assert due_date(fourth, SAID, JANUARY).date == date(2026, 1, 26)
    assert due_date(deadline(type="fiscal_year", year=2025), SAID, None).basis == "open_ended"


def test_month_arithmetic_keeps_month_ends() -> None:
    assert add_duration(date(2025, 1, 31), 1, "months") == date(2025, 2, 28)
    assert add_duration(date(2024, 2, 29), 1, "years") == date(2025, 2, 28)
    assert add_duration(date(2025, 1, 1), 2, "weeks") == date(2025, 1, 15)


def test_the_answer_schema_turns_blanks_into_missing_values() -> None:
    answer = FramingAnswer(
        subject="vehicle production",
        target_kind="multiple",
        target_value=2,
        deadline=DeadlineAnswer(phrase="within a year", type="duration", amount=1, unit="years"),
    )
    framing = answer.framing()
    assert framing.metric is None and framing.event is None and framing.baseline_value is None
    assert framing.deadline.year is None and framing.deadline.offset is None
    assert framing.deadline.amount == 1
    assert FramingAnswer(subject="x", target_kind="event").framing().target_value is None
    profit = FramingAnswer(subject="x", target_kind="threshold", target_value=0).framing()
    assert profit.target_value == 0
    schema = FramingAnswer.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])


def claim(quote: str, paragraph: str = "", said: date = SAID) -> ClaimInput:
    return ClaimInput(
        claim_key="k",
        statement_id=1,
        article_id=1,
        ticker="TSLA",
        company="Tesla",
        speaker="Elon Musk",
        statement_date=said,
        article_date=said,
        quote=quote,
        paragraph=paragraph,
        publisher="Example News",
    )


def answer(**values: Any) -> FramingAnswer:
    values.setdefault("subject", "vehicle production")
    values.setdefault("deadline", DeadlineAnswer(type="none"))
    return FramingAnswer(**values)


def actuals(*values: tuple[str, int, int | None, str, date]) -> Actuals:
    return Actuals(
        (metric, year, quarter, Reported(Decimal(amount), f"acc-{year}-{quarter}", filed))
        for metric, year, quarter, amount, filed in values
    )


def test_a_multiple_with_an_article_baseline_gives_a_specific_bold_rule() -> None:
    framed = frame(
        claim(
            "We will double production within a year",
            "Tesla produced 410,831 vehicles last quarter.",
        ),
        answer(
            target_kind="multiple",
            target_text="double",
            target_value=2,
            unit="vehicles",
            measure_period="quarter",
            baseline_text="410,831 vehicles",
            baseline_value=410831,
            baseline_period_text="last quarter",
            deadline=DeadlineAnswer(
                phrase="within a year", type="duration", amount=1, unit="years"
            ),
        ).framing(),
        DECEMBER,
        None,
    )
    assert framed.specificity == "specific" and framed.bold
    assert framed.required_value == Decimal(821662)
    assert framed.baseline_source == "article"
    assert framed.resolution_rule == (
        "True if Tesla's vehicle production reaches at least 2 x 410,831 vehicles (last "
        "quarter), that is 821,662 vehicles, in any quarter ending on or before 2026-03-10."
    )


def test_numbers_not_in_the_text_are_dropped() -> None:
    framed = frame(
        claim("Robotaxis will be in many cities by next summer"),
        answer(
            subject="robotaxi cities",
            target_kind="absolute",
            target_value=10,
            unit="cities",
            baseline_text="2 cities",
            baseline_value=2,
            deadline=DeadlineAnswer(type="season", season="summer", offset=1),
        ).framing(),
        DECEMBER,
        None,
    )
    assert framed.target_value is None and framed.specificity == "partial"
    assert any("not written" in note for note in framed.notes)
    assert framed.resolution_rule.startswith("Not resolvable")


def test_baselines_come_from_values_filed_before_the_statement() -> None:
    stored = actuals(
        ("revenue", 2024, 4, "25000000000", date(2025, 1, 29)),
        ("revenue", 2025, 1, "19000000000", date(2025, 4, 22)),  # filed after the claim
    )
    framed = frame(
        claim("Revenue will grow 60% this year"),
        answer(
            subject="revenue",
            metric="revenue",
            measure_period="quarter",
            target_kind="change",
            target_value=60,
            unit="pct",
            deadline=DeadlineAnswer(type="calendar_year", offset=0),
        ).framing(),
        DECEMBER,
        stored,
    )
    assert framed.baseline is not None
    assert (framed.baseline.period, framed.baseline_source) == ("FY2024Q4", "actuals")
    assert framed.required_value == Decimal(40_000_000_000)
    assert framed.bold and framed.due_date == date(2025, 12, 31)


def test_profitability_from_a_loss_is_bold_and_vague_claims_are_vague() -> None:
    stored = actuals(("operating_income", 2024, 4, "-500000000", date(2025, 1, 29)))
    profitable = frame(
        claim("The unit will be profitable next year"),
        answer(
            subject="operating income",
            metric="operating_income",
            measure_period="quarter",
            target_kind="threshold",
            target_value=0,
            deadline=DeadlineAnswer(type="calendar_year", offset=1),
        ).framing(),
        DECEMBER,
        stored,
    )
    assert profitable.bold and profitable.specificity == "specific"
    assert "is above zero" in profitable.resolution_rule
    vague = frame(
        claim("We are poised for our next wave of growth"),
        answer(subject="growth").framing(),
        DECEMBER,
        None,
    )
    assert vague.specificity == "vague" and not vague.bold


def test_figures_as_rules_write_them() -> None:
    assert show(Decimal("25000000000"), "usd") == "$25 billion"
    assert show(Decimal("12.5"), "pct") == "12.5%"
    assert show(Decimal(410831), "vehicles") == "410,831 vehicles"


def evidence(log: EvidenceLog, tool: str, data: dict[str, Any]) -> str:
    return str(log.add("sec_filing", "https://www.sec.gov/x", tool, data)["id"])


def test_cited_numbers_must_be_in_the_text_and_alone() -> None:
    log = EvidenceLog()
    item = log.get(
        evidence(
            log,
            "search_filings",
            {"filed": "2026-04-02", "text": "We produced 836,000 vehicles and delivered 800,000."},
        )
    )
    assert item is not None
    found = cited_number(item, "produced 836,000 vehicles")
    assert (found.value, found.unit) == (Decimal(836000), "vehicles")
    with pytest.raises(ToolFailed, match="2 numbers"):
        cited_number(item, "produced 836,000 vehicles and delivered 800,000")
    with pytest.raises(ToolFailed, match="not in the text"):
        cited_number(item, "produced 900,000 vehicles")


FACTS = PromiseFacts(
    statement_date=date(2025, 3, 10),
    due_date=date(2026, 3, 31),
    target_kind="absolute",
    direction="at_least",
    required_value=Decimal(800_000),
    baseline_value=Decimal(410_831),
    resolution_rule="True if production reaches at least 800,000 vehicles.",
    quote="We will make 800,000 vehicles",
)


def items_with(*dated: tuple[str, str]) -> dict[str, Any]:
    log = EvidenceLog()
    for filed, text in dated:
        evidence(log, "search_filings", {"filed": filed, "text": text})
    return log.items


def measured(value: int | None) -> Measured:
    return Measured(None if value is None else Decimal(value), "vehicles", None)


def test_delivered_needs_the_target_met_on_time() -> None:
    items = items_with(("2026-04-02", "We produced 836,000 vehicles."))
    judgment = Judgment(
        verdict="delivered", evidence_ids=["E1"], first_proof_id="E1",
        reasoning="Production reached 836,000 vehicles.", confidence=0.9,
    )  # fmt: skip
    ok = check(FACTS, judgment, items, measured(836_000), date(2026, 4, 20), date(2026, 10, 1))
    assert ok.verdict == "delivered" and not ok.failed
    late = check(FACTS, judgment, items, measured(836_000), date(2026, 3, 31), date(2026, 10, 1))
    assert late.verdict == "delivered_late" and late.days_late == 2
    short = check(FACTS, judgment, items, measured(700_000), date(2026, 4, 20), date(2026, 10, 1))
    assert short.verdict == "cannot_determine" and "measured_meets_target" in short.failed
    none = check(FACTS, judgment, items, measured(None), date(2026, 4, 20), date(2026, 10, 1))
    assert "measured_from_tools" in none.failed


def test_evidence_must_be_cited_and_dated_after_the_statement() -> None:
    items = items_with(("2025-03-01", "We produced 836,000 vehicles."))
    judgment = Judgment(
        verdict="delivered", evidence_ids=["E1", "E9"],
        reasoning="Production reached 836,000 vehicles.", confidence=0.9,
    )  # fmt: skip
    result = check(FACTS, judgment, items, measured(836_000), None, date(2026, 10, 1))
    assert {"evidence_exist", "evidence_after_statement"} <= set(result.failed)
    assert result.verdict == "cannot_determine"


def test_numbers_in_the_reasoning_must_be_grounded() -> None:
    items = items_with(("2026-04-02", "We produced 836,000 vehicles."))
    judgment = Judgment(
        verdict="delivered", evidence_ids=["E1"], first_proof_id="E1",
        reasoning="Production reached 912,000 vehicles.", confidence=0.9,
    )  # fmt: skip
    result = check(FACTS, judgment, items, measured(836_000), date(2026, 4, 20), date(2026, 10, 1))
    assert result.failed == ["numbers_grounded"]


def test_silence_is_not_failure() -> None:
    items = items_with(("2026-04-02", "We produced 600,000 vehicles."))
    judgment = Judgment(
        verdict="not_delivered", evidence_ids=["E1"],
        reasoning="Production was 600,000 vehicles.", confidence=0.8,
    )  # fmt: skip
    unreported = check(FACTS, judgment, items, measured(600_000), None, date(2026, 10, 1))
    assert "due_period_reported" in unreported.failed
    reported = check(
        FACTS, judgment, items, measured(600_000), date(2026, 4, 20), date(2026, 10, 1)
    )
    assert reported.verdict == "not_delivered"


def test_partly_delivered_lies_between_baseline_and_target() -> None:
    items = items_with(("2026-04-02", "We produced 600,000 vehicles."))
    judgment = Judgment(
        verdict="partly_delivered", evidence_ids=["E1"],
        reasoning="Production was 600,000 vehicles.", confidence=0.8,
    )  # fmt: skip
    between = check(FACTS, judgment, items, measured(600_000), date(2026, 4, 20), date(2026, 10, 1))
    assert between.verdict == "partly_delivered"
    below = check(FACTS, judgment, items, measured(400_000), date(2026, 4, 20), date(2026, 10, 1))
    assert "partly_between" in below.failed


def test_open_ended_claims_stay_open_unless_delivered() -> None:
    open_ended = PromiseFacts(
        statement_date=date(2025, 3, 10), due_date=None, target_kind="event",
        direction="at_least", required_value=None, baseline_value=None,
        resolution_rule="True if Tesla launches the Roadster.", quote="Roadster",
    )  # fmt: skip
    items = items_with(("2026-04-02", "No Roadster news."))
    judgment = Judgment(
        verdict="not_delivered", evidence_ids=["E1"], reasoning="No launch.", confidence=0.5
    )
    result = check(open_ended, judgment, items, measured(None), None, date(2026, 10, 1))
    assert result.verdict == "too_early" and not result.failed
    early = Judgment(verdict="too_early", reasoning="Not yet.", confidence=0.5)
    after_due = check(FACTS, early, items, measured(None), date(2026, 4, 20), date(2026, 10, 1))
    assert "not_too_early" in after_due.failed


def scored(verdict: str | None, *, speaker: str = "Elon Musk", bold: bool = False,
           days_late: int | None = None) -> Scored:  # fmt: skip
    return Scored("TSLA", speaker, verdict, bold, days_late, "promise")


def test_track_records_need_five_resolved_claims() -> None:
    claims = [
        scored("delivered", bold=True),
        scored("delivered"),
        scored("delivered_late", days_late=30),
        scored("delivered_late", days_late=90, bold=True),
        scored("not_delivered", bold=True),
        scored("walked_back"),
        scored("too_early"),
        scored(None),
        scored("delivered", speaker="company statement"),
    ]
    records = {r.key: r for r in track_records(claims, {("TSLA", "Elon Musk"): 2}, "executive")}
    musk = records["Elon Musk (TSLA)"]
    assert (musk.claims, musk.resolved, musk.vague) == (8, 6, 2)
    assert musk.on_time_rate == pytest.approx(2 / 6)
    assert musk.median_days_late == 60
    assert musk.walked_back_share == pytest.approx(1 / 6)
    assert musk.bold_share == pytest.approx(3 / 6)
    assert musk.bold_hit_rate == pytest.approx(2 / 3)
    company = records["TSLA (company)"]
    assert not company.enough and company.on_time_rate is None
    by_company = track_records(claims, {}, "company")
    assert [r.key for r in by_company] == ["TSLA"] and by_company[0].resolved == 7


def test_the_covering_quarter_allows_for_52_53_week_years() -> None:
    from talk_is_cheap.edgar.fiscal import FiscalQuarter
    from talk_is_cheap.promises.resolve import covering_quarter

    amd = FiscalCalendar(12, 26)
    assert covering_quarter(amd, date(2024, 12, 31)) == FiscalQuarter(2024, 4)
    assert covering_quarter(amd, date(2025, 1, 10)) == FiscalQuarter(2025, 1)
    assert covering_quarter(DECEMBER, date(2026, 6, 30)) == FiscalQuarter(2026, 2)
    assert covering_quarter(DECEMBER, date(2026, 5, 15)) == FiscalQuarter(2026, 2)


def test_a_year_over_year_change_is_measured_from_a_year_earlier() -> None:
    stored = actuals(
        ("revenue", 2025, 3, "94000000000", date(2025, 8, 1)),
        ("revenue", 2026, 2, "111000000000", date(2026, 5, 1)),
    )
    framed = frame(
        claim(
            "Revenue will grow 9% to 11% year over year in the fiscal third quarter",
            said=date(2026, 5, 2),
        ),
        answer(
            subject="revenue",
            metric="revenue",
            measure_period="quarter",
            target_kind="change",
            target_value=9,
            unit="pct",
            deadline=DeadlineAnswer(type="fiscal_quarter", quarter=3, year=2026),
        ).framing(),
        FiscalCalendar(9, 26),
        stored,
    )
    assert framed.baseline is not None and framed.baseline.period == "FY2025Q3"
    assert framed.required_value == Decimal("102460000000")
    assert any("year-over-year" in note for note in framed.notes)


def test_claims_sharing_a_quote_get_their_own_prompts() -> None:
    from dataclasses import replace

    from talk_is_cheap.promises.frame import load_prompt, render_prompt

    base = claim("Su projected $400 million for Q4 and more than $2 billion in 2024")
    plain = render_prompt(load_prompt(), base, "CEO", DECEMBER)
    first = render_prompt(load_prompt(), replace(base, focus="$400 million in Q4"), "CEO", DECEMBER)
    second = render_prompt(
        load_prompt(), replace(base, focus="$2 billion in 2024"), "CEO", DECEMBER
    )
    assert len({plain, first, second}) == 3 and first.startswith(plain)
    assert "Frame only this one: $400 million in Q4" in first
