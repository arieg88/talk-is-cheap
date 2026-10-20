"""Read stored statements with their article, outcome and matching guidance row."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Article, Guidance, Statement, StatementOutcome
from talk_is_cheap.statements.roster import normalize_name

CHECKABLE = ("numeric_forecast", "directional")


@dataclass(frozen=True, slots=True)
class StatementRow:
    statement: Statement
    outcome: StatementOutcome | None  # None for claims not made by management
    publisher: str | None
    url: str
    published_at: datetime
    guidance: Guidance | None  # the guidance row the claim was compared with


def target_label(row: Statement | Guidance) -> str | None:
    if row.target_fiscal_year is None:
        return None
    if row.target_fiscal_quarter is None:
        return f"FY{row.target_fiscal_year}"
    return f"FY{row.target_fiscal_year}Q{row.target_fiscal_quarter}"


async def company_statements(
    session: AsyncSession,
    ticker: str | None,
    *,
    metric: str | None = None,
    period: str | None = None,
    speaker: str | None = None,
    management_only: bool = True,
    checkable_only: bool = False,
    claim_types: Sequence[str] | None = None,
) -> list[StatementRow]:
    """A company's statements (every company's when `ticker` is None), newest first.
    `period` is a target period ("FY2025", "FY2026Q3") or a calendar year of the
    statement date ("2025"); `speaker` matches the article's or the roster's name, or a
    surname."""
    statement = (
        select(Statement, StatementOutcome, Article.publisher, Article.url, Article.published_at)
        .join(Article, Article.id == Statement.article_id)
        .outerjoin(StatementOutcome, StatementOutcome.statement_id == Statement.id)
        .order_by(Statement.ticker, Statement.statement_date.desc(), Statement.id)
    )
    if ticker:
        statement = statement.where(Statement.ticker == ticker.upper())
    if metric:
        statement = statement.where(Statement.metric == metric)
    if management_only:
        statement = statement.where(Statement.management)
    if checkable_only:
        statement = statement.where(Statement.claim_type.in_(CHECKABLE))
    if claim_types:
        statement = statement.where(Statement.claim_type.in_(list(claim_types)))
    rows = (await session.execute(statement)).all()
    wanted_period = period.replace(" ", "").upper() if period else None
    wanted_speaker = normalize_name(speaker) if speaker else None
    selected = []
    for row, outcome, publisher, url, published_at in rows:
        if wanted_period and not _in_period(row, wanted_period):
            continue
        if wanted_speaker and not _spoken_by(row, wanted_speaker):
            continue
        selected.append((row, outcome, publisher, url, published_at))
    guidance_ids = {o.guidance_id for _, o, *_ in selected if o is not None and o.guidance_id}
    guidance = {}
    if guidance_ids:
        found = await session.scalars(select(Guidance).where(Guidance.id.in_(guidance_ids)))
        guidance = {g.id: g for g in found}
    return [
        StatementRow(
            row,
            outcome,
            publisher,
            url,
            published_at,
            guidance.get(outcome.guidance_id) if outcome and outcome.guidance_id else None,
        )
        for row, outcome, publisher, url, published_at in selected
    ]


def _in_period(row: Statement, wanted: str) -> bool:
    if wanted.isdigit():
        return row.statement_date.year == int(wanted) or row.target_fiscal_year == int(wanted)
    label = target_label(row)
    if label is None:
        return False
    return label == wanted or (len(wanted) == 6 and label.startswith(wanted))


def _spoken_by(row: Statement, wanted: str) -> bool:
    for name in (row.speaker_name, row.executive_name):
        if not name:
            continue
        known = normalize_name(name)
        if wanted == known or wanted in known.split() or known.endswith(" " + wanted):
            return True
    return False
