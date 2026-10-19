"""Figures of a guidance statement, read from its wording: "$108.0 billion, plus or minus
2%" → 105.84 to 110.16 billion dollars.

The model copies the figure as written ("value_text"); everything numeric happens here, so
the same wording always gives the same numbers. Amounts are in dollars, percentages in
percent (74.0 means 74%), per-share amounts in dollars per share.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Unit = Literal["usd", "pct", "usd_per_share", "other"]
PlusMinusKind = Literal["absolute", "percent"]

_SCALES = {
    "trillion": Decimal(10) ** 12,
    "t": Decimal(10) ** 12,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "b": Decimal(10) ** 9,
    "million": Decimal(10) ** 6,
    "mm": Decimal(10) ** 6,
    "m": Decimal(10) ** 6,
    "thousand": Decimal(10) ** 3,
    "k": Decimal(10) ** 3,
}

_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})

# "$(0.21)", "(79)%", "$4.665 billion", "1.15B", "50 basis points", "12 percent"
_AMOUNT = re.compile(
    r"(?P<open>\()?\s*(?P<cur>\$)?\s*(?P<open2>\()?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<close>\))?"
    r"(?:\s*(?P<scale>trillion|billion|million|thousand|bn|mm|[tbmk])(?![a-z]))?"
    r"\s*(?P<pct>%|percent(?:age points?)?|basis points?|bps|pts?|points?)?",
    re.IGNORECASE,
)
_PLUS_MINUS = re.compile(r"plus\s+or\s+minus|\+\s*/\s*-|\u00b1|\+\s+or\s+-", re.IGNORECASE)
# Words that make a figure a description rather than a number: "mid-70% range",
# "low double-digit", "slightly above 12%", "high-30% range".
_HEDGE = re.compile(
    r"\b(?:low|lower|mid|middle|high|upper)[- ]+(?:to[- ]+(?:low|lower|mid|high|upper)[- ]+)?"
    r"(?:\d|single|double|teens|twenties)"
    r"|\bslightly\b|\bdigits?\b|\bteens\b|\brange of\b(?!\s*\$?\d)",
    re.IGNORECASE,
)
_RANGE_JOIN = re.compile(r"^\s*(?:-|to|through|and)\s*$", re.IGNORECASE)
_BETWEEN = re.compile(r"\b(?:between|from)\b", re.IGNORECASE)
# Bound words count only right before the figure ("over $5 billion"), so "year over
# year" after it does not make a point a bound.
_AT_LEAST = re.compile(
    r"\b(?:at\s+least|more\s+than|greater\s+than|over|above|exceed(?:ing)?|no\s+less\s+than)"
    r"\s+[$(]?\d|\b(?:or\s+more|or\s+higher|or\s+greater)\b|\d\s*%?\s*\+",
    re.IGNORECASE,
)
_AT_MOST = re.compile(
    r"\b(?:up\s+to|at\s+most|no\s+more\s+than|less\s+than|below|under)\s+[$(]?\d"
    r"|\b(?:or\s+less|or\s+lower)\b",
    re.IGNORECASE,
)
_DECLINE = re.compile(r"\b(?:decline|decrease|down|drop|fall|lower)\w*\b", re.IGNORECASE)
_FLAT = re.compile(r"\bflat\b", re.IGNORECASE)


class UnparsedValue(ValueError):
    """The wording is not a figure this parser reads: a description ("low double-digit"),
    or several figures for different things."""


@dataclass(frozen=True, slots=True)
class ParsedValue:
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    plus_minus: Decimal | None
    plus_minus_kind: PlusMinusKind | None
    unit: Unit


@dataclass(frozen=True, slots=True)
class _Amount:
    value: Decimal
    currency: bool
    scale: Decimal | None
    kind: str | None  # "pct", "bps", "points" or None
    start: int
    end: int

    @property
    def scaled(self) -> Decimal:
        return self.value * (self.scale or 1)


def normalize_text(text: str) -> str:
    """Dashes as "-", no-break spaces as spaces."""
    return text.translate(_DASHES).replace("\u00a0", " ").replace("\u202f", " ")


def parse_value(text: str, *, per_share: bool = False) -> ParsedValue:
    """Read a guided figure: a point, a range, a value plus or minus an amount, or a bound.

    A percentage plus or minus a percentage (a tax rate of "17.0%, plus or minus 1%") is
    plus or minus percentage points; an amount plus or minus a percentage is relative.
    Raises UnparsedValue when the wording is not a figure.
    """
    text = normalize_text(text).strip()
    if _HEDGE.search(text):
        raise UnparsedValue(f"descriptive figure: {text!r}")
    parts = _PLUS_MINUS.split(text, maxsplit=1)
    base_text = parts[0]
    spread_text = parts[1] if len(parts) > 1 else ""

    amounts = _amounts(base_text)
    if not amounts:
        if _FLAT.search(base_text):
            return ParsedValue(None, None, Decimal(0), None, None, "pct")
        raise UnparsedValue(f"no figure in {text!r}")
    if len(amounts) > 2:
        raise UnparsedValue(f"more than two figures in {text!r}")

    if len(amounts) == 2:
        first, second = amounts
        between = base_text[first.end : second.start]
        joined_by_and = between.strip().lower() == "and"
        if not _RANGE_JOIN.match(between) or (
            joined_by_and and not _BETWEEN.search(base_text[: first.start])
        ):
            raise UnparsedValue(f"two figures that are not a range: {text!r}")
        first, second = _share_notation(first, second)
        unit = _unit(amounts, per_share)
        low, high = sorted((_value(first, unit), _value(second, unit)))
        if _FLAT.search(base_text[: first.start]):
            low = Decimal(0)
        return ParsedValue(low, high, None, None, None, unit)

    (amount,) = amounts
    unit = _unit(amounts, per_share)
    value = _value(amount, unit)
    if unit == "pct" and _DECLINE.search(base_text[: amount.start]):
        value = -value
    if _FLAT.search(base_text[: amount.start]):
        return ParsedValue(Decimal(0), value, None, None, None, unit)
    if spread_text:
        return _with_spread(value, unit, amount, spread_text, text)
    if _AT_LEAST.search(base_text):
        return ParsedValue(value, None, None, None, None, unit)
    if _AT_MOST.search(base_text):
        return ParsedValue(None, value, None, None, None, unit)
    return ParsedValue(None, None, value, None, None, unit)


def _with_spread(
    value: Decimal, unit: Unit, amount: _Amount, spread_text: str, text: str
) -> ParsedValue:
    spreads = _amounts(spread_text)
    if len(spreads) != 1:
        raise UnparsedValue(f"unreadable plus or minus in {text!r}")
    (spread,) = spreads
    kind: PlusMinusKind
    if spread.kind == "bps":
        if unit != "pct":
            raise UnparsedValue(f"basis points around a non-percentage in {text!r}")
        width, kind = spread.value / 100, "absolute"
    elif spread.kind in ("pct", "points"):
        if unit == "pct":
            width, kind = spread.value, "absolute"  # percentage points
        else:
            width, kind = value * spread.value / 100, "percent"
    else:
        width, kind = _scaled_like(spread, amount), "absolute"
    spread_value = spread.value if kind == "percent" else width
    return ParsedValue(value - width, value + width, value, spread_value, kind, unit)


def _scaled_like(spread: _Amount, base: _Amount) -> Decimal:
    """A spread written without a scale ("$13 billion, plus or minus $300 million" has
    one; "$1.5 billion plus or minus 0.1") takes the base's."""
    return spread.value * (spread.scale or base.scale or 1)


