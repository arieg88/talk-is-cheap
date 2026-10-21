"""`talk-is-cheap promises frame`, `promises resolve`, `promises NVDA`, `promises show`,
`promises review`, `promises track-record` and `eval promises`."""

import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_engine, create_session_factory
from talk_is_cheap.statements.commands import QUOTE_WORDS, short_quote
from talk_is_cheap.universe import Universe, UniverseCompany, load_universe

if TYPE_CHECKING:
    from talk_is_cheap.promises.queries import PromiseRow
    from talk_is_cheap.promises.track import TrackRecord


def _universe() -> Universe | None:
    settings = get_settings()
    try:
        return load_universe(settings.universe_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return None


def _companies(universe: Universe, tickers: Sequence[str] | None) -> list[UniverseCompany]:
    wanted = universe.select(tickers)
    return [c for c in universe.companies if c.ticker in wanted]


async def frame_command(
    tickers: Sequence[str] | None,
    limit: int | None,
    dry: bool,
    missing_only: bool,
    model: str | None,
    promise_ids: Sequence[int] | None = None,
    upgrade: bool = False,
    check_budget: bool = False,
) -> int:
    from talk_is_cheap.guidance.llm import (
        CacheOnlyModel,
        ExtractionModel,
        LangChainModel,
        LlmError,
        Pricing,
        ResponseCache,
        build_chat_model,
        model_label,
        pricing_for,
    )
    from talk_is_cheap.promises.frame import (
        MAX_OUTPUT_TOKENS,
        PROMPT_NAME,
        PROMPT_VERSION,
        DryRun,
        FrameResult,
        frame_claims,
        load_prompt,
        plan_framing,
    )
    from talk_is_cheap.promises.schema import FramingAnswer
    from talk_is_cheap.spend import budget_line, command_budget, over_budget
    from talk_is_cheap.statements.extract import calendars

    settings = get_settings()
    universe = _universe()
    if universe is None:
        return 1
    try:
        companies = _companies(universe, tickers)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if limit is not None and limit < 1:
        print("--limit must be 1 or more", file=sys.stderr)
        return 2
    label = model_label(settings, model)
    pricing = pricing_for(label, settings)
    cache = ResponseCache(settings.llm_cache_dir)
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            fiscal = await calendars(session)
            only = await _claim_keys(session, promise_ids) if promise_ids else None
            plans = await plan_framing(
                session, companies, label, cache, fiscal, template=load_prompt(),
                missing_only=missing_only, limit=limit, only=only, upgrade=upgrade,
            )  # fmt: skip
        plan = DryRun(plans, pricing)
        print(f"model {label}, prompt {PROMPT_NAME}/{PROMPT_VERSION}")
        print(f"\n{'TICKER':<7}{'CLAIMS':>7}{'UP TO DATE':>11}{'CACHED':>8}{'CALLS':>6}")
        for p in plans:
            print(
                f"{p.ticker:<7}{p.claims:>7}{p.count('up_to_date'):>11}{p.count('cached'):>8}"
                f"{p.count('call'):>6}"
            )
        cost, typical = plan.estimated_cost, plan.typical_cost
        print(
            f"\n{plan.calls} calls, at most {plan.estimated.input_tokens:,} input and "
            f"{plan.estimated.output_tokens:,} output tokens: at most "
            + (f"${cost:.4f} (typically ${typical:.4f})" if cost is not None else "unknown")
        )
        budget = await command_budget(
            sessions, settings, pricing or Pricing(Decimal(0), Decimal(0)), "promises frame"
        )
        print(budget_line(budget), flush=True)
        if dry:
            return 0
        if check_budget and over_budget(budget, typical, "framing"):
            return 3
        if plan.calls and pricing is None:
            print(f"no price known for {label}: set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE")
            return 1
        framer: ExtractionModel[FramingAnswer]
        if plan.calls == 0:
            framer = CacheOnlyModel(label, FramingAnswer)
        else:
            try:
                chat = build_chat_model(settings, model, max_tokens=MAX_OUTPUT_TOKENS)
            except LlmError as exc:
                print(f"{exc}; {plan.calls} claims need a model call", file=sys.stderr)
                return 1
            framer = LangChainModel(chat, label, FramingAnswer)

        def report(r: FrameResult) -> None:
            kinds = ", ".join(f"{n} {k}" for k, n in sorted(r.by_specificity.items())) or "none"
            print(
                f"{r.ticker:<6} {r.claims} claims ({r.up_to_date} up to date, {r.cached} "
                f"cached, {r.called} called); current promises: {kinds}; "
                f"{r.usage.input_tokens:,}/{r.usage.output_tokens:,} tokens, ${r.cost:.4f}",
                file=sys.stdout if r.ok else sys.stderr,
                flush=True,
            )
            for error in r.errors:
                print(f"       FAILED {error}", file=sys.stderr, flush=True)

        results = await frame_claims(
            sessions, framer, cache, budget, companies=companies, calendars=fiscal,
            missing_only=missing_only, limit=limit, only=only, upgrade=upgrade,
            on_result=report,
        )  # fmt: skip
    finally:
        await engine.dispose()
    print(
        f"done: {budget.calls} model calls, {budget.usage.input_tokens:,} input and "
        f"{budget.usage.output_tokens:,} output tokens, ${budget.spent:.4f}"
    )
    print(budget_line(budget))
    return 0 if all(r.ok for r in results) else 1


async def _claim_keys(session: Any, promise_ids: Sequence[int]) -> frozenset[str]:
    """The claim keys of these promises (any version)."""
    from sqlalchemy import select

    from talk_is_cheap.db.models import Promise

    keys = await session.scalars(select(Promise.claim_key).where(Promise.id.in_(promise_ids)))
    return frozenset(keys)


async def resolve_command(
    tickers: Sequence[str] | None,
    limit: int | None,
    dry: bool,
    due_only: bool,
    model: str | None,
    check_budget: bool = False,
) -> int:
    from talk_is_cheap.agent.tools import ToolContext
    from talk_is_cheap.guidance.llm import (
        LangChainModel,
        LlmError,
        Pricing,
        ResponseCache,
        build_chat_model,
        model_label,
        pricing_for,
    )
    from talk_is_cheap.promises.frame import fiscal_note
    from talk_is_cheap.promises.resolve import (
        JUDGE_OUTPUT_TOKENS,
        PLAN_OUTPUT_TOKENS,
        Candidate,
        DryRun,
        ResolveDeps,
        ResolveResult,
        plan_resolution,
        resolve_promises,
    )
    from talk_is_cheap.promises.schema import EvidencePlan, Judgment
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker
    from talk_is_cheap.spend import budget_line, command_budget, over_budget
    from talk_is_cheap.statements.extract import calendars

    settings = get_settings()
    universe = _universe()
    if universe is None:
        return 1
    try:
        companies = _companies(universe, tickers)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if limit is not None and limit < 1:
        print("--limit must be 1 or more", file=sys.stderr)
        return 2
    label = model_label(settings, model)
    pricing = pricing_for(label, settings)
    today = date.today()
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            fiscal = await calendars(session)
            plans = await plan_resolution(
                session, [c.ticker for c in companies], fiscal, today, due_only=due_only,
                grace_days=settings.promise_grace_days, limit=limit,
            )  # fmt: skip
        plan = DryRun(plans, pricing)
        print(
            f"model {label}; {'due claims only' if due_only else 'every scorable claim'}; "
            f"grace period: "
            + (
                f"{settings.promise_grace_days} days"
                if settings.promise_grace_days is not None
                else "until the first 10-Q or 10-K covering the due period"
            )
        )
        print(f"\n{'TICKER':<7}{'SCORABLE':>9}{'DUE':>5}{'TO RESOLVE':>11}{'NEW':>5}{'RECHECK':>8}")
        for p in plans:
            print(
                f"{p.ticker:<7}{p.promises:>9}{p.due:>5}{len(p.candidates):>11}"
                f"{p.count('new'):>5}{p.count('recheck'):>8}"
            )
        worst, typical = plan.worst_cost, plan.typical_cost
        print(
            f"\n{plan.claims} claims (one per group of identical promises), two calls each "
            "unless cached: at most "
            + (f"${worst:.4f} (typically ${typical:.4f})" if worst is not None else "unknown")
        )
        budget = await command_budget(
            sessions, settings, pricing or Pricing(Decimal(0), Decimal(0)), "promises resolve"
        )
        print(budget_line(budget), flush=True)
        if dry or not plan.claims:
            return 0
        if check_budget and over_budget(budget, typical, "resolution"):
            return 3
        if pricing is None:
            print(f"no price known for {label}: set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE")
            return 1
        try:
            planner = LangChainModel(
                build_chat_model(settings, model, max_tokens=PLAN_OUTPUT_TOKENS),
                label,
                EvidencePlan,
            )
            judge = LangChainModel(
                build_chat_model(settings, model, max_tokens=JUDGE_OUTPUT_TOKENS), label, Judgment
            )
        except LlmError as exc:
            print(exc, file=sys.stderr)
            return 1
        tools = ToolContext(
            sessions=sessions,
            engine=engine,
            universe=universe,
            embedder=lambda: load_embedder(settings),
            reranker=lambda: load_reranker(settings),
        )
        deps = ResolveDeps(
            planner, judge, ResponseCache(settings.llm_cache_dir), budget, tools, today
        )
        notes = {t: fiscal_note(f, today) for t, f in fiscal.items()}

        def claim(c: Candidate, verdict: str) -> None:
            members = f" (+{len(c.members) - 1} same)" if len(c.members) > 1 else ""
            print(f"  {c.promise.id:>6} {c.promise.ticker:<5} {verdict:<17} "
                  f"{c.promise.resolution_rule[:90]}{members}", flush=True)  # fmt: skip

        def report(r: ResolveResult) -> None:
            verdicts = ", ".join(f"{n} {v}" for v, n in sorted(r.verdicts.items())) or "none"
            failed = ", ".join(f"{n} {c}" for c, n in sorted(r.failed_checks.items()))
            timing = (
                f"; {r.seconds:.0f}s, {r.seconds / r.resolved:.1f}s a claim "
                f"({sum(r.claim_seconds) / r.resolved:.1f}s each on its own)"
                if r.resolved
                else ""
            )
            print(
                f"{r.ticker:<6} {r.resolved} resolved ({r.up_to_date} unchanged, {r.cached} "
                f"cached, {r.called} called): {verdicts}"
                + (f"; checks failed: {failed}" if failed else "")
                + f"; {r.usage.input_tokens:,}/{r.usage.output_tokens:,} tokens, ${r.cost:.4f}"
                + timing,
                file=sys.stdout if r.ok else sys.stderr,
                flush=True,
            )
            for error in r.errors:
                print(f"       FAILED {error}", file=sys.stderr, flush=True)

        results = await resolve_promises(
            sessions, deps, plans, notes, calendars=fiscal, on_claim=claim, on_result=report
        )
    finally:
        await engine.dispose()
    print(
        f"done: {budget.calls} model calls, {budget.usage.input_tokens:,} input and "
        f"{budget.usage.output_tokens:,} output tokens, ${budget.spent:.4f}"
    )
    print(budget_line(budget))
    return 0 if all(r.ok for r in results) else 1


def _target(row: "PromiseRow") -> str:
    from talk_is_cheap.promises.baselines import metric_unit
    from talk_is_cheap.promises.framing import show

    p = row.promise
    if p.target_kind == "event":
        return "event"
    if p.required_value is not None:
        # A multiple's or a change's required value is in the baseline's unit.
        unit = p.unit if p.target_kind in ("absolute", "threshold") else metric_unit(p.metric)
        return show(p.required_value, unit)
    if p.target_value is not None:
        return show(p.target_value, p.unit)
    return "-"


def _measured(row: "PromiseRow") -> str:
    from talk_is_cheap.promises.framing import show

    v = row.verdict
    if v is None or v.measured_value is None:
        return "-"
    unit = {"USD": "usd", "percent": "pct"}.get(v.measured_unit or "", v.measured_unit)
    return show(v.measured_value, unit)


def _verdict(row: "PromiseRow") -> str:
    if row.promise.specificity == "vague":
        return "vague"
    if row.promise.guidance_id is not None:
        return "formal guidance"
    verdict = row.effective or "unresolved"
    return ("REVIEWED " if row.review else "") + verdict.replace("_", " ")


async def list_command(ticker: str, verdict: str | None, open_only: bool, bold_only: bool) -> int:
    from talk_is_cheap.promises.queries import promise_rows

    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            rows = await promise_rows(
                session,
                None if ticker.lower() == "all" else ticker,
                verdicts=[verdict] if verdict else None,
                open_only=open_only,
                bold_only=bold_only,
            )
    finally:
        await engine.dispose()
    if not rows:
        print(f"{ticker.upper()}: no promises; run `talk-is-cheap promises frame`")
        return 0
    print(
        f"{'ID':>6}  {'DATE':<11}{'SPEAKER':<22}{'CLAIM':<40}{'DUE':<11}{'VERDICT':<20}"
        "MEASURED / TARGET"
    )
    for row in rows:
        p = row.promise
        bold = "*" if p.bold else " "
        print(
            f"{p.id:>6}  {p.statement_date.isoformat():<11}{p.speaker[:21]:<22}"
            f"{bold}{p.subject[:38]:<39}{(p.due_date.isoformat() if p.due_date else 'open'):<11}"
            f"{_verdict(row)[:19]:<20}{_measured(row)} / {_target(row)}"
        )
        print(f"{'':8}{p.resolution_rule}")
    print("\n* bold claims")
    return 0


async def show_command(promise_id: str) -> int:
    from talk_is_cheap.promises.queries import promise_row

    if not promise_id.isdigit():
        print(f"promises show takes a promise ID, got {promise_id!r}", file=sys.stderr)
        return 2
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            row = await promise_row(session, int(promise_id))
    finally:
        await engine.dispose()
    if row is None:
        print(f"no promise {promise_id}", file=sys.stderr)
        return 1
    _print_promise(row)
    return 0


def _print_promise(row: "PromiseRow") -> None:
    p, v = row.promise, row.verdict
    print(f"Promise {p.id} ({p.ticker}), version {p.version}{'' if p.current else ' (old)'}")
    print(f"  {p.speaker}, {p.statement_date.isoformat()}, reported by {row.publisher}: {row.url}")
    print(f'  "{short_quote(p.quote, QUOTE_WORDS)}"')
    if p.claim_type == "scheduled_action":
        print("  a scheduled action: resolved, but left out of track records")
    if p.guidance_id is not None:
        print(f"  restates formal guidance row {p.guidance_id}: scored with the guidance")
    print(f"  subject: {p.subject}; specificity: {p.specificity}; bold: {p.bold}"
          + (f" ({p.bold_basis})" if p.bold_basis else ""))  # fmt: skip
    due = p.due_date.isoformat() if p.due_date else "none"
    print(f"  due: {due} ({p.due_basis}" + (f', "{p.deadline_text}")' if p.deadline_text else ")"))
    if p.baseline_source:
        baseline = (
            f"{p.baseline_value.normalize():f} ({p.baseline_period}, {p.baseline_source})"
            if p.baseline_value is not None
            else "unknown"
        )
        print(f"  baseline: {baseline}")
    print(f"  rule: {p.resolution_rule}")
    for note in p.notes:
        print(f"  framing note: {note}")
    if v is None:
        print("\n  not resolved yet")
    else:
        judge = f" (the judge said {v.model_verdict})" if v.model_verdict != v.verdict else ""
        print(
            f"\n  verdict: {v.verdict}{judge}, judged against rule version {v.rule_version}, "
            f"resolved {v.resolved_at:%Y-%m-%d}"
        )
        if v.measured_value is not None:
            print(f"  measured: {_measured(row)} against {_target(row)}")
        if v.proof_date:
            source = "stated in the evidence" if v.proof_date_source == "event" else "published"
            print(f"  delivered: {v.proof_date} ({source}); grace period until "
                  f"{v.grace_end or 'not yet known'}"
                  + (f"; {v.days_late} days late" if v.days_late is not None else ""))  # fmt: skip
        if v.revised_before_due:
            print("  revised before its due date (a later statement delayed or lowered it)")
        print(f"  reasoning: {v.reasoning}")
        if v.resolved_with:
            print(f"  resolved together with promise {v.resolved_with} (the same promise)")
        relevant = [f for f in v.assessments if f.get("finding") != "irrelevant"]
        print(f"\n  judge's findings ({len(v.assessments) - len(relevant)} items irrelevant):")
        for f in relevant:
            note = f": {f['note']}" if f.get("note") else ""
            print(f"    {f['evidence_id']:<5}{f['finding']}{note}"[:200])
        print("\n  checks:")
        for c in v.checks:
            mark = {True: "pass", False: "FAIL", None: "note"}[c["passed"]]  # type: ignore[index]
            print(f"    {mark:<5}{c['check']}" + (f": {c['detail']}" if c["detail"] else ""))
        print("\n  evidence cited:")
        for e in v.evidence:
            print(f"    {e['id']:<5}{e['date'] or 'undated':<11}{e['tool']:<15}{_describe(e)}")
            if e.get("url"):
                print(f"{'':36}{e['url']}")
        if v.measured:
            data: dict[str, Any] = v.measured.get("data") or {}  # type: ignore[assignment]
            print(f"    measured: {data.get('display')} from {', '.join(data.get('inputs', []))}")
        print(f"\n  gathered: {len(v.gathered)} items")
    if row.review is not None:
        note = f" ({row.review.note})" if row.review.note else ""
        print(f"\n  REVIEWED as {row.review.verdict}{note}, {row.review.decided_at:%Y-%m-%d}: "
              "the review's verdict counts")  # fmt: skip


def _describe(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    for key in ("display", "headline", "claim", "text", "span"):
        value = data.get(key)
        if value:
            label = f"{data.get('metric', '')} {data.get('period', '')}".strip()
            text = " ".join(str(value).split())
            return (f"{label}: " if label and key == "display" else "") + text[:110]
    return ""


async def review_command(promise_id: str, verdict: str | None, note: str | None) -> int:
    from talk_is_cheap.promises.review import review

    if not promise_id.isdigit() or verdict is None:
        print("usage: talk-is-cheap promises review ID --verdict V [--note TEXT]", file=sys.stderr)
        return 2
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session, session.begin():
            try:
                summary = await review(session, int(promise_id), verdict, note)
            except (LookupError, ValueError) as exc:
                print(exc, file=sys.stderr)
                return 1
    finally:
        await engine.dispose()
    print(summary)
    return 0


async def track_command(by: str, ticker: str | None, specific_only: bool) -> int:
    from talk_is_cheap.promises.track import TOO_FEW, company_track_records

    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            records = await company_track_records(
                session, by=by, ticker=ticker, specific_only=specific_only
            )
    finally:
        await engine.dispose()
    print(
        f"Track records by {by}: specific{'' if specific_only else ' and partial'} claims, "
        "each counted once; numeric forecasts already scored count too. Rates are of "
        f"resolved claims; fewer than 5 resolved is {TOO_FEW!r}. EXPIRED: open-ended claims "
        "not shown delivered 18 months on (not resolved, never hidden). Vague claims, "
        "scheduled actions and claims restating formal guidance are never scored.\n"
    )
    print(
        f"{'GROUP':<30}{'CLAIMS':>7}{'RESOLVED':>9}  {'DEL/LATE/PART/NOT/WALK':<24}"
        f"{'ON TIME':>8}{'DAYS LATE':>10}{'WALKED':>7}{'BOLD':>6}{'BOLD HIT':>9}{'EXPIRED':>8}"
        f"{'VAGUE':>6}"
    )
    for r in records:
        _print_record(r)
    return 0


def _print_record(r: "TrackRecord") -> None:
    from talk_is_cheap.promises.track import TOO_FEW

    counts = "/".join(
        str(r.by_verdict.get(v, 0))
        for v in ("delivered", "delivered_late", "partly_delivered", "not_delivered", "walked_back")
    )

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.0%}"

    if r.enough:
        days = "-" if r.median_days_late is None else f"{r.median_days_late:.0f}"
        rates = (
            f"{pct(r.on_time_rate):>8}{days:>10}{pct(r.walked_back_share):>7}"
            f"{pct(r.bold_share):>6}{pct(r.bold_hit_rate):>9}"
        )
    else:
        rates = f"  {TOO_FEW:<38}"
    print(
        f"{r.key[:29]:<30}{r.claims:>7}{r.resolved:>9}  {counts:<24}{rates}{r.expired:>8}"
        f"{r.vague:>6}"
    )


async def eval_command(labels_dir: Path, dry: bool, details: bool) -> int:
    from talk_is_cheap.promises.evaluate import evaluate_command

    return await evaluate_command(labels_dir, dry, details)
