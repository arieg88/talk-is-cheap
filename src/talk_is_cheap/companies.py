"""What is stored about each loaded company: its latest reported quarter, guidance rows,
linked news, management claims, and claims due to be judged."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import ArticleCompany, Guidance, Statement
from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.financials import company_summaries
from talk_is_cheap.news.queries import DEFAULT_MIN_CONFIDENCE


@dataclass(frozen=True, slots=True)
class CompanyCoverage:
    ticker: str
    cik: int
    name: str
    fiscal_year_end: str | None
    filings: int
    latest_quarter: str | None
    guidance_rows: int
    # Articles linked with a confidence of 0.5 or more, as `news` shows them.
    linked_articles: int
    management_claims: int
    # Promise groups whose due date and grace period have passed (`promises resolve`'s
    # own rule), judged or not.
    due_claims: int


async def company_coverage(
    session: AsyncSession,
    calendars: Mapping[str, FiscalCalendar],
    today: date,
    *,
    grace_days: int | None = None,
) -> list[CompanyCoverage]:
    """Every loaded company, by ticker. `grace_days`: PROMISE_GRACE_DAYS."""
    from talk_is_cheap.promises.resolve import plan_resolution

    summaries = await company_summaries(session)
    guidance = dict(
        (await session.execute(select(Guidance.cik, func.count()).group_by(Guidance.cik))).all()
    )
    linked = dict(
        (
            await session.execute(
                select(ArticleCompany.ticker, func.count(func.distinct(ArticleCompany.article_id)))
                .where(ArticleCompany.confidence >= DEFAULT_MIN_CONFIDENCE)
                .group_by(ArticleCompany.ticker)
            )
        ).all()
    )
    claims = dict(
        (
            await session.execute(
                select(Statement.ticker, func.count())
                .where(Statement.management)
                .group_by(Statement.ticker)
            )
        ).all()
    )
    plans = await plan_resolution(
        session, [s.ticker for s in summaries], calendars, today, grace_days=grace_days
    )
    due = {plan.ticker: plan.due for plan in plans}
    return [
        CompanyCoverage(
            ticker=s.ticker,
            cik=s.cik,
            name=s.name,
            fiscal_year_end=s.fiscal_year_end,
            filings=s.filings,
            latest_quarter=s.latest_quarter,
            guidance_rows=guidance.get(s.cik, 0),
            linked_articles=linked.get(s.ticker, 0),
            management_claims=claims.get(s.ticker, 0),
            due_claims=due.get(s.ticker, 0),
        )
        for s in summaries
    ]
