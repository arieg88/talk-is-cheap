"""From what the model returned to guidance items: figures parsed, periods resolved,
quotes grounded in the source text, and duplicates merged.

Everything here is deterministic; the same model answer always gives the same items.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.grounding import locate
from talk_is_cheap.guidance.numbers import ParsedValue, Unit, UnparsedValue, parse_value
from talk_is_cheap.guidance.periods import FiscalPeriod, resolve_period
from talk_is_cheap.guidance.schema import ExtractedItem, Extraction

# Raise when post-processing changes: stored extractions are then processed again from
# their cached responses, without calling the model.
PROCESSING_VERSION = "3"


@dataclass(frozen=True, slots=True)
class SourceSection:
    """An outlook section: `text` is `documents.text[char_start:char_end]`."""

    title: str
    text: str
    char_start: int


@dataclass(frozen=True, slots=True)
class SourceDocument:
    accession_no: str
    document_name: str
    doc_type: str
    url: str
    issued: FiscalQuarter  # the quarter the release reports
    sections: Sequence[SourceSection]


@dataclass(frozen=True, slots=True)
class Source:
    """Where an item is written: characters `char_start` to `char_end` of a document."""

    accession_no: str
    document_name: str
    doc_type: str
    url: str
    section_title: str
    char_start: int
    char_end: int
    quote: str  # the document's own text over that range


@dataclass(frozen=True, slots=True)
class GuidanceItem:
    metric: str
    metric_raw: str
    segment: str | None
    basis: str
    measure: str
    constant_currency: bool
    period_text: str
    target: FiscalPeriod | None
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    plus_minus: Decimal | None
    plus_minus_kind: str | None
    unit: Unit
    value_text: str | None
    qualitative: str | None
    conditions: str | None
    sources: tuple[Source, ...] = field(default=())

    @property
    def has_figure(self) -> bool:
        return any(v is not None for v in (self.low, self.high, self.point))

    def key(self) -> tuple[object, ...]:
        """Items with the same key are the same statement."""
        return (
            self.metric,
            self.basis,
            _fold(self.segment),
            self.measure,
            self.constant_currency,
            self.target,
            self.low,
            self.high,
            self.point,
            self.unit,
            None if self.has_figure else _fold(self.qualitative or self.value_text),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "metric_raw": self.metric_raw,
            "segment": self.segment,
            "basis": self.basis,
            "measure": self.measure,
            "constant_currency": self.constant_currency,
            "period_text": self.period_text,
            "target": self.target.label if self.target else None,
            "low": _text(self.low),
            "high": _text(self.high),
            "point": _text(self.point),
            "plus_minus": _text(self.plus_minus),
            "plus_minus_kind": self.plus_minus_kind,
            "unit": self.unit,
            "value_text": self.value_text,
            "qualitative": self.qualitative,
            "conditions": self.conditions,
            "sources": [
                {
                    "accession_no": s.accession_no,
                    "document_name": s.document_name,
                    "doc_type": s.doc_type,
                    "url": s.url,
                    "section_title": s.section_title,
                    "char_start": s.char_start,
                    "char_end": s.char_end,
                    "quote": s.quote,
                }
                for s in self.sources
            ],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "GuidanceItem":
        return cls(
            metric=data["metric"],
            metric_raw=data["metric_raw"],
            segment=data["segment"],
            basis=data["basis"],
            measure=data["measure"],
            constant_currency=data["constant_currency"],
            period_text=data["period_text"],
            target=FiscalPeriod.parse(data["target"]) if data["target"] else None,
            low=_decimal(data["low"]),
            high=_decimal(data["high"]),
            point=_decimal(data["point"]),
            plus_minus=_decimal(data["plus_minus"]),
            plus_minus_kind=data["plus_minus_kind"],
            unit=data["unit"],
            value_text=data["value_text"],
            qualitative=data["qualitative"],
            conditions=data["conditions"],
            sources=tuple(Source(**s) for s in data["sources"]),
        )


@dataclass(frozen=True, slots=True)
class Dropped:
    """A model item left out, and why."""

    reason: str
    item: dict[str, Any]


def process(
    document: SourceDocument, extraction: Extraction, calendar: FiscalCalendar
) -> tuple[list[GuidanceItem], list[Dropped]]:
    """Ground, parse and resolve every item the model returned for one document. Items
    whose quote is not in the document's outlook sections are dropped; duplicates within
    the document are merged."""
    items: list[GuidanceItem] = []
    dropped: list[Dropped] = []
    for extracted in extraction.items:
        source = _ground(extracted.quote, document)
        if source is None:
            dropped.append(
                Dropped("quote not found in the outlook sections", extracted.model_dump())
            )
            continue
        items.append(_item(extracted, document.issued, calendar, source))
    return merge(scope_conditions(items, document)), dropped


# Sentences end at ".", "!" or "?" followed by a space and a capital or an opening mark.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"\u201c])")


def scope_conditions(items: Sequence[GuidanceItem], document: SourceDocument) -> list[GuidanceItem]:
    """Drop conditions that belong to another item of the document.

    Models attach a condition written in one item's bullet to its neighbours too
    (Nvidia's "not assuming any Data Center compute revenue from China" on margin and
    expense items). Each condition (parts separated by ";") is found in the source: when
    the sentence it is in, or else the line (a paragraph, bullet or table row), contains
    the quote of other items but not this item's, it belongs to them and is dropped.
    Conditions not found in the text, or written apart from every item (a sentence for
    all of the guidance), are kept.
    """
    spans = [(s.char_start, s.char_end) for item in items for s in item.sources[:1]]
    scoped = []
    for item in items:
        if not item.conditions or not item.sources:
            scoped.append(item)
            continue
        own = (item.sources[0].char_start, item.sources[0].char_end)
        others = [span for span in spans if not _overlaps(span, own)]
        kept = [
            part
            for part in (p.strip() for p in item.conditions.split(";"))
            if part and _belongs(part, own, others, document)
        ]
        scoped.append(replace(item, conditions="; ".join(kept) or None))
    return scoped


def _belongs(
    condition: str,
    own: tuple[int, int],
    others: Sequence[tuple[int, int]],
    document: SourceDocument,
) -> bool:
    for section in document.sections:
        found = locate(condition, section.text)
        if found is None:
            continue
        position = section.char_start + found[0]
        for scope in (_sentence(section, position), _line(section, position)):
            if _overlaps(scope, own):
                return True
            if any(_overlaps(scope, other) for other in others):
                return False
        return True
    return True


def _line(section: SourceSection, position: int) -> tuple[int, int]:
    local = position - section.char_start
    start = section.text.rfind("\n", 0, local) + 1
    end = section.text.find("\n", local)
    end = len(section.text) if end < 0 else end
    return section.char_start + start, section.char_start + end


def _sentence(section: SourceSection, position: int) -> tuple[int, int]:
    line_start, line_end = _line(section, position)
    text = section.text[line_start - section.char_start : line_end - section.char_start]
    start = 0
    for boundary in _SENTENCE_END.finditer(text):
        if line_start + boundary.start() >= position:
            return line_start + start, line_start + boundary.start()
        start = boundary.end()
    return line_start + start, line_end


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def merge(items: Iterable[GuidanceItem]) -> list[GuidanceItem]:
    """One item per statement, in order of first appearance, with the sources of every
    copy (a press release and its CFO commentary, or a text and a table)."""
    merged: dict[tuple[object, ...], GuidanceItem] = {}
    for item in items:
        key = item.key()
        found = merged.get(key)
        if found is None:
            merged[key] = item
            continue
        sources = found.sources + tuple(s for s in item.sources if s not in found.sources)
        merged[key] = replace(
            found,
            conditions=found.conditions or item.conditions,
            sources=sources,
        )
    return list(merged.values())


def default_unit(metric: str, measure: str) -> Unit:
    """The unit an item of this metric and measure is stated in."""
    if measure != "level" or metric in ("gross_margin", "operating_margin", "tax_rate"):
        return "pct"
    if metric == "eps":
        return "usd_per_share"
    if metric == "other":
        return "other"
    return "usd"


def _item(
    extracted: ExtractedItem,
    issued: FiscalQuarter,
    calendar: FiscalCalendar,
    source: Source,
) -> GuidanceItem:
    parsed: ParsedValue | None = None
    qualitative = _clean(extracted.qualitative)
    value_text = _clean(extracted.value_text)
    if value_text:
        try:
            parsed = parse_value(value_text, per_share=extracted.metric == "eps")
        except UnparsedValue:
            qualitative = qualitative or value_text  # "in the mid-70% range"
    unit = parsed.unit if parsed else default_unit(extracted.metric, extracted.measure)
    return GuidanceItem(
        metric=extracted.metric,
        metric_raw=extracted.metric_raw.strip(),
        segment=_clean(extracted.segment),
        basis=extracted.basis,
        measure=extracted.measure,
        constant_currency=extracted.constant_currency,
        period_text=extracted.period_text.strip(),
        target=resolve_period(extracted.period_text, issued, calendar),
        low=parsed.low if parsed else None,
        high=parsed.high if parsed else None,
        point=parsed.point if parsed else None,
        plus_minus=parsed.plus_minus if parsed else None,
        plus_minus_kind=parsed.plus_minus_kind if parsed else None,
        unit=unit,
        value_text=value_text,
        qualitative=None if parsed else qualitative,
        conditions=_clean(extracted.conditions),
        sources=(source,),
    )


def _ground(quote: str, document: SourceDocument) -> Source | None:
    for section in document.sections:
        span = locate(quote, section.text)
        if span is None:
            continue
        start, end = span
        return Source(
            accession_no=document.accession_no,
            document_name=document.document_name,
            doc_type=document.doc_type,
            url=document.url,
            section_title=section.title,
            char_start=section.char_start + start,
            char_end=section.char_start + end,
            quote=section.text[start:end],
        )
    return None


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if text and text.lower() not in ("null", "none", "n/a") else None


def _fold(text: str | None) -> str | None:
    return None if text is None else " ".join(text.casefold().split())


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
