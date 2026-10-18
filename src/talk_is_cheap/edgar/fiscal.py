"""Fiscal calendar logic.

Companies end their fiscal years on different dates (Apple in late September, Nvidia in
late January, Microsoft on June 30), and many use 52/53-week years, so period end dates
drift by a few days from year to year. All assignments here allow that drift.

Fiscal years are labelled by the calendar year in which they end, which matches how most
companies name them (Nvidia's year ending January 2026 is fiscal 2026).
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

DRIFT = timedelta(days=10)
_DAYS_PER_QUARTER = 365.25 / 4


class Duration(StrEnum):
    """Length of a reported period, as found in XBRL facts."""

    QUARTER = "3M"
    HALF = "6M"
    NINE_MONTHS = "9M"
    YEAR = "12M"

    @property
    def quarters(self) -> int:
        return {"3M": 1, "6M": 2, "9M": 3, "12M": 4}[self.value]


# Inclusive day counts, wide enough for 13/14-week quarters and 52/53-week years.
_DURATION_DAYS = {
    Duration.QUARTER: (84, 98),
    Duration.HALF: (175, 189),
    Duration.NINE_MONTHS: (266, 280),
    Duration.YEAR: (357, 371),
}


def classify_duration(start: date, end: date) -> Duration | None:
    days = (end - start).days + 1
    for duration, (low, high) in _DURATION_DAYS.items():
        if low <= days <= high:
            return duration
    return None


@dataclass(frozen=True, slots=True)
class FiscalCalendar:
    month: int
    day: int

    @classmethod
    def parse(cls, mmdd: str) -> "FiscalCalendar":
        if len(mmdd) != 4 or not mmdd.isdigit():
            raise ValueError(f"fiscal year end must be MMDD, got {mmdd!r}")
        month, day = int(mmdd[:2]), int(mmdd[2:])
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            raise ValueError(f"invalid fiscal year end {mmdd!r}")
        return cls(month, day)

    def year_end_in(self, year: int) -> date:
        last_day = calendar.monthrange(year, self.month)[1]
        return date(year, self.month, min(self.day, last_day))

    def year_end_for(self, period_end: date) -> date:
        """The nominal end of the fiscal year containing `period_end`."""
        for year in (period_end.year - 1, period_end.year, period_end.year + 1):
            year_end = self.year_end_in(year)
            if period_end <= year_end + DRIFT:
                return year_end
        raise AssertionError("unreachable: next year's end is always later")  # pragma: no cover

    def fiscal_year(self, period_end: date) -> int:
        return self.year_end_for(period_end).year

    def fiscal_quarter(self, period_end: date) -> int:
        """1-4, from how far `period_end` falls before its fiscal year end."""
        days_before = (self.year_end_for(period_end) - period_end).days
        return min(4, max(1, 4 - round(days_before / _DAYS_PER_QUARTER)))

    def year_start(self, fiscal_year: int) -> date:
        return self.year_end_in(fiscal_year - 1) + timedelta(days=1)

    def starts_fiscal_year(self, period_start: date, fiscal_year: int) -> bool:
        return abs(period_start - self.year_start(fiscal_year)) <= DRIFT
