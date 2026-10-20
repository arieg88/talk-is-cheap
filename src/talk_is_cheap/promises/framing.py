"""From the model's framing of a claim to a promise: figures grounded in the text, the
due date computed, the baseline looked up, specificity and boldness decided, and the
resolution rule written. Everything here is deterministic: the same framing of the same
claim always gives the same promise and the same rule.

- **Figures are grounded.** A target or stated baseline is kept only when its number is
  written in the quote or its paragraph ("double" and "triple" are 2 and 3; "profitable"
  and "break even" are 0). Otherwise it is dropped, with a note.
- **Specificity**: `specific` with a checkable target and a deadline, `partial` with only
  one of them, `vague` with neither. Only the first two are ever scored.
- **Baselines** of a change follow its comparison basis: the same period a year earlier
  for `yoy`, the latest period reported before the statement for `sequential` (and for
  framings without a basis whose quote does not say "year over year").
- **Bold**, in code wherever numbers allow: a multiple of 1.5 or more, a change of more
  than 50%, an absolute target more than 50% above (or below half of) a stored baseline,
  or turning a loss into a profit. Without numbers to compare, a claim about a new
  market or product is bold.
- **The resolution rule** is one sentence written now, with the target, baseline and
  due date in it, and never rewritten: framing a claim again with a new version adds a
  promise row and keeps the old one.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from talk_is_cheap.agent.checks import mentions
from talk_is_cheap.edgar.fiscal import DRIFT, FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.matching import Actuals
from talk_is_cheap.promises.baselines import (
    Baseline,
    metric_unit,
    reported_baseline,
    year_earlier_baseline,
)
from talk_is_cheap.promises.deadlines import due_date
from talk_is_cheap.promises.schema import Framing

# Raise when post-processing changes: claims are then framed again from the cached
# responses, as a new version, without calling the model.
PROCESSING_VERSION = "5"

NUMERIC_KINDS = ("multiple", "absolute", "change", "threshold")
# Targets bold enough to say so: a multiple of the baseline, a change in percent, or an
# absolute target against a stored baseline (above BOLD_RATIO times it, or below
# 1 / BOLD_RATIO for a ceiling).
BOLD_MULTIPLE = Decimal("1.5")
BOLD_CHANGE_PCT = Decimal(50)
BOLD_RATIO = Decimal("1.5")

_MULTIPLE_WORDS = {
    r"\bdoubl\w*|\btwice\b|\btwo-?fold\b": Decimal(2),
    r"\btripl\w*|\bthree-?fold\b": Decimal(3),
    r"\bquadrupl\w*|\bfour-?fold\b": Decimal(4),
    r"\bfive-?fold\b": Decimal(5),
    r"\bten-?fold\b": Decimal(10),
}
_ZERO_WORDS = re.compile(
    r"\b(profit\w*|break[- ]?even|positive|in the black|self[- ]funding|zero)\b", re.IGNORECASE
)
# A change measured against the same period a year earlier.
_YEAR_OVER_YEAR = re.compile(
    r"\b(year[- ]over[- ]year|y/y|yoy|from a year (?:ago|earlier)|than a year ago)\b",
    re.IGNORECASE,
)
# Numbers written with a multiple: "2x", "3 times", "10-fold".
_TIMES = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x\b|times\b|-?fold\b)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClaimInput:
    """What framing reads about one management claim."""

    claim_key: str
    statement_id: int
    article_id: int
    ticker: str
    company: str
    speaker: str
    statement_date: date
    article_date: date
    quote: str
    paragraph: str
    publisher: str | None
    # The claim in brief, when its quote holds other claims too: the prompt then says
    # which one to frame.
    focus: str | None = None
    # The latest fiscal quarter whose results were filed before the statement date.
    reported_through: FiscalQuarter | None = None


@dataclass(frozen=True, slots=True)
class GuidedFigure:
    """A formal guidance row's figure, for linking a claim that restates it."""

    id: int
    metric: str
    period: tuple[int, int | None]  # (fiscal year, fiscal quarter or None)
    issued_at: date
    values: tuple[Decimal, ...]  # low, high, point as given


@dataclass(slots=True)
class Framed:
    """A promise, ready to store."""

    subject: str
    metric: str | None
    measure_period: str | None
    target_kind: str | None
    direction: str
    target_value: Decimal | None
    unit: str | None
    target_text: str | None
    required_value: Decimal | None
    event: str | None
    baseline: Baseline | None
    baseline_source: str | None
    due_date: date | None
    due_basis: str
    deadline_text: str | None
    resolution_rule: str
    specificity: str
    bold: bool
    bold_basis: str | None
    group_key: str
    related_companies: list[str] = field(default_factory=list)
    comparison_basis: str | None = None
    claim_type: str = "promise"
    guidance_id: int | None = None
    notes: list[str] = field(default_factory=list)


