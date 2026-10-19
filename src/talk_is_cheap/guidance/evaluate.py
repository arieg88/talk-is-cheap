"""Guidance extraction benchmark: extracted items against hand-labeled ones.

`evals/guidance/labels.jsonl` holds one line per document: the guidance statements a
reader finds in its outlook sections, labeled from the text (never from model output).
A label's figures are in dollars, dollars per share or percent; "108.0B" and "300M" are
shorthand for billions and millions.

An extracted item matches a label of the same document when metric, basis, measure,
currency basis and (except for metric "other") whether it names a segment agree, and it
either quotes overlapping text or has the same period or figures. Pairs are made
greedily, best agreement first. Then:

- precision, recall and F1 of items, overall and separately for items with a figure
  (numeric) and qualitative items;
- figures exact: of matched pairs, the share whose low, high, point and unit all agree;
- period accuracy: of matched pairs, the share with the same resolved fiscal period;
- conditions captured: of matched labels that state conditions, the share whose item
  has conditions; and spurious conditions: of matched labels without conditions, the
  share whose item has some;
- tokens and cost per document, as the responses cost when they were made.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Company, Document
from talk_is_cheap.guidance.extract import (
    PROMPT_VERSION,
    OutlookDocument,
    company_documents,
    load_prompt,
    render_prompt,
)
from talk_is_cheap.guidance.grounding import locate
from talk_is_cheap.guidance.items import GuidanceItem, process
from talk_is_cheap.guidance.llm import (
    Budget,
    ExtractionModel,
    ResponseCache,
    Usage,
    complete,
)
from talk_is_cheap.guidance.periods import FiscalPeriod

_SHORTHAND = {"B": Decimal(10) ** 9, "M": Decimal(10) ** 6}

# Items with a figure, and qualitative items, are scored apart as well as together.
Kind = Literal["numeric", "qualitative"]
KINDS: tuple[Kind, ...] = ("numeric", "qualitative")


@dataclass(frozen=True, slots=True)
class LabeledItem:
    metric: str
    basis: str
    measure: str
    constant_currency: bool
    segment: str | None
    target: FiscalPeriod | None
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    unit: str
    quote: str
    conditions: str | None = None
    note: str | None = None
    span: tuple[int, int] | None = None  # in the document text, once located


@dataclass(frozen=True, slots=True)
class LabeledDocument:
    id: str
    ticker: str
    accession_no: str
    document_name: str
    items: tuple[LabeledItem, ...]


def amount(value: object) -> Decimal | None:
    """A labeled figure: a number, or a string with an optional B or M suffix."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    scale = _SHORTHAND.get(text[-1:].upper()) if text[-1:].isalpha() else None
    if scale is not None:
        return Decimal(text[:-1]) * scale
    return Decimal(text)


