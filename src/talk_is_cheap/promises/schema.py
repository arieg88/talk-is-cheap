"""What the model returns when it frames one claim, and what it returns when it plans
and judges a resolution.

The model reads; code computes. A framing gives the claim's subject, its target as
written and the structure of its deadline ("in N months", "end of year Y", "fiscal year
+1"); code computes the due date (`deadlines.py`), looks up the baseline in stored data
(`baselines.py`) and writes the resolution rule (`framing.py`). A judgment names the
evidence and the exact text span a measured number comes from; code reads the number
(`cite_value`) and checks the verdict (`checks.py`).

What the model fills in has no nullable fields (`FramingAnswer`): providers refuse
structured-output schemas with many optional unions, so "not given" is an empty string,
zero or "none", and `FramingAnswer.framing()` turns it into the `Framing` code reads.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from talk_is_cheap.guidance.schema import Metric

TargetKind = Literal["multiple", "absolute", "change", "threshold", "event", "none"]
DeadlineType = Literal[
    "duration",
    "calendar_year",
    "calendar_half",
    "calendar_quarter",
    "month",
    "date",
    "season",
    "fiscal_year",
    "fiscal_quarter",
    "none",
]
Verdict = Literal[
    "delivered",
    "delivered_late",
    "partly_delivered",
    "not_delivered",
    "walked_back",
    "too_early",
    "expired_unproven",
    "cannot_determine",
]

ComparisonBasis = Literal["yoy", "sequential", "absolute"]
Finding = Literal["satisfies", "partly", "contradicts", "walks_back", "irrelevant"]

TARGET_KINDS: tuple[str, ...] = TargetKind.__args__  # type: ignore[attr-defined]
DEADLINE_TYPES: tuple[str, ...] = DeadlineType.__args__  # type: ignore[attr-defined]
VERDICTS: tuple[str, ...] = Verdict.__args__  # type: ignore[attr-defined]


class Deadline(BaseModel):
    """The deadline as written and its structure; code computes the date."""

    phrase: str | None = Field(
        default=None,
        description="The deadline exactly as written ('within a year', 'by the end of next "
        "year', 'next fiscal year', 'by next summer'); null when the claim gives none.",
    )
    type: DeadlineType = Field(
        description="duration: 'in/within N days, weeks, months or years'; calendar_year: "
        "'by (the end of) 2026', 'next year', 'later this year'; calendar_half: 'first "
        "half of 2026'; calendar_quarter: 'by Q3 2026' for a calendar quarter; month: 'by "
        "June', 'in March 2026'; date: a day ('June 10, 2024'); season: 'next summer'; "
        "fiscal_year: 'fiscal 2026', 'next fiscal year', 'this fiscal year'; "
        "fiscal_quarter: 'next quarter', 'this quarter', 'the fiscal fourth quarter', 'Q2 "
        "fiscal 2025'; none: no deadline ('over time', 'eventually', 'soon')."
    )
    amount: int | None = Field(default=None, description="duration: the N.")
    unit: Literal["days", "weeks", "months", "years"] | None = Field(
        default=None, description="duration: its unit."
    )
    year: int | None = Field(
        default=None,
        description="A year the deadline names, four digits, as written (a fiscal year as "
        "the company names it: 'fiscal 2026' is 2026). Null when it is said relative to "
        "the statement ('next year').",
    )
    offset: int | None = Field(
        default=None,
        description="For a year or quarter said relative to the statement: 0 for this "
        "year or quarter ('later this year', 'this quarter'), 1 for the next ('next "
        "year', 'next fiscal year', 'next quarter'), 2 for the one after. Null when a "
        "year is named.",
    )
    quarter: int | None = Field(default=None, ge=1, le=4, description="A quarter number, 1-4.")
    half: int | None = Field(default=None, ge=1, le=2, description="calendar_half: 1 or 2.")
    month: int | None = Field(default=None, ge=1, le=12, description="month or date: 1-12.")
    day: int | None = Field(default=None, ge=1, le=31, description="date: the day of month.")
    season: Literal["spring", "summer", "fall", "winter"] | None = None


class Framing(BaseModel):
    """One management claim, made resolvable."""

    subject: str = Field(
        description="What the claim is about, in a few words: 'vehicle production', "
        "'robotaxi cities', 'data-center revenue', 'Reality Labs operating loss'."
    )
    metric: Metric | None = Field(
        default=None,
        description="The company-wide reported metric the target is about, when it is one "
        "of these; null for anything else (segments, units, users, products, events).",
    )
    measure_period: Literal["quarter", "year"] | None = Field(
        default=None,
        description="Whether the target is a quarterly or an annual figure ('quarterly "
        "production', 'annual revenue'); null for events and one-off counts.",
    )
    target_kind: TargetKind = Field(
        description="multiple: double, triple, 10x; absolute: reach a level ('$10 billion', "
        "'10 cities', '1 million units'); change: grow or cut by a percentage ('grow 20%'); "
        "threshold: cross a line ('profitable', 'cash-flow positive', 'break even'); event: "
        "a concrete, observable event (a named product launches or ships, production "
        "starts, a deal closes, a plant opens); none: no checkable target ('poised for "
        "growth', 'demand will stay strong')."
    )
    target_text: str | None = Field(
        default=None,
        description="The target exactly as written in the quote ('double', '10 cities', "
        "'$2 billion', 'profitable'); null for none.",
    )
    target_value: float | None = Field(
        default=None,
        description="The target as a number: 2 for 'double', 10 for '10 cities', "
        "2000000000 for '$2 billion' (amounts in dollars), 20 for '20%', 0 for "
        "'profitable' or 'break even'. Null for events and none.",
    )
    unit: str | None = Field(
        default=None,
        description="usd, pct, x (multiples), or the counted thing in one plural word "
        "('cities', 'vehicles', 'users'). Null for events and none.",
    )
    direction: Literal["at_least", "at_most"] = Field(
        default="at_least",
        description="at_most when the target is a ceiling (cut losses below $1 billion, "
        "keep expenses under $X).",
    )
    event: str | None = Field(
        default=None,
        description="event: the event in one short sentence with the company named "
        "('Tesla starts volume production of the Cybercab').",
    )
    baseline_text: str | None = Field(
        default=None,
        description="When the quote or paragraph states what the target is measured from "
        "('from 410,831 vehicles last quarter'), that figure exactly as written; else null. "
        "Never a figure from memory.",
    )
    baseline_value: float | None = Field(
        default=None, description="baseline_text as a number, in the target's unit."
    )
    baseline_period_text: str | None = Field(
        default=None, description="The period of the stated baseline as written ('Q1 2025')."
    )
    deadline: Deadline
    new_market_or_product: bool = Field(
        default=False,
        description="True when the claim is about entering a new market or launching a "
        "product the company does not sell yet.",
    )
    related_companies: list[str] = Field(
        default_factory=list,
        description="Other companies the claim names as partners, customers, suppliers or "
        "makers of the product (Dell, HP, Microsoft); used to widen the evidence search.",
    )
    comparison_basis: ComparisonBasis | None = Field(
        default=None,
        description="What a change is measured against: yoy (the same period a year "
        "earlier), sequential (the period before), absolute (a level, not a change).",
    )


class EvidencePlan(BaseModel):
    """The evidence to gather for one claim."""

    actuals_periods: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Fiscal periods of the claim's metric to fetch, 'FY2026Q1' or "
        "'FY2026', after the statement and up to the due date. Empty without a metric.",
    )
    filing_queries: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Searches of later earnings releases, deliveries reports, 10-Qs and "
        "10-Ks: the operating figure or event to find ('vehicle production quarter', "
        "'Blackwell shipments began').",
    )
    news_queries: list[str] = Field(
        default_factory=list,
        max_length=2,
        description="Searches of later news headlines: launches, delays, cancellations.",
    )
    check_later_statements: bool = Field(
        default=True,
        description="Whether to read the company's later statements, for a claim walked "
        "back, restated or revised.",
    )


class MeasuredSource(BaseModel):
    model_config = ConfigDict(json_schema_extra=lambda s: _all_required(s))

    evidence_id: str = Field(description="The id of the evidence item holding the number.")
    span: str = Field(
        default="",
        description="For passages, headlines and quotes: the exact text, copied character "
        "for character, that contains the one number measured. Empty for reported values "
        "(get_actuals), which carry their number.",
    )


class Assessment(BaseModel):
    model_config = ConfigDict(json_schema_extra=lambda s: _all_required(s))

    evidence_id: str
    finding: Finding
    note: str = ""
    # For a satisfying item: the exact text of the item that says when the thing
    # happened ("effective June 7, 2024"), copied character for character; empty if the
    # item gives no date. Code finds it in the item and reads the date.
    event_date_text: str = ""


class Judgment(BaseModel):
    """What each gathered item says about the rule. The judge gives no verdict: code
    derives it from the findings and the items' dates (`checks.py`)."""

    model_config = ConfigDict(json_schema_extra=lambda s: _all_required(s))

    assessments: list[Assessment] = Field(default_factory=list)
    measured: list[MeasuredSource] = Field(default_factory=list, max_length=4)
    measured_operation: Literal["value", "sum", "ratio"] = "value"
    partial_reason: str = ""
    reasoning: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


