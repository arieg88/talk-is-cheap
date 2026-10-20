"""Review decisions on statements, as for guidance rows: `ok` clears a plausibility flag
(or confirms a claim), `wrong` leaves it out of outcome counts. Decisions are keyed by
article, company and item key, so they survive re-extraction."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Statement, StatementReview
from talk_is_cheap.statements.outcomes import rebuild_outcomes


async def review(session: AsyncSession, statement_id: int, decision: str) -> str:
    """Record a decision on a statement and rebuild the company's outcomes. Returns a line
    describing what was decided. Raises LookupError for an unknown statement."""
    row = await session.get(Statement, statement_id)
    if row is None:
        raise LookupError(f"no statement {statement_id}")
    speaker = row.executive_name or row.speaker_name or row.attribution.replace("_", " ")
    summary = f"{row.ticker} {row.statement_date.isoformat()} {speaker}: {row.claim}" + (
        f" ({row.value_text})" if row.value_text else ""
    )
    statement = insert(StatementReview).values(
        article_id=row.article_id,
        ticker=row.ticker,
        item_key=row.item_key,
        decision=decision,
        summary=summary,
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_statement_reviews_item",
            set_={
                "decision": decision,
                "summary": summary,
                "decided_at": statement.excluded.decided_at,
            },
        )
    )
    await rebuild_outcomes(session, row.ticker)
    effect = "counted" if decision == "ok" else "left out of outcome counts"
    return f"statement {statement_id} reviewed as {decision} ({effect}): {summary}"
