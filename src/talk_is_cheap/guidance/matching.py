"""Verdicts: each guidance statement against what the company later reported for the
target period, as first reported.

A verdict compares like with like or says why it cannot:

- Actuals are XBRL values, which are GAAP and company-wide, so non-GAAP, segment and
  constant-currency guidance is not comparable. Guidance without a GAAP or non-GAAP label
  (most revenue and capital expenditure guidance) is compared with the GAAP actual and
  flagged `basis_assumed`: SEC rules require non-GAAP measures to be identified as such.
- Values come from the earliest filing that reported the period (vintage "first" in
  `financial_values`), never later restatements. Margins are computed from first-reported
  gross profit, operating income and revenue, and compared in percentage points.
- Growth is computed from the first-reported values of both periods.
- Per-share guidance issued before a stock split and reported after it is not compared.

beat means above the guided range (or above a point by more than its tolerance), missed
below it, met within it. That is a statement about direction only: for expenses and
capital expenditures, "beat" means spending more than guided. The signed difference is
stored either way.

A guided value implausible against the company's own result for the period before the
target (`PLAUSIBLE_*`) is flagged for review: more likely an extraction error than
guidance. Flagged rows keep their verdict but are left out of verdict counts until
reviewed (`talk-is-cheap guidance review`).
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files
from typing import Protocol

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import (
    Company,
    FinancialValue,
    Guidance,
    GuidanceReview,
    StockSplit,
)

# A point estimate is met when the actual is within this many percentage points (rates)...
POINT_TOLERANCE_POINTS = Decimal("0.5")
# ...or within this share of the point (amounts and per-share values)...
POINT_TOLERANCE_SHARE = Decimal("0.01")
# ...and never less than half a cent per share, the rounding of reported EPS.
POINT_TOLERANCE_PER_SHARE = Decimal("0.005")

# A guided value far from the company's own result for the period before the target is
# more likely an extraction error than guidance (an exchange-rate "headwind of $200
# million" read as revenue). Such rows keep their verdict and are flagged for review.
# Amounts: the guided value over the prior period's, outside these bounds. Capital
# expenditures and operating income move more than revenue and expenses (AI build-outs
# more than doubled capex in a year; operating income swings with one-time items).
PLAUSIBLE_RATIO: Mapping[str, tuple[Decimal, Decimal]] = {
    "revenue": (Decimal("0.5"), Decimal("2.0")),
    "operating_expenses": (Decimal("0.5"), Decimal("2.0")),
    "capex": (Decimal(1) / 3, Decimal("3.0")),
    "operating_income": (Decimal(1) / 3, Decimal("3.0")),
    "eps": (Decimal("0.25"), Decimal("4.0")),
}
# A ratio means nothing against a base that is zero, negative (Intel's operating losses)
# or tiny; such rows are recorded as skipped.
PLAUSIBLE_MIN_BASE: Mapping[str, Decimal] = {
    "usd": Decimal(10_000_000),
    "usd_per_share": Decimal("0.10"),
}
# Rates, in percentage points from the prior period's: margins, growth (against the
# prior period's growth) and shares of revenue.
PLAUSIBLE_POINTS: Mapping[str, Decimal] = {
    "margin_points": Decimal(15),
    "growth_points": Decimal(30),
    "share_points": Decimal(10),
}

# Reported metrics that make up a guided one, beyond the metric of the same name. Meta
# guides "capital expenditures, including principal payments on finance leases".
ACTUAL_ADDITIONS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("META", "capex"): ("finance_lease_principal_payments",),
}

# The reported metric behind each guided amount.
_LEVEL_METRICS = {
    "revenue": "revenue",
    "operating_income": "operating_income",
    "operating_expenses": "operating_expenses",
    "capex": "capex",
    "eps": "eps_diluted",
}
_RATIO_METRICS = {"gross_margin": "gross_profit", "operating_margin": "operating_income"}
_EXPECTED_UNIT = {"eps": "usd_per_share", "gross_margin": "pct", "operating_margin": "pct"}


@dataclass(frozen=True, slots=True)
class Reported:
    value: Decimal
    accession_no: str
    filed: date


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str
    reason: str | None = None
    actual: Decimal | None = None
    difference: Decimal | None = None
    percent_difference: Decimal | None = None
    actual_accession_no: str | None = None
    actual_filed: date | None = None


@dataclass(frozen=True, slots=True)
class Split:
    effective_date: date
    ratio: Decimal


@dataclass(frozen=True, slots=True)
class ConfiguredSplit:
    ticker: str
    effective_date: date
    ratio: Decimal
    source: str


Period = tuple[int, int | None]  # (fiscal year, fiscal quarter or None for the year)


class Forecast(Protocol):
    """A guided figure for a fiscal period: a guidance row, or an executive's numeric
    forecast reported in the news (`statements/outcomes.py`), judged by the same rules."""

    @property
    def issued_at(self) -> date: ...
    @property
    def metric(self) -> str: ...
    @property
    def basis(self) -> str: ...
    @property
    def segment(self) -> str | None: ...
    @property
    def measure(self) -> str: ...
    @property
    def constant_currency(self) -> bool: ...
    @property
    def target_fiscal_year(self) -> int | None: ...
    @property
    def target_fiscal_quarter(self) -> int | None: ...
    @property
    def low(self) -> Decimal | None: ...
    @property
    def high(self) -> Decimal | None: ...
    @property
    def point(self) -> Decimal | None: ...
    @property
    def unit(self) -> str: ...


class Actuals:
    """First-reported values of one company, by metric and period, and which periods the
    company has reported at all."""

    def __init__(self, values: Iterable[tuple[str, int, int | None, Reported]]) -> None:
        self._values: dict[tuple[str, int, int | None], Reported] = {}
        self._reported: set[Period] = set()
        for metric, year, quarter, reported in values:
            self._values.setdefault((metric, year, quarter), reported)
            self._reported.add((year, quarter))

    def get(self, metric: str, period: Period) -> Reported | None:
        return self._values.get((metric, *period))

    def period_reported(self, period: Period) -> bool:
        return period in self._reported

    def periods(self) -> list[Period]:
        """Every reported period, newest first, a year after its fourth quarter."""
        return sorted(self._reported, key=lambda p: (p[0], p[1] or 5), reverse=True)


def load_split_config() -> list[ConfiguredSplit]:
    text = files("talk_is_cheap.guidance").joinpath("splits.yaml").read_text(encoding="utf-8")
    return [
        ConfiguredSplit(
            ticker=str(entry["ticker"]).upper(),
            effective_date=entry["effective_date"],
            ratio=Decimal(str(entry["ratio"])),
            source=str(entry["source"]),
        )
        for entry in yaml.safe_load(text)["splits"]
    ]


async def sync_splits(session: AsyncSession, configured: Sequence[ConfiguredSplit]) -> int:
    """Make `stock_splits` hold the configured splits of loaded companies."""
    ciks = dict((await session.execute(select(Company.ticker, Company.cik))).all())
    existing = {
        (s.cik, s.effective_date): s for s in (await session.scalars(select(StockSplit))).all()
    }
    wanted = set()
    for split in configured:
        cik = ciks.get(split.ticker)
        if cik is None:
            continue
        wanted.add((cik, split.effective_date))
        row = existing.get((cik, split.effective_date))
        if row is None:
            session.add(
                StockSplit(
                    cik=cik,
                    effective_date=split.effective_date,
                    ratio=split.ratio,
                    source=split.source,
                )
            )
        else:
            row.ratio, row.source = split.ratio, split.source
    for key, row in existing.items():
        if key not in wanted:
            await session.delete(row)
    await session.flush()
    return len(wanted)


async def load_actuals(session: AsyncSession, cik: int) -> Actuals:
    """Every metric's value as first reported: the earliest filing, a directly reported
    value before a derived one from the same filing."""
    statement = (
        select(FinancialValue)
        .where(FinancialValue.cik == cik)
        .order_by(
            FinancialValue.metric,
            FinancialValue.fiscal_year,
            FinancialValue.fiscal_quarter,
            FinancialValue.period_type,
            FinancialValue.filed,
            FinancialValue.derived,
            FinancialValue.accession_no,
        )
    )
    rows = (await session.scalars(statement)).all()
    return Actuals(
        (
            fv.metric,
            fv.fiscal_year,
            fv.fiscal_quarter if fv.period_type == "quarter" else None,
            Reported(fv.value, fv.accession_no, fv.filed),
        )
        for fv in rows
    )


async def refresh_verdicts(session: AsyncSession, cik: int) -> dict[str, int]:
    """Recompute the verdict, plausibility flag and review decision of every guidance row
    of a company. Returns counts by verdict of the rows that count (`counts_in_verdicts`),
    and of rows that need review (`needs_review`) or were reviewed as wrong (`wrong`)."""
    ticker = await session.scalar(select(Company.ticker).where(Company.cik == cik))
    actuals = await load_actuals(session, cik)
    splits = [
        Split(s.effective_date, s.ratio)
        for s in (await session.scalars(select(StockSplit).where(StockSplit.cik == cik))).all()
    ]
    decisions = {
        (r.accession_no, r.item_key): r.decision
        for r in (
            await session.scalars(select(GuidanceReview).where(GuidanceReview.cik == cik))
        ).all()
    }
    counts: dict[str, int] = defaultdict(int)
    rows = (await session.scalars(select(Guidance).where(Guidance.cik == cik))).all()
    for row in rows:
        result = judge(row, actuals, splits, ticker or "")
        row.verdict = result.verdict
        row.verdict_reason = result.reason
        row.basis_assumed = row.basis == "unspecified" and result.verdict != "not_comparable"
        row.actual = result.actual
        row.difference = result.difference
        row.percent_difference = result.percent_difference
        row.actual_accession_no = result.actual_accession_no
        row.actual_filed = result.actual_filed
        row.matched_at = func.now()
        plausibility = (
            None
            if result.verdict == "not_comparable"
            else check_plausibility(row, actuals, ticker or "")
        )
        row.plausibility = plausibility.status if plausibility else None
        row.plausibility_rule = plausibility.rule if plausibility else None
        row.plausibility_note = plausibility.note if plausibility else None
        row.item_key = row.item_key or item_key(row)
        row.review = decisions.get((row.accession_no, row.item_key))
        counts[_count_as(row)] += 1
    await session.flush()
    return dict(counts)


def counts_in_verdicts(row: Guidance) -> bool:
    """Whether a row's verdict counts: not flagged for review (unless reviewed as ok)
    and not reviewed as wrong."""
    if row.review == "wrong":
        return False
    return row.plausibility != "flagged" or row.review == "ok"


def needs_review(row: Guidance) -> bool:
    return row.plausibility == "flagged" and row.review is None


def _count_as(row: Guidance) -> str:
    if row.review == "wrong":
        return "wrong"
    if needs_review(row):
        return "needs_review"
    return row.verdict


def item_key(row: Guidance) -> str:
    """A hash of what makes the statement: the same statement of the same filing has the
    same key whenever its rows are rebuilt (`items.GuidanceItem.key`, from the row)."""
    has_figure = any(v is not None for v in (row.low, row.high, row.point))
    parts = [
        row.metric,
        row.basis,
        " ".join(row.segment.casefold().split()) if row.segment else "",
        row.measure,
        str(row.constant_currency),
        str(row.target_fiscal_year),
        str(row.target_fiscal_quarter),
        *(f"{v.normalize():f}" if v is not None else "" for v in (row.low, row.high, row.point)),
        row.unit,
        ""
        if has_figure
        else " ".join((row.qualitative or row.value_text or "").casefold().split()),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def judge(row: Forecast, actuals: Actuals, splits: Sequence[Split], ticker: str) -> Verdict:
    """The verdict of one guidance row or forecast."""
    reason = _not_comparable(row)
    if reason is not None:
        return Verdict("not_comparable", reason)
    assert row.target_fiscal_year is not None
    period: Period = (row.target_fiscal_year, row.target_fiscal_quarter)

    found = _actual(row, actuals, period, ticker)
    if isinstance(found, str):
        return Verdict("not_comparable", found)
    if found is None:
        if actuals.period_reported(period):
            return Verdict("not_comparable", f"no reported {_reported_name(row)} for the period")
        return Verdict("pending", "target period not reported yet")
    actual, accession_no, filed = found

    if row.metric == "eps":
        for split in splits:
            if row.issued_at < split.effective_date <= filed:
                return Verdict(
                    "not_comparable",
                    f"per-share guidance issued before the {split.ratio.normalize():f}-for-1 "
                    f"stock split of {split.effective_date.isoformat()}",
                    actual=actual,
                    actual_accession_no=accession_no,
                    actual_filed=filed,
                )
    return _compare(row, actual, accession_no, filed)


@dataclass(frozen=True, slots=True)
class Plausibility:
    """Whether a guided value is plausible against the company's own result for the
    period before the target: "passed", "flagged" (needs review) or "skipped", which
    rule was applied, and why."""

    status: str
    rule: str
    note: str


def check_plausibility(row: Forecast, actuals: Actuals, ticker: str) -> Plausibility:
    """Compare a comparable row's guided value with the prior period's actual (the
    quarter before a quarterly target, the year before a full-year one), as first
    reported and in the same measure."""
    rule = _plausibility_rule(row)
    assert row.target_fiscal_year is not None
    year, quarter = row.target_fiscal_year, row.target_fiscal_quarter
    prior: Period = (
        (year - 1, None) if quarter is None else _base_period("sequential_growth", (year, quarter))
    )
    label = f"FY{prior[0]}" + (f"Q{prior[1]}" if prior[1] else "")
    found = _actual(row, actuals, prior, ticker)
    if not isinstance(found, tuple):
        return Plausibility("skipped", rule, f"no {label} value in the stored history")
    base, guided = found[0], _reference(row)
    if rule == "ratio":
        low, high = PLAUSIBLE_RATIO[row.metric]
        floor = PLAUSIBLE_MIN_BASE[row.unit]
        if base <= floor:
            return Plausibility(
                "skipped",
                rule,
                f"{label} base {_show(base, row.unit)} is zero, negative or below "
                f"{_show(floor, row.unit)}",
            )
        if guided <= 0:
            return Plausibility(
                "skipped", rule, f"guided {_show(guided, row.unit)} is zero or negative"
            )
        ratio = guided / base
        note = (
            f"guided {_show(guided, row.unit)} is {ratio:.0%} of {label}'s "
            f"{_show(base, row.unit)} (plausible: {low:.0%} to {high:.0%})"
        )
        return Plausibility("passed" if low <= ratio <= high else "flagged", rule, note)
    limit = PLAUSIBLE_POINTS[rule]
    note = (
        f"guided {_show(guided, row.unit)} is {guided - base:+.1f} pp from {label}'s "
        f"{_show(base, row.unit)} (plausible: within {limit} pp)"
    )
    return Plausibility("passed" if abs(guided - base) <= limit else "flagged", rule, note)


def _plausibility_rule(row: Forecast) -> str:
    if row.measure == "level":
        return "ratio" if row.metric in PLAUSIBLE_RATIO else "margin_points"
    if row.measure == "share_of_revenue":
        return "share_points"
    return "growth_points"


def _reference(row: Forecast) -> Decimal:
    """The guided value a row is measured by: its point, else the middle of its range,
    else its bound."""
    if row.point is not None:
        return row.point
    if row.low is not None and row.high is not None:
        return (row.low + row.high) / 2
    bound = row.low if row.low is not None else row.high
    assert bound is not None
    return bound


def _show(value: Decimal, unit: str) -> str:
    if unit == "pct":
        return f"{value:.1f}%"
    if unit == "usd_per_share":
        return f"${value:.2f}"
    if abs(value) >= 10**9:
        return f"${value / 10**9:,.2f}B"
    return f"${value / 10**6:,.1f}M"


def _not_comparable(row: Forecast) -> str | None:
    if row.low is None and row.high is None and row.point is None:
        return "qualitative guidance"
    if row.metric == "other":
        return "metric not tracked in reported financials"
    if row.metric == "tax_rate":
        return "tax rate is not compared"
    if row.basis == "non_gaap":
        return "non-GAAP guidance; reported values are GAAP"
    if row.segment:
        return "segment guidance; reported values are company totals"
    if row.constant_currency:
        return "constant-currency guidance; reported values are in US dollars"
    if row.target_fiscal_year is None:
        return "target period not resolved"
    if row.measure == "sequential_growth" and row.target_fiscal_quarter is None:
        return "sequential growth of a full year"
    expected = "pct" if row.measure != "level" else _EXPECTED_UNIT.get(row.metric, "usd")
    if row.unit != expected:
        return f"guided in {row.unit}, not {expected}"
    if row.measure in ("yoy_growth", "sequential_growth", "share_of_revenue") and (
        row.metric in _RATIO_METRICS
    ):
        return f"{row.measure.replace('_', ' ')} of a margin is not compared"
    return None


def _actual(
    row: Forecast, actuals: Actuals, period: Period, ticker: str
) -> tuple[Decimal, str, date] | str | None:
    """The reported value in the guidance's unit, with the filing that completed it; None
    when not reported yet; a reason when it cannot be computed."""
    current = reported_level(row.metric, actuals, period, ticker)
    if current is None:
        return None
    value, accession_no, filed = current
    if row.measure == "level":
        return value, accession_no, filed
    if row.measure == "share_of_revenue":
        revenue = actuals.get("revenue", period)
        if revenue is None:
            return None
        if revenue.value == 0:
            return "reported revenue is zero"
        latest = max((filed, accession_no), (revenue.filed, revenue.accession_no))
        return value / revenue.value * 100, latest[1], latest[0]
    base_period = _base_period(row.measure, period)
    base = reported_level(row.metric, actuals, base_period, ticker)
    if base is None:
        return "no reported value for the comparison period"
    if base[0] == 0:
        return "comparison period value is zero"
    return (value / base[0] - 1) * 100, accession_no, filed


def _base_period(measure: str, period: Period) -> Period:
    year, quarter = period
    if measure == "yoy_growth":
        return year - 1, quarter
    assert quarter is not None
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def reported_level(
    metric: str, actuals: Actuals, period: Period, ticker: str
) -> tuple[Decimal, str, date] | None:
    """A guided metric's reported value for a period, as first reported (margins in
    percent, Meta's capex with finance leases), and the filing that completed it."""
    if metric in _RATIO_METRICS:
        numerator = actuals.get(_RATIO_METRICS[metric], period)
        revenue = actuals.get("revenue", period)
        if numerator is None or revenue is None or revenue.value == 0:
            return None
        parts = [numerator, revenue]
        value = numerator.value / revenue.value * 100
    else:
        names = (_LEVEL_METRICS[metric], *ACTUAL_ADDITIONS.get((ticker, metric), ()))
        found = [actuals.get(name, period) for name in names]
        if any(f is None for f in found):
            return None
        parts = [f for f in found if f is not None]
        value = sum((p.value for p in parts), Decimal(0))
    last = max(parts, key=lambda p: (p.filed, p.accession_no))
    return value, last.accession_no, last.filed