def _all_required(schema: dict[str, Any]) -> None:
    """Every property required in the schema the model reads; the model's answer may
    still leave one out, which then takes its default."""
    schema["required"] = list(schema.get("properties", {}))


class DeadlineAnswer(BaseModel):
    """The deadline as written and its structure; code computes the date. Every field
    is required in the schema (optional ones make it too complex for providers); zero,
    -1, "none" or empty means not given."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    phrase: str = ""
    type: DeadlineType = "none"
    amount: int = 0
    unit: Literal["days", "weeks", "months", "years", "none"] = "none"
    year: int = 0
    offset: int = -1
    quarter: int = 0
    half: int = 0
    month: int = 0
    day: int = 0
    season: Literal["spring", "summer", "fall", "winter", "none"] = "none"

    def deadline(self) -> Deadline:
        def given(value: int, low: int, high: int) -> int | None:
            return value if low <= value <= high else None

        return Deadline(
            phrase=self.phrase.strip() or None,
            type=self.type,
            amount=self.amount if self.amount > 0 else None,
            unit=None if self.unit == "none" else self.unit,
            year=given(self.year, 1900, 2100),
            offset=self.offset if self.offset >= 0 else None,
            quarter=given(self.quarter, 1, 4),
            half=given(self.half, 1, 2),
            month=given(self.month, 1, 12),
            day=given(self.day, 1, 31),
            season=None if self.season == "none" else self.season,
        )


class FramingAnswer(BaseModel):
    """One management claim, made resolvable, as the prompt describes each field
    (`prompts/promise_framing`): the schema carries no descriptions, to keep each call
    small. Empty strings, zeros and "none" mean not given."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    subject: str
    metric: Literal[Metric, "none"] = "none"
    measure_period: Literal["quarter", "year", "none"] = "none"
    target_kind: TargetKind = "none"
    target_text: str = ""
    target_value: float = 0
    unit: str = ""
    direction: Literal["at_least", "at_most"] = "at_least"
    event: str = ""
    baseline_text: str = ""
    baseline_value: float = 0
    baseline_period_text: str = ""
    deadline: DeadlineAnswer = Field(default_factory=DeadlineAnswer)
    new_market_or_product: bool = False
    related_companies: list[str] = Field(default_factory=list)
    comparison_basis: Literal["yoy", "sequential", "absolute", "none"] = "none"

    def framing(self) -> Framing:
        numeric = self.target_kind in ("multiple", "absolute", "change", "threshold")
        return Framing(
            subject=self.subject,
            metric=None if self.metric == "none" else self.metric,
            measure_period=None if self.measure_period == "none" else self.measure_period,
            target_kind=self.target_kind,
            target_text=self.target_text.strip() or None,
            target_value=self.target_value
            if numeric and (self.target_value or self.target_kind == "threshold")
            else None,
            unit=self.unit.strip() or None,
            direction=self.direction,
            event=self.event.strip() or None,
            baseline_text=self.baseline_text.strip() or None,
            baseline_value=self.baseline_value if self.baseline_text.strip() else None,
            baseline_period_text=self.baseline_period_text.strip() or None,
            deadline=self.deadline.deadline(),
            new_market_or_product=self.new_market_or_product,
            related_companies=[c.strip() for c in self.related_companies if c.strip()],
            comparison_basis=None if self.comparison_basis == "none" else self.comparison_basis,
        )
