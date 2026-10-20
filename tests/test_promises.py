"""The promise tracker's deterministic parts: due dates, framing (grounding, baselines,
specificity, boldness, the rule), the resolver's checks, cited numbers and track
records."""

from dataclasses import replace
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
from talk_is_cheap.promises.schema import (
    Assessment,
    Deadline,
    DeadlineAnswer,
    FramingAnswer,
    Judgment,
    MeasuredSource,
)
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


def judged(
    *findings: tuple[str, str], measured_ids: tuple[str, ...] = (), reason: str = ""
) -> Judgment:
    return Judgment(
        assessments=[Assessment(evidence_id=i, finding=f) for i, f in findings],  # type: ignore[arg-type]
        measured=[MeasuredSource(evidence_id=i) for i in measured_ids],
        reasoning=reason,
        confidence=0.9,
    )


def test_code_dates_delivery_from_the_earliest_satisfying_evidence() -> None:
    items = items_with(
        ("2026-05-02", "We expect to produce 800,000 vehicles."),
        ("2026-04-02", "We produced 836,000 vehicles."),
        ("2026-04-25", "Production reached 836,000 vehicles."),
    )
    judgment = judged(("E1", "irrelevant"), ("E2", "satisfies"), ("E3", "satisfies"),
                      measured_ids=("E2",))  # fmt: skip
    ok = check(FACTS, judgment, items, measured(836_000), date(2026, 4, 20), date(2026, 10, 1))
    assert ok.verdict == "delivered" and ok.proof_date == date(2026, 4, 2) and not ok.failed
    assert ok.cited == ["E2", "E3"]  # the irrelevant item is not cited
    late = check(FACTS, judgment, items, measured(836_000), date(2026, 3, 31), date(2026, 10, 1))
    assert late.verdict == "delivered_late" and late.days_late == 2
    short = check(FACTS, judgment, items, measured(700_000), date(2026, 4, 20), date(2026, 10, 1))
    assert short.verdict == "cannot_determine" and "measured_meets_target" in short.failed
    none = check(FACTS, judgment, items, measured(None), date(2026, 4, 20), date(2026, 10, 1))
    assert "measured_from_tools" in none.failed


def test_evidence_must_exist_and_be_dated_after_the_statement() -> None:
    items = items_with(("2025-03-01", "We produced 836,000 vehicles."))
    judgment = judged(("E1", "satisfies"), ("E9", "satisfies"), measured_ids=("E1",))
    result = check(FACTS, judgment, items, measured(836_000), None, date(2026, 10, 1))
    assert {"evidence_exist", "evidence_after_statement"} <= set(result.failed)
    assert result.verdict == "cannot_determine"


def test_numbers_in_the_reasoning_must_be_grounded() -> None:
    items = items_with(("2026-04-02", "We produced 836,000 vehicles."))
    judgment = judged(("E1", "satisfies"), measured_ids=("E1",),
                      reason="Production reached 912,000 vehicles.")  # fmt: skip
    result = check(FACTS, judgment, items, measured(836_000), date(2026, 4, 20), date(2026, 10, 1))
    assert result.failed == ["numbers_grounded"]


def test_silence_is_not_failure() -> None:
    items = items_with(("2026-04-02", "We produced 300,000 vehicles."))
    contradicting = judged(("E1", "contradicts"), measured_ids=("E1",))
    early = check(FACTS, contradicting, items, measured(300_000), None, date(2026, 10, 1))
    assert early.verdict == "too_early" and not early.failed
    reported = check(
        FACTS, contradicting, items, measured(300_000), date(2026, 4, 20), date(2026, 10, 1)
    )
    assert reported.verdict == "not_delivered"
    silent = check(FACTS, judged(("E1", "irrelevant")), items, measured(None),
                   date(2026, 4, 20), date(2026, 10, 1))  # fmt: skip
    assert silent.verdict == "cannot_determine" and not silent.failed


def test_partly_delivered_lies_between_baseline_and_target() -> None:
    items = items_with(("2026-04-02", "We produced 600,000 vehicles."))
    judgment = judged(("E1", "partly"), measured_ids=("E1",))
    grace, today = date(2026, 4, 20), date(2026, 10, 1)
    assert (
        check(FACTS, judgment, items, measured(600_000), grace, today).verdict == "partly_delivered"
    )
    below = check(FACTS, judgment, items, measured(400_000), grace, today)
    assert below.verdict == "not_delivered"  # below the baseline: code reads a contradiction
    no_baseline = replace(FACTS, baseline_value=None)
    unknown = check(no_baseline, judgment, items, measured(400_000), grace, today)
    assert unknown.verdict == "not_delivered"


