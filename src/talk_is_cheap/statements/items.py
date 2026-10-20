"""From what the model returned to statements: quotes grounded in the article text,
figures parsed, periods resolved and speakers matched to the executive roster.

Everything here is deterministic; the same model answer always gives the same claims.
Figures and periods use the guidance code (`guidance/numbers.py`, `guidance/periods.py`),
so a claim and a guidance row of the same words get the same numbers. A period is
resolved as if the statement were made in a release for the last fiscal quarter that
ended before the statement date: "this year" is the fiscal year under way, "next
quarter" and "this quarter" the quarter under way.
"""

import hashlib
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.guidance.grounding import locate
from talk_is_cheap.guidance.items import default_unit
from talk_is_cheap.guidance.numbers import UnparsedValue, parse_value
from talk_is_cheap.guidance.periods import FiscalPeriod, resolve_period
from talk_is_cheap.statements.roster import match_executive
from talk_is_cheap.statements.schema import ExtractedClaim, StatementExtraction
from talk_is_cheap.universe import UniverseCompany

# Raise when post-processing changes: stored extractions are then processed again from
# their cached responses, without calling the model.
PROCESSING_VERSION = "3"

# Attributions that make a claim management's, besides a verified executive.
COMPANY_VOICES = ("company_spokesperson", "company_statement")
# A statement date the model gives is used only within this many days before the article.
MAX_STATEMENT_AGE = timedelta(days=366)


@dataclass(frozen=True, slots=True)
class SourceText:
    """The text a story was read from (`candidates.StoryText`) and its date."""

    article_id: int
    text: str
    published: date


@dataclass(frozen=True, slots=True)
class Claim:
    quote: str  # source text[char_start:char_end]
    char_start: int
    char_end: int
    speaker_name: str | None
    speaker_role: str | None
    attribution: str
    executive_name: str | None
    executive_role: str | None
    management: bool
    is_direct_quote: bool
    venue: str
    statement_date: date
    date_from_article: bool
    claim_type: str
    claim: str
    metric: str | None
    metric_raw: str | None
    segment: str | None
    basis: str
    measure: str
    period_text: str | None
    target: FiscalPeriod | None
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    unit: str
    value_text: str | None
    direction: str | None

    @property
    def has_figure(self) -> bool:
        return any(v is not None for v in (self.low, self.high, self.point))

    def key(self) -> str:
        """A hash of what makes the claim: the same claim of the same article has the
        same key whenever it is extracted again, so review decisions survive."""
        parts = [
            " ".join(self.quote.casefold().split()),
            self.claim_type,
            self.metric or "",
            self.target.label if self.target else "",
            *(
                f"{v.normalize():f}" if v is not None else ""
                for v in (self.low, self.high, self.point)
            ),
            self.direction or "",
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["statement_date"] = self.statement_date.isoformat()
        data["target"] = self.target.label if self.target else None
        for name in ("low", "high", "point"):
            data[name] = None if data[name] is None else str(data[name])
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Claim":
        values = dict(data)
        values["statement_date"] = date.fromisoformat(values["statement_date"])
        values["target"] = FiscalPeriod.parse(values["target"]) if values["target"] else None
        for name in ("low", "high", "point"):
            values[name] = None if values[name] is None else Decimal(values[name])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Dropped:
    reason: str
    item: dict[str, Any]


def process(
    source: SourceText,
    extraction: StatementExtraction,
    company: UniverseCompany,
    calendar: FiscalCalendar | None,
) -> tuple[list[Claim], list[Dropped]]:
    """Ground, parse, resolve and attribute every claim the model returned for one story.
    Claims whose quote is not in the text are dropped; repeats of a claim are merged."""
    claims: list[Claim] = []
    dropped: list[Dropped] = []
    seen: set[str] = set()
    for extracted in extraction.claims:
        span = locate(extracted.quote, source.text)
        if span is None:
            dropped.append(Dropped("quote not found in the article text", extracted.model_dump()))
            continue
        claim = _claim(extracted, span, source, company, calendar)
        if claim.key() not in seen:
            seen.add(claim.key())
            claims.append(claim)
    return claims, dropped


def _claim(
    extracted: ExtractedClaim,
    span: tuple[int, int],
    source: SourceText,
    company: UniverseCompany,
    calendar: FiscalCalendar | None,
) -> Claim:
    statement_date, from_article = _statement_date(extracted.statement_date, source.published)
    attribution: str = extracted.attribution
    executive = None
    if attribution == "named_executive":
        executive = match_executive(
            extracted.speaker_name, extracted.speaker_role, company, statement_date
        )
        if executive is None:
            attribution = "unverified_executive"
    metric = extracted.metric
    value_text = _clean(extracted.value_text)
    parsed = None
    if value_text:
        try:
            parsed = parse_value(value_text, per_share=metric == "eps")
        except UnparsedValue:
            parsed = None
    claim_type = extracted.claim_type
    direction = extracted.direction
    if claim_type == "numeric_forecast" and parsed is None:
        claim_type = "directional" if direction else "qualitative"
    if claim_type != "directional":
        direction = None
    measure = extracted.measure
    unit = parsed.unit if parsed else default_unit(metric or "other", measure)
    target = None
    period_text = _clean(extracted.period_text)
    if period_text and calendar is not None:
        target = resolve_period(
            period_text, calendar.quarter_ended_before(statement_date), calendar
        )
    start, end = span
    claim = Claim(
        quote=source.text[start:end],
        char_start=start,
        char_end=end,
        speaker_name=_clean(extracted.speaker_name),
        speaker_role=_clean(extracted.speaker_role),
        attribution=attribution,
        executive_name=executive.name if executive else None,
        executive_role=executive.role if executive else None,
        management=executive is not None or attribution in COMPANY_VOICES,
        is_direct_quote=extracted.is_direct_quote,
        venue=extracted.venue,
        statement_date=statement_date,
        date_from_article=from_article,
        claim_type=claim_type,
        claim=" ".join(extracted.claim.split()),
        metric=metric,
        metric_raw=_clean(extracted.metric_raw),
        segment=_clean(extracted.segment),
        basis=extracted.basis,
        measure=measure,
        period_text=period_text,
        target=target,
        low=parsed.low if parsed and claim_type == "numeric_forecast" else None,
        high=parsed.high if parsed and claim_type == "numeric_forecast" else None,
        point=parsed.point if parsed and claim_type == "numeric_forecast" else None,
        unit=unit,
        value_text=value_text,
        direction=direction,
    )
    if claim.metric is None and claim.claim_type in ("numeric_forecast", "directional"):
        claim = replace(claim, metric="other", unit="other")
    return claim


def _statement_date(text: str | None, published: date) -> tuple[date, bool]:
    """The date the model read from the article, when it is a real date no later than
    the article and no more than a year before it; otherwise the article's date."""
    if text:
        try:
            day = date.fromisoformat(text.strip())
        except ValueError:
            day = None
        if day is not None and published - MAX_STATEMENT_AGE <= day <= published:
            return day, False
    return published, True


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if text and text.lower() not in ("null", "none", "n/a") else None
