"""Grounding quotes in the source, and turning model answers into items: figures,
periods, units, offsets and deduplication."""

from decimal import Decimal
from typing import Any

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.guidance.grounding import locate
from talk_is_cheap.guidance.items import (
    GuidanceItem,
    SourceDocument,
    SourceSection,
    merge,
    process,
)
from talk_is_cheap.guidance.periods import FiscalPeriod
from talk_is_cheap.guidance.schema import ExtractedItem, Extraction

B = Decimal(10) ** 9

# Shaped like Nvidia's outlook (press release of 2026-08-26), with typographic quotes.
OUTLOOK = (
    "## Outlook\n\n"
    "NVIDIA\u2019s outlook for the third quarter of fiscal 2027 is as follows:\n\n"
    "- Revenue is expected to be $108.0 billion, plus or minus 2%. NVIDIA is not assuming "
    "any Data Center compute revenue from China in its outlook.\n\n"
    "- GAAP and non-GAAP gross margins are expected to be 74.0%, plus or minus 50 basis "
    "points.\n\n"
    "| Q3 FY 2027 | GAAP | Non-GAAP |\n| --- | --- | --- |\n"
    "| **Gross margin** | 74.0% | 74.0% |\n"
)
PREFIX = "x" * 500  # the section starts 500 characters into the document
DOCUMENT_TEXT = PREFIX + OUTLOOK
CALENDAR = FiscalCalendar.parse("0131")


def document(name: str = "release.htm", doc_type: str = "press_release") -> SourceDocument:
    return SourceDocument(
        accession_no="0001045810-26-000001",
        document_name=name,
        doc_type=doc_type,
        url=f"https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001/{name}",
        issued=FiscalQuarter(2027, 2),
        sections=[SourceSection("Outlook", OUTLOOK, len(PREFIX))],
    )


def extracted(**overrides: Any) -> ExtractedItem:
    values: dict[str, Any] = {
        "metric": "revenue",
        "metric_raw": "Revenue",
        "segment": None,
        "basis": "unspecified",
        "measure": "level",
        "constant_currency": False,
        "period_text": "third quarter of fiscal 2027",
        "value_text": "$108.0 billion, plus or minus 2%",
        "qualitative": None,
        "conditions": "No Data Center compute revenue from China assumed",
        "quote": "Revenue is expected to be $108.0 billion, plus or minus 2%.",
    }
    return ExtractedItem(**{**values, **overrides})


def test_quotes_are_found_despite_quotes_dashes_case_and_markup() -> None:
    text = "NVIDIA\u2019s outlook \u2014 **Revenue** | $1\u20132 billion |"
    span = locate("nvidia's outlook - Revenue $1-2 billion", text)
    assert span is not None
    assert text[span[0] : span[1]] == "NVIDIA\u2019s outlook \u2014 **Revenue** | $1\u20132 billion"


def test_quotes_with_an_ellipsis_cover_the_text_between() -> None:
    text = "Revenue is expected to be $108.0 billion, plus or minus 2%."
    span = locate("Revenue is expected ... plus or minus 2%", text)
    assert span == (0, len(text) - 1)
    assert locate("Revenue ... 2%", text) is None  # pieces too short to trust


def test_quotes_not_in_the_text_are_not_found() -> None:
    assert locate("Revenue is expected to be $110.0 billion", OUTLOOK) is None
    assert locate("", OUTLOOK) is None


def test_case_folding_keeps_offsets() -> None:
    text = "Stra\u00dfe revenue of $5 billion"  # "ß" folds to two letters
    span = locate("revenue of $5 billion", text)
    assert span is not None and text[span[0] : span[1]] == "revenue of $5 billion"


def test_items_are_parsed_resolved_and_grounded_with_document_offsets() -> None:
    items, dropped = process(document(), Extraction(items=[extracted()]), CALENDAR)

    assert dropped == []
    (item,) = items
    assert (item.low, item.high, item.point) == (
        Decimal("105.84") * B,
        Decimal("110.16") * B,
        Decimal(108) * B,
    )
    assert (item.unit, item.plus_minus, item.plus_minus_kind) == ("usd", Decimal(2), "percent")
    assert item.target == FiscalPeriod(2027, 3)
    (source,) = item.sources
    assert DOCUMENT_TEXT[source.char_start : source.char_end] == source.quote
    assert source.quote == "Revenue is expected to be $108.0 billion, plus or minus 2%."


def test_items_whose_quote_is_not_in_the_source_are_dropped() -> None:
    invented = extracted(quote="Revenue is expected to be $110.0 billion, plus or minus 2%.")
    items, dropped = process(document(), Extraction(items=[invented]), CALENDAR)
    assert items == []
    assert len(dropped) == 1 and "not found" in dropped[0].reason
    assert dropped[0].item["quote"] == invented.quote


