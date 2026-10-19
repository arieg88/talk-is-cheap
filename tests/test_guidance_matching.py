"""Verdicts of guidance against first-reported values."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from talk_is_cheap.db.models import Guidance
from talk_is_cheap.guidance.matching import (
    Actuals,
    Reported,
    Split,
    check_plausibility,
    counts_in_verdicts,
    item_key,
    judge,
    needs_review,
)

B = Decimal(10) ** 9
FILED = date(2026, 11, 20)


def guidance(**overrides: Any) -> Guidance:
    values: dict[str, Any] = {
        "metric": "revenue",
        "segment": None,
        "basis": "unspecified",
        "measure": "level",
        "constant_currency": False,
        "target_fiscal_year": 2027,
        "target_fiscal_quarter": 3,
        "low": Decimal("105.84") * B,
        "high": Decimal("110.16") * B,
        "point": Decimal(108) * B,
        "unit": "usd",
        "issued_at": date(2026, 8, 26),
    }
    return Guidance(**{**values, **overrides})


def actuals(*values: tuple[str, int, int | None, Decimal], filed: date = FILED) -> Actuals:
    return Actuals(
        (metric, year, quarter, Reported(value, f"acc-{metric}-{year}-{quarter}", filed))
        for metric, year, quarter, value in values
    )


REVENUE = actuals(("revenue", 2027, 3, Decimal(112) * B))


@pytest.mark.parametrize(
    ("revenue", "verdict"),
    [
        (Decimal(112) * B, "beat"),
        (Decimal("110.16") * B, "met"),  # the edge of the range is met
        (Decimal(107) * B, "met"),
        (Decimal(105) * B, "missed"),
    ],
)
def test_ranges(revenue: Decimal, verdict: str) -> None:
    result = judge(guidance(), actuals(("revenue", 2027, 3, revenue)), [], "NVDA")
    assert result.verdict == verdict
    assert result.actual == revenue


def test_difference_is_from_the_point_and_in_percent() -> None:
    result = judge(guidance(), REVENUE, [], "NVDA")
    assert result.difference == 4 * B
    assert result.percent_difference == Decimal("3.7037")
    assert (result.actual_accession_no, result.actual_filed) == ("acc-revenue-2027-3", FILED)


def test_a_range_without_a_point_is_measured_from_its_midpoint() -> None:
    row = guidance(low=Decimal(61) * B, high=Decimal(64) * B, point=None)
    result = judge(row, actuals(("revenue", 2027, 3, Decimal(63) * B)), [], "META")
    assert (result.verdict, result.difference) == ("met", Decimal("0.5") * B)


def test_point_estimates_use_a_named_tolerance() -> None:
    opex = guidance(metric="operating_expenses", low=None, high=None, point=Decimal("9.2") * B)
    close = actuals(("operating_expenses", 2027, 3, Decimal("9.25") * B))  # within 1%
    assert judge(opex, close, [], "NVDA").verdict == "met"
    above = actuals(("operating_expenses", 2027, 3, Decimal("9.5") * B))
    assert judge(opex, above, [], "NVDA").verdict == "beat"  # spent more than guided


def test_margins_are_computed_from_first_reported_values_in_points() -> None:
    row = guidance(
        metric="gross_margin", basis="gaap", low=Decimal("73.5"), high=Decimal("74.5"),
        point=Decimal(74), unit="pct",
    )  # fmt: skip
    found = actuals(
        ("gross_profit", 2027, 3, Decimal("75.6") * B), ("revenue", 2027, 3, Decimal(100) * B)
    )
    result = judge(row, found, [], "NVDA")
    assert (result.verdict, result.actual, result.difference) == (
        "beat",
        Decimal("75.600000"),
        Decimal("1.600000"),
    )


def test_only_the_first_report_counts() -> None:
    restated = Actuals(
        [
            ("revenue", 2027, 3, Reported(Decimal(107) * B, "first", FILED)),
            ("revenue", 2027, 3, Reported(Decimal(112) * B, "restated", date(2027, 3, 1))),
        ]
    )
    result = judge(guidance(), restated, [], "NVDA")
    assert (result.verdict, result.actual_accession_no) == ("met", "first")


def test_full_year_guidance_uses_the_annual_value() -> None:
    row = guidance(target_fiscal_quarter=None, low=Decimal(90) * B, high=None, point=None)
    found = actuals(("revenue", 2027, None, Decimal(95) * B), ("revenue", 2027, 4, Decimal(30) * B))
    assert judge(row, found, [], "ORCL").verdict == "met"  # "at least $90 billion"


def test_growth_is_computed_from_both_periods() -> None:
    row = guidance(measure="yoy_growth", low=Decimal(9), high=Decimal(12), point=None, unit="pct")
    found = actuals(("revenue", 2027, 3, Decimal(110) * B), ("revenue", 2026, 3, Decimal(100) * B))
    result = judge(row, found, [], "AMZN")
    assert (result.verdict, result.actual) == ("met", Decimal("10.000000"))
    sequential = guidance(measure="sequential_growth", low=None, high=None, point=Decimal(13),
                          unit="pct")  # fmt: skip
    found = actuals(("revenue", 2027, 3, Decimal(113) * B), ("revenue", 2027, 2, Decimal(100) * B))
    assert judge(sequential, found, [], "AMD").verdict == "met"


def test_share_of_revenue() -> None:
    row = guidance(metric="capex", measure="share_of_revenue", target_fiscal_quarter=None,
                   low=None, high=None, point=Decimal("1.5"), unit="pct")  # fmt: skip
    found = actuals(("capex", 2027, None, Decimal("0.7") * B), ("revenue", 2027, None, 46 * B))
    result = judge(row, found, [], "CRM")
    assert result.verdict == "met" and result.actual == Decimal("1.521739")


def test_meta_capex_includes_finance_lease_principal_payments() -> None:
    row = guidance(metric="capex", target_fiscal_quarter=None, low=Decimal(130) * B,
                   high=Decimal(145) * B, point=None)  # fmt: skip
    found = actuals(
        ("capex", 2026, None, Decimal(127) * B),
        ("finance_lease_principal_payments", 2026, None, Decimal(4) * B),
    )
    row.target_fiscal_year = 2026
    assert judge(row, found, [], "META").actual == Decimal(131) * B
    assert judge(row, found, [], "NVDA").actual == Decimal(127) * B


def test_pending_until_the_period_is_reported() -> None:
    result = judge(guidance(), actuals(("revenue", 2027, 2, Decimal(1))), [], "NVDA")
    assert result.verdict == "pending"


def test_a_reported_period_without_the_metric_is_not_comparable() -> None:
    opex = guidance(metric="operating_expenses", low=None, high=None, point=Decimal(9) * B)
    result = judge(opex, REVENUE, [], "NFLX")
    assert result.verdict == "not_comparable"
    assert result.reason == "no reported operating_expenses for the period"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"basis": "non_gaap"}, "non-GAAP guidance; reported values are GAAP"),
        ({"segment": "Data Center"}, "segment guidance; reported values are company totals"),
        ({"constant_currency": True, "measure": "yoy_growth", "unit": "pct"},
         "constant-currency guidance; reported values are in US dollars"),
        ({"metric": "tax_rate", "unit": "pct"}, "tax rate is not compared"),
        ({"metric": "other"}, "metric not tracked in reported financials"),
        ({"low": None, "high": None, "point": None}, "qualitative guidance"),
        ({"target_fiscal_year": None, "target_fiscal_quarter": None},
         "target period not resolved"),
        ({"unit": "pct"}, "guided in pct, not usd"),
    ],
)  # fmt: skip
def test_not_comparable_with_a_reason(overrides: dict[str, Any], reason: str) -> None:
    result = judge(guidance(**overrides), REVENUE, [], "NVDA")
    assert (result.verdict, result.reason) == ("not_comparable", reason)


def test_per_share_guidance_across_a_split_is_not_comparable() -> None:
    eps = guidance(metric="eps", low=None, high=None, point=Decimal("5.95"), unit="usd_per_share",
                   issued_at=date(2024, 5, 22), target_fiscal_year=2025,
                   target_fiscal_quarter=2)  # fmt: skip
    found = actuals(("eps_diluted", 2025, 2, Decimal("0.67")), filed=date(2024, 8, 28))
    split = Split(date(2024, 6, 10), Decimal(10))
    result = judge(eps, found, [split], "NVDA")
    assert result.verdict == "not_comparable"
    assert (
        result.reason == "per-share guidance issued before the 10-for-1 stock split of 2024-06-10"
    )
    assert result.actual == Decimal("0.67")  # shown, not judged
    # Guidance issued after the split compares normally.
    after = guidance(metric="eps", low=None, high=None, point=Decimal("0.67"),
                     unit="usd_per_share", issued_at=date(2024, 6, 20), target_fiscal_year=2025,
                     target_fiscal_quarter=2)  # fmt: skip
    assert judge(after, found, [split], "NVDA").verdict == "met"


def test_per_share_point_tolerance_is_at_least_half_a_cent() -> None:
    eps = guidance(metric="eps", low=None, high=None, point=Decimal("0.00"), unit="usd_per_share")
    assert (
        judge(eps, actuals(("eps_diluted", 2027, 3, Decimal("0.00"))), [], "INTC").verdict == "met"
    )
    assert (
        judge(eps, actuals(("eps_diluted", 2027, 3, Decimal("0.01"))), [], "INTC").verdict == "beat"
    )


# Plausibility: the guided value against the prior period's actual.


def test_adobes_exchange_rate_headwind_read_as_revenue_is_flagged() -> None:
    headwind = guidance(target_fiscal_quarter=None, target_fiscal_year=2025, low=None,
                        high=None, point=Decimal(200_000_000))  # fmt: skip
    found = actuals(("revenue", 2024, None, Decimal("21.505") * B))
    result = check_plausibility(headwind, found, "ADBE")
    assert (result.status, result.rule) == ("flagged", "ratio")
    assert result.note == ("guided $200.0M is 1% of FY2024's $21.50B (plausible: 50% to 200%)")


def test_plausible_guidance_passes() -> None:
    found = actuals(("revenue", 2027, 2, Decimal(96) * B))  # the quarter before FY2027Q3
    result = check_plausibility(guidance(), found, "NVDA")
    assert result.status == "passed" and "112% of FY2027Q2's $96.00B" in result.note


@pytest.mark.parametrize(
    ("metric", "unit", "base", "guided", "status"),
    [
        ("revenue", "usd", Decimal(50) * B, Decimal(101) * B, "flagged"),  # above 200%
        ("capex", "usd", Decimal(20) * B, Decimal(50) * B, "passed"),  # capex allows 300%
        ("operating_income", "usd", Decimal(-2) * B, Decimal(1) * B, "skipped"),  # a loss
        ("operating_income", "usd", Decimal(5_000_000), Decimal(1) * B, "skipped"),  # tiny
        ("eps", "usd_per_share", Decimal("0.05"), Decimal("0.20"), "skipped"),
        ("eps", "usd_per_share", Decimal("6.88"), Decimal("1.36"), "flagged"),
        ("eps", "usd_per_share", Decimal("0.50"), Decimal("-0.10"), "skipped"),  # guided loss
    ],
)
def test_ratio_bounds_per_metric(
    metric: str, unit: str, base: Decimal, guided: Decimal, status: str
) -> None:
    reported = {"eps": "eps_diluted"}.get(metric, metric)
    row = guidance(metric=metric, unit=unit, low=None, high=None, point=guided)
    assert check_plausibility(row, actuals((reported, 2027, 2, base)), "X").status == status


def test_skips_without_a_prior_period() -> None:
    result = check_plausibility(guidance(), actuals(), "NVDA")
    assert (result.status, result.note) == ("skipped", "no FY2027Q2 value in the stored history")
    first_quarter = guidance(target_fiscal_quarter=1)  # the quarter before is last year's Q4
    assert "FY2026Q4" in check_plausibility(first_quarter, actuals(), "NVDA").note


def test_rates_are_checked_in_points() -> None:
    margin = guidance(metric="gross_margin", basis="gaap", low=None, high=None,
                      point=Decimal("36.5"), unit="pct")  # fmt: skip
    after_impairment = actuals(("gross_profit", 2027, 2, Decimal(15)), ("revenue", 2027, 2, 100))
    result = check_plausibility(margin, after_impairment, "INTC")
    assert (result.status, result.rule) == ("flagged", "margin_points")
    assert "+21.5 pp from FY2027Q2's 15.0%" in result.note
    growth = guidance(measure="yoy_growth", low=None, high=None, point=Decimal(12), unit="pct")
    found = actuals(
        ("revenue", 2027, 2, Decimal(110)), ("revenue", 2026, 2, Decimal(100))
    )  # prior growth 10%
    assert check_plausibility(growth, found, "AMZN").status == "passed"
    fx = guidance(measure="yoy_growth", low=None, high=None, point=Decimal(-41), unit="pct")
    assert check_plausibility(fx, found, "AMZN").rule == "growth_points"
    assert check_plausibility(fx, found, "AMZN").status == "flagged"


def test_flags_and_reviews_decide_what_counts() -> None:
    row = guidance(verdict="beat", plausibility="flagged", review=None)
    assert needs_review(row) and not counts_in_verdicts(row)
    row.review = "ok"
    assert not needs_review(row) and counts_in_verdicts(row)
    row.review = "wrong"
    assert not counts_in_verdicts(row)
    assert counts_in_verdicts(guidance(verdict="met", plausibility="passed", review=None))


def test_item_keys_ignore_how_figures_are_written() -> None:
    first = guidance(low=Decimal("105840000000.0"), segment="Data  Center")
    stored = guidance(low=Decimal("105840000000.000000"), segment="data center")
    assert item_key(first) == item_key(stored)
    assert item_key(first) != item_key(guidance(low=Decimal(106) * B))