def claim_key(article_id: int, ticker: str, item_key: str) -> str:
    """The hash that identifies a claim across re-extraction, framing and resolution."""
    return hashlib.sha256(f"{article_id}|{ticker}|{item_key}".encode()).hexdigest()


def frame(
    claim: ClaimInput,
    framing: Framing,
    fiscal: FiscalCalendar | None,
    actuals: Actuals | None,
    guidance: Sequence[GuidedFigure] = (),
) -> Framed:
    notes: list[str] = []
    text = f"{claim.quote}\n{claim.paragraph}"
    kind: str | None = None if framing.target_kind == "none" else framing.target_kind
    unit = _unit(framing.unit)
    # The unit of what is counted, before a multiple's unit becomes "x".
    counted = unit if unit not in ("x", "pct") else None
    value = _decimal(framing.target_value)
    if kind in NUMERIC_KINDS and value is not None and not _target_grounded(kind, value, text):
        notes.append(f"target {value:f} is not written in the quote or paragraph: dropped")
        value = None
    if kind == "multiple":
        unit = "x"
    direction = framing.direction
    if kind == "change" and value is not None and value < 0:
        direction = "at_most"
    event = " ".join(framing.event.split()) if framing.event and kind == "event" else None

    metric = framing.metric if framing.metric not in (None, "other", "tax_rate") else None
    due = due_date(framing.deadline, claim.statement_date, fiscal, claim.reported_through)
    if due.note:
        notes.append(f"deadline {framing.deadline.phrase or framing.deadline.type!r}: {due.note}")
    baseline, baseline_source = _baseline(
        claim, framing, kind, value, metric, counted, text, actuals, notes
    )
    basis = framing.comparison_basis
    if basis is None and kind == "change" and _YEAR_OVER_YEAR.search(claim.quote):
        basis = "yoy"  # framings without the field: read from the quote's wording
    if (
        kind == "change"
        and basis == "yoy"
        and metric is not None
        and actuals is not None
        and fiscal is not None
        and due.date is not None
        and baseline_source != "article"
    ):
        target = covering_period(fiscal, due.date, framing.measure_period)
        earlier = year_earlier_baseline(actuals, metric, target, claim.ticker)
        if earlier is not None:
            baseline, baseline_source = earlier, "actuals"
        else:
            baseline, baseline_source = None, "unknown"
        notes.append("a year-over-year change: measured from the same period a year earlier")
    required = _required(kind, value, baseline)
    has_target = (kind in NUMERIC_KINDS and value is not None) or (kind == "event" and bool(event))
    has_deadline = due.date is not None
    specificity = (
        "specific"
        if has_target and has_deadline
        else "partial"
        if has_target or has_deadline
        else "vague"
    )
    if not has_target:
        kind = kind if kind == "event" and event else None
    bold, bold_basis = _bold(kind, value, unit, direction, baseline, framing.new_market_or_product)
    subject = " ".join(framing.subject.split()) or "the claim"
    rule = resolution_rule(
        claim,
        subject=subject,
        kind=kind,
        direction=direction,
        value=value,
        unit=unit,
        required=required,
        event=event,
        baseline=baseline,
        measure_period=framing.measure_period,
        due=due.date,
    )
    group = "|".join(
        [
            claim.ticker,
            claim.speaker.casefold(),
            subject.casefold(),
            kind or "",
            f"{value.normalize():f}" if value is not None else "",
            unit or "",
            due.date.isoformat() if due.date else "",
        ]
    )
    claim_type = (
        "scheduled_action"
        if due.date is not None
        and _SCHEDULED.search(f"{subject} {event or ''} {claim.quote}")
        and kind in ("event", "absolute", "threshold", None)
        else "promise"
    )
    guidance_id = None
    if metric is not None and value is not None and due.date is not None and fiscal is not None:
        period = covering_period(fiscal, due.date, framing.measure_period)
        guidance_id = _guidance_row(guidance, metric, period, value, claim.statement_date)
        if guidance_id is not None:
            notes.append(f"restates formal guidance row {guidance_id}: scored there")
    return Framed(
        claim_type=claim_type,
        guidance_id=guidance_id,
        subject=subject,
        metric=metric,
        measure_period=framing.measure_period,
        target_kind=kind,
        direction=direction,
        target_value=value,
        unit=unit if kind in NUMERIC_KINDS else None,
        target_text=framing.target_text,
        required_value=required,
        event=event,
        baseline=baseline,
        baseline_source=baseline_source,
        due_date=due.date,
        due_basis=due.basis,
        deadline_text=framing.deadline.phrase,
        resolution_rule=rule,
        specificity=specificity,
        bold=bold,
        bold_basis=bold_basis,
        group_key=hashlib.sha256(group.encode()).hexdigest(),
        related_companies=list(dict.fromkeys(framing.related_companies)),
        comparison_basis=basis,
        notes=notes,
    )


