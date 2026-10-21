"""What the pages say: verdict names and colours, amounts with units and periods, and the
sentences for thin and empty data. Plain functions, without Streamlit."""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from talk_is_cheap.api.schemas import GuidanceOut, PromiseOut, TrackRecordOut

Tone = Literal["green", "amber", "red", "grey", "outline"]

# Fill colours of the verdict badges; white text reads on each in light and dark themes.
# Outlined badges have no fill and take the theme's text colour.
TONE_COLOURS: dict[Tone, str] = {
    "green": "#15803d",
    "amber": "#b45309",
    "red": "#b91c1c",
    "grey": "#4b5563",
    "outline": "transparent",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    name: str
    label: str
    tone: Tone
    meaning: str


# Promise verdicts as docs/promises.md defines them, and the states of claims without one.
PROMISE_VERDICTS: dict[str, Verdict] = {
    v.name: v
    for v in (
        Verdict(
            "delivered",
            "delivered",
            "green",
            "the rule was met on time: the event date the evidence states is on or before "
            "the due date, or, without one, the evidence was published by the end of the "
            "grace period",
        ),
        Verdict(
            "delivered_late",
            "delivered late",
            "amber",
            "met, but late; marked revised before due when the company revised the claim "
            "before its due period was reported",
        ),
        Verdict(
            "partly_delivered",
            "partly delivered",
            "amber",
            "a numeric target moved from the baseline toward the target without reaching "
            "it, or part of an event happened",
        ),
        Verdict(
            "not_delivered",
            "not delivered",
            "red",
            "the due period was reported and the rule was not met",
        ),
        Verdict(
            "walked_back",
            "walked back",
            "red",
            "a later statement by the company withdrew, delayed or lowered the claim, and "
            "it was never delivered",
        ),
        Verdict(
            "too_early",
            "too early",
            "grey",
            "the due date and grace period have not passed, or an open-ended claim not "
            "shown delivered within 18 months of the statement",
        ),
        Verdict(
            "expired_unproven",
            "expired unproven",
            "outline",
            "an open-ended claim not shown delivered 18 months after the statement",
        ),
        Verdict(
            "cannot_determine",
            "cannot determine",
            "outline",
            "the evidence does not say, or a code check failed",
        ),
        Verdict("pending", "pending", "grey", "not judged yet"),
        Verdict(
            "not_scored",
            "not scored",
            "grey",
            "vague claims are never scored; claims restating formal guidance are scored "
            "with the guidance",
        ),
    )
}

GUIDANCE_VERDICTS: dict[str, Verdict] = {
    v.name: v
    for v in (
        Verdict("beat", "beat", "green", "the result was better than the guided range"),
        Verdict("met", "met", "amber", "the result was within the guided range"),
        Verdict("missed", "missed", "red", "the result fell short of the guided range"),
        Verdict("pending", "pending", "grey", "the target period is not reported yet"),
        Verdict(
            "not_comparable",
            "not comparable",
            "outline",
            "no reported value to compare with (non-GAAP, segment, tax rate, qualitative)",
        ),
    )
}

COUNTED = ("beat", "met", "missed")
# Track-record figures of a group, in the order of the strip.
STRIP_VERDICTS = ("delivered", "delivered_late", "walked_back", "expired_unproven")


def promise_state(p: PromiseOut) -> str:
    """The verdict name a claim is shown with: its verdict, `pending`, or `not_scored`."""
    if not p.scored:
        return "not_scored"
    return p.verdict or "pending"


# Speakers and companies.

_LEGAL_SUFFIX = re.compile(
    r"(,?\s+(inc\.?|corp\.?|corporation|co\.?|com|plc|ltd\.?)(/[a-z]{2})?)+$", re.IGNORECASE
)
COMPANY_SPEAKERS = ("company statement", "company spokesperson")


def company_name(name: str) -> str:
    """ "NVIDIA CORP" as "Nvidia", "Meta Platforms, Inc." as "Meta Platforms"."""
    short = _LEGAL_SUFFIX.sub("", name.strip()) or name
    return short.title() if short.isupper() else short


def speaker_key(ticker: str, speaker: str) -> str:
    """The track-record group of a speaker (`promises/track.py: speaker_key`)."""
    if speaker in COMPANY_SPEAKERS:
        return f"{ticker} (company)"
    return f"{speaker} ({ticker})"


def speaker_label(key: str) -> str:
    """ "Jen-Hsun Huang (NVDA)" as "Jen-Hsun Huang · NVDA"; "NVDA (company)" as "NVDA
    company statements"."""
    found = re.fullmatch(r"(.+) \(([^()]+)\)", key)
    if not found:
        return key
    name, ticker = found.groups()
    if ticker == "company":
        return f"{name} company statements"
    return f"{name} · {ticker}"


def speaker_text(p: PromiseOut) -> str:
    if p.speaker in COMPANY_SPEAKERS:
        return f"{p.ticker} {p.speaker}"
    return f"{p.speaker}, {p.role}" if p.role else p.speaker


# Numbers.


def _trim(value: Decimal, places: int) -> str:
    return f"{value:,.{places}f}"


def usd(value: Decimal) -> str:
    """$108.0 billion as "$108.0B", 9,200,000 as "$9.2M"; per-share figures as cents."""
    sign = "-" if value < 0 else ""
    v = abs(value)
    for size, suffix in (
        (Decimal(10) ** 12, "T"),
        (Decimal(10) ** 9, "B"),
        (Decimal(10) ** 6, "M"),
    ):
        if v >= size:
            return f"{sign}${_trim(v / size, 1)}{suffix}"
    return f"{sign}${_trim(v, 2 if v < 100 else 0)}"


def amount(value: Decimal, unit: str, measure: str = "level") -> str:
    """A guided or reported figure in its unit: "$108.0B", "74.0%", "$1.23 a share",
    "+9.0% y/y"."""
    if measure in ("yoy_growth", "sequential_growth"):
        suffix = " y/y" if measure == "yoy_growth" else " q/q"
        if unit == "pct":
            return f"{value:+.1f}%{suffix}"
        return f"{'+' if value >= 0 else ''}{usd(value)}{suffix}"
    if unit == "pct":
        text = f"{value:.1f}%"
        return f"{text} of revenue" if measure == "share_of_revenue" else text
    if unit == "usd":
        return usd(value)
    if unit == "usd_per_share":
        return f"${value:,.2f} a share"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def guided(row: GuidanceOut) -> str:
    """The guided range, point or words."""
    if row.low is not None and row.high is not None:
        low, high = amount(row.low, row.unit, row.measure), amount(row.high, row.unit, row.measure)
        return f"{low} to {high}"
    if row.point is not None:
        return amount(row.point, row.unit, row.measure)
    if row.low is not None:
        return "at least " + amount(row.low, row.unit, row.measure)
    if row.high is not None:
        return "at most " + amount(row.high, row.unit, row.measure)
    return row.value_text or row.qualitative or "—"


def actual(row: GuidanceOut) -> str:
    if row.actual is None:
        return "—"
    return amount(row.actual, row.unit, row.measure)


def excerpt(text: str) -> str:
    """A passage of filing text as a sentence: without its markdown headings, bold
    markers and table rules."""
    cleaned = re.sub(r"(^|\s)#{1,6}\s+", r"\1", text)
    cleaned = cleaned.replace("**", "").replace("|", " ")
    return " ".join(cleaned.split())


def period(label: str | None) -> str:
    """ "FY2027Q3" as "FY2027 Q3"; a year stays "FY2027"."""
    if not label:
        return "—"
    return re.sub(r"^(FY\d{4})\s*(Q\d)$", r"\1 \2", label)


def percent(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.0%}"


def days(value: float | None) -> str:
    if value is None:
        return "—"
    count = round(value)
    return f"{count} day{'s' if count != 1 else ''}"


def month(day: date) -> str:
    return day.strftime("%B %Y")


def count(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n:,} {noun if n == 1 else (plural or noun + 's')}"


def metric_name(metric: str) -> str:
    return {"eps": "EPS", "capex": "capex", "other": "other"}.get(metric, metric.replace("_", " "))


# Sentences for thin and empty data.


def claims_summary(claims: Sequence[PromiseOut], today: date, whose: str) -> str:
    """What a selection of claims amounts to, plainly: "4 claims, none due yet. The
    earliest is due in March 2027."."""
    if not claims:
        return f"No management claims are stored for {whose} yet."
    total = count(len(claims), "claim")
    scorable = [p for p in claims if p.scored]
    if not scorable:
        return f"{total}, none of them scorable: vague claims are never scored."
    judged = [p for p in scorable if p.verdict and p.verdict != "too_early"]
    due = [p for p in scorable if p.due_date is not None and p.due_date <= today]
    if not judged and not due:
        upcoming = sorted(p.due_date for p in scorable if p.due_date is not None)
        if upcoming:
            return f"{total}, none due yet. The earliest is due in {month(upcoming[0])}."
        return f"{total}, none due yet: none has a deadline, so each is checked for 18 months."
    parts = [total, f"{len(due)} past their due date", f"{len(judged)} with a verdict"]
    return ", ".join(parts) + "."


def record_summary(record: TrackRecordOut | None, min_resolved: int) -> str:
    """Why a track record has no rates; empty when it has them."""
    if record is None or record.claims == 0:
        return "No claim here can be scored yet, so there is no track record."
    if not record.too_few_to_judge:
        return ""
    return (
        f"Too few to judge: {count(record.resolved, 'resolved claim')} of "
        f"{count(record.claims, 'scorable claim')}; rates need {min_resolved}."
    )


# The scorecard.


@dataclass(slots=True)
class Scorecard:
    """Beat, met and missed over the rows that count, and what was left out and why."""

    rows: int = 0
    counted: Counter[str] = field(default_factory=Counter)
    pending: int = 0
    not_comparable: Counter[str] = field(default_factory=Counter)
    needs_review: int = 0
    wrong: int = 0

    @property
    def comparable(self) -> int:
        return sum(self.counted.values())

    @property
    def excluded(self) -> int:
        return self.rows - self.comparable

    def exclusions(self) -> list[str]:
        """One phrase per reason a row was left out, largest first."""
        found = [(n, f"{n} not comparable ({reason})") for reason, n in self.not_comparable.items()]
        found.append((self.pending, f"{self.pending} pending (target period not reported yet)"))
        found.append((self.needs_review, f"{self.needs_review} flagged for review"))
        found.append((self.wrong, f"{self.wrong} reviewed as extraction errors"))
        return [text for n, text in sorted(found, key=lambda f: -f[0]) if n]


def scorecard(rows: Iterable[GuidanceOut]) -> Scorecard:
    card = Scorecard()
    for row in rows:
        card.rows += 1
        if row.mark == "WRONG":
            card.wrong += 1
        elif row.mark == "REVIEW":
            card.needs_review += 1
        elif row.verdict in COUNTED:
            card.counted[row.verdict] += 1
        elif row.verdict == "pending":
            card.pending += 1
        else:
            card.not_comparable[row.verdict_reason or "no reason given"] += 1
    return card


def guidance_flag(row: GuidanceOut) -> str:
    """ "needs review", "reviewed: wrong" or "reviewed: ok"; empty for ordinary rows."""
    return row.review_status or ""