def load_labels(path: Path) -> list[LabeledDocument]:
    documents = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            documents.append(
                LabeledDocument(
                    id=data["id"],
                    ticker=data["ticker"],
                    accession_no=data["accession_no"],
                    document_name=data["document_name"],
                    items=tuple(_labeled_item(i) for i in data["items"]),
                )
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise ValueError(f"line {number}: {exc}") from exc
    return documents


def _labeled_item(data: dict[str, Any]) -> LabeledItem:
    return LabeledItem(
        metric=data["metric"],
        basis=data.get("basis", "unspecified"),
        measure=data.get("measure", "level"),
        constant_currency=bool(data.get("constant_currency", False)),
        segment=data.get("segment"),
        target=FiscalPeriod.parse(data["target"]) if data.get("target") else None,
        low=amount(data.get("low")),
        high=amount(data.get("high")),
        point=amount(data.get("point")),
        unit=data["unit"],
        quote=data["quote"],
        conditions=data.get("conditions"),
        note=data.get("note"),
    )


@dataclass(slots=True)
class DocumentScore:
    id: str
    labeled: int
    extracted: int
    matched: list[tuple[LabeledItem, GuidanceItem]] = field(default_factory=list)
    missed: list[LabeledItem] = field(default_factory=list)
    extra: list[GuidanceItem] = field(default_factory=list)
    dropped: int = 0
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    cost: Decimal = Decimal(0)
    error: str | None = None


@dataclass(slots=True)
class ModelScore:
    model: str
    documents: list[DocumentScore]

    def _sum(self, attribute: str) -> int:
        return sum(int(getattr(d, attribute)) for d in self.documents)

    @property
    def labeled(self) -> int:
        return self._sum("labeled")

    @property
    def extracted(self) -> int:
        return self._sum("extracted")

    @property
    def matched(self) -> int:
        return sum(len(d.matched) for d in self.documents)

    def precision(self, kind: Kind | None = None) -> float:
        """Of the extracted items (of a kind), the share that match a label."""
        items = [i for d in self.documents for i in (*(p[1] for p in d.matched), *d.extra)]
        matched = [p for d in self.documents for p in d.matched]
        if kind is not None:
            items = [i for i in items if _item_kind(i) == kind]
            matched = [p for p in matched if _item_kind(p[1]) == kind]
        return len(matched) / len(items) if items else 0.0

    def recall(self, kind: Kind | None = None) -> float:
        """Of the labels (of a kind), the share that an extracted item matches."""
        labels = [lab for d in self.documents for lab in (*(p[0] for p in d.matched), *d.missed)]
        matched = [p for d in self.documents for p in d.matched]
        if kind is not None:
            labels = [lab for lab in labels if _label_kind(lab) == kind]
            matched = [p for p in matched if _label_kind(p[0]) == kind]
        return len(matched) / len(labels) if labels else 0.0

    def f1(self, kind: Kind | None = None) -> float:
        p, r = self.precision(kind), self.recall(kind)
        return 2 * p * r / (p + r) if p + r else 0.0

    def count(self, kind: Kind) -> int:
        """Labels of a kind."""
        return sum(
            _label_kind(lab) == kind
            for d in self.documents
            for lab in (*(p[0] for p in d.matched), *d.missed)
        )

    @property
    def conditions_spurious(self) -> float:
        """Of matched labels without conditions, the share whose item has some anyway
        (conditions attached to the wrong item)."""
        pairs = [p for d in self.documents for p in d.matched if not p[0].conditions]
        return sum(p[1].conditions is not None for p in pairs) / len(pairs) if pairs else 0.0

    @property
    def conditions_captured(self) -> float:
        """Of matched labels that state conditions, the share whose item has conditions."""
        pairs = [p for d in self.documents for p in d.matched if p[0].conditions]
        return sum(p[1].conditions is not None for p in pairs) / len(pairs) if pairs else 0.0

    def _share(self, agree: Callable[[LabeledItem, GuidanceItem], bool]) -> float:
        pairs = [pair for d in self.documents for pair in d.matched]
        return sum(agree(*pair) for pair in pairs) / len(pairs) if pairs else 0.0

    @property
    def figures_exact(self) -> float:
        return self._share(
            lambda label, item: (
                (label.low, label.high, label.point, label.unit)
                == (item.low, item.high, item.point, item.unit)
            )
        )

    @property
    def period_accuracy(self) -> float:
        return self._share(lambda label, item: label.target == item.target)

    @property
    def dropped(self) -> int:
        return self._sum("dropped")

    @property
    def errors(self) -> int:
        return sum(d.error is not None for d in self.documents)

    @property
    def cost(self) -> Decimal:
        return sum((d.cost for d in self.documents), Decimal(0))

    @property
    def usage(self) -> Usage:
        total = Usage(0, 0)
        for document in self.documents:
            total += document.usage
        return total


async def labeled_documents(
    sessions: async_sessionmaker[AsyncSession],
    labels: Sequence[LabeledDocument],
    names: Mapping[str, str] | None = None,
) -> list[tuple[LabeledDocument, OutlookDocument, str]]:
    """Each labeled document with its outlook sections and full text, and its labels'
    quotes located in that text. `names` are the company names extraction uses, so the
    prompts are the ones a run sends. Raises ValueError for a document that is not loaded or a
    label whose quote is not in it."""
    found = []
    async with sessions() as session:
        for label in labels:
            cik = await session.scalar(select(Company.cik).where(Company.ticker == label.ticker))
            documents = (
                {}
                if cik is None
                else {d.key: d for d in await company_documents(session, cik, names)}
            )
            document = documents.get((label.accession_no, label.document_name))
            text = await session.scalar(
                select(Document.text).where(
                    Document.accession_no == label.accession_no,
                    Document.document_name == label.document_name,
                )
            )
            if document is None or text is None:
                raise ValueError(f"{label.id}: document with outlook not loaded")
            located = []
            for item in label.items:
                span = locate(item.quote, text)
                if span is None:
                    raise ValueError(f"{label.id}: label quote not in the document: {item.quote!r}")
                located.append(replace(item, span=span))
            found.append((replace(label, items=tuple(located)), document, text))
    return found


async def evaluate(
    documents: Sequence[tuple[LabeledDocument, OutlookDocument, str]],
    model: ExtractionModel,
    cache: ResponseCache,
    budget: Budget,
    *,
    prompt_version: str = PROMPT_VERSION,
) -> ModelScore:
    """Extract every labeled document with `model` (cached responses first) and score it."""
    template = load_prompt(prompt_version)
    scores = []
    for label, document, _ in documents:
        prompt = render_prompt(template, document)
        score = DocumentScore(label.id, labeled=len(label.items), extracted=0)
        try:
            response = await complete(model, prompt, prompt_version, cache, budget)
        except Exception as exc:  # scored as extracting nothing
            score.error = f"{type(exc).__name__}: {exc}"
            score.missed = list(label.items)
            scores.append(score)
            continue
        items, dropped = process(document.source, response.extraction, document.calendar)
        score.extracted = len(items)
        score.dropped = len(dropped)
        score.usage = response.usage
        score.cost = response.cost
        score.matched, score.missed, score.extra = match_items(label.items, items)
        scores.append(score)
    return ModelScore(model.name, scores)


def match_items(
    labels: Sequence[LabeledItem], items: Sequence[GuidanceItem]
) -> tuple[list[tuple[LabeledItem, GuidanceItem]], list[LabeledItem], list[GuidanceItem]]:
    """Pair labels with extracted items, best agreement first."""
    candidates = []
    for i, label in enumerate(labels):
        for j, item in enumerate(items):
            score = _agreement(label, item)
            if score is not None:
                candidates.append((score, i, j))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_labels: set[int] = set()
    used_items: set[int] = set()
    matched = []
    for _, i, j in candidates:
        if i in used_labels or j in used_items:
            continue
        used_labels.add(i)
        used_items.add(j)
        matched.append((labels[i], items[j]))
    missed = [label for i, label in enumerate(labels) if i not in used_labels]
    extra = [item for j, item in enumerate(items) if j not in used_items]
    return matched, missed, extra


def _agreement(label: LabeledItem, item: GuidanceItem) -> int | None:
    if (label.metric, label.basis, label.measure, label.constant_currency) != (
        item.metric,
        item.basis,
        item.measure,
        item.constant_currency,
    ):
        return None
    # For named metrics a segment changes what is measured. For "other" the metric has no
    # name, and naming it in `segment` ("energy storage") or in `metric_raw` is equivalent.
    if label.metric != "other" and (label.segment is None) != (item.segment is None):
        return None
    same_period = label.target == item.target
    same_figures = (label.low, label.high, label.point) == (item.low, item.high, item.point)
    overlaps = label.span is not None and any(
        s.char_start < label.span[1] and label.span[0] < s.char_end for s in item.sources
    )
    if not (same_period or same_figures or overlaps):
        return None
    return 4 * same_figures + 2 * same_period + overlaps


def _label_kind(label: LabeledItem) -> Kind:
    has_figure = any(v is not None for v in (label.low, label.high, label.point))
    return "numeric" if has_figure else "qualitative"


def _item_kind(item: GuidanceItem) -> Kind:
    return "numeric" if item.has_figure else "qualitative"
