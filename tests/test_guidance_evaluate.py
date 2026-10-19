"""Scoring guidance extraction against labels: pairing, precision and recall overall and
by kind, figures, periods and conditions. No database and no real model."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.evaluate import (
    DocumentScore,
    LabeledDocument,
    LabeledItem,
    ModelScore,
    amount,
    evaluate,
    load_labels,
    match_items,
)
from talk_is_cheap.guidance.extract import OutlookDocument
from talk_is_cheap.guidance.items import GuidanceItem, Source, SourceDocument, SourceSection
from talk_is_cheap.guidance.llm import Budget, Pricing, ResponseCache
from talk_is_cheap.guidance.periods import FiscalPeriod
from talk_is_cheap.guidance.schema import ExtractedItem, Extraction
from tests.guidance_fakes import FakeModel

B = Decimal(10) ** 9
LABELS = Path(__file__).resolve().parents[1] / "evals" / "guidance" / "labels.jsonl"


def label(**overrides: Any) -> LabeledItem:
    values: dict[str, Any] = {
        "metric": "revenue", "basis": "unspecified", "measure": "level",
        "constant_currency": False, "segment": None, "target": FiscalPeriod(2027, 3),
        "low": 105 * B, "high": 110 * B, "point": None, "unit": "usd",
        "quote": "Revenue is expected to be $105-110 billion.", "span": (0, 44),
    }  # fmt: skip
    return LabeledItem(**{**values, **overrides})


def extracted(**overrides: Any) -> GuidanceItem:
    values: dict[str, Any] = {
        "metric": "revenue", "metric_raw": "Revenue", "segment": None, "basis": "unspecified",
        "measure": "level", "constant_currency": False, "period_text": "Q3",
        "target": FiscalPeriod(2027, 3), "low": 105 * B, "high": 110 * B, "point": None,
        "plus_minus": None, "plus_minus_kind": None, "unit": "usd", "value_text": "$105-110B",
        "qualitative": None, "conditions": None,
        "sources": (Source("a", "d.htm", "press_release", "u", "Outlook", 0, 44, "q"),),
    }  # fmt: skip
    return GuidanceItem(**{**values, **overrides})


def test_shorthand_amounts() -> None:
    assert amount("108.0B") == Decimal("108.0") * B
    assert amount("300M") == 300 * Decimal(10) ** 6
    assert amount("-0.25") == Decimal("-0.25")
    assert amount(None) is None


def test_the_labels_file_is_well_formed() -> None:
    documents = load_labels(LABELS)
    assert len(documents) >= 20
    assert len({d.ticker for d in documents}) >= 10
    assert sum(len(d.items) for d in documents) > 150
    assert len({(d.accession_no, d.document_name) for d in documents}) == len(documents)


def test_pairs_need_metric_basis_and_measure_to_agree() -> None:
    gaap = label(basis="gaap")
    matched, missed, extra = match_items([gaap], [extracted()])
    assert (matched, len(missed), len(extra)) == ([], 1, 1)


def test_segments_must_agree_except_for_other_metrics() -> None:
    assert match_items([label()], [extracted(segment="Cloud")])[0] == []
    other = label(metric="other", unit="other")
    named = extracted(metric="other", unit="other", segment="energy storage")
    assert len(match_items([other], [named])[0]) == 1


def test_the_best_agreement_is_paired_first() -> None:
    q3, fy = label(), label(target=FiscalPeriod(2027), low=420 * B, high=430 * B, span=(50, 90))
    fy_item = extracted(target=FiscalPeriod(2027), low=420 * B, high=430 * B)
    matched, missed, extra = match_items([q3, fy], [fy_item, extracted()])
    assert {(lab.target, item.target) for lab, item in matched} == {
        (FiscalPeriod(2027), FiscalPeriod(2027)),
        (FiscalPeriod(2027, 3), FiscalPeriod(2027, 3)),
    }
    assert missed == [] and extra == []


def test_scores_overall_and_by_kind() -> None:
    qualitative_label = label(metric="operating_income", low=None, high=None, span=(60, 90))
    qualitative_item = extracted(metric="operating_income", low=None, high=None,
                                 qualitative="above 2025")  # fmt: skip
    wrong_period = extracted(target=FiscalPeriod(2027, 4))
    score = ModelScore(
        "m",
        [
            DocumentScore(
                "d", labeled=3, extracted=3,
                matched=[(label(), wrong_period), (qualitative_label, qualitative_item)],
                missed=[label(metric="capex", span=(100, 120))],
                extra=[extracted(metric="other", low=None, high=None)],
            )
        ],
    )  # fmt: skip
    assert (score.precision(), score.recall()) == (2 / 3, 2 / 3)
    assert (score.precision("numeric"), score.recall("numeric")) == (1.0, 0.5)
    assert (score.precision("qualitative"), score.recall("qualitative")) == (0.5, 1.0)
    assert (score.count("numeric"), score.count("qualitative")) == (2, 1)
    assert score.period_accuracy == 0.5
    assert score.figures_exact == 1.0


def test_conditions_captured() -> None:
    with_condition = label(conditions="excluding discrete items")
    score = ModelScore(
        "m",
        [
            DocumentScore(
                "d", labeled=2, extracted=2,
                matched=[(with_condition, extracted(conditions="excludes discrete items")),
                         (label(conditions="no China revenue", span=(50, 60)), extracted())],
            )
        ],
    )  # fmt: skip
    assert score.conditions_captured == 0.5
    spurious = ModelScore(
        "m", [DocumentScore("d", labeled=1, extracted=1,
                            matched=[(label(), extracted(conditions="invented"))])],
    )  # fmt: skip
    assert spurious.conditions_spurious == 1.0


async def test_evaluate_extracts_each_labeled_document(tmp_path: Path) -> None:
    text = "## Outlook\n\nRevenue is expected to be $105-110 billion."
    document = OutlookDocument(
        cik=1, ticker="TEST", company="Testco", calendar=FiscalCalendar.parse("0131"),
        period_end=date(2026, 7, 31), filed_at=date(2026, 8, 20),
        source=SourceDocument("a", "d.htm", "press_release", "u", FiscalQuarter(2027, 2),
                              [SourceSection("Outlook", text, 0)]),
    )  # fmt: skip
    labeled = LabeledDocument("TEST-1", "TEST", "a", "d.htm", (label(span=(12, 55)),))
    answer = Extraction(
        items=[
            ExtractedItem(
                metric="revenue", metric_raw="Revenue", segment=None, basis="unspecified",
                measure="level", constant_currency=False, period_text="third quarter",
                value_text="$105-110 billion", qualitative=None, conditions=None,
                quote="Revenue is expected to be $105-110 billion.",
            )
        ]
    )  # fmt: skip
    fake = FakeModel(lambda prompt: answer)
    budget = Budget(Decimal(1), Pricing(Decimal(2), Decimal(10)))
    score = await evaluate([(labeled, document, text)], fake, ResponseCache(tmp_path), budget)
    assert (score.precision(), score.recall(), score.figures_exact) == (1.0, 1.0, 1.0)
    assert score.cost == Decimal("0.007") and len(fake.prompts) == 1


def test_labels_without_required_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    entry = {"id": "x", "ticker": "T", "accession_no": "a", "document_name": "d",
             "items": [{"metric": "revenue"}]}  # fmt: skip
    path.write_text(json.dumps(entry))
    with pytest.raises(ValueError, match="line 1"):
        load_labels(path)
