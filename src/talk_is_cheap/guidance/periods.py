"""The fiscal period a guidance statement is about, from its wording and the period of the
release that gave it.

Releases name target periods in many ways: "third quarter of fiscal 2027", "Q4 FY26",
"Q4'25", "the fourth quarter", "next quarter", "fiscal 2027", "full year", "2026", "the
September quarter". Years
are fiscal years, labelled by the calendar year in which they end (Nvidia's fiscal 2027
ends in January 2027), as companies name them. A quarter or year without a number is the
next one after the release's quarter. Periods given by month ("the quarter ending
October 31, 2026") are placed with the company's fiscal calendar.
"""

import re
from dataclasses import dataclass
from datetime import date

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.numbers import normalize_text


@dataclass(frozen=True, slots=True, order=True)
class FiscalPeriod:
    """A fiscal quarter, or a fiscal year when `fiscal_quarter` is None."""

    fiscal_year: int
    fiscal_quarter: int | None = None

    @property
    def label(self) -> str:
        if self.fiscal_quarter is None:
            return f"FY{self.fiscal_year}"
        return f"FY{self.fiscal_year}Q{self.fiscal_quarter}"

    @classmethod
    def parse(cls, label: str) -> "FiscalPeriod":
        """ "FY2027Q3" or "FY2027"."""
        compact = label.replace(" ", "").upper()
        if re.fullmatch(r"FY\d{4}", compact):
            return cls(int(compact[2:]))
        quarter = FiscalQuarter.parse(compact)
        return cls(quarter.fiscal_year, quarter.fiscal_quarter)


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3,
             "4th": 4}  # fmt: skip
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

# "FY27", "FY 2027", "fiscal 2027", "fiscal year 2027", "'25" (as in "Q4'25"), "2026".
# Two digits count as a year only after "FY", "fiscal" or an apostrophe.
_YEAR = (
    r"(?:fy\s*'?(?P<y1>\d{4}|\d{2})\b|fiscal\s+(?:year\s+)?'?(?P<y2>\d{4}|\d{2})\b"
    r"|'(?P<y3>\d{2})\b|(?P<y4>(?:19|20)\d{2})\b)"
)
_QUARTER_WORD = (
    r"(?:(?P<ord>first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter"
    r"|\bq(?P<qnum>[1-4])"
    r"|\b(?P<qnum2>[1-4])q)"
)
_QUARTER_WITH_YEAR = re.compile(
    _QUARTER_WORD + r"(?:\s*(?:of|for|in|,)?\s*(?:the\s+)?" + _YEAR + r")?",
    re.IGNORECASE,
)
_YEAR_FIRST_QUARTER = re.compile(
    _YEAR + r"\s+(?:(?P<ord>first|second|third|fourth)\s+quarter|q(?P<qnum>[1-4]))",
    re.IGNORECASE,
)
_NEXT_QUARTER = re.compile(
    r"\b(?:next|current|coming|upcoming|following|this)\s+(?:fiscal\s+)?quarter\b"
    r"|^\s*(?:the\s+)?quarter\s*$",
    re.IGNORECASE,
)
_FULL_YEAR = re.compile(
    r"\b(?:full[- ]year|whole\s+year|annual|for\s+the\s+year|the\s+year|this\s+year|"
    r"current\s+(?:fiscal\s+)?year|full\s+fiscal\s+year|fiscal\s+year)\b",
    re.IGNORECASE,
)
_NEXT_YEAR = re.compile(r"\bnext\s+(?:fiscal\s+)?year\b", re.IGNORECASE)
_PARTIAL_YEAR = re.compile(
    r"\b(?:first|second)\s+half\b|\b[12]h\s*'?\d{2}\b|\bh[12]\b|\bnine\s+months\b|"
    r"\bsix\s+months\b|\bremainder\b|\bremaining\b|\brest\s+of\b",
    re.IGNORECASE,
)
_CALENDAR = re.compile(r"\bcalendar\b|\bcy\s*'?\d{2}", re.IGNORECASE)
_ENDING = re.compile(
    r"\b(?:quarter|three\s+months|period)\s+(?:ending|ended|that\s+ends)\s+(?:on\s+)?"
    r"(?P<month>[a-z]+)\.?\s+(?:(?P<day>\d{1,2}),?\s+)?(?P<year>\d{4})",
    re.IGNORECASE,
)
# "the September quarter", "the December quarter of 2026": the quarter ending in that month.
_MONTH_QUARTER = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+quarter\b"
    r"(?:\s*(?:of|,)?\s*(?P<year>(?:19|20)\d{2}))?",
    re.IGNORECASE,
)
_BARE_YEAR = re.compile(r"(?<![\d$.,])(?P<year>(?:19|20)\d{2})(?![\d%])")
_SHORT_FY = re.compile(r"\bfy\s*'?(?P<year>\d{2})\b|\bfiscal\s+'(?P<year2>\d{2})\b", re.IGNORECASE)


