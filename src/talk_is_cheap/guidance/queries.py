"""Read stored guidance: one company's statements with their verdicts and sources."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Guidance, GuidanceSource


@dataclass(frozen=True, slots=True)
class GuidanceRow:
    guidance: Guidance
    sources: list[GuidanceSource]


async def company_guidance(
    session: AsyncSession,
    cik: int | None,
    metric: str | None = None,
    *,
    flagged_only: bool = False,
) -> list[GuidanceRow]:
    """A company's guidance (every company's when `cik` is None), newest issue first;
    within an issue, by target period and metric. `flagged_only`: rows flagged by the
    plausibility check, reviewed or not."""
    statement = select(Guidance)
    if cik is not None:
        statement = statement.where(Guidance.cik == cik)
    if metric:
        statement = statement.where(Guidance.metric == metric)
    if flagged_only:
        statement = statement.where(Guidance.plausibility == "flagged")
    statement = statement.order_by(
        Guidance.cik,
        Guidance.issued_at.desc(),
        Guidance.target_fiscal_year,
        Guidance.target_fiscal_quarter.nulls_last(),
        Guidance.metric,
        Guidance.basis,
        Guidance.id,
    )
    rows = (await session.scalars(statement)).all()
    sources: dict[int, list[GuidanceSource]] = {row.id: [] for row in rows}
    if rows:
        found = await session.scalars(
            select(GuidanceSource)
            .where(GuidanceSource.guidance_id.in_(sources))
            .order_by(GuidanceSource.guidance_id, GuidanceSource.position)
        )
        for source in found:
            sources[source.guidance_id].append(source)
    return [GuidanceRow(row, sources[row.id]) for row in rows]
