"""The ledger of LLM spending: every paid call, by command and model, against the total
budget LLM_BUDGET_USD. Cached responses cost nothing and are not recorded."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from talk_is_cheap.config import Settings
from talk_is_cheap.db.models import LlmSpend
from talk_is_cheap.guidance.llm import Budget, Pricing, Usage


class DatabaseLedger:
    """Records each paid call of one command in `llm_spend`, in its own transaction, so a
    run that stops or fails keeps the record of what it spent."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], command: str) -> None:
        self._sessions = sessions
        self._command = command

    async def record(
        self, model: str, prompt_version: str | None, usage: Usage, cost: Decimal
    ) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                LlmSpend(
                    command=self._command,
                    model=model,
                    prompt_version=prompt_version,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost=cost,
                    counted=True,
                )
            )


async def command_budget(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    pricing: Pricing,
    command: str,
    *,
    cap: Decimal | None = None,
) -> Budget:
    """A budget for one command: its cost cap (LLM_COST_CAP unless `cap` is given), the
    project's total budget less what the ledger has recorded, and the ledger to record
    its calls."""
    async with sessions() as session:
        spent = await counted_spend(session)
    return Budget(
        settings.llm_cost_cap if cap is None else cap,
        pricing,
        limit=settings.llm_budget_usd,
        spent_before=spent,
        ledger=DatabaseLedger(sessions, command),
    )


def budget_line(budget: Budget) -> str:
    assert budget.limit is not None
    remaining = max(budget.limit - budget.total_spent, Decimal(0))
    return (
        f"LLM budget: ${budget.total_spent:.4f} of ${budget.limit:.2f} spent, "
        f"${remaining:.4f} remaining (talk-is-cheap spend)"
    )


def over_budget(budget: Budget, estimate: Decimal | None, what: str) -> bool:
    """Whether a run's estimated cost exceeds what is left of the budget; says so when it
    does (`--check-budget`, `make refresh-claims`)."""
    assert budget.limit is not None
    remaining = max(budget.limit - budget.total_spent, Decimal(0))
    if estimate is None or estimate <= remaining:
        return False
    print(
        f"not run: {what} is estimated at ${estimate:.4f}, more than the ${remaining:.4f} "
        "left of LLM_BUDGET_USD"
    )
    return True


async def counted_spend(session: AsyncSession) -> Decimal:
    """Total cost of the calls that count toward the budget."""
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmSpend.cost), 0)).where(LlmSpend.counted)
    )
    return Decimal(total or 0)


@dataclass(frozen=True, slots=True)
class SpendLine:
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost: Decimal


@dataclass(frozen=True, slots=True)
class SpendReport:
    total: Decimal
    calls: int
    by_command: list[SpendLine]
    by_model: list[SpendLine]
    notes: list[LlmSpend]


async def spend_report(session: AsyncSession) -> SpendReport:
    async def grouped(column: InstrumentedAttribute[str]) -> list[SpendLine]:
        rows = await session.execute(
            select(
                column,
                func.count(),
                func.sum(LlmSpend.input_tokens),
                func.sum(LlmSpend.output_tokens),
                func.sum(LlmSpend.cost),
            )
            .where(LlmSpend.counted)
            .group_by(column)
            .order_by(func.sum(LlmSpend.cost).desc())
        )
        return [SpendLine(str(k), int(n), int(i), int(o), Decimal(c)) for k, n, i, o, c in rows]

    by_command = await grouped(LlmSpend.command)
    notes = (
        await session.scalars(
            select(LlmSpend).where(LlmSpend.counted.is_(False)).order_by(LlmSpend.recorded_at)
        )
    ).all()
    return SpendReport(
        total=await counted_spend(session),
        calls=sum(line.calls for line in by_command),
        by_command=by_command,
        by_model=await grouped(LlmSpend.model),
        notes=list(notes),
    )


async def add_note(
    session: AsyncSession, model: str, usage: Usage, cost: Decimal, note: str
) -> None:
    """Record spending from before the ledger, without counting it toward the budget."""
    session.add(
        LlmSpend(
            command="before the ledger",
            model=model,
            prompt_version=None,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost=cost,
            counted=False,
            note=note,
        )
    )
