"""The verifier's code checks on a draft, run before any model is asked:

- `numbers_grounded`: every number in the draft, after normalizing units ("$96.2
  billion", "96,221 million", "74.98%", "$(0.21)"), appears in the evidence or in a
  `compute` result, within the rounding the draft wrote it with;
- `citations_exist`: every citation id names an item of evidence that has a URL;
- `numbers_cited`: every sentence with a number carries a citation.

Not numbers: citation ids, fiscal periods ("FY2027 Q2", "Q3", "fiscal 2026"), years,
dates, form names ("10-K", "8-K"), list markers, and integers up to SMALL_INTEGER
written without a unit ("the last 4 quarters"). Numbers in the question are allowed too.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from talk_is_cheap.agent.evidence import Evidence

CITATION = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]")
CITATION_ID = re.compile(r"E\d+")

# Integers this small, without a unit or decimals, are counts ("4 quarters"), not figures.
SMALL_INTEGER = 12
# Slack on top of the rounding a number was written with: float noise only.
RELATIVE_SLACK = Decimal("0.000001")

_SCALES = {
    "trillion": Decimal(10) ** 12,
    "tn": Decimal(10) ** 12,
    "t": Decimal(10) ** 12,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "b": Decimal(10) ** 9,
    "million": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "mm": Decimal(10) ** 6,
    "m": Decimal(10) ** 6,
    "thousand": Decimal(10) ** 3,
    "k": Decimal(10) ** 3,
}
_PERCENT_WORDS = ("%", "percent", "percentage points", "pp", "points")
_BASIS_POINTS = ("basis points", "bps", "bp")

_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
# Text that holds digits but no figure, removed before numbers are read.
_NOT_FIGURES = [
    CITATION,
    re.compile(r"\[\d+(?:\s*,\s*\d+)*\]"),  # rendered citation markers
    re.compile(r"https?://\S+"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\b"),
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}(?:\s+\d{{4}})?", re.IGNORECASE),
    re.compile(r"\bFY\s?'?\d{2,4}(?:\s?Q[1-4])?\b", re.IGNORECASE),
    re.compile(r"\bQ[1-4](?:\s?(?:FY\s?)?'?\d{2,4})?\b", re.IGNORECASE),
    re.compile(r"\b[1-4](?:st|nd|rd|th)?\s+quarter\b", re.IGNORECASE),
    re.compile(r"\b(?:fiscal|calendar)(?:\s+year)?\s+'?\d{2,4}\b", re.IGNORECASE),
    re.compile(r"\b(?:10-K|10-Q|8-K|20-F|6-K)(?:/A)?\b", re.IGNORECASE),
    re.compile(r"\b(?:Item|Part)\s+\d+[A-C]?\b", re.IGNORECASE),
    re.compile(r"\bE\d+\b"),
    re.compile(r"(?m)^\s*\d+[.)]\s"),  # numbered list markers
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE),
    re.compile(r"\b(?:19|20)\d{2}s?\b(?![.,]\d)(?!\s*(?:%|percent|billion|million))"),
]

_NUMBER = re.compile(
    r"(?P<minus>[-\u2212\u2013]\s?)?(?P<open>\()?(?P<dollar>\$\s?)?(?P<open2>\()?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?P<close>\))?"
    r"(?:\s?(?P<scale>trillion|billion|million|thousand|bn|tn|mn|mm|[BMKT])\b)?"
    r"(?:\s?(?P<unit>%|percent(?:age points)?|pp\b|points\b|basis points|bps\b|bp\b|x\b))?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Mention:
    """A number as the draft wrote it: its value in base units (dollars, percent, a
    plain number) and how finely it was rounded."""

    text: str
    value: Decimal
    rounding: Decimal  # half of the last written digit, in base units
    kind: str  # "usd", "pct", "plain"
    small: bool  # an integer count without a unit


@dataclass(frozen=True, slots=True)
class Failure:
    check: str  # numbers_grounded, citations_exist, numbers_cited, llm_support, ...
    detail: str


def strip_non_figures(text: str) -> str:
    for pattern in _NOT_FIGURES:
        text = pattern.sub(" ", text)
    return text


def mentions(text: str) -> list[Mention]:
    """The numbers written in `text`, normalized."""
    found = []
    for match in _NUMBER.finditer(strip_non_figures(text)):
        digits = match["int"].replace(",", "")
        decimals = match["dec"] or ""
        try:
            number = Decimal(f"{digits}.{decimals}" if decimals else digits)
        except InvalidOperation:
            continue
        scale_word = (match["scale"] or "").lower()
        scale = _SCALES.get(scale_word, Decimal(1))
        unit = (match["unit"] or "").lower()
        if unit in _BASIS_POINTS:
            number, scale, kind = number / 100, Decimal(1), "pct"
        elif unit in _PERCENT_WORDS or unit.startswith("percent"):
            kind = "pct"
        elif match["dollar"]:
            kind = "usd"
        else:
            kind = "plain"
        negative = bool(match["minus"]) or bool(
            (match["open"] or match["open2"]) and match["close"]
        )
        step = Decimal(1).scaleb(-len(decimals)) / 2
        if unit in _BASIS_POINTS:
            step = step / 100
        value = number * scale
        small = (
            kind == "plain" and not decimals and not scale_word and not unit
        ) and number <= SMALL_INTEGER
        found.append(
            Mention(
                text=match.group(0).strip(),
                value=-value if negative else value,
                rounding=step * scale,
                kind=kind,
                small=small,
            )
        )
    return found


def evidence_values(items: Iterable[Evidence], extra_text: str = "") -> list[Decimal]:
    """Every number in the evidence: numeric fields, and numbers written in its text
    fields (quotes, passages, headlines), plus numbers in `extra_text`."""
    values: list[Decimal] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, int | float | Decimal):
            values.append(Decimal(str(node)))
        elif isinstance(node, str):
            values.extend(m.value for m in mentions(node))
        elif isinstance(node, Mapping):
            for key, value in node.items():
                if key not in ("url", "id"):
                    walk(value)
        elif isinstance(node, list | tuple):
            for value in node:
                walk(value)

    for item in items:
        walk(item["data"])
    values.extend(m.value for m in mentions(extra_text))
    return values


def grounded(mention: Mention, values: Iterable[Decimal]) -> bool:
    """Whether a number matches an evidence value within the rounding it was written
    with. Signs are not compared (wording carries them: "a loss of $1.2 billion"); a
    percentage also matches a fraction of 1 ("0.7498")."""
    target = abs(mention.value)
    for value in values:
        candidates = [abs(value)]
        if mention.kind == "pct":
            candidates.append(abs(value) * 100)
        for candidate in candidates:
            tolerance = mention.rounding + candidate * RELATIVE_SLACK
            if abs(candidate - target) <= tolerance:
                return True
    return False


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[(*•-])|\n+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]


def cited_ids(text: str) -> list[str]:
    """Citation ids in order of first appearance."""
    seen: list[str] = []
    for group in CITATION.findall(text):
        for evidence_id in CITATION_ID.findall(group):
            if evidence_id not in seen:
                seen.append(evidence_id)
    return seen


def code_checks(draft: str, evidence: Mapping[str, Evidence], question: str = "") -> list[Failure]:
    """The three code checks; an empty list means the draft passed."""
    failures: list[Failure] = []
    values = evidence_values(evidence.values(), question)
    for sentence in sentences(draft):
        figures = [m for m in mentions(sentence) if not m.small]
        for mention in figures:
            if not grounded(mention, values):
                failures.append(
                    Failure("numbers_grounded", f"{mention.text!r} is in no evidence: {sentence}")
                )
        if figures and not CITATION.search(sentence):
            failures.append(
                Failure("numbers_cited", f"a sentence with a number has no citation: {sentence}")
            )
    for evidence_id in cited_ids(draft):
        item = evidence.get(evidence_id)
        if item is None:
            failures.append(Failure("citations_exist", f"[{evidence_id}] is not an evidence id"))
        elif not item["url"] and item["tool"] != "compute":
            failures.append(
                Failure(
                    "citations_exist",
                    f"[{evidence_id}] has no URL to cite ({item['tool']}); cite the filing or "
                    "article the figure comes from",
                )
            )
    return failures


def failing_sentences(
    draft: str, evidence: Mapping[str, Evidence], question: str = ""
) -> list[str]:
    """The sentences of a draft that fail a code check."""
    return [s for s in sentences(draft) if code_checks(s, evidence, question)]