def resolve_period(
    text: str, issued: FiscalQuarter, calendar: FiscalCalendar
) -> FiscalPeriod | None:
    """The fiscal quarter or year `text` names, as said in a release for fiscal quarter
    `issued`. None when it names no single quarter or year ("second half of 2025",
    "calendar 2026" for a company whose year does not end in December)."""
    text = normalize_text(text).replace("\u2019", "'").replace("\u2018", "'").strip()
    if _PARTIAL_YEAR.search(text):
        return None

    ending = _ENDING.search(text)
    if ending:
        return _by_month(ending, calendar)

    named_month = _MONTH_QUARTER.search(text)
    if named_month:
        return _by_quarter_month(named_month, issued, calendar)

    if _CALENDAR.search(text):
        if (calendar.month, calendar.day) < (12, 20):
            return None  # a calendar year is not one of this company's fiscal years
        text = _CALENDAR.sub(" ", text)

    following = _following(issued)
    quarter_match = _YEAR_FIRST_QUARTER.search(text) or _QUARTER_WITH_YEAR.search(text)
    if quarter_match:
        quarter = _quarter_number(quarter_match)
        groups = quarter_match.groupdict()
        year = _year(next((groups[g] for g in ("y1", "y2", "y3", "y4") if groups.get(g)), None))
        if year is None:
            return _next_with_quarter(issued, quarter)
        return FiscalPeriod(year, quarter)

    if _NEXT_QUARTER.search(text):
        return FiscalPeriod(following.fiscal_year, following.fiscal_quarter)

    year = _named_year(text)
    if year is not None:
        return FiscalPeriod(year)
    if _NEXT_YEAR.search(text):
        return FiscalPeriod(following.fiscal_year + 1)
    if _FULL_YEAR.search(text):
        # The year still under way after the release: a release for the fourth quarter
        # speaks of the new year.
        return FiscalPeriod(following.fiscal_year)
    return None


def _following(issued: FiscalQuarter) -> FiscalQuarter:
    if issued.fiscal_quarter == 4:
        return FiscalQuarter(issued.fiscal_year + 1, 1)
    return FiscalQuarter(issued.fiscal_year, issued.fiscal_quarter + 1)


def _next_with_quarter(issued: FiscalQuarter, quarter: int) -> FiscalPeriod:
    """The first fiscal quarter numbered `quarter` after the release's quarter."""
    year = issued.fiscal_year if quarter > issued.fiscal_quarter else issued.fiscal_year + 1
    return FiscalPeriod(year, quarter)


def _quarter_number(match: re.Match[str]) -> int:
    ordinal = match.groupdict().get("ord")
    if ordinal:
        return _ORDINALS[ordinal.lower()]
    number = match.groupdict().get("qnum") or match.groupdict().get("qnum2")
    assert number is not None
    return int(number)


def _year(text: str | None) -> int | None:
    if not text:
        return None
    return int(text) if len(text) == 4 else 2000 + int(text)


def _named_year(text: str) -> int | None:
    short = _SHORT_FY.search(text)
    if short:
        return _year(short["year"] or short["year2"])
    years = {int(m["year"]) for m in _BARE_YEAR.finditer(text)}
    return years.pop() if len(years) == 1 else None


def _by_month(match: re.Match[str], calendar: FiscalCalendar) -> FiscalPeriod | None:
    month = _MONTHS.get(match["month"].lower())
    if month is None:
        return None
    year = int(match["year"])
    day = int(match["day"]) if match["day"] else 28
    try:
        end = date(year, month, day)
    except ValueError:
        return None
    quarter = calendar.quarter_of(end)
    if "quarter" not in match.group(0).lower() and "three months" not in match.group(0).lower():
        return FiscalPeriod(quarter.fiscal_year)
    return FiscalPeriod(quarter.fiscal_year, quarter.fiscal_quarter)


def _by_quarter_month(
    match: re.Match[str], issued: FiscalQuarter, calendar: FiscalCalendar
) -> FiscalPeriod | None:
    """The fiscal quarter ending in the named month: in the named calendar year, or else
    the first such quarter after the release's quarter."""
    month = _MONTHS.get(match["month"].lower().rstrip("."))
    if month is None:
        return None
    if match["year"]:
        quarter = calendar.quarter_of(date(int(match["year"]), month, 28))
        return FiscalPeriod(quarter.fiscal_year, quarter.fiscal_quarter)
    following = _following(issued)
    for _ in range(4):
        if calendar.quarter_end(following).month == month:
            return FiscalPeriod(following.fiscal_year, following.fiscal_quarter)
        following = _following(following)
    return None
