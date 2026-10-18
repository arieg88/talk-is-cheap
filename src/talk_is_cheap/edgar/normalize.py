"""Turn raw XBRL facts into canonical quarterly and annual metrics.

Every value keeps the filing it came from, so the same quarter can have several versions:
the one first reported, and later restated ones. Quarters a filing does not report
directly are derived from year-to-date totals (cash-flow statements are year-to-date
only, and annual reports give no fourth-quarter figures), using the totals that were
public on that filing's date.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import Literal

import yaml

from talk_is_cheap.edgar.fiscal import Duration, FiscalCalendar, classify_duration
from talk_is_cheap.edgar.records import RawFact

PeriodType = Literal["quarter", "year"]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    name: str
    unit: str
    additive: bool
    concepts: tuple[str, ...]
    fallback: tuple[str, str] | None = None  # (minuend, subtrahend) metrics


@dataclass(frozen=True, slots=True)
class FinancialValue:
    metric: str
    period_type: PeriodType
    fiscal_year: int
    fiscal_quarter: int | None  # None for full years
    period_start: date
    period_end: date
    value: Decimal
    unit: str
    source_concept: str
    accession_no: str
    form: str
    filed: date
    derived: bool
    derivation: str | None


@dataclass(frozen=True, slots=True)
class _Observation:
    metric: str
    duration: Duration
    fiscal_year: int
    through_quarter: int  # last fiscal quarter covered
    start: date
    end: date
    value: Decimal
    concept: str
    accession_no: str
    form: str
    filed: date
    derivation: str | None


def load_metric_specs() -> dict[str, MetricSpec]:
    text = files("talk_is_cheap.edgar").joinpath("metrics.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    specs = {}
    for name, body in raw.items():
        fallback = body.get("fallback")
        specs[name] = MetricSpec(
            name=name,
            unit=str(body["unit"]),
            additive=bool(body["additive"]),
            concepts=tuple(body["concepts"]),
            fallback=(str(fallback[0]), str(fallback[1])) if fallback else None,
        )
    return specs


def required_concepts(specs: Mapping[str, MetricSpec]) -> set[str]:
    return {concept for spec in specs.values() for concept in spec.concepts}


def normalize(
    facts: Iterable[RawFact],
    specs: Mapping[str, MetricSpec],
    fiscal_calendar: FiscalCalendar,
) -> list[FinancialValue]:
    picked = _pick_concepts(facts, specs)
    _apply_fallbacks(picked, specs)
    observations = _classify(picked, fiscal_calendar)

    results: dict[tuple[str, date, date, str], FinancialValue] = {}
    for obs in observations:
        if obs.duration is Duration.QUARTER:
            _keep(results, _to_value(obs, specs, "quarter", obs.through_quarter, derived=False))
        elif obs.duration is Duration.YEAR:
            _keep(results, _to_value(obs, specs, "year", None, derived=False))

    for value in _derive_quarters(observations, specs):
        _keep(results, value)

    return sorted(results.values(), key=lambda v: (v.metric, v.period_end, v.filed, v.derived))


# A picked value: (concept priority, value, concept label, accession metadata source fact,
# derivation note).
_Picked = dict[tuple[str, str, date, date], tuple[int, Decimal, str, RawFact, str | None]]


def _pick_concepts(facts: Iterable[RawFact], specs: Mapping[str, MetricSpec]) -> _Picked:
    """Per filing and period, take each metric from its highest-priority concept present."""
    by_concept: dict[str, tuple[MetricSpec, int]] = {}
    for spec in specs.values():
        for priority, concept in enumerate(spec.concepts):
            by_concept[concept] = (spec, priority)

    picked: _Picked = {}
    for fact in facts:
        hit = by_concept.get(fact.qualified_concept)
        if hit is None or fact.period_start is None:
            continue
        spec, priority = hit
        if fact.unit != spec.unit:
            continue
        key = (spec.name, fact.accession_no, fact.period_start, fact.period_end)
        if key not in picked or priority < picked[key][0]:
            picked[key] = (priority, fact.value, fact.qualified_concept, fact, None)
    return picked


def _apply_fallbacks(picked: _Picked, specs: Mapping[str, MetricSpec]) -> None:
    for spec in specs.values():
        if spec.fallback is None:
            continue
        minuend, subtrahend = spec.fallback
        for (metric, accession, start, end), (_, value, _, fact, _) in list(picked.items()):
            if metric != minuend or (spec.name, accession, start, end) in picked:
                continue
            other = picked.get((subtrahend, accession, start, end))
            if other is None:
                continue
            picked[(spec.name, accession, start, end)] = (
                len(spec.concepts),
                value - other[1],
                f"{minuend} - {subtrahend}",
                fact,
                f"{minuend} - {subtrahend}",
            )


def _classify(picked: _Picked, fiscal_calendar: FiscalCalendar) -> list[_Observation]:
    observations = []
    for (metric, accession, start, end), (_, value, concept, fact, note) in picked.items():
        duration = classify_duration(start, end)
        if duration is None:
            continue
        fiscal_year = fiscal_calendar.fiscal_year(end)
        quarter = fiscal_calendar.fiscal_quarter(end)
        if duration is not Duration.QUARTER and (
            quarter != duration.quarters
            or not fiscal_calendar.starts_fiscal_year(start, fiscal_year)
        ):
            continue  # not a year-to-date window aligned with the fiscal year
        observations.append(
            _Observation(
                metric=metric,
                duration=duration,
                fiscal_year=fiscal_year,
                through_quarter=quarter,
                start=start,
                end=end,
                value=value,
                concept=concept,
                accession_no=accession,
                form=fact.form,
                filed=fact.filed,
                derivation=note,
            )
        )
    return observations


@dataclass(frozen=True, slots=True)
class _Cumulative:
    value: Decimal
    end: date
    label: str


def _derive_quarters(
    observations: list[_Observation], specs: Mapping[str, MetricSpec]
) -> list[FinancialValue]:
    quarters: dict[tuple[str, int, int], list[_Observation]] = defaultdict(list)
    cumulative: dict[tuple[str, int, int], list[_Observation]] = defaultdict(list)
    reported_quarters: set[tuple[str, str, int, int]] = set()
    for obs in observations:
        key = (obs.metric, obs.fiscal_year, obs.through_quarter)
        if obs.duration is Duration.QUARTER:
            quarters[key].append(obs)
            reported_quarters.add(
                (obs.metric, obs.accession_no, obs.fiscal_year, obs.through_quarter)
            )
        else:
            cumulative[key].append(obs)

    def latest(candidates: list[_Observation], as_of: date, accession: str) -> _Observation | None:
        same_filing = [c for c in candidates if c.accession_no == accession]
        if same_filing:
            return same_filing[0]
        known = [c for c in candidates if c.filed <= as_of]
        return max(known, key=lambda c: c.filed) if known else None

    def year_to_date(
        metric: str, fiscal_year: int, through: int, as_of: date, accession: str
    ) -> _Cumulative | None:
        if through == 1:
            first = latest(quarters[(metric, fiscal_year, 1)], as_of, accession)
            return None if first is None else _Cumulative(first.value, first.end, "Q1")
        total = latest(cumulative[(metric, fiscal_year, through)], as_of, accession)
        if total is not None:
            return _Cumulative(total.value, total.end, f"{total.duration} YTD")
        earlier = year_to_date(metric, fiscal_year, through - 1, as_of, accession)
        quarter = latest(quarters[(metric, fiscal_year, through)], as_of, accession)
        if earlier is None or quarter is None:
            return None
        return _Cumulative(
            earlier.value + quarter.value, quarter.end, f"{earlier.label} + Q{through}"
        )

    derived = []
    for obs in observations:
        if obs.duration is Duration.QUARTER or not specs[obs.metric].additive:
            continue
        quarter = obs.through_quarter
        if (obs.metric, obs.accession_no, obs.fiscal_year, quarter) in reported_quarters:
            continue  # the filing reports this quarter directly
        before = year_to_date(obs.metric, obs.fiscal_year, quarter - 1, obs.filed, obs.accession_no)
        if before is None:
            continue
        start = before.end + timedelta(days=1)
        if classify_duration(start, obs.end) is not Duration.QUARTER:
            continue
        derived.append(
            FinancialValue(
                metric=obs.metric,
                period_type="quarter",
                fiscal_year=obs.fiscal_year,
                fiscal_quarter=quarter,
                period_start=start,
                period_end=obs.end,
                value=obs.value - before.value,
                unit=specs[obs.metric].unit,
                source_concept=obs.concept,
                accession_no=obs.accession_no,
                form=obs.form,
                filed=obs.filed,
                derived=True,
                derivation=f"{obs.duration} YTD - {before.label}",
            )
        )
    return derived


def _to_value(
    obs: _Observation,
    specs: Mapping[str, MetricSpec],
    period_type: PeriodType,
    fiscal_quarter: int | None,
    *,
    derived: bool,
) -> FinancialValue:
    return FinancialValue(
        metric=obs.metric,
        period_type=period_type,
        fiscal_year=obs.fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_start=obs.start,
        period_end=obs.end,
        value=obs.value,
        unit=specs[obs.metric].unit,
        source_concept=obs.concept,
        accession_no=obs.accession_no,
        form=obs.form,
        filed=obs.filed,
        derived=derived or obs.derivation is not None,
        derivation=obs.derivation,
    )


def _keep(
    results: dict[tuple[str, date, date, str], FinancialValue], value: FinancialValue
) -> None:
    key = (value.metric, value.period_start, value.period_end, value.accession_no)
    current = results.get(key)
    if current is None or (current.derived and not value.derived):
        results[key] = value