def test_table_rows_are_grounded() -> None:
    row = extracted(
        metric="gross_margin",
        basis="non_gaap",
        value_text="74.0%",
        quote="| Gross margin | 74.0% | 74.0% |",
        conditions=None,
    )
    (item,), _ = process(document(), Extraction(items=[row]), CALENDAR)
    # Markup around the words is not part of the quote; markup between them is.
    assert item.sources[0].quote == "Gross margin** | 74.0% | 74.0%"


def test_descriptive_figures_become_qualitative() -> None:
    mid = extracted(
        metric="gross_margin",
        value_text="in the mid-70% range",
        period_text="full year",
        quote="GAAP and non-GAAP gross margins are expected to be 74.0%",
    )
    (item,), _ = process(document(), Extraction(items=[mid]), CALENDAR)
    assert (item.low, item.high, item.point) == (None, None, None)
    assert item.qualitative == "in the mid-70% range"
    assert item.unit == "pct" and item.target == FiscalPeriod(2027)


def test_per_share_units() -> None:
    eps = extracted(metric="eps", value_text="$1.32 - $1.34", quote="Revenue is expected")
    (item,), _ = process(document(), Extraction(items=[eps]), CALENDAR)
    assert item.unit == "usd_per_share" and (item.low, item.high) == (
        Decimal("1.32"),
        Decimal("1.34"),
    )


def test_the_same_statement_twice_is_one_item_with_both_sources() -> None:
    text_version = extracted(
        metric="gross_margin",
        basis="gaap",
        value_text="74.0%, plus or minus 50 basis points",
        quote="GAAP and non-GAAP gross margins are expected to be 74.0%, plus or minus 50 "
        "basis points.",
    )
    other_basis = text_version.model_copy(update={"basis": "non_gaap"})
    items, _ = process(document(), Extraction(items=[text_version, other_basis]), CALENDAR)
    assert [i.basis for i in items] == ["gaap", "non_gaap"]  # different statements

    # The CFO commentary repeats the release: merged across documents of a filing.
    release, _ = process(document(), Extraction(items=[extracted()]), CALENDAR)
    commentary, _ = process(
        document("commentary.htm", "earnings_exhibit"), Extraction(items=[extracted()]), CALENDAR
    )
    (merged,) = merge([*release, *commentary])
    assert [s.document_name for s in merged.sources] == ["release.htm", "commentary.htm"]


def test_a_different_figure_is_a_different_statement() -> None:
    first, _ = process(document(), Extraction(items=[extracted()]), CALENDAR)
    raised = extracted(value_text="$110.0 billion, plus or minus 2%")
    second, _ = process(document(), Extraction(items=[raised]), CALENDAR)
    assert len(merge([*first, *second])) == 2


def test_items_survive_a_json_round_trip() -> None:
    (item,), _ = process(document(), Extraction(items=[extracted()]), CALENDAR)
    assert GuidanceItem.from_json(item.to_json()) == item


def test_conditions_written_in_another_items_bullet_are_dropped() -> None:
    # Models copy the revenue bullet's China assumption onto the margin item.
    revenue = extracted(
        conditions="NVIDIA is not assuming any Data Center compute revenue from China"
    )
    margin = extracted(
        metric="gross_margin",
        basis="gaap",
        value_text="74.0%, plus or minus 50 basis points",
        conditions="not assuming any Data Center compute revenue from China; per the outlook",
        quote="GAAP and non-GAAP gross margins are expected to be 74.0%, plus or minus 50 "
        "basis points.",
    )
    items, _ = process(document(), Extraction(items=[revenue, margin]), CALENDAR)
    by_metric = {i.metric: i for i in items}
    assert by_metric["revenue"].conditions == revenue.conditions  # its own bullet
    # Found in the revenue bullet: dropped. Not found in the text: kept.
    assert by_metric["gross_margin"].conditions == "per the outlook"


def test_conditions_for_all_of_the_guidance_are_kept() -> None:
    text = OUTLOOK + "\nThis outlook excludes future acquisitions.\n"
    source = SourceDocument(
        "acc", "release.htm", "press_release", "u", FiscalQuarter(2027, 2),
        [SourceSection("Outlook", text, 0)],
    )  # fmt: skip
    margin = extracted(
        metric="gross_margin",
        value_text="74.0%",
        conditions="excludes future acquisitions",
        quote="GAAP and non-GAAP gross margins are expected to be 74.0%",
    )
    (item,), _ = process(source, Extraction(items=[margin]), CALENDAR)
    assert item.conditions == "excludes future acquisitions"
