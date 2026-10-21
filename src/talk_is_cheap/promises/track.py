"""Track records per executive and per company.

Rules:

- Only `specific` and `partial` promises count; `vague` ones are listed separately and
  never scored. Scheduled actions (splits, dividends, record dates on a fixed date) and
  claims that restate formal guidance (scored with the guidance) are left out, from the
  counts and the bold-claim figures alike.
- Open-ended claims not shown delivered 18 months after the statement are
  `expired_unproven`: reported in a column of their own, never hidden and never counted
  as resolved.
- Each promise counts once, however many outlets reported it (its group), with the
  verdict that counts: a review's, else the resolver's.
- Numeric forecasts already scored against reported results (`statement_outcomes`)
  count too, once per forecast: beat or met is `delivered`, missed is `not_delivered`.
- Rates are over resolved claims (delivered, delivered late, partly delivered, not
  delivered, walked back). A group with fewer than MIN_RESOLVED of them is "too few to
  judge".
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Statement, StatementOutcome
from talk_is_cheap.promises.queries import RESOLVED, PromiseRow, promise_rows
from talk_is_cheap.statements.outcomes import same_claim_key

MIN_RESOLVED = 5
FORECAST_VERDICTS = {"beat": "delivered", "met": "delivered", "missed": "not_delivered"}
TOO_FEW = "too few to judge"


@dataclass(frozen=True, slots=True)
class Scored:
    """One claim as a track record counts it."""

    ticker: str
    speaker: str
    verdict: str | None  # None: not resolved yet
    bold: bool
    days_late: int | None
    source: str  # "promise" or "forecast"
    promise_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class TrackRecord:
    key: str  # an executive ("Jen-Hsun Huang (NVDA)"), a ticker, or "all"
    claims: int = 0  # scorable claims, resolved or not
    by_verdict: dict[str, int] = field(default_factory=dict)
    days_late: list[int] = field(default_factory=list)
    bold: int = 0
    bold_kept: int = 0
    vague: int = 0
    promise_ids: list[int] = field(default_factory=list)

    @property
    def expired(self) -> int:
        return self.by_verdict.get("expired_unproven", 0)

    @property
    def resolved(self) -> int:
        return sum(self.by_verdict.get(v, 0) for v in RESOLVED)

    @property
    def enough(self) -> bool:
        return self.resolved >= MIN_RESOLVED

    def rate(self, numerator: int, denominator: int | None = None) -> float | None:
        base = self.resolved if denominator is None else denominator
        if not self.enough or not base:
            return None
        return numerator / base

    @property
    def on_time_rate(self) -> float | None:
        return self.rate(self.by_verdict.get("delivered", 0))

    @property
    def walked_back_share(self) -> float | None:
        return self.rate(self.by_verdict.get("walked_back", 0))

    @property
    def bold_share(self) -> float | None:
        return self.rate(self.bold)

    @property
    def bold_hit_rate(self) -> float | None:
        return self.rate(self.bold_kept, self.bold) if self.bold else None

    @property
    def median_days_late(self) -> float | None:
        if not self.enough or not self.days_late:
            return None
        return float(statistics.median(self.days_late))


def speaker_key(ticker: str, speaker: str) -> str:
    if speaker in ("company statement", "company spokesperson"):
        return f"{ticker} (company)"
    return f"{speaker} ({ticker})"


Vague = dict[tuple[str, str], int]  # vague claims by (ticker, speaker)


def scored_promises(rows: Iterable[PromiseRow]) -> tuple[list[Scored], Vague]:
    """Specific and partial promises, once per group; and vague ones counted per
    company."""
    groups: dict[str, list[PromiseRow]] = defaultdict(list)
    vague: Vague = defaultdict(int)
    for row in rows:
        if row.promise.claim_type == "scheduled_action" or row.promise.guidance_id is not None:
            continue
        if row.promise.specificity == "vague":
            vague[(row.promise.ticker, row.promise.speaker)] += 1
            continue
        groups[row.promise.group_key].append(row)
    scored = []
    for members in groups.values():
        reviewed = [m for m in members if m.review is not None]
        head = reviewed[0] if reviewed else min(members, key=lambda m: m.promise.statement_date)
        scored.append(
            Scored(
                ticker=head.promise.ticker,
                speaker=head.promise.speaker,
                verdict=head.effective,
                bold=head.promise.bold,
                days_late=head.verdict.days_late if head.verdict and not head.review else None,
                source="promise",
                promise_ids=tuple(m.promise.id for m in members),
            )
        )
    return scored, dict(vague)


async def scored_forecasts(session: AsyncSession, ticker: str | None) -> list[Scored]:
    """Numeric forecasts with a verdict against the reported result, once each."""
    statement = (
        select(Statement, StatementOutcome)
        .join(StatementOutcome, StatementOutcome.statement_id == Statement.id)
        .where(Statement.management, StatementOutcome.verdict.in_(list(FORECAST_VERDICTS)))
        .order_by(Statement.statement_date, Statement.id)
    )
    if ticker:
        statement = statement.where(Statement.ticker == ticker.upper())
    seen: set[tuple[object, ...]] = set()
    scored = []
    for row, outcome in (await session.execute(statement)).all():
        if outcome.review == "wrong" or (outcome.plausibility == "flagged" and not outcome.review):
            continue
        key = same_claim_key(row)
        if key in seen:
            continue
        seen.add(key)
        speaker = row.executive_name or row.attribution.replace("_", " ")
        scored.append(
            Scored(row.ticker, speaker, FORECAST_VERDICTS[outcome.verdict], False, None, "forecast")
        )
    return scored


def group_key(by: str, ticker: str, speaker: str) -> str:
    if by == "executive":
        return speaker_key(ticker, speaker)
    return ticker if by == "company" else "all"


def track_records(scored: Sequence[Scored], vague: Vague, by: str) -> list[TrackRecord]:
    """Records by "executive" or "company", or one of every claim ("all"), most resolved
    claims first."""
    records: dict[str, TrackRecord] = {}

    def record(key: str) -> TrackRecord:
        return records.setdefault(key, TrackRecord(key))

    for claim in scored:
        r = record(group_key(by, claim.ticker, claim.speaker))
        r.claims += 1
        r.promise_ids.extend(claim.promise_ids)
        if claim.verdict is None:
            continue
        r.by_verdict[claim.verdict] = r.by_verdict.get(claim.verdict, 0) + 1
        if claim.verdict == "delivered_late" and claim.days_late is not None:
            r.days_late.append(claim.days_late)
        if claim.bold and claim.verdict in RESOLVED:
            r.bold += 1
            r.bold_kept += claim.verdict in ("delivered", "delivered_late")
    for (ticker, speaker), count in vague.items():
        record(group_key(by, ticker, speaker)).vague += count
    return sorted(records.values(), key=lambda r: (-r.resolved, -r.claims, r.key))


async def company_track_records(
    session: AsyncSession,
    *,
    by: str = "executive",
    ticker: str | None = None,
    specific_only: bool = False,
    speaker: str | None = None,
) -> list[TrackRecord]:
    rows = await promise_rows(session, ticker, speaker=speaker)
    if specific_only:
        rows = [r for r in rows if r.promise.specificity in ("specific", "vague")]
    scored, vague = scored_promises(rows)
    forecasts = await scored_forecasts(session, ticker)
    if speaker:
        from talk_is_cheap.statements.roster import normalize_name

        wanted = normalize_name(speaker)
        forecasts = [f for f in forecasts if wanted in normalize_name(f.speaker)]
    return track_records([*scored, *forecasts], vague, by)