EVENT = PromiseFacts(
    statement_date=date(2024, 1, 8), due_date=None, target_kind="event",
    direction="at_least", required_value=None, baseline_value=None,
    resolution_rule="True if this happens at any time after 2024-01-08 (no deadline): Dell "
    "and HP ship laptops with GeForce RTX 40 SUPER GPUs.",
    quote="laptops", company_names=("Nvidia", "NVDA"),
)  # fmt: skip


def test_key_terms_must_be_in_the_cited_evidence() -> None:
    from talk_is_cheap.promises.checks import key_terms

    terms = key_terms(EVENT.resolution_rule, EVENT.company_names)
    assert {"Dell", "HP", "GeForce", "RTX", "SUPER", "GPUs"} <= set(terms)
    assert "Nvidia" not in terms and "2024-01-08" not in terms
    later = items_with(("2026-10-16", "RTX Spark laptops from Dell and HP are available today."))
    wrong = check(
        EVENT, judged(("E1", "satisfies")), later, measured(None), None, date(2026, 10, 1)
    )
    assert wrong.verdict == "cannot_determine" and wrong.failed == ["key_terms"]
    right = items_with(
        ("2024-01-30", "Dell and HP laptops with GeForce RTX 40 SUPER GPUs ship this week.")
    )
    shipped = check(EVENT, judged(("E1", "satisfies")), right, measured(None), None,
                    date(2026, 10, 1))  # fmt: skip
    assert shipped.verdict == "delivered" and not shipped.failed


def test_open_ended_claims_stay_open_unless_delivered() -> None:
    items = items_with(("2026-04-02", "No laptop news."))
    open_ = check(EVENT, judged(("E1", "contradicts")), items, measured(None), None,
                  date(2026, 10, 1))  # fmt: skip
    assert open_.verdict == "too_early" and not open_.failed
    after_due = check(FACTS, judged(("E1", "irrelevant")), items, measured(None),
                      date(2026, 9, 1), date(2026, 10, 1))  # fmt: skip
    assert after_due.verdict == "cannot_determine"


def test_a_later_statement_walks_the_claim_back() -> None:
    items = items_with(("2026-02-01", "We now expect to reach 800,000 vehicles in 2027."))
    result = check(FACTS, judged(("E1", "walks_back")), items, measured(None), None,
                   date(2026, 3, 1))  # fmt: skip
    assert result.verdict == "walked_back" and not result.failed


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


def test_this_and_next_month_end_with_their_month() -> None:
    said = date(2024, 5, 22)
    assert due_date(deadline(type="month", offset=1), said, DECEMBER) == due_date(
        deadline(type="month", offset=1), said, None
    )
    assert due_date(deadline(type="month", offset=1), said, None).date == date(2024, 6, 30)
    assert due_date(deadline(type="month", offset=0), said, None).date == date(2024, 5, 31)
    assert due_date(deadline(type="month", offset=8), said, None).date == date(2025, 1, 31)


def test_gathering_widens_to_related_companies_and_the_target_period() -> None:
    from talk_is_cheap.db.models import Promise
    from talk_is_cheap.promises.resolve import tool_calls
    from talk_is_cheap.promises.schema import EvidencePlan
    from talk_is_cheap.universe import Universe, UniverseCompany

    universe = Universe(
        history_start=date(2024, 1, 1),
        forms=["10-Q"],
        companies=[
            UniverseCompany(ticker="MSFT", name="Microsoft"),
            UniverseCompany(ticker="AMD", name="AMD"),
        ],
    )
    promise = Promise(
        ticker="MSFT", statement_date=date(2024, 5, 25), subject="MI300X instances",
        metric="revenue", measure_period="quarter", due_date=date(2024, 9, 30),
        related_companies=["AMD", "Globex"],
    )  # fmt: skip
    plan = EvidencePlan(
        actuals_periods=["FY2025Q1"], filing_queries=["MI300X Azure"], news_queries=["Azure"],
        check_later_statements=True,
    )  # fmt: skip
    calls = tool_calls(promise, plan, universe, FiscalCalendar(6, 30))
    assert calls[0] == (
        "get_actuals",
        {"ticker": "MSFT", "metrics": ["revenue"], "periods": ["FY2025Q1"], "vintage": "first"},
    )
    own = [c for c in calls if c[1].get("ticker") == "MSFT"]
    assert len(own) == 4  # actuals, filings, news, statements: the capped calls
    # Then each related company on its own, with its own results.
    assert calls[-2:] == [
        (
            "search_filings",
            {"query": "MI300X instances", "date_from": "2024-05-26", "k": 5, "ticker": "AMD"},
        ),
        ("search_news", {"query": "Globex MI300X instances", "date_from": "2024-05-26", "k": 5}),
    ]


