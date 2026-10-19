"""Review decisions on guidance rows: `ok` clears a plausibility flag, `wrong` leaves
the row out of verdicts. Decisions are keyed by filing and item key, so they survive
rebuilding the filing's rows."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Company, Guidance, GuidanceReview
from talk_is_cheap.guidance.matching import item_key, refresh_verdicts


async def review(session: AsyncSession, row_id: int, decision: str) -> str:
    """Record a decision on a guidance row and refresh the company's verdicts. Returns a
    line describing what was decided. Raises LookupError for an unknown row."""
    row = await session.get(Guidance, row_id)
    if row is None:
        raise LookupError(f"no guidance row {row_id}")
    key = row.item_key or item_key(row)
    ticker = await session.scalar(select(Company.ticker).where(Company.cik == row.cik))
    target = f"FY{row.target_fiscal_year}" + (
        f"Q{row.target_fiscal_quarter}" if row.target_fiscal_quarter else ""
    )
    summary = (
        f"{ticker} {row.metric} ({row.basis}, {row.measure}) for {target}, issued "
        f"FY{row.fiscal_year}Q{row.fiscal_quarter}: {row.value_text or row.qualitative}"
        + (f"; flagged: {row.plausibility_note}" if row.plausibility == "flagged" else "")
    )
    statement = insert(GuidanceReview).values(
        cik=row.cik,
        accession_no=row.accession_no,
        item_key=key,
        decision=decision,
        summary=summary,
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_guidance_reviews_item",
            set_={
                "decision": decision,
                "summary": summary,
                "decided_at": statement.excluded.decided_at,
            },
        )
    )
    await refresh_verdicts(session, row.cik)
    effect = "flag cleared" if decision == "ok" else "left out of verdicts"
    return f"row {row_id} reviewed as {decision} ({effect}): {summary}"
