"""What the model returns for one document: guidance statements as written.

The model reads and classifies; it does not compute. Figures stay as the release words
them (`value_text`) and periods as named (`period_text`); numbers, units, ranges and
fiscal periods are worked out in code (`numbers.py`, `periods.py`).
"""

from typing import Literal

from pydantic import BaseModel, Field

Metric = Literal[
    "revenue",
    "gross_margin",
    "operating_margin",
    "operating_expenses",
    "operating_income",
    "eps",
    "capex",
    "tax_rate",
    "other",
]
Basis = Literal["gaap", "non_gaap", "unspecified"]
# What the figure measures: the amount itself, its change from the same period a year
# earlier or from the previous quarter, or its size as a share of revenue.
Measure = Literal["level", "yoy_growth", "sequential_growth", "share_of_revenue"]

METRICS: tuple[Metric, ...] = Metric.__args__  # type: ignore[attr-defined]
MEASURES: tuple[Measure, ...] = Measure.__args__  # type: ignore[attr-defined]


class ExtractedItem(BaseModel):
    """One guidance statement: one metric, basis, segment, measure and period."""

    metric: Metric = Field(description="Canonical metric; 'other' for anything else.")
    metric_raw: str = Field(description="The metric in the company's words.")
    segment: str | None = Field(
        description="Business segment, product line or revenue stream the figure covers, "
        "as named; null for the whole company."
    )
    basis: Basis = Field(
        description="'gaap' or 'non_gaap' when the text says so (including 'GAAP and "
        "non-GAAP ... are expected to be X' for both); otherwise 'unspecified'."
    )
    measure: Measure
    constant_currency: bool = Field(
        description="True when the figure is in constant currency or F/X neutral."
    )
    period_text: str = Field(
        description="The target period exactly as the release names it, including from "
        "a heading or lead-in sentence ('third quarter of fiscal 2027', 'Q4 FY26', 'full "
        "year', '2026')."
    )
    value_text: str | None = Field(
        description="This item's figure as written, with its range or plus-or-minus "
        "wording ('$108.0 billion, plus or minus 2%', '$61-64 billion', 'approximately "
        "56%'); null when the statement gives no figure."
    )
    qualitative: str | None = Field(
        description="For guidance without a figure, the expectation in brief ('above "
        "2025 operating income'); otherwise null."
    )
    conditions: str | None = Field(
        description="Assumptions, inclusions and exclusions stated for this item; null if none."
    )
    quote: str = Field(
        description="The exact text the item comes from, copied character for character "
        "from one section: one sentence, bullet or table row. No ellipses."
    )


class Extraction(BaseModel):
    items: list[ExtractedItem]
