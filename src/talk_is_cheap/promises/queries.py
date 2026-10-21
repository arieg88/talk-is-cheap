"""Read promises with their verdict, review and article."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Article, Promise, PromiseReview, PromiseVerdict
from talk_is_cheap.news.queries import ARTICLE_LINK, ARTICLE_PUBLISHER
from talk_is_cheap.statements.roster import normalize_name

# Verdicts that settle a claim; the others leave it open or undecided.
RESOLVED = ("delivered", "delivered_late", "partly_delivered", "not_delivered", "walked_back")
OPEN = ("too_early",)


@dataclass(frozen=True, slots=True)
class PromiseRow:
    promise: Promise
    verdict: PromiseVerdict | None
    review: PromiseReview | None
    publisher: str | None
    url: str

    @property
    def effective(self) -> str | None:
        """The verdict that counts: a review's, else the resolver's, else None (not
        resolved yet)."""
        if self.review is not None:
            return self.review.verdict
        return self.verdict.verdict if self.verdict is not None else None

    @property
    def is_open(self) -> bool:
        return self.effective is None or self.effective in OPEN


async def promise_rows(
    session: AsyncSession,
    ticker: str | None,
    *,
    verdicts: Sequence[str] | None = None,
    open_only: bool = False,
    bold_only: bool = False,
    speaker: str | None = None,
    due_before: date | None = None,
    due_after: date | None = None,
    specificity: Sequence[str] | None = None,
) -> list[PromiseRow]:
    """Current promises, newest statement first (every company's when `ticker` is
    None)."""
    statement = (
        select(Promise, PromiseVerdict, PromiseReview, ARTICLE_PUBLISHER, ARTICLE_LINK)
        .join(Article, Article.id == Promise.article_id)
        .outerjoin(PromiseVerdict, PromiseVerdict.promise_id == Promise.id)
        .outerjoin(PromiseReview, PromiseReview.claim_key == Promise.claim_key)
        .where(Promise.current)
        .order_by(Promise.statement_date.desc(), Promise.id)
    )
    if ticker:
        statement = statement.where(Promise.ticker == ticker.upper())
    if bold_only:
        statement = statement.where(Promise.bold)
    if due_before is not None:
        statement = statement.where(Promise.due_date <= due_before)
    if due_after is not None:
        statement = statement.where(Promise.due_date >= due_after)
    if specificity:
        statement = statement.where(Promise.specificity.in_(list(specificity)))
    wanted = normalize_name(speaker) if speaker else None
    rows = []
    for promise, verdict, review, publisher, url in (await session.execute(statement)).all():
        row = PromiseRow(promise, verdict, review, publisher, url)
        if verdicts and row.effective not in verdicts:
            continue
        if open_only and not row.is_open:
            continue
        if wanted and not _spoken_by(promise.speaker, wanted):
            continue
        rows.append(row)
    return rows


async def promise_row(session: AsyncSession, promise_id: int) -> PromiseRow | None:
    found = (
        await session.execute(
            select(Promise, PromiseVerdict, PromiseReview, ARTICLE_PUBLISHER, ARTICLE_LINK)
            .join(Article, Article.id == Promise.article_id)
            .outerjoin(PromiseVerdict, PromiseVerdict.promise_id == Promise.id)
            .outerjoin(PromiseReview, PromiseReview.claim_key == Promise.claim_key)
            .where(Promise.id == promise_id)
        )
    ).first()
    return None if found is None else PromiseRow(*found)


def _spoken_by(name: str, wanted: str) -> bool:
    known = normalize_name(name)
    return wanted == known or wanted in known.split() or known.endswith(" " + wanted)