def _compare(row: Forecast, actual: Decimal, accession_no: str, filed: date) -> Verdict:
    low, high, point = row.low, row.high, row.point
    if low is not None and high is not None:
        reference = point if point is not None else (low + high) / 2
        verdict = "beat" if actual > high else "missed" if actual < low else "met"
    elif point is not None:
        reference = point
        tolerance = _tolerance(row.unit, point)
        verdict = (
            "beat"
            if actual > point + tolerance
            else "missed"
            if actual < point - tolerance
            else "met"
        )
    elif low is not None:
        reference = low
        verdict = "met" if actual >= low else "missed"
    else:
        assert high is not None
        reference = high
        verdict = "beat" if actual > high else "met"
    difference = actual - reference
    percent = None if reference == 0 else difference / abs(reference) * 100
    return Verdict(
        verdict,
        None,
        actual=_round(actual),
        difference=_round(difference),
        percent_difference=None if percent is None else percent.quantize(Decimal("0.0001")),
        actual_accession_no=accession_no,
        actual_filed=filed,
    )


def _tolerance(unit: str, point: Decimal) -> Decimal:
    if unit == "pct":
        return POINT_TOLERANCE_POINTS
    tolerance = abs(point) * POINT_TOLERANCE_SHARE
    if unit == "usd_per_share":
        return max(tolerance, POINT_TOLERANCE_PER_SHARE)
    return tolerance


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _reported_name(row: Forecast) -> str:
    if row.metric in _RATIO_METRICS:
        return f"{_RATIO_METRICS[row.metric]} or revenue"
    return _LEVEL_METRICS[row.metric]
