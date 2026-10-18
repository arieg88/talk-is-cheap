"""The fiscal period an earnings release reports.

An earnings 8-K's report date is the release date, not the quarter end. The release
reports the most recent fiscal quarter that ended before it was filed
(`FiscalCalendar.quarter_ended_before`), and its opening text names that quarter:
"results for the second quarter ended July 26, 2026", "Second Quarter Fiscal 2027",
"fiscal 2026 third quarter", "Q4 FY26". The quarter named first in the opening prose
(tables are skipped: their headers list comparison periods)
confirms the date-based quarter, or corrects it to the quarter before when a release
comes unusually late; anything else is a mention of some other period and is ignored.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter

OPENING_CHARS = 5000

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sept?\.?|Oct\.?|Nov\.?|Dec\.?"
)
_ORDINAL = r"(?P<ordinal>first|second|third|fourth)"
_YEAR = r"(?P<year>(?:19|20)\d{2})"

_QUARTER_ENDED = re.compile(
    rf"(?:quarter|three\s+months)\s+(?:of\s+fiscal\s+\d{{4}}\s+)?"
    rf"(?:and\s+(?:fiscal\s+)?year\s+)?ended\s+"
    rf"(?P<date>(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}})",
    re.IGNORECASE,
)
_PATTERNS = (
    # "second quarter of fiscal 2027", "fourth quarter and full year 2025", "Q3 fiscal 2026"
    re.compile(
        rf"\b{_ORDINAL}[\s-]+(?:fiscal[\s-]+)?quarter\s+(?:(?:and|&)\s+(?:full[\s-]+)?"
        rf"(?:fiscal\s+)?(?:year\s+)?)?(?:of\s+)?(?:fiscal\s+(?:year\s+)?)?{_YEAR}\b",
        re.IGNORECASE,
    ),
    # "fiscal 2026 third quarter", "2025 fourth-quarter"
    re.compile(rf"\b(?:fiscal\s+(?:year\s+)?)?{_YEAR}\s+{_ORDINAL}[\s-]+quarter", re.IGNORECASE),
    # "Q2 FY27", "Q3 2026", "Q2 Fiscal 2027", "Q4 and FY 2023"
    re.compile(
        r"\bQ(?P<number>[1-4])\s*(?:and\s+)?(?:'|FY\s?|fiscal\s+(?:year\s+)?)?"
        r"(?P<year>\d{4}|\d{2})\b",
        re.IGNORECASE,
    ),
    # "FY26 Q4", "fiscal year 2026 Q4"
    re.compile(
        r"\b(?:FY\s?|fiscal\s+(?:year\s+)?)(?P<year>\d{4}|\d{2})\s+Q(?P<number>[1-4])\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class ReleasePeriod:
    quarter: FiscalQuarter
    period_end: date | None  # when the release states the quarter's end date


def named_quarter(text: str, fiscal_calendar: FiscalCalendar) -> ReleasePeriod | None:
    """The first fiscal quarter the opening of a release names, if any."""
    prose = (line for line in text.split("\n") if not line.startswith("|"))
    opening = "\n".join(prose)[:OPENING_CHARS]
    found: list[tuple[int, ReleasePeriod]] = []

    for match in _QUARTER_ENDED.finditer(opening):
        ended = _parse_date(match.group("date"))
        if ended is not None:
            found.append((match.start(), ReleasePeriod(fiscal_calendar.quarter_of(ended), ended)))
            break
    for pattern in _PATTERNS:
        for match in pattern.finditer(opening):
            quarter = _quarter(match)
            if quarter is not None:
                found.append((match.start(), ReleasePeriod(quarter, None)))
                break
    if not found:
        return None
    first = min(found, key=lambda item: item[0])[1]
    # The same quarter's end date may be stated a little later than its name.
    for _, period in found:
        if period.quarter == first.quarter and period.period_end is not None:
            return period
    return first


def release_period(
    text: str, filed: date, fiscal_calendar: FiscalCalendar
) -> tuple[FiscalQuarter, date | None]:
    """The quarter an earnings release filed on `filed` reports, and its end date when the
    release states it."""
    expected = fiscal_calendar.quarter_ended_before(filed)
    named = named_quarter(text, fiscal_calendar)
    if named is not None and named.quarter in (expected, expected.previous):
        return named.quarter, named.period_end
    return expected, None


def _quarter(match: re.Match[str]) -> FiscalQuarter | None:
    groups = match.groupdict()
    number = _ORDINALS[groups["ordinal"].lower()] if groups.get("ordinal") else None
    if number is None and groups.get("number"):
        number = int(groups["number"])
    year_text = groups.get("year") or ""
    if number is None or not year_text.isdigit():
        return None
    year = int(year_text) + (2000 if len(year_text) == 2 else 0)
    return FiscalQuarter(year, number)


def _parse_date(text: str) -> date | None:
    cleaned = re.sub(r"\s+", " ", text.replace(",", "")).strip()
    month, day, year = cleaned.split(" ")
    month = month.rstrip(".")[:3].title()
    try:
        return datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()
    except ValueError:
        return None