def test_hyphenated_words_and_measures_are_not_key_terms_themselves() -> None:
    from talk_is_cheap.promises.checks import key_terms

    rule = (
        "True if this happens at any time after 2024-05-22 (no deadline): Dell, HPE and "
        "Lenovo offer Blackwell-based machines after a 10-for-1 split, with RTX-class GPUs."
    )
    assert key_terms(rule, ["Nvidia"]) == ["Dell", "HPE", "Lenovo", "Blackwell", "RTX", "GPUs"]
    eps = (
        "True if Qualcomm's fiscal third-quarter adjusted EPS reaches at least $2.1 in any quarter."
    )
    assert key_terms(eps, ["Qualcomm"]) == []


def test_code_judges_the_measured_items_of_a_numeric_target() -> None:
    items = items_with(("2026-04-02", "We produced 836,000 vehicles."))
    wrong = judged(("E1", "contradicts"), measured_ids=("E1",))
    grace, today = date(2026, 4, 20), date(2026, 10, 1)
    result = check(FACTS, wrong, items, measured(836_000), grace, today)
    assert result.verdict == "delivered" and not result.failed
    toward = check(FACTS, judged(("E1", "satisfies"), measured_ids=("E1",)), items,
                   measured(600_000), grace, today)  # fmt: skip
    assert toward.verdict == "partly_delivered" and not toward.failed
    short = check(FACTS, judged(("E1", "satisfies"), measured_ids=("E1",)), items,
                  measured(300_000), grace, today)  # fmt: skip
    assert short.verdict == "not_delivered" and not short.failed


def test_a_quarter_without_a_year_is_the_next_one_not_yet_reported() -> None:
    from talk_is_cheap.edgar.fiscal import FiscalQuarter

    apple = FiscalCalendar(9, 26)
    q3 = deadline(type="fiscal_quarter", quarter=3)
    said = date(2026, 7, 30)  # results day for fiscal Q3 2026, which ended June 27
    assert due_date(q3, said, apple, FiscalQuarter(2026, 2)).date == date(2026, 6, 26)
    assert due_date(q3, said, apple, FiscalQuarter(2026, 3)).date == date(2027, 6, 26)
    assert due_date(q3, said, apple).date == date(2027, 6, 26)  # without filings: the next


def test_dates_stated_in_the_evidence_decide_timeliness() -> None:
    from talk_is_cheap.promises.checks import parse_date

    assert parse_date("effective June 7, 2024") == date(2024, 6, 7)
    assert parse_date("on 2024-06-28") == date(2024, 6, 28)
    assert parse_date("in June 2024") == date(2024, 6, 30)
    assert parse_date("in the fourth quarter") is None
    event = replace(EVENT, due_date=date(2024, 6, 30), resolution_rule="True if: the split.")
    items = items_with(("2024-11-20", "The ten-for-one split was effective June 7, 2024."))
    findings = Judgment(
        assessments=[
            Assessment(
                evidence_id="E1", finding="satisfies", event_date_text="effective June 7, 2024"
            )
        ]
    )
    on_time = check(event, findings, items, measured(None), date(2024, 8, 28), date(2026, 1, 1))
    assert on_time.verdict == "delivered" and on_time.proof_date == date(2024, 6, 7)
    assert on_time.proof_date_source == "event"
    repeated = items_with(("2024-01-09", "The split takes effect on January 2, 2024."))
    earlier = Judgment(
        assessments=[
            Assessment(evidence_id="E1", finding="satisfies", event_date_text="January 2, 2024")
        ]
    )
    stale = check(replace(event, statement_date=date(2024, 1, 5)), earlier, repeated,
                  measured(None), date(2024, 8, 28), date(2026, 1, 1))  # fmt: skip
    assert stale.verdict == "cannot_determine"  # an event before the claim is no proof


def test_revised_before_due_and_delivered_later_is_late() -> None:
    items = items_with(
        ("2024-08-28", "The Blackwell production ramp is now scheduled for the fourth quarter."),
        ("2025-02-26", "We delivered $11.0 billion of Blackwell revenue."),
    )
    blackwell = replace(
        EVENT, statement_date=date(2024, 5, 23), due_date=date(2024, 7, 31),
        resolution_rule="True if this happens on or before 2024-07-31: Nvidia ships Blackwell.",
        quote="Blackwell",
    )  # fmt: skip
    findings = judged(("E1", "walks_back"), ("E2", "satisfies"))
    late = check(blackwell, findings, items, measured(None), date(2024, 8, 28), date(2026, 1, 1))
    assert late.verdict == "delivered_late" and late.revised_before_due
    never = check(blackwell, judged(("E1", "walks_back")), items, measured(None),
                  date(2024, 8, 28), date(2026, 1, 1))  # fmt: skip
    assert never.verdict == "walked_back"


