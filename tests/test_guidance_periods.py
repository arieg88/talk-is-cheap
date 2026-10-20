"""Target periods of guidance, resolved from the release's fiscal quarter, for fiscal
years ending in January (Nvidia, Salesforce), June (Microsoft), September (Apple,
Qualcomm) and December (AMD, Meta)."""

import pytest

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.periods import FiscalPeriod, resolve_period

JANUARY = FiscalCalendar.parse("0131")
JUNE = FiscalCalendar.parse("0630")
SEPTEMBER = FiscalCalendar.parse("0927")
DECEMBER = FiscalCalendar.parse("1231")


def q(year: int, quarter: int) -> FiscalPeriod:
    return FiscalPeriod(year, quarter)


def fy(year: int) -> FiscalPeriod:
    return FiscalPeriod(year)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("third quarter of fiscal 2027", q(2027, 3)),
        ("Q3 FY27", q(2027, 3)),
        ("Q3 FY 2027", q(2027, 3)),
        ("fiscal 2027 third quarter", q(2027, 3)),
        ("FY27 Q3", q(2027, 3)),
        ("next quarter", q(2027, 3)),
        ("the third quarter", q(2027, 3)),
        ("fourth quarter", q(2027, 4)),
        ("first quarter", q(2028, 1)),  # the next first quarter is in the next year
        ("full year fiscal 2027", fy(2027)),
        ("fiscal year 2027", fy(2027)),
        ("FY27", fy(2027)),
        ("full year", fy(2027)),
        ("next fiscal year", fy(2028)),
    ],
)
def test_january_year_end(text: str, expected: FiscalPeriod) -> None:
    # Nvidia's release for the quarter ended July 2026, fiscal 2027 Q2.
    assert resolve_period(text, FiscalQuarter(2027, 2), JANUARY) == expected


def test_a_fourth_quarter_release_speaks_of_the_new_year() -> None:
    issued = FiscalQuarter(2026, 4)
    assert resolve_period("next quarter", issued, JANUARY) == q(2027, 1)
    assert resolve_period("first quarter", issued, JANUARY) == q(2027, 1)
    assert resolve_period("full year", issued, JANUARY) == fy(2027)
    assert resolve_period("for the year", issued, DECEMBER) == fy(2027)


@pytest.mark.parametrize(
    ("text", "issued", "expected"),
    [
        ("Q1 FY 2027", FiscalQuarter(2026, 4), q(2027, 1)),
        ("fiscal 2027", FiscalQuarter(2026, 4), fy(2027)),
        ("Q2 FY 2026", FiscalQuarter(2026, 1), q(2026, 2)),
        # Microsoft's year ends June 30: the quarter ending December 31, 2026 is fiscal 2027 Q2.
        ("the quarter ending December 31, 2026", FiscalQuarter(2027, 1), q(2027, 2)),
        ("calendar 2026", FiscalQuarter(2026, 4), None),
    ],
)
def test_june_year_end(text: str, issued: FiscalQuarter, expected: FiscalPeriod | None) -> None:
    assert resolve_period(text, issued, JUNE) == expected


@pytest.mark.parametrize(
    ("text", "issued", "expected"),
    [
        ("Q4 FY26", FiscalQuarter(2026, 3), q(2026, 4)),  # Qualcomm
        ("fourth quarter of fiscal 2026", FiscalQuarter(2026, 3), q(2026, 4)),
        ("first quarter", FiscalQuarter(2026, 4), q(2027, 1)),
        ("the quarter ending December 27, 2026", FiscalQuarter(2026, 4), q(2027, 1)),
        ("fiscal year ending September 2027", FiscalQuarter(2026, 4), fy(2027)),
        ("the September quarter", FiscalQuarter(2026, 3), q(2026, 4)),  # Apple's wording
        ("the December quarter", FiscalQuarter(2026, 3), q(2027, 1)),
        ("the December quarter of 2026", FiscalQuarter(2026, 2), q(2027, 1)),
    ],
)
def test_september_year_end(
    text: str, issued: FiscalQuarter, expected: FiscalPeriod | None
) -> None:
    assert resolve_period(text, issued, SEPTEMBER) == expected


@pytest.mark.parametrize(
    ("text", "issued", "expected"),
    [
        ("third quarter of 2026", FiscalQuarter(2026, 2), q(2026, 3)),  # AMD
        ("third quarter 2026", FiscalQuarter(2026, 2), q(2026, 3)),  # Meta
        ("Q4'25", FiscalQuarter(2025, 3), q(2025, 4)),  # Netflix
        ("Q4\u201925", FiscalQuarter(2025, 3), q(2025, 4)),
        ("2026", FiscalQuarter(2025, 4), fy(2026)),
        ("full year 2026", FiscalQuarter(2026, 1), fy(2026)),
        ("calendar 2026", FiscalQuarter(2025, 4), fy(2026)),  # same as the fiscal year
        ("Q1", FiscalQuarter(2025, 4), q(2026, 1)),
    ],
)
def test_december_year_end(text: str, issued: FiscalQuarter, expected: FiscalPeriod | None) -> None:
    assert resolve_period(text, issued, DECEMBER) == expected


@pytest.mark.parametrize(
    "text",
    [
        "second half of 2025",
        "1H25",
        "remaining quarters of 2026",
        "the rest of the year",
        "over time",
    ],
)
def test_periods_that_are_not_one_quarter_or_year(text: str) -> None:
    assert resolve_period(text, FiscalQuarter(2025, 2), DECEMBER) is None


def test_labels_round_trip() -> None:
    assert FiscalPeriod.parse("FY2027Q3") == q(2027, 3)
    assert FiscalPeriod.parse("FY2027") == fy(2027)
    assert q(2027, 3).label == "FY2027Q3" and fy(2027).label == "FY2027"
