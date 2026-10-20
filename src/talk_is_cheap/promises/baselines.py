"""What a target is measured from, looked up in stored data, never taken from the model's
memory.

A baseline the article states ("double from 410,831 vehicles") is used only when its
figure is written in the quote or its paragraph. Otherwise, for a claim about a reported
metric, the baseline is that metric's value, as first reported, for the latest period of
the claim's kind (a quarter, or a year) whose filing was public before the statement
date. Anything else has an `unknown` baseline: operating figures that are only in
filing text (deliveries, users, cities) have no stored series to look up.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from talk_is_cheap.guidance.matching import Actuals, reported_level

# Metrics with a reported series, and the unit their values are in.
METRIC_UNITS = {
    "revenue": "usd",
    "operating_income": "usd",
    "operating_expenses": "usd",
    "capex": "usd",
    "eps": "usd_per_share",
    "gross_margin": "pct",
    "operating_margin": "pct",
}


@dataclass(frozen=True, slots=True)
class Baseline:
    value: Decimal
    period: str  # "FY2026Q2", "FY2025"
    source: str  # article, actuals
    unit: str | None  # usd, pct, usd_per_share, a counted thing, or None when not stated
    accession_no: str | None = None


def reported_baseline(
    actuals: Actuals, metric: str, measure_period: str | None, said: date, ticker: str
) -> Baseline | None:
    """The metric's latest value, as first reported, filed before `said`: a quarter, or a
    fiscal year when `measure_period` is "year"."""
    if metric not in METRIC_UNITS:
        return None
    yearly = measure_period == "year"
    for period in actuals.periods():
        if (period[1] is None) != yearly:
            continue
        found = reported_level(metric, actuals, period, ticker)
        if found is None:
            continue
        value, accession_no, filed = found
        if filed < said:
            year, quarter = period
            label = f"FY{year}" if quarter is None else f"FY{year}Q{quarter}"
            return Baseline(value, label, "actuals", METRIC_UNITS[metric], accession_no)
    return None


def year_earlier_baseline(
    actuals: Actuals, metric: str, period: tuple[int, int | None], ticker: str
) -> Baseline | None:
    """The metric's value, as first reported, for the period a year before `period`: the
    base of a year-over-year change."""
    if metric not in METRIC_UNITS:
        return None
    year, quarter = period
    found = reported_level(metric, actuals, (year - 1, quarter), ticker)
    if found is None:
        return None
    label = f"FY{year - 1}" if quarter is None else f"FY{year - 1}Q{quarter}"
    return Baseline(found[0], label, "actuals", METRIC_UNITS[metric], found[1])


def metric_unit(metric: str | None) -> str | None:
    return METRIC_UNITS.get(metric) if metric else None