# Corporate actions on a fixed date: resolved, but not counted as promises in track records.
_SCHEDULED = re.compile(
    r"\b(stock split|split-adjusted|forward split|dividend|record date|shareholders of record|"
    r"holders of record|ex-dividend)\b",
    re.IGNORECASE,
)
# Guidance a claim restates: issued this many days before or after the statement.
GUIDANCE_WINDOW_DAYS = 120


def _guidance_row(
    guidance: Sequence[GuidedFigure],
    metric: str,
    period: tuple[int, int | None],
    value: Decimal,
    said: date,
) -> int | None:
    """The formal guidance row with the claim's metric, period and figure (a low, high or
    point within 0.5%), issued closest to the statement."""
    found = [
        g
        for g in guidance
        if g.metric == metric
        and g.period == period
        and abs((g.issued_at - said).days) <= GUIDANCE_WINDOW_DAYS
        and any(v != 0 and abs(v - value) <= abs(v) * Decimal("0.005") for v in g.values)
    ]
    if not found:
        return None
    return min(found, key=lambda g: (abs((g.issued_at - said).days), g.id)).id


def covering_period(
    fiscal: FiscalCalendar, due: date, measure_period: str | None
) -> tuple[int, int | None]:
    """The fiscal quarter (or year) a due date closes, allowing for 52/53-week years."""
    before = fiscal.quarter_ended_before(due - DRIFT)
    year, quarter = (
        (before.fiscal_year + 1, 1)
        if before.fiscal_quarter == 4
        else (before.fiscal_year, before.fiscal_quarter + 1)
    )
    return (year, None) if measure_period == "year" else (year, quarter)


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _unit(unit: str | None) -> str | None:
    if not unit:
        return None
    folded = unit.strip().lower()
    aliases = {
        "$": "usd",
        "dollars": "usd",
        "usd": "usd",
        "%": "pct",
        "percent": "pct",
        "pct": "pct",
        "times": "x",
        "x": "x",
        "usd_per_share": "usd_per_share",
        "per share": "usd_per_share",
    }
    return aliases.get(folded, folded)


def number_written(value: Decimal, text: str) -> bool:
    """Whether `value` is a number written in `text`, within the rounding it is written
    with ("$2 billion" for 2000000000, "20%" for 20)."""
    for mention in mentions(text):
        if abs(abs(mention.value) - abs(value)) <= mention.rounding:
            return True
    return False


def _target_grounded(kind: str, value: Decimal, text: str) -> bool:
    if kind == "multiple":
        if any(
            re.search(p, text, re.IGNORECASE) and v == value for p, v in _MULTIPLE_WORDS.items()
        ):
            return True
        return any(Decimal(m) == value for m in _TIMES.findall(text))
    if kind == "threshold" and value == 0 and _ZERO_WORDS.search(text):
        return True
    return number_written(value, text)


def _baseline(
    claim: ClaimInput,
    framing: Framing,
    kind: str | None,
    value: Decimal | None,
    metric: str | None,
    unit: str | None,
    text: str,
    actuals: Actuals | None,
    notes: list[str],
) -> tuple[Baseline | None, str | None]:
    if kind not in NUMERIC_KINDS or value is None:
        return None, None  # no target, so nothing is measured from a baseline
    stated = _decimal(framing.baseline_value)
    if stated is not None:
        if number_written(stated, text):
            period = framing.baseline_period_text or "as stated"
            return Baseline(stated, period, "article", unit), "article"
        notes.append(f"baseline {stated:f} is not written in the quote or paragraph: dropped")
    if metric is not None and actuals is not None:
        if unit in (None, metric_unit(metric)):
            found = reported_baseline(
                actuals, metric, framing.measure_period, claim.statement_date, claim.ticker
            )
            if found is not None:
                return found, "actuals"
        else:
            notes.append(f"unit {unit} does not fit {metric}: no stored baseline")
    return None, "unknown"


