from datetime import date

import pytest

from talk_is_cheap.edgar.fiscal import Duration, FiscalCalendar, classify_duration

APPLE = FiscalCalendar.parse("0927")  # 52/53-week year ending late September
NVIDIA = FiscalCalendar.parse("0126")  # year ends late January, named after that year
MICROSOFT = FiscalCalendar.parse("0630")
CALENDAR_YEAR = FiscalCalendar.parse("1231")


@pytest.mark.parametrize(
    ("calendar", "period_end", "expected"),
    [
        # Apple: Q1 FY2025 ended Dec 28, 2024; FY2024 ended Sep 28, 2024 (one day of drift).
        (APPLE, date(2024, 12, 28), (2025, 1)),
        (APPLE, date(2025, 3, 29), (2025, 2)),
        (APPLE, date(2025, 6, 28), (2025, 3)),
        (APPLE, date(2025, 9, 27), (2025, 4)),
        (APPLE, date(2024, 9, 28), (2024, 4)),
        # Nvidia: fiscal 2026 runs Feb 2025 to Jan 2026.
        (NVIDIA, date(2025, 4, 27), (2026, 1)),
        (NVIDIA, date(2025, 10, 26), (2026, 3)),
        (NVIDIA, date(2026, 1, 25), (2026, 4)),
        # A 53-week year can end a few days after the nominal date.
        (NVIDIA, date(2026, 2, 1), (2026, 4)),
        # Microsoft: fiscal 2026 runs Jul 2025 to Jun 2026.
        (MICROSOFT, date(2025, 9, 30), (2026, 1)),
        (MICROSOFT, date(2026, 6, 30), (2026, 4)),
        (CALENDAR_YEAR, date(2025, 3, 31), (2025, 1)),
        (CALENDAR_YEAR, date(2025, 12, 31), (2025, 4)),
    ],
)
def test_fiscal_year_and_quarter(
    calendar: FiscalCalendar, period_end: date, expected: tuple[int, int]
) -> None:
    assert (calendar.fiscal_year(period_end), calendar.fiscal_quarter(period_end)) == expected


def test_fiscal_year_start_allows_drift() -> None:
    assert APPLE.year_start(2025) == date(2024, 9, 28)
    assert APPLE.starts_fiscal_year(date(2024, 9, 29), 2025)  # actual start of FY2025
    assert not APPLE.starts_fiscal_year(date(2024, 12, 29), 2025)


def test_february_year_end_is_clamped_in_non_leap_years() -> None:
    assert FiscalCalendar.parse("0229").year_end_in(2025) == date(2025, 2, 28)


@pytest.mark.parametrize("bad", ["", "927", "1301", "0000", "09-27"])
def test_rejects_malformed_fiscal_year_end(bad: str) -> None:
    with pytest.raises(ValueError):
        FiscalCalendar.parse(bad)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2025, 1, 1), date(2025, 3, 31), Duration.QUARTER),
        (date(2025, 1, 1), date(2025, 6, 30), Duration.HALF),
        (date(2025, 1, 1), date(2025, 9, 30), Duration.NINE_MONTHS),
        (date(2025, 1, 1), date(2025, 12, 31), Duration.YEAR),
        (date(2025, 9, 29), date(2025, 12, 28), Duration.QUARTER),  # 13-week quarter
        (date(2025, 1, 27), date(2026, 2, 1), Duration.YEAR),  # 53-week year
        (date(2025, 1, 1), date(2025, 2, 14), None),
        (date(2025, 1, 1), date(2026, 6, 30), None),
    ],
)
def test_classify_duration(start: date, end: date, expected: Duration | None) -> None:
    assert classify_duration(start, end) is expected