def _amounts(text: str) -> list[_Amount]:
    found = []
    for match in _AMOUNT.finditer(text):
        kind = match["pct"]
        if kind:
            lowered = kind.lower()
            if lowered.startswith("basis") or lowered == "bps":
                kind = "bps"
            elif lowered in ("%", "percent"):
                kind = "pct"
            else:
                kind = "points"
        value = Decimal(match["num"].replace(",", ""))
        negative = bool(match["open"] or match["open2"]) and bool(match["close"])
        scale = match["scale"]
        found.append(
            _Amount(
                value=-value if negative else value,
                currency=bool(match["cur"]),
                scale=_SCALES[scale.lower()] if scale else None,
                kind=kind,
                start=match.start(),
                end=match.end(),
            )
        )
    return found


def _share_notation(first: _Amount, second: _Amount) -> tuple[_Amount, _Amount]:
    """In "$15.8-16.8 billion" and "$61-64 billion" the dollar sign, scale and percent
    sign written once apply to both ends."""
    currency = first.currency or second.currency
    scale = first.scale or second.scale
    kind = first.kind or second.kind
    return tuple(  # type: ignore[return-value]
        _Amount(a.value, currency, a.scale or scale, a.kind or kind, a.start, a.end)
        for a in (first, second)
    )


def _unit(amounts: list[_Amount], per_share: bool) -> Unit:
    if any(a.kind in ("pct", "points") for a in amounts):
        return "pct"
    if any(a.currency for a in amounts):
        return "usd_per_share" if per_share else "usd"
    return "other"


def _value(amount: _Amount, unit: Unit) -> Decimal:
    if unit == "pct":
        return amount.value
    return amount.scaled