def _required(kind: str | None, value: Decimal | None, baseline: Baseline | None) -> Decimal | None:
    """The level the measured value must reach (or, for a ceiling, stay under)."""
    if value is None:
        return None
    if kind in ("absolute", "threshold"):
        return value
    if baseline is None:
        return None
    if kind == "multiple":
        return baseline.value * value
    if kind == "change":
        return baseline.value * (1 + value / 100)
    return None


def _bold(
    kind: str | None,
    value: Decimal | None,
    unit: str | None,
    direction: str,
    baseline: Baseline | None,
    new_market_or_product: bool,
) -> tuple[bool, str | None]:
    if kind == "multiple" and value is not None:
        bold = value >= BOLD_MULTIPLE
        return bold, f"a target of {value.normalize():f} times the baseline"
    if kind == "change" and value is not None:
        return abs(value) > BOLD_CHANGE_PCT, f"a change of {value.normalize():f}%"
    if kind == "threshold" and value is not None and baseline is not None:
        crossing = baseline.value < value if direction == "at_least" else baseline.value > value
        return crossing, (
            f"from {baseline.value.normalize():f} ({baseline.period}) to "
            f"{'at least' if direction == 'at_least' else 'at most'} {value.normalize():f}"
        )
    if kind == "absolute" and value is not None and baseline is not None and baseline.value > 0:
        ratio = value / baseline.value
        bold = ratio > BOLD_RATIO if direction == "at_least" else ratio < 1 / BOLD_RATIO
        return bold, f"{ratio:.2f} times the baseline ({baseline.period})"
    if new_market_or_product:
        return True, "a new market or product"
    return False, None


def show(value: Decimal, unit: str | None) -> str:
    """A figure as the rule writes it: "$2.5 billion", "20%", "2x", "410,831 vehicles"."""
    if unit == "usd":
        size = abs(value)
        sign = "-" if value < 0 else ""
        for scale, word in ((10**12, "trillion"), (10**9, "billion"), (10**6, "million")):
            if size >= scale:
                return f"{sign}${_trim(size / scale)} {word}"
        return f"{sign}${_trim(size)}"
    if unit == "usd_per_share":
        return f"${value:.2f} per share"
    if unit == "pct":
        return f"{_trim(value)}%"
    if unit == "x":
        return f"{_trim(value)}x"
    number = f"{value:,.0f}" if value == value.to_integral_value() else _trim(value)
    return f"{number} {unit}" if unit else number


def _trim(value: Decimal) -> str:
    rounded = round(value, 3)
    text = f"{rounded:,f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def resolution_rule(
    claim: ClaimInput,
    *,
    subject: str,
    kind: str | None,
    direction: str,
    value: Decimal | None,
    unit: str | None,
    required: Decimal | None,
    event: str | None,
    baseline: Baseline | None,
    measure_period: str | None,
    due: date | None,
) -> str:
    """One sentence saying exactly what would make the claim true."""
    who = f"{claim.company}'s {subject}"
    said = claim.statement_date.isoformat()
    if due is None:
        when = f"at any time after {said} (no deadline)"
    elif measure_period == "quarter":
        when = f"in any quarter ending on or before {due.isoformat()}"
    elif measure_period == "year":
        when = f"for a fiscal year ending on or before {due.isoformat()}"
    else:
        when = f"on or before {due.isoformat()}"
    reach = "reaches at least" if direction == "at_least" else "is at most"
    if kind == "event" and event:
        return f"True if this happens {when}: {event.rstrip('.')}."
    if kind is None or value is None:
        return f"Not resolvable: the claim states no checkable target ({subject})."
    if kind in ("absolute", "threshold"):
        if kind == "threshold" and value == 0:
            level = "is above zero" if direction == "at_least" else "is at or below zero"
            return f"True if {who} {level} {when}."
        return f"True if {who} {reach} {show(value, unit)} {when}."
    since = f"its level in the latest period reported before {said}"
    if kind == "multiple":
        if baseline is not None and required is not None:
            return (
                f"True if {who} {reach} {_trim(value)} x {show(baseline.value, baseline.unit)} "
                f"({baseline.period}), that is {show(required, baseline.unit)}, {when}."
            )
        return f"True if {who} {reach} {_trim(value)} times {since}, {when}."
    verb = "grows" if value >= 0 else "falls"
    change = f"{verb} by at least {_trim(abs(value))}%"
    if baseline is not None and required is not None:
        bound = "at least" if value >= 0 else "at most"
        return (
            f"True if {who} {change} from {show(baseline.value, baseline.unit)} "
            f"({baseline.period}), to {bound} {show(required, baseline.unit)}, {when}."
        )
    return f"True if {who} {change} from {since}, {when}."
