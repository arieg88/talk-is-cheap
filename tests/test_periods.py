"""The fiscal quarter an earnings release reports, from its filing date and its text."""

from datetime import date

import pytest

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.filings.periods import named_quarter, release_period

APPLE = FiscalCalendar.parse("0927")
NVIDIA = FiscalCalendar.parse("0126")
MICROSOFT = FiscalCalendar.parse("0630")
CALENDAR_YEAR = FiscalCalendar.parse("1231")


@pytest.mark.parametrize(
    ("calendar", "text", "expected", "period_end"),
    [
        (
            NVIDIA,
            "## NVIDIA Announces Financial Results for Second Quarter Fiscal 2027\n\n"
            "NVIDIA today reported revenue for the second quarter ended July 26, 2026.",
            FiscalQuarter(2027, 2),
            date(2026, 7, 26),
        ),
        (
            APPLE,
            "Apple reports third quarter results\n\nApple today announced financial results "
            "for its fiscal 2026 third quarter ended June 27, 2026.",
            FiscalQuarter(2026, 3),
            date(2026, 6, 27),
        ),
        (
            MICROSOFT,
            "Microsoft today announced the following results for the quarter ended "
            "June 30, 2026, as compared to the corresponding period of last fiscal year:",
            FiscalQuarter(2026, 4),
            date(2026, 6, 30),
        ),
        (
            CALENDAR_YEAR,
            "Meta Reports Fourth Quarter and Full Year 2025 Results",
            FiscalQuarter(2025, 4),
            None,
        ),
        (
            APPLE,
            "Qualcomm Announces Fourth Quarter and Fiscal 2024 Results\n\nresults for its "
            "fiscal fourth quarter and year ended September 29, 2024.",
            FiscalQuarter(2024, 4),
            date(2024, 9, 29),
        ),
        (CALENDAR_YEAR, "Q4 and FY 2023 Update", FiscalQuarter(2023, 4), None),
        (NVIDIA, "Q2 FY27 CFO Commentary", FiscalQuarter(2027, 2), None),
        (MICROSOFT, "FY26 Q4 results", FiscalQuarter(2026, 4), None),
    ],
)
def test_named_quarter(
    calendar: FiscalCalendar, text: str, expected: FiscalQuarter, period_end: date | None
) -> None:
    named = named_quarter(text, calendar)
    assert named is not None
    assert (named.quarter, named.period_end) == (expected, period_end)


def test_tables_are_not_read_for_the_quarter() -> None:
    text = "| | Q4'22 | Q1'23 |\n| --- | --- | --- |\n\nFellow shareholders, in Q4 we grew."
    assert named_quarter(text, CALENDAR_YEAR) is None


def test_release_period_confirms_the_filing_date_quarter() -> None:
    text = "results for the second quarter ended July 26, 2026"
    assert release_period(text, date(2026, 8, 26), NVIDIA) == (
        FiscalQuarter(2027, 2),
        date(2026, 7, 26),
    )


def test_release_period_falls_back_to_the_filing_date() -> None:
    assert release_period("In Q2, revenue grew.", date(2026, 7, 16), CALENDAR_YEAR) == (
        FiscalQuarter(2026, 2),
        None,
    )


def test_release_period_ignores_other_periods_named_first() -> None:
    # A quarter after the filing date, or long before it, is not the one reported.
    text = "Our outlook for the first quarter of fiscal 2025 follows."
    assert release_period(text, date(2024, 11, 6), APPLE) == (FiscalQuarter(2024, 4), None)
    text = "Compared with the second quarter of 2023, revenue grew."
    assert release_period(text, date(2025, 7, 30), CALENDAR_YEAR)[0] == FiscalQuarter(2025, 2)


def test_release_period_accepts_a_late_release_for_the_quarter_before() -> None:
    text = "Results for the fourth quarter of 2025."
    assert release_period(text, date(2026, 4, 2), CALENDAR_YEAR)[0] == FiscalQuarter(2025, 4)
