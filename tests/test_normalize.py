from datetime import date
from decimal import Decimal

import pytest

from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.edgar.normalize import (
    FinancialValue,
    MetricSpec,
    load_metric_specs,
    normalize,
    required_concepts,
)
from talk_is_cheap.edgar.records import RawFact

CALENDAR_YEAR = FiscalCalendar.parse("1231")
SPECS = load_metric_specs()

# Filings of a calendar-year company: three 10-Qs, then the 10-K.
Q1_10Q = ("0000000001-25-000001", "10-Q", date(2025, 4, 30))
Q2_10Q = ("0000000001-25-000002", "10-Q", date(2025, 7, 30))
Q3_10Q = ("0000000001-25-000003", "10-Q", date(2025, 10, 30))
FY_10K = ("0000000001-26-000001", "10-K", date(2026, 2, 20))
NEXT_Q3_10Q = ("0000000001-26-000003", "10-Q", date(2026, 10, 29))


def fact(
    concept: str,
    value: str,
    start: date | None,
    end: date,
    filing: tuple[str, str, date],
    *,
    unit: str = "USD",
) -> RawFact:
    taxonomy, name = concept.split(":")
    accession, form, filed = filing
    return RawFact(
        taxonomy=taxonomy,
        concept=name,
        unit=unit,
        value=Decimal(value),
        period_start=start,
        period_end=end,
        accession_no=accession,
        form=form,
        filed=filed,
        fiscal_year=None,
        fiscal_period=None,
    )


def values_for(
    values: list[FinancialValue], metric: str, *, quarter: int | None
) -> list[FinancialValue]:
    return [
        v
        for v in values
        if v.metric == metric
        and v.fiscal_quarter == quarter
        and v.period_type == ("year" if quarter is None else "quarter")
    ]


def only(values: list[FinancialValue]) -> FinancialValue:
    assert len(values) == 1, values
    return values[0]


def test_metric_config_is_consistent() -> None:
    assert {"revenue", "gross_profit", "operating_income", "eps_diluted", "capex"} <= set(SPECS)
    assert not SPECS["eps_diluted"].additive
    assert SPECS["gross_profit"].fallback == ("revenue", "cost_of_revenue")
    assert all(":" in c for c in required_concepts(SPECS))


