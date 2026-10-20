"""The promise tracker's benchmark: framing accuracy on `framing_labels.jsonl` and
verdict agreement on `resolution_labels.jsonl` (both in `evals/promises/`).

Labels name a claim by its company, its article's URL and the start of its quote, so
they survive re-extraction. A line with only `_note` is a comment.

Framing: each labeled claim is framed through the cache (no call when the production
run framed it already) and compared on its due date (exact), target (kind, and value
within 0.5%), baseline (source, and value within 0.5%) and specificity.

Resolution: each labeled claim's current promise is resolved through the cache (the
same plan, evidence and judge as `promises resolve`, so a claim resolved by the
production run costs nothing) and compared with its label, before any review. The most
important number is the false "delivered" rate: of claims the system calls delivered
(on time or late), the share whose label says otherwise.
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Article, Company, Promise, Statement
from talk_is_cheap.promises.framing import Framed

DELIVERED = ("delivered", "delivered_late")
TOLERANCE = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class ClaimLabel:
    id: str
    ticker: str
    url: str
    quote: str  # the start of the claim's quote
    fields: dict[str, Any]


def load_labels(path: Path) -> list[ClaimLabel]:
    labels = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        if set(data) == {"_note"}:
            continue
        try:
            labels.append(
                ClaimLabel(
                    data.pop("id"), data.pop("ticker"), data.pop("url"), data.pop("quote"), data
                )
            )
        except KeyError as exc:
            raise ValueError(f"{path}:{number}: missing {exc}") from exc
    ids = [label.id for label in labels]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: label ids must be unique")
    return labels


def _fold(text: str) -> str:
    return " ".join(text.replace("“", '"').replace("”", '"').casefold().split())


async def find_statement(session: AsyncSession, label: ClaimLabel) -> Statement | None:
    rows = (
        await session.scalars(
            select(Statement)
            .join(Article, Article.id == Statement.article_id)
            .where(Statement.ticker == label.ticker, Article.url == label.url, Statement.management)
        )
    ).all()
    wanted = _fold(label.quote)
    return next((r for r in rows if _fold(r.quote).startswith(wanted)), None)


async def find_promise(session: AsyncSession, label: ClaimLabel) -> Promise | None:
    rows = (
        await session.scalars(
            select(Promise)
            .join(Article, Article.id == Promise.article_id)
            .where(Promise.ticker == label.ticker, Article.url == label.url, Promise.current)
        )
    ).all()
    wanted = _fold(label.quote)
    return next((r for r in rows if _fold(r.quote).startswith(wanted)), None)


def _same(value: Any, expected: Any) -> bool:
    if value is None or expected is None:
        return value is None and expected is None
    a, b = Decimal(str(value)), Decimal(str(expected))
    return abs(a - b) <= abs(b) * TOLERANCE


@dataclass(slots=True)
class FramingScore:
    right: dict[str, int] = field(default_factory=dict)
    total: int = 0
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, label: ClaimLabel, got: Any, expected: Any) -> None:
        self.right[name] = self.right.get(name, 0) + ok
        if not ok:
            self.errors.append(f"{label.id}: {name} {got!r}, label {expected!r}")


def score_framing(label: ClaimLabel, promise: "Framed", score: FramingScore) -> None:
    """Compare a framed claim with its label."""
    f = label.fields
    due = promise.due_date
    score.add("due_date", (due.isoformat() if due else None) == f.get("due_date"), label, due,
              f.get("due_date"))  # fmt: skip
    kind_ok = promise.target_kind == f.get("target_kind")
    value_ok = _same(promise.target_value, f.get("target_value"))
    score.add("target", kind_ok and value_ok, label,
              (promise.target_kind, promise.target_value),
              (f.get("target_kind"), f.get("target_value")))  # fmt: skip
    source = promise.baseline_source
    value = promise.baseline.value if promise.baseline else None
    ok = source == f.get("baseline_source") and _same(value, f.get("baseline_value"))
    score.add("baseline", ok, label, (source, value),
              (f.get("baseline_source"), f.get("baseline_value")))  # fmt: skip
    score.add("specificity", promise.specificity == f.get("specificity"), label,
              promise.specificity, f.get("specificity"))  # fmt: skip
    score.total += 1


@dataclass(slots=True)
class ResolutionScore:
    pairs: list[tuple[str, str, str]] = field(default_factory=list)  # id, system, label
    missing: list[str] = field(default_factory=list)

    def false_rate(self, verdicts: tuple[str, ...]) -> tuple[int, int]:
        """Of claims the system gave one of `verdicts`, how many the labels do not."""
        said = [(s, lab) for _, s, lab in self.pairs if s in verdicts]
        return sum(lab not in verdicts for _, lab in said), len(said)

    @property
    def cannot_determine(self) -> tuple[int, int]:
        return sum(s == "cannot_determine" for _, s, _ in self.pairs), len(self.pairs)


async def evaluate_command(labels_dir: Path, dry: bool, details: bool) -> int:
    from talk_is_cheap.agent.tools import ToolContext
    from talk_is_cheap.config import get_settings
    from talk_is_cheap.db import create_engine, create_session_factory
    from talk_is_cheap.guidance.llm import (
        CacheOnlyModel,
        ExtractionModel,
        LangChainModel,
        LlmError,
        ResponseCache,
        build_chat_model,
        complete,
        model_label,
        pricing_for,
    )
    from talk_is_cheap.promises import frame as framing_run
    from talk_is_cheap.promises import resolve
    from talk_is_cheap.promises.framing import frame
    from talk_is_cheap.promises.schema import EvidencePlan, FramingAnswer, Judgment
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker
    from talk_is_cheap.spend import budget_line, command_budget
    from talk_is_cheap.statements.extract import calendars
    from talk_is_cheap.universe import load_universe

    settings = get_settings()
    try:
        framing_labels = load_labels(labels_dir / "framing_labels.jsonl")
        resolution_labels = load_labels(labels_dir / "resolution_labels.jsonl")
    except (OSError, ValueError) as exc:
        print(f"cannot use the labels in {labels_dir}: {exc}", file=sys.stderr)
        return 1
    universe = load_universe(settings.universe_file)
    companies = {c.ticker: c for c in universe.companies}
    label = model_label(settings, None)
    pricing = pricing_for(label, settings)
    if pricing is None:
        print(f"{label}: no price known; set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE")
        return 1
    cache = ResponseCache(settings.llm_cache_dir)
    today = date.today()
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        budget = await command_budget(sessions, settings, pricing, "eval promises")
        template = framing_run.load_prompt()
        async with sessions() as session:
            fiscal = await calendars(session)
            to_frame = []
            framing_score = FramingScore()
            for item in framing_labels:
                row = await find_statement(session, item)
                if row is None:
                    framing_score.missing.append(item.id)
                    continue
                claims = await framing_run.claims_to_frame(session, companies[item.ticker])
                found = next(((c, r) for c, r in claims if c.statement_id == row.id), None)
                if found is None:
                    framing_score.missing.append(item.id)
                    continue
                claim, role = found
                prompt = framing_run.render_prompt(template, claim, role, fiscal.get(item.ticker))
                to_frame.append((item, claim, prompt))
            framing_calls = sum(
                cache.get(label, framing_run.PROMPT_VERSION, p, FramingAnswer) is None
                for *_, p in to_frame
            )
            promises = []
            resolution_score = ResolutionScore()
            for item in resolution_labels:
                promise = await find_promise(session, item)
                if promise is None:
                    resolution_score.missing.append(item.id)
                else:
                    promises.append((item, promise))
            filings = {
                t: await resolve.company_filings(session, cik)
                for t, cik in (await session.execute(select(Company.ticker, Company.cik))).all()
            }
        print(
            f"{len(to_frame)} framing labels ({len(framing_score.missing)} not found), "
            f"{framing_calls} framing calls; {len(promises)} resolution labels "
            f"({len(resolution_score.missing)} not found), at most 2 calls each unless "
            "resolved by the production run (cached)"
        )
        print(budget_line(budget), flush=True)
        if dry:
            return 0
        if framing_calls:
            try:
                framer: ExtractionModel[FramingAnswer] = LangChainModel(
                    build_chat_model(settings, max_tokens=framing_run.MAX_OUTPUT_TOKENS),
                    label,
                    FramingAnswer,
                )
            except LlmError as exc:
                print(exc, file=sys.stderr)
                return 1
        else:
            framer = CacheOnlyModel(label, FramingAnswer)
        async with sessions() as session:
            for item, claim, prompt in to_frame:
                response = await complete(
                    framer, prompt, framing_run.PROMPT_VERSION, cache, budget,
                    framing_run.estimate_usage(prompt),
                )  # fmt: skip
                actuals = await framing_run._actuals(session, item.ticker)
                framed = frame(
                    claim, response.extraction.framing(), fiscal.get(item.ticker), actuals
                )
                score_framing(item, framed, framing_score)
        try:
            planner = LangChainModel(
                build_chat_model(settings, max_tokens=resolve.PLAN_OUTPUT_TOKENS),
                label,
                EvidencePlan,
            )
            judge = LangChainModel(
                build_chat_model(settings, max_tokens=resolve.JUDGE_OUTPUT_TOKENS),
                label,
                Judgment,
            )
            planner_model: ExtractionModel[EvidencePlan] = planner
            judge_model: ExtractionModel[Judgment] = judge
        except LlmError:
            planner_model = CacheOnlyModel(label, EvidencePlan)
            judge_model = CacheOnlyModel(label, Judgment)
        tools = ToolContext(
            sessions=sessions,
            engine=engine,
            universe=universe,
            embedder=lambda: load_embedder(settings),
            reranker=lambda: load_reranker(settings),
        )
        deps = resolve.ResolveDeps(planner_model, judge_model, cache, budget, tools, today)
        notes = {t: framing_run.fiscal_note(f, today) for t, f in fiscal.items()}
        graph = resolve.build_resolver(deps, notes)
        for item, promise in promises:
            end = resolve.grace_end(
                promise.due_date,
                filings.get(item.ticker, []),
                fiscal.get(item.ticker),
                settings.promise_grace_days,
            )
            try:
                resolution = await resolve.resolve_one(graph, deps, promise, end)
            except LlmError as exc:
                print(f"{item.id}: {exc}", file=sys.stderr)
                resolution_score.missing.append(item.id)
                continue
            resolution_score.pairs.append(
                (item.id, resolution.checked.verdict, str(item.fields["verdict"]))
            )
    finally:
        await engine.dispose()
    _report(framing_score, resolution_score, details)
    print(f"\n${budget.spent:.4f} spent by this run")
    print(budget_line(budget))
    return 0


def _report(framing: FramingScore, resolution: ResolutionScore, details: bool) -> None:
    def share(right: int, total: int) -> str:
        return f"{right}/{total}" + (f" ({right / total:.3f})" if total else "")

    print("\nframing accuracy:")
    for name in ("due_date", "target", "baseline", "specificity"):
        print(f"  {name:<12} {share(framing.right.get(name, 0), framing.total)}")
    if framing.missing:
        print(f"  labels without a stored claim: {', '.join(framing.missing)}")
    print("\nresolution:")
    agreed = sum(s == lab for _, s, lab in resolution.pairs)
    print(f"  verdict agreement: {share(agreed, len(resolution.pairs))}")
    wrong, said = resolution.false_rate(DELIVERED)
    print(f"  false 'delivered' rate (of system delivered or late): {share(wrong, said)}")
    wrong, said = resolution.false_rate(("not_delivered",))
    print(f"  false 'not delivered' rate: {share(wrong, said)}")
    print(f"  cannot_determine rate: {share(*resolution.cannot_determine)}")
    if resolution.missing:
        print(f"  labels without a current promise: {', '.join(resolution.missing)}")
    if details:
        for error in framing.errors:
            print(f"  framing  {error}")
        for id_, system, expected in resolution.pairs:
            if system != expected:
                print(f"  verdict  {id_}: {system}, label {expected}")
