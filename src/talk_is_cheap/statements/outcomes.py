"""Management claims compared with what the company reported, and with its formal
guidance. Rebuilt from `statements` at no cost, keeping review decisions.

Only management claims are scored (a verified executive, a company spokesperson or a
company statement); other claims are stored without an outcome.

- `numeric_forecast`: the guidance rules (`guidance/matching.py: judge`), with the same
  tolerances, basis and comparability rules and the plausibility check: `beat`, `met`,
  `missed`, `pending`, `not_comparable`.
- `directional`: the target period's reported value against the period before it (the
  quarter before a quarter, the year before a year), as first reported: `confirmed`,
  `contradicted`, `pending` or `not_comparable`. A change within the point tolerance
  (1% of an amount, 0.5 points of a rate) is flat.
- `plan` and `qualitative`: `not_checkable`. A plan gets the most relevant passage of the
  company's next 10-Q or 10-K as context (`find_context`), not as a verdict.

Then each numeric or directional claim is compared with formal guidance for the same
metric, measure, segment and target period issued within `GUIDANCE_WINDOW` of the claim:
`consistent`, `differs` (outside the guided range, or the opposite direction) or
`no_formal_guidance`, with the guidance row's id.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import (
    Company,
    Guidance,
    Statement,
    StatementOutcome,
    StatementReview,
    StockSplit,
)
from talk_is_cheap.guidance.matching import (
    POINT_TOLERANCE_POINTS,
    POINT_TOLERANCE_SHARE,
    Actuals,
    Split,
    check_plausibility,
    counts_in_verdicts,
    judge,
    load_actuals,
    reported_level,
)

GUIDANCE_WINDOW = timedelta(days=45)


@dataclass(frozen=True, slots=True)
class Context:
    """A filing passage shown with a plan."""

    url: str
    label: str  # "10-Q FY2025Q2 · mdna"
    text: str


# Finds the context of a plan: (the session, the statement) -> a filing passage or None.
FindContext = Callable[[AsyncSession, Statement], Awaitable[Context | None]]


@dataclass(frozen=True, slots=True)
class StatementForecast:
    """A numeric claim in the shape `matching.judge` reads (`matching.Forecast`)."""

    issued_at: date
    metric: str
    basis: str
    segment: str | None
    measure: str
    constant_currency: bool
    target_fiscal_year: int | None
    target_fiscal_quarter: int | None
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    unit: str

    @classmethod
    def of(cls, row: Statement) -> "StatementForecast":
        return cls(
            issued_at=row.statement_date,
            metric=row.metric or "other",
            basis=row.basis,
            segment=row.segment,
            measure=row.measure,
            constant_currency=row.constant_currency,
            target_fiscal_year=row.target_fiscal_year,
            target_fiscal_quarter=row.target_fiscal_quarter,
            low=row.low,
            high=row.high,
            point=row.point,
            unit=row.unit,
        )


def same_claim_key(row: Statement) -> tuple[object, ...]:
    """What makes two management claims the same forecast, whichever outlet reported it:
    one numeric or directional claim about one metric, period and figure is counted once
    in hit rates, however many stories repeat it."""
    return (
        row.ticker,
        row.claim_type,
        row.metric,
        row.measure,
        row.target_fiscal_year,
        row.target_fiscal_quarter,
        row.low,
        row.high,
        row.point,
        row.direction,
    )


def counts_as(outcome: StatementOutcome) -> str:
    """How an outcome is counted: its verdict, or `needs_review` / `wrong`."""
    if outcome.review == "wrong":
        return "wrong"
    if outcome.plausibility == "flagged" and outcome.review is None:
        return "needs_review"
    return outcome.verdict


async def rebuild_outcomes(
    session: AsyncSession, ticker: str, find_context: FindContext | None = None
) -> dict[str, int]:
    """Replace the outcomes of every management claim of a company. Returns counts by
    `counts_as`. Without `find_context`, plans keep the context they had."""
    company = (await session.scalars(select(Company).where(Company.ticker == ticker))).first()
    statements = (
        await session.scalars(
            select(Statement).where(Statement.ticker == ticker, Statement.management)
        )
    ).all()
    # Without a way to search filings (a review decision), plans keep their context.
    kept = {
        o.statement_id: Context(o.context_url, o.context_label or "", o.context_text or "")
        for o in (
            await session.scalars(
                select(StatementOutcome).where(
                    StatementOutcome.statement_id.in_([s.id for s in statements]),
                    StatementOutcome.context_url.is_not(None),
                )
            )
        ).all()
        if o.context_url
    }
    await session.execute(
        delete(StatementOutcome).where(
            StatementOutcome.statement_id.in_(
                select(Statement.id).where(Statement.ticker == ticker)
            )
        )
    )
    if company is None:
        return {}
    actuals = await load_actuals(session, company.cik)
    splits = [
        Split(s.effective_date, s.ratio)
        for s in (
            await session.scalars(select(StockSplit).where(StockSplit.cik == company.cik))
        ).all()
    ]
    guidance = [
        g
        for g in (await session.scalars(select(Guidance).where(Guidance.cik == company.cik))).all()
        if g.review != "wrong"
    ]
    decisions = {
        (r.article_id, r.item_key): r.decision
        for r in (
            await session.scalars(select(StatementReview).where(StatementReview.ticker == ticker))
        ).all()
    }
    counts: dict[str, int] = defaultdict(int)
    for row in statements:
        outcome = StatementOutcome(statement_id=row.id)
        if row.claim_type == "numeric_forecast":
            _numeric(outcome, row, actuals, splits, ticker)
        elif row.claim_type == "directional":
            _directional(outcome, row, actuals, ticker)
        else:
            outcome.verdict = "not_checkable"
            outcome.verdict_reason = f"{row.claim_type} claim: no reported figure to compare"
            if row.claim_type == "plan":
                context = await find_context(session, row) if find_context else kept.get(row.id)
                if context is not None:
                    outcome.context_url = context.url
                    outcome.context_label = context.label
                    outcome.context_text = context.text
        _compare_with_guidance(outcome, row, guidance, actuals, ticker)
        outcome.review = decisions.get((row.article_id, row.item_key))
        session.add(outcome)
        counts[counts_as(outcome)] += 1
    await session.flush()
    return dict(counts)


def _numeric(
    outcome: StatementOutcome,
    row: Statement,
    actuals: Actuals,
    splits: Sequence[Split],
    ticker: str,
) -> None:
    forecast = StatementForecast.of(row)
    verdict = judge(forecast, actuals, splits, ticker)
    outcome.verdict = verdict.verdict
    outcome.verdict_reason = verdict.reason
    outcome.actual = verdict.actual
    outcome.difference = verdict.difference
    outcome.percent_difference = verdict.percent_difference
    outcome.actual_accession_no = verdict.actual_accession_no
    outcome.actual_filed = verdict.actual_filed
    if verdict.verdict != "not_comparable":
        plausibility = check_plausibility(forecast, actuals, ticker)
        outcome.plausibility = plausibility.status
        outcome.plausibility_rule = plausibility.rule
        outcome.plausibility_note = plausibility.note


def _directional(outcome: StatementOutcome, row: Statement, actuals: Actuals, ticker: str) -> None:
    reason = _directional_not_comparable(row)
    if reason is not None:
        outcome.verdict, outcome.verdict_reason = "not_comparable", reason
        return
    assert row.metric is not None and row.target_fiscal_year is not None
    period = (row.target_fiscal_year, row.target_fiscal_quarter)
    prior = previous_period(period)
    current = reported_level(row.metric, actuals, period, ticker)
    if current is None:
        if actuals.period_reported(period):
            outcome.verdict = "not_comparable"
            outcome.verdict_reason = f"no reported {row.metric} for the period"
        else:
            outcome.verdict, outcome.verdict_reason = "pending", "target period not reported yet"
        return
    before = reported_level(row.metric, actuals, prior, ticker)
    if before is None:
        outcome.verdict = "not_comparable"
        outcome.verdict_reason = "no reported value for the period before the target"
        return
    observed = observed_direction(before[0], current[0], row.unit)
    outcome.verdict = "confirmed" if observed == row.direction else "contradicted"
    outcome.verdict_reason = f"reported change: {observed}"
    outcome.actual, outcome.prior_actual = current[0], before[0]
    outcome.difference = current[0] - before[0]
    if before[0] != 0:
        outcome.percent_difference = (outcome.difference / abs(before[0]) * 100).quantize(
            Decimal("0.0001")
        )
    outcome.actual_accession_no, outcome.actual_filed = current[1], current[2]


def _directional_not_comparable(row: Statement) -> str | None:
    if row.metric in (None, "other", "tax_rate"):
        return "metric not tracked in reported financials"
    if row.direction is None:
        return "no direction stated"
    if row.basis == "non_gaap":
        return "non-GAAP measure; reported values are GAAP"
    if row.segment:
        return "segment claim; reported values are company totals"
    if row.measure != "level":
        return "direction of a growth rate or share is not compared"
    if row.target_fiscal_year is None:
        return "target period not resolved"
    return None


def previous_period(period: tuple[int, int | None]) -> tuple[int, int | None]:
    """The quarter before a quarter, the year before a year."""
    year, quarter = period
    if quarter is None:
        return year - 1, None
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def observed_direction(before: Decimal, after: Decimal, unit: str) -> str:
    """increase, decrease or flat: a change within the point tolerance is flat."""
    tolerance = POINT_TOLERANCE_POINTS if unit == "pct" else abs(before) * POINT_TOLERANCE_SHARE
    if after - before > tolerance:
        return "increase"
    if before - after > tolerance:
        return "decrease"
    return "flat"


def _compare_with_guidance(
    outcome: StatementOutcome,
    row: Statement,
    guidance: Sequence[Guidance],
    actuals: Actuals,
    ticker: str,
) -> None:
    outcome.guidance_comparison = "no_formal_guidance"
    if row.claim_type not in ("numeric_forecast", "directional") or row.metric is None:
        outcome.guidance_note = "not a figure or direction for a metric"
        return
    if row.target_fiscal_year is None:
        outcome.guidance_note = "target period not resolved"
        return
    match = closest_guidance(row, guidance)
    if match is None:
        outcome.guidance_note = (
            f"no guidance for {row.metric} {_label(row)} issued within {GUIDANCE_WINDOW.days} days"
        )
        return
    outcome.guidance_id = match.id
    issued = f"guidance issued {match.issued_at.isoformat()}"
    if row.claim_type == "numeric_forecast":
        claimed = _reference(row.low, row.high, row.point)
        low, high = _guided_range(match)
        if claimed is None or low is None or high is None:
            outcome.guidance_note = f"{issued}: no figure to compare"
            return
        inside = low <= claimed <= high
        outcome.guidance_comparison = "consistent" if inside else "differs"
        outcome.guidance_note = f"{issued}: {low:f} to {high:f} {match.unit}; claim {claimed:f}"
        return
    guided = _guided_direction(match, actuals, ticker)
    if guided is None:
        outcome.guidance_note = f"{issued}: the direction it implies could not be determined"
        return
    outcome.guidance_comparison = "consistent" if guided == row.direction else "differs"
    outcome.guidance_note = f"{issued} implies {guided}; claim says {row.direction}"


def closest_guidance(row: Statement, guidance: Sequence[Guidance]) -> Guidance | None:
    """The guidance row for the claim's metric, measure, segment and target period issued
    closest to the claim, within `GUIDANCE_WINDOW`; a stated basis must agree. Rows that
    need review or were reviewed as wrong are left out."""
    found = [
        g
        for g in guidance
        if g.metric == row.metric
        and _same_measure(row, g)
        and g.target_fiscal_year == row.target_fiscal_year
        and g.target_fiscal_quarter == row.target_fiscal_quarter
        and _fold(g.segment) == _fold(row.segment)
        and (row.basis == "unspecified" or g.basis in (row.basis, "unspecified"))
        and abs(g.issued_at - row.statement_date) <= GUIDANCE_WINDOW
        and counts_in_verdicts(g)
    ]
    if not found:
        return None
    return min(
        found,
        key=lambda g: (
            abs(g.issued_at - row.statement_date),
            g.issued_at > row.statement_date,
            g.basis != "gaap",
            g.id,
        ),
    )


def _same_measure(row: Statement, guidance: Guidance) -> bool:
    """A numeric claim needs guidance in its own measure. A directional claim (about the
    period before the target) also reads growth against that period: sequential growth
    for a quarter, year-over-year growth for a year."""
    if guidance.measure == row.measure:
        return True
    if row.claim_type != "directional" or row.measure != "level":
        return False
    growth = "yoy_growth" if row.target_fiscal_quarter is None else "sequential_growth"
    return guidance.measure == growth


def _guided_range(row: Guidance) -> tuple[Decimal | None, Decimal | None]:
    if row.low is not None and row.high is not None:
        return row.low, row.high
    if row.point is not None:
        tolerance = (
            POINT_TOLERANCE_POINTS if row.unit == "pct" else abs(row.point) * POINT_TOLERANCE_SHARE
        )
        return row.point - tolerance, row.point + tolerance
    if row.low is not None:
        return row.low, Decimal("Infinity")
    if row.high is not None:
        return Decimal("-Infinity"), row.high
    return None, None


def _guided_direction(row: Guidance, actuals: Actuals, ticker: str) -> str | None:
    reference = _reference(row.low, row.high, row.point)
    if reference is None or row.target_fiscal_year is None:
        return None
    if row.measure in ("yoy_growth", "sequential_growth"):
        return observed_direction(Decimal(0), reference, "pct")
    period = (row.target_fiscal_year, row.target_fiscal_quarter)
    before = reported_level(row.metric, actuals, previous_period(period), ticker)
    if before is None:
        return None
    return observed_direction(before[0], reference, row.unit)


def _reference(low: Decimal | None, high: Decimal | None, point: Decimal | None) -> Decimal | None:
    if point is not None:
        return point
    if low is not None and high is not None:
        return (low + high) / 2
    return low if low is not None else high


def _label(row: Statement) -> str:
    if row.target_fiscal_quarter is None:
        return f"FY{row.target_fiscal_year}"
    return f"FY{row.target_fiscal_year}Q{row.target_fiscal_quarter}"


def _fold(text: str | None) -> str | None:
    return None if text is None else " ".join(text.casefold().split())