def test_reported_quarter_and_year_are_kept_as_reported() -> None:
    values = normalize(
        [
            fact("us-gaap:Revenues", "100", date(2025, 1, 1), date(2025, 3, 31), Q1_10Q),
            fact("us-gaap:Revenues", "400", date(2025, 1, 1), date(2025, 12, 31), FY_10K),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    q1 = only(values_for(values, "revenue", quarter=1))
    assert (q1.fiscal_year, q1.value, q1.derived, q1.accession_no) == (
        2025,
        Decimal("100"),
        False,
        Q1_10Q[0],
    )
    year = only(values_for(values, "revenue", quarter=None))
    assert (year.value, year.derived) == (Decimal("400"), False)


def test_q4_is_derived_from_full_year_minus_nine_months() -> None:
    values = normalize(
        [
            fact("us-gaap:Revenues", "270", date(2025, 1, 1), date(2025, 9, 30), Q3_10Q),
            fact("us-gaap:Revenues", "400", date(2025, 1, 1), date(2025, 12, 31), FY_10K),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    q4 = only(values_for(values, "revenue", quarter=4))
    assert q4.value == Decimal("130")
    assert q4.derived
    assert q4.derivation == "12M YTD - 9M YTD"
    assert (q4.period_start, q4.period_end) == (date(2025, 10, 1), date(2025, 12, 31))
    assert (q4.accession_no, q4.filed) == (FY_10K[0], FY_10K[2])


def test_cash_flow_quarters_are_derived_from_cumulative_statements() -> None:
    concept = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
    values = normalize(
        [
            fact(concept, "10", date(2025, 1, 1), date(2025, 3, 31), Q1_10Q),
            fact(concept, "25", date(2025, 1, 1), date(2025, 6, 30), Q2_10Q),
            fact(concept, "45", date(2025, 1, 1), date(2025, 9, 30), Q3_10Q),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    quarters = {
        v.fiscal_quarter: v.value
        for v in values
        if v.metric == "operating_cash_flow" and v.period_type == "quarter"
    }
    assert quarters == {1: Decimal("10"), 2: Decimal("15"), 3: Decimal("20")}


def test_derivation_uses_values_public_on_the_filing_date() -> None:
    """A later restatement of the nine-month total must not change Q4 as first reported."""
    values = normalize(
        [
            fact("us-gaap:Revenues", "270", date(2025, 1, 1), date(2025, 9, 30), Q3_10Q),
            fact("us-gaap:Revenues", "400", date(2025, 1, 1), date(2025, 12, 31), FY_10K),
            # Next year's Q3 10-Q restates the prior-year comparative.
            fact("us-gaap:Revenues", "280", date(2025, 1, 1), date(2025, 9, 30), NEXT_Q3_10Q),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    q4 = only(values_for(values, "revenue", quarter=4))
    assert q4.value == Decimal("130")


def test_every_filing_version_of_a_quarter_is_kept() -> None:
    values = normalize(
        [
            fact("us-gaap:Revenues", "100", date(2025, 1, 1), date(2025, 3, 31), Q1_10Q),
            fact("us-gaap:Revenues", "105", date(2025, 1, 1), date(2025, 3, 31), NEXT_Q3_10Q),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    versions = sorted((v.filed, v.value) for v in values_for(values, "revenue", quarter=1))
    assert versions == [(Q1_10Q[2], Decimal("100")), (NEXT_Q3_10Q[2], Decimal("105"))]


def test_per_share_values_are_never_derived() -> None:
    values = normalize(
        [
            fact(
                "us-gaap:EarningsPerShareDiluted",
                "2.70",
                date(2025, 1, 1),
                date(2025, 9, 30),
                Q3_10Q,
                unit="USD/shares",
            ),
            fact(
                "us-gaap:EarningsPerShareDiluted",
                "4.00",
                date(2025, 1, 1),
                date(2025, 12, 31),
                FY_10K,
                unit="USD/shares",
            ),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    assert values_for(values, "eps_diluted", quarter=4) == []
    assert only(values_for(values, "eps_diluted", quarter=None)).value == Decimal("4.00")


def test_highest_priority_concept_wins_within_a_filing() -> None:
    q1 = (date(2025, 1, 1), date(2025, 3, 31))
    values = normalize(
        [
            fact("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "90", *q1, Q1_10Q),
            fact("us-gaap:Revenues", "100", *q1, Q1_10Q),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    picked = only(values_for(values, "revenue", quarter=1))
    assert (picked.value, picked.source_concept) == (Decimal("100"), "us-gaap:Revenues")


def test_a_concept_switch_between_filings_is_still_one_metric() -> None:
    contract = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    values = normalize(
        [
            fact(contract, "270", date(2025, 1, 1), date(2025, 9, 30), Q3_10Q),
            fact("us-gaap:Revenues", "400", date(2025, 1, 1), date(2025, 12, 31), FY_10K),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    assert only(values_for(values, "revenue", quarter=4)).value == Decimal("130")


def test_gross_profit_falls_back_to_revenue_minus_cost() -> None:
    q1 = (date(2025, 1, 1), date(2025, 3, 31))
    values = normalize(
        [
            fact("us-gaap:Revenues", "100", *q1, Q1_10Q),
            fact("us-gaap:CostOfRevenue", "40", *q1, Q1_10Q),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    gross = only(values_for(values, "gross_profit", quarter=1))
    assert gross.value == Decimal("60")
    assert gross.derived
    assert gross.derivation == "revenue - cost_of_revenue"


def test_reported_gross_profit_beats_the_fallback() -> None:
    q1 = (date(2025, 1, 1), date(2025, 3, 31))
    values = normalize(
        [
            fact("us-gaap:Revenues", "100", *q1, Q1_10Q),
            fact("us-gaap:CostOfRevenue", "40", *q1, Q1_10Q),
            fact("us-gaap:GrossProfit", "61", *q1, Q1_10Q),
        ],
        SPECS,
        CALENDAR_YEAR,
    )
    gross = only(values_for(values, "gross_profit", quarter=1))
    assert (gross.value, gross.derived) == (Decimal("61"), False)


@pytest.mark.parametrize(
    "ignored",
    [
        # Wrong unit for the metric.
        fact("us-gaap:Revenues", "100", date(2025, 1, 1), date(2025, 3, 31), Q1_10Q, unit="EUR"),
        # Point-in-time value: no period start.
        fact("us-gaap:Revenues", "100", None, date(2025, 3, 31), Q1_10Q),
        # A six-month window that does not start the fiscal year.
        fact("us-gaap:Revenues", "200", date(2025, 4, 1), date(2025, 9, 30), Q3_10Q),
        # A concept no metric uses.
        fact("us-gaap:Assets", "100", date(2025, 1, 1), date(2025, 3, 31), Q1_10Q),
    ],
)
def test_unusable_facts_are_ignored(ignored: RawFact) -> None:
    assert normalize([ignored], SPECS, CALENDAR_YEAR) == []


def test_non_calendar_fiscal_year_derivation() -> None:
    """Nvidia-style year ending in late January: Q4 of fiscal 2026 ends Jan 25, 2026."""
    nvidia = FiscalCalendar.parse("0126")
    q3 = ("0001045810-25-000230", "10-Q", date(2025, 11, 19))
    annual = ("0001045810-26-000021", "10-K", date(2026, 2, 25))
    values = normalize(
        [
            fact("us-gaap:Revenues", "300", date(2025, 1, 27), date(2025, 10, 26), q3),
            fact("us-gaap:Revenues", "420", date(2025, 1, 27), date(2026, 1, 25), annual),
        ],
        SPECS,
        nvidia,
    )
    q4 = only(values_for(values, "revenue", quarter=4))
    assert (q4.fiscal_year, q4.value) == (2026, Decimal("120"))
    assert q4.period_start == date(2025, 10, 27)


def test_spec_without_fallback_has_none() -> None:
    spec = MetricSpec(name="x", unit="USD", additive=True, concepts=("us-gaap:X",))
    assert spec.fallback is None
