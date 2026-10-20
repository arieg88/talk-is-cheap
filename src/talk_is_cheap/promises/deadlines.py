"""Due dates, computed in code from a deadline's structure, the statement date and the
company's fiscal calendar. The model only says what the deadline is ("in 12 months",
"end of year 2026", "fiscal year +1"); it never computes a date.

- "in a year", said 2025-03-10: `relative`, 2026-03-10;
- "by the end of next year": `explicit`, December 31 of that calendar year;
- "first half of 2026", "by Q3 2026", "by June", "June 10, 2024", "next summer":
  `explicit`, the last day of that half, quarter or month, the day, the season's end;
- "next fiscal year", "fiscal 2026": `fiscal_period`, that fiscal year's end on the
  company's calendar; "next quarter", "the fiscal fourth quarter": that quarter's end;
- "over time", "eventually", none: `open_ended`, no due date.

A quarter, month or season named without a year is the next one that ends on or after
the statement date. A deadline that cannot be computed (a duration without an amount, a
month without a number) is open-ended, with a note saying why.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.promises.schema import Deadline

# The last day of each season, northern hemisphere, as a (month, day): "by next summer"
# is due when summer ends.
SEASON_END = {"spring": (6, 20), "summer": (9, 22), "fall": (12, 20), "winter": (3, 19)}


@dataclass(frozen=True, slots=True)
class Due:
    date: date | None
    basis: str  # explicit, relative, fiscal_period, open_ended
    note: str | None = None  # why a deadline the model gave could not be computed


OPEN = Due(None, "open_ended")


def due_date(deadline: Deadline, said: date, fiscal: FiscalCalendar | None) -> Due:
    """When a claim said on `said` is due."""
    kind = deadline.type
    if kind == "none":
        return OPEN
    if kind == "duration":
        if not deadline.amount or deadline.amount < 0 or deadline.unit is None:
            return Due(None, "open_ended", "a duration without an amount or unit")
        return Due(add_duration(said, deadline.amount, deadline.unit), "relative")
    if kind == "date":
        year = _calendar_year(deadline, said)
        if deadline.month is None or deadline.day is None:
            return Due(None, "open_ended", "a date without a month or day")
        try:
            day = date(year, deadline.month, deadline.day)
        except ValueError:
            return Due(None, "open_ended", "not a real date")
        if deadline.year is None and deadline.offset is None and day < said:
            day = day.replace(year=day.year + 1)
        return Due(day, "explicit")
    if kind == "calendar_year":
        return Due(date(_calendar_year(deadline, said), 12, 31), "explicit")
    if kind == "calendar_half":
        if deadline.half is None:
            return Due(None, "open_ended", "a half year without its number")
        month = 6 * deadline.half
        return Due(_next_month_end(deadline, said, month), "explicit")
    if kind == "calendar_quarter":
        if deadline.quarter is None:
            return Due(None, "open_ended", "a quarter without its number")
        return Due(_next_month_end(deadline, said, 3 * deadline.quarter), "explicit")
    if kind == "month":
        if deadline.month is None:
            return Due(None, "open_ended", "a month without its number")
        return Due(_next_month_end(deadline, said, deadline.month), "explicit")
    if kind == "season":
        if deadline.season is None:
            return Due(None, "open_ended", "a season without its name")
        return Due(_season_end(deadline, said), "explicit")
    if fiscal is None:
        return Due(None, "open_ended", "no fiscal calendar for the company")
    if kind == "fiscal_year":
        year = (
            deadline.year
            if deadline.year is not None
            else fiscal.fiscal_year(said) + (deadline.offset or 0)
        )
        return Due(fiscal.year_end_in(year), "fiscal_period")
    assert kind == "fiscal_quarter"
    return _fiscal_quarter(deadline, said, fiscal)


def add_duration(start: date, amount: int, unit: str) -> date:
    """`start` plus a number of days, weeks, months or years; a month end that does not
    exist is the month's last day (January 31 plus a month is February 28)."""
    if unit == "days":
        return start + timedelta(days=amount)
    if unit == "weeks":
        return start + timedelta(weeks=amount)
    months = amount * 12 if unit == "years" else amount
    index = start.year * 12 + start.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _calendar_year(deadline: Deadline, said: date) -> int:
    if deadline.year is not None:
        return deadline.year
    return said.year + (deadline.offset or 0)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _next_month_end(deadline: Deadline, said: date, month: int) -> date:
    """The end of `month` in the named or relative year, or, with neither, the first
    one on or after the statement date."""
    if deadline.year is not None or deadline.offset is not None:
        return _month_end(_calendar_year(deadline, said), month)
    end = _month_end(said.year, month)
    return end if end >= said else _month_end(said.year + 1, month)


def _season_end(deadline: Deadline, said: date) -> date:
    assert deadline.season is not None
    month, day = SEASON_END[deadline.season]
    # Winter ends in the year after the one it starts in.
    shift = 1 if deadline.season == "winter" else 0
    if deadline.year is not None or deadline.offset is not None:
        return date(_calendar_year(deadline, said) + shift, month, day)
    end = date(said.year, month, day)
    return end if end >= said else date(said.year + 1, month, day)


def _fiscal_quarter(deadline: Deadline, said: date, fiscal: FiscalCalendar) -> Due:
    under_way = _following(fiscal.quarter_ended_before(said))
    if deadline.quarter is None:
        quarter = under_way
        for _ in range(deadline.offset or 0):
            quarter = _following(quarter)
        return Due(fiscal.quarter_end(quarter), "fiscal_period")
    if deadline.year is not None:
        quarter = FiscalQuarter(deadline.year, deadline.quarter)
    else:
        year = under_way.fiscal_year + (deadline.offset or 0)
        quarter = FiscalQuarter(year, deadline.quarter)
        if deadline.offset is None and quarter < under_way:
            quarter = FiscalQuarter(year + 1, deadline.quarter)
    return Due(fiscal.quarter_end(quarter), "fiscal_period")


def _following(quarter: FiscalQuarter) -> FiscalQuarter:
    if quarter.fiscal_quarter == 4:
        return FiscalQuarter(quarter.fiscal_year + 1, 1)
    return FiscalQuarter(quarter.fiscal_year, quarter.fiscal_quarter + 1)