def test_open_ended_claims_expire_unproven_after_18_months() -> None:
    items = items_with(("2026-04-02", "No laptop news."))
    expiring = replace(EVENT, expires=date(2025, 7, 8))
    expired = check(expiring, judged(("E1", "irrelevant")), items, measured(None), None,
                    date(2026, 1, 1))  # fmt: skip
    assert expired.verdict == "expired_unproven"
    still = check(expiring, judged(("E1", "irrelevant")), items, measured(None), None,
                  date(2025, 1, 1))  # fmt: skip
    assert still.verdict == "too_early"


def test_per_share_targets_are_split_adjusted() -> None:
    dividend = replace(
        FACTS, target_kind="threshold", required_value=Decimal("0.10"), baseline_value=None,
        per_share=True, split_ratio=Decimal(10), resolution_rule="True if: a dividend.",
        statement_date=date(2024, 5, 22), due_date=date(2024, 6, 28),
    )  # fmt: skip
    items = items_with(("2024-08-28", "a quarterly cash dividend of $0.01 per share"))
    paid = Measured(Decimal("0.01"), "USD", None)
    findings = judged(("E1", "satisfies"), measured_ids=("E1",))
    result = check(dividend, findings, items, paid, date(2024, 8, 28), date(2026, 1, 1))
    assert result.verdict == "delivered"
    assert "split_adjusted" in [c["check"] for c in result.checks]


def test_same_day_evidence_counts_when_it_is_later_or_reports_a_later_period() -> None:
    from datetime import UTC, datetime

    said = replace(FACTS, statement_date=date(2026, 7, 29),
                   statement_at=datetime(2026, 7, 29, 14, tzinfo=UTC))  # fmt: skip
    log = EvidenceLog()
    later = evidence(
        log,
        "get_actuals",
        {"filed": "2026-07-29", "accepted": "2026-07-29T20:05:00+00:00", "display": "x"},
    )
    earlier = evidence(
        log,
        "get_actuals",
        {"filed": "2026-07-29", "accepted": "2026-07-29T08:00:00+00:00", "display": "x"},
    )
    period = evidence(
        log, "search_filings", {"filed": "2026-07-29", "period_end": "2026-09-27", "text": "x"}
    )
    for item_id, ok in ((later, True), (earlier, False), (period, True)):
        result = check(said, judged((item_id, "irrelevant"), measured_ids=(item_id,)), log.items,
                       measured(None), None, date(2026, 8, 1))  # fmt: skip
        failed = "evidence_after_statement" in result.failed
        assert failed is not ok, item_id


def test_satisfying_evidence_resolves_a_claim_before_its_due_date() -> None:
    items = items_with(("2025-06-02", "We produced 836,000 vehicles."))
    early = check(FACTS, judged(("E1", "satisfies"), measured_ids=("E1",)), items,
                  measured(836_000), None, date(2025, 7, 1))  # fmt: skip
    assert early.verdict == "delivered" and early.proof_date == date(2025, 6, 2)
    nothing = check(FACTS, judged(("E1", "irrelevant")), items, measured(None), None,
                    date(2025, 7, 1))  # fmt: skip
    assert nothing.verdict == "too_early"


def test_a_delivered_claim_is_never_walked_back() -> None:
    items = items_with(
        ("2025-05-01", "We now expect to reach 800,000 vehicles later than planned."),
        ("2026-09-01", "We produced 836,000 vehicles."),
    )
    for grace in (None, date(2026, 4, 20)):
        findings = judged(("E1", "walks_back"), ("E2", "satisfies"), measured_ids=("E2",))
        result = check(FACTS, findings, items, measured(836_000), grace, date(2026, 10, 1))
        assert result.verdict == "delivered_late" and result.revised_before_due


def test_the_judge_reads_whole_passages_or_windows_around_key_terms() -> None:
    from talk_is_cheap.promises.resolve import JUDGE_TEXT_CHARS, judge_text

    short = "Microsoft announced Azure ND MI300X v5 instances."
    assert judge_text(short, ["MI300X"]) == short
    long = (
        "x " * 1500
        + "Microsoft announced general availability of Azure ND MI300X v5 instances. "
        + "y " * 1500
    )
    read = judge_text(long, ["MI300X", "Azure"])
    assert "Azure ND MI300X v5" in read and len(read) <= JUDGE_TEXT_CHARS + 10
    assert judge_text(long, ["Nothing"]).startswith("x x")
