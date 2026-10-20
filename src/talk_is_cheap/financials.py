"""Read normalized financials: quarterly values as first reported or latest, and restatements."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Company, Filing, FinancialValue

Vintage = Literal["first", "latest"]

# A change smaller than this share of the original value is treated as rounding.
RESTATEMENT_TOLERANCE = Decimal("0.0005")


@dataclass(frozen=True, slots=True)
class MetricValue:
    value: Decimal
    unit: str
    filed: date
    accession_no: str
    form: str
    derived: bool
    derivation: str | None


@dataclass(slots=True)
class QuarterFinancials:
    fiscal_year: int
    fiscal_quarter: int
    period_end: date
    metrics: dict[str, MetricValue] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"FY{self.fiscal_year} Q{self.fiscal_quarter}"

    def get(self, metric: str) -> Decimal | None:
        found = self.metrics.get(metric)
        return None if found is None else found.value

    def ratio(self, numerator: str, denominator: str) -> Decimal | None:
        top, bottom = self.get(numerator), self.get(denominator)
        if top is None or bottom is None or bottom == 0:
            return None
        return top / bottom

    @property
    def gross_margin(self) -> Decimal | None:
        return self.ratio("gross_profit", "revenue")

    @property
    def operating_margin(self) -> Decimal | None:
        return self.ratio("operating_income", "revenue")


@dataclass(frozen=True, slots=True)
class Restatement:
    metric: str
    fiscal_year: int
    fiscal_quarter: int
    first: MetricValue
    latest: MetricValue

    @property
    def change(self) -> Decimal | None:
        if self.first.value == 0:
            return None
        return (self.latest.value - self.first.value) / abs(self.first.value)


@dataclass(frozen=True, slots=True)
class CompanySummary:
    ticker: str
    cik: int
    name: str
    fiscal_year_end: str | None
    filings: int
    latest_quarter: str | None


async def find_company(session: AsyncSession, ticker: str) -> Company | None:
    return (
        await session.execute(select(Company).where(Company.ticker == ticker.upper()))
    ).scalar_one_or_none()


@dataclass(slots=True)
class YearFinancials:
    fiscal_year: int
    period_end: date
    metrics: dict[str, MetricValue] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"FY{self.fiscal_year}"


def _versions(
    cik: int, period_type: str, vintage: Vintage, metrics: Sequence[str] | None
) -> Select[FinancialValue]:
    """One version of each metric per period: the earliest filing that reported it
    ("first") or the most recent one ("latest"). A directly reported value is preferred
    over a derived one from the same filing."""
    filed_order = FinancialValue.filed.asc() if vintage == "first" else FinancialValue.filed.desc()
    statement = (
        select(FinancialValue)
        .where(FinancialValue.cik == cik, FinancialValue.period_type == period_type)
        .ext(
            distinct_on(
                FinancialValue.metric, FinancialValue.fiscal_year, FinancialValue.fiscal_quarter
            )
        )
        .order_by(
            FinancialValue.metric,
            FinancialValue.fiscal_year,
            FinancialValue.fiscal_quarter,
            filed_order,
            FinancialValue.derived.asc(),
            FinancialValue.accession_no,
        )
    )
    if metrics:
        statement = statement.where(FinancialValue.metric.in_(metrics))
    return statement


async def quarterly_financials(
    session: AsyncSession,
    cik: int,
    *,
    vintage: Vintage = "first",
    quarters: int | None = 8,
    metrics: Sequence[str] | None = None,
) -> list[QuarterFinancials]:
    """One row per fiscal quarter, newest first, each metric taken from one version:
    the earliest filing that reported it ("first") or the most recent one ("latest").
    A directly reported value is preferred over a derived one from the same filing."""
    rows: dict[tuple[int, int], QuarterFinancials] = {}
    for fv in (await session.execute(_versions(cik, "quarter", vintage, metrics))).scalars():
        assert fv.fiscal_quarter is not None
        key = (fv.fiscal_year, fv.fiscal_quarter)
        row = rows.setdefault(
            key, QuarterFinancials(fv.fiscal_year, fv.fiscal_quarter, fv.period_end)
        )
        row.period_end = max(row.period_end, fv.period_end)
        row.metrics[fv.metric] = _to_metric_value(fv)

    ordered = [rows[key] for key in sorted(rows, reverse=True)]
    return ordered if quarters is None else ordered[:quarters]


async def annual_financials(
    session: AsyncSession,
    cik: int,
    *,
    vintage: Vintage = "first",
    years: int | None = None,
    metrics: Sequence[str] | None = None,
) -> list[YearFinancials]:
    """`quarterly_financials` for fiscal years, newest first."""
    rows: dict[int, YearFinancials] = {}
    for fv in (await session.execute(_versions(cik, "year", vintage, metrics))).scalars():
        row = rows.setdefault(fv.fiscal_year, YearFinancials(fv.fiscal_year, fv.period_end))
        row.period_end = max(row.period_end, fv.period_end)
        row.metrics[fv.metric] = _to_metric_value(fv)
    ordered = [rows[key] for key in sorted(rows, reverse=True)]
    return ordered if years is None else ordered[:years]


async def restatements(
    session: AsyncSession, cik: int, *, tolerance: Decimal = RESTATEMENT_TOLERANCE
) -> list[Restatement]:
    """Quarterly values whose latest version differs from the first reported one."""
    first = await quarterly_financials(session, cik, vintage="first", quarters=None)
    latest = {
        (q.fiscal_year, q.fiscal_quarter): q
        for q in await quarterly_financials(session, cik, vintage="latest", quarters=None)
    }
    changes = []
    for quarter in first:
        newer = latest.get((quarter.fiscal_year, quarter.fiscal_quarter))
        if newer is None:
            continue
        for metric, original in quarter.metrics.items():
            current = newer.metrics.get(metric)
            if current is None or current.accession_no == original.accession_no:
                continue
            difference = abs(current.value - original.value)
            if difference > tolerance * max(abs(original.value), Decimal(1)):
                changes.append(
                    Restatement(
                        metric, quarter.fiscal_year, quarter.fiscal_quarter, original, current
                    )
                )
    return sorted(changes, key=lambda r: (r.fiscal_year, r.fiscal_quarter, r.metric), reverse=True)


async def company_summaries(session: AsyncSession) -> list[CompanySummary]:
    filing_counts = (
        select(Filing.cik, func.count().label("filings")).group_by(Filing.cik).subquery()
    )
    latest_period = (
        select(
            FinancialValue.cik,
            func.max(FinancialValue.fiscal_year * 10 + FinancialValue.fiscal_quarter).label("fq"),
        )
        .where(FinancialValue.period_type == "quarter")
        .group_by(FinancialValue.cik)
        .subquery()
    )
    statement = (
        select(Company, filing_counts.c.filings, latest_period.c.fq)
        .outerjoin(filing_counts, filing_counts.c.cik == Company.cik)
        .outerjoin(latest_period, latest_period.c.cik == Company.cik)
        .order_by(Company.ticker)
    )
    summaries = []
    for company, filings, fq in (await session.execute(statement)).all():
        summaries.append(
            CompanySummary(
                ticker=company.ticker,
                cik=company.cik,
                name=company.name,
                fiscal_year_end=company.fiscal_year_end,
                filings=filings or 0,
                latest_quarter=f"FY{fq // 10} Q{fq % 10}" if fq else None,
            )
        )
    return summaries


def _to_metric_value(fv: FinancialValue) -> MetricValue:
    return MetricValue(
        value=fv.value,
        unit=fv.unit,
        filed=fv.filed,
        accession_no=fv.accession_no,
        form=fv.form,
        derived=fv.derived,
        derivation=fv.derivation,
    )
