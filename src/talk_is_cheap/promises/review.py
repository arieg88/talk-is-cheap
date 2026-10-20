"""Review decisions on promises, as for guidance rows and statements: a reviewer's
verdict, keyed by the claim's hash (its article, company and statement item key), so it
survives re-extraction, re-framing and re-resolution. A reviewed verdict always wins."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import PROMISE_VERDICTS, Promise, PromiseReview


async def review(session: AsyncSession, promise_id: int, verdict: str, note: str | None) -> str:
    """Record a verdict on a promise. Returns a line describing it. Raises LookupError for
    an unknown promise and ValueError for an unknown verdict."""
    if verdict not in PROMISE_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(PROMISE_VERDICTS)}")
    row = await session.get(Promise, promise_id)
    if row is None:
        raise LookupError(f"no promise {promise_id}")
    summary = f"{row.ticker} {row.statement_date.isoformat()} {row.speaker}: {row.resolution_rule}"
    statement = insert(PromiseReview).values(
        claim_key=row.claim_key, ticker=row.ticker, verdict=verdict, note=note, summary=summary
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_promise_reviews_claim",
            set_={
                "verdict": verdict,
                "note": note,
                "summary": summary,
                "decided_at": statement.excluded.decided_at,
            },
        )
    )
    return f"promise {promise_id} reviewed as {verdict}: {summary}"
