"""`talk-is-cheap ingest article-text`, `extract statements`, `statements`, `statements
review` and `eval statements`."""

import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_engine, create_session_factory
from talk_is_cheap.universe import Universe, UniverseCompany, load_universe

if TYPE_CHECKING:
    from talk_is_cheap.statements.extract import DryRun
    from talk_is_cheap.statements.queries import StatementRow


def _universe() -> Universe | None:
    settings = get_settings()
    try:
        return load_universe(settings.universe_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return None


async def article_text_command(tickers: Sequence[str] | None, limit: int | None) -> int:
    from talk_is_cheap.news.article_text import Candidate, Fetched, fetch_texts
    from talk_is_cheap.news.poll import new_client

    settings = get_settings()
    universe = _universe()
    if universe is None:
        return 1
    try:
        selected = [c.ticker for c in universe.select_news(tickers)] if tickers else None
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    engine = create_engine(settings)

    def report(candidate: Candidate, fetched: Fetched) -> None:
        outcome = "ok" if fetched.status == "ok" else f"unavailable ({fetched.reason})"
        size = f", {len(fetched.text):,} chars" if fetched.text else ""
        print(f"  {candidate.publisher[:28]:<29}{outcome}{size}  {candidate.url[:90]}", flush=True)

    try:
        sessions = create_session_factory(engine)
        async with new_client(settings.article_user_agent) as client:
            summary = await fetch_texts(
                sessions,
                client,
                settings.article_user_agent.split()[0].split("/")[0],
                tickers=selected,
                limit=limit,
                on_result=report,
            )
    finally:
        await engine.dispose()
    print(f"\n{'PUBLISHER':<34}{'FETCHED':>8}{'UNAVAILABLE':>12}{'SKIPPED':>8}  REASONS")
    rows = sorted(
        summary.by_publisher.items(),
        key=lambda item: -(item[1].fetched + item[1].unavailable + item[1].skipped),
    )
    for publisher, count in rows:
        reasons = ", ".join(f"{k} {n}" for k, n in count.reasons.most_common())
        print(
            f"{publisher[:33]:<34}{count.fetched:>8}{count.unavailable:>12}{count.skipped:>8}"
            f"  {reasons}"
        )
    print(
        f"\n{summary.clusters} linked clusters: {summary.total('fetched')} fetched, "
        f"{summary.total('unavailable')} unavailable, {summary.total('skipped')} skipped "
        f"(text stored, or no article left to try); {summary.requests} requests in "
        f"{summary.seconds:.0f}s"
    )
    return 0


def _companies(universe: Universe, tickers: Sequence[str] | None) -> list[UniverseCompany]:
    """Companies whose filings are loaded: all, or the given ones."""
    wanted = universe.select(tickers)
    return [c for c in universe.companies if c.ticker in wanted]


async def extract_command(
    tickers: Sequence[str] | None,
    limit: int | None,
    dry: bool,
    missing_only: bool,
    model: str | None,
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
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker
    from talk_is_cheap.spend import budget_line, command_budget
    from talk_is_cheap.statements.context import filing_context
    from talk_is_cheap.statements.extract import (
        MAX_OUTPUT_TOKENS,
        DryRun,
        ExtractResult,
        calendars,
        extract_statements,
        load_prompt,
        plan_companies,
    )
    from talk_is_cheap.statements.schema import StatementExtraction

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
            plans = await plan_companies(
                session, companies, label, cache, template=load_prompt(),
                missing_only=missing_only, limit=limit,
            )  # fmt: skip
        plan = DryRun(plans, pricing)
        _print_plan(plan, label, limit)
        budget = await command_budget(
            sessions, settings, pricing or Pricing(Decimal(0), Decimal(0)), "extract statements"
        )
        print(budget_line(budget), flush=True)
        if dry:
            return 0
        if plan.calls and pricing is None:
            print(
                f"no price known for {label}: set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE",
                file=sys.stderr,
            )
            return 1
        extractor: ExtractionModel[StatementExtraction]
        if plan.calls == 0:
            extractor = CacheOnlyModel(label, StatementExtraction)
        else:
            try:
                chat = build_chat_model(settings, model, max_tokens=MAX_OUTPUT_TOKENS)
            except LlmError as exc:
                print(f"{exc}; {plan.calls} stories need a model call", file=sys.stderr)
                return 1
            extractor = LangChainModel(chat, label, StatementExtraction)
        async with sessions() as session:
            fiscal = await calendars(session)
        find_context = filing_context(
            lambda: load_embedder(settings), lambda: load_reranker(settings)
        )

        def report(r: ExtractResult) -> None:
            types = ", ".join(f"{n} {t}" for t, n in sorted(r.by_type.items())) or "none"
            outcomes = ", ".join(f"{n} {v}" for v, n in sorted(r.outcomes.items())) or "none"
            print(
                f"{r.ticker:<6} {r.candidates} stories ({r.up_to_date} up to date, "
                f"{r.reprocessed} reprocessed, {r.cached} cached, {r.called} called), "
                f"{r.claims} claims, {r.management} by management ({types}); outcomes: "
                f"{outcomes}; {r.dropped} dropped by grounding; "
                f"{r.usage.input_tokens:,}/{r.usage.output_tokens:,} tokens, ${r.cost:.4f}, "
                f"{r.seconds:.0f}s",
                file=sys.stdout if r.ok else sys.stderr,
                flush=True,
            )
            for quote in r.dropped_quotes:
                print(f"       dropped (quote not found): {quote!r}", flush=True)
            for error in r.errors:
                print(f"       FAILED {error}", file=sys.stderr, flush=True)

        results = await extract_statements(
            sessions, extractor, cache, budget, companies=companies, calendars=fiscal,
            missing_only=missing_only, limit=limit, find_context=find_context,
            on_result=report,
        )  # fmt: skip
    finally:
        await engine.dispose()
    print(
        f"done: {sum(r.claims for r in results)} claims, "
        f"{sum(r.management for r in results)} by management, "
        f"{sum(r.dropped for r in results)} dropped by grounding, {budget.calls} model calls, "
        f"{budget.usage.input_tokens:,} input and {budget.usage.output_tokens:,} output "
        f"tokens, ${budget.spent:.4f}"
    )
    print(budget_line(budget))
    return 0 if all(r.ok for r in results) else 1


def _print_plan(plan: "DryRun", label: str, limit: int | None) -> None:
    print(f"model {label}" + (f", at most {limit} stories per company" if limit else ""))
    print(
        f"\n{'TICKER':<7}{'STORIES':>8}{'CANDIDATES':>11}{'UP TO DATE':>11}{'REPROCESS':>10}"
        f"{'CACHED':>8}{'CALLS':>6}"
    )
    for p in plan.plans:
        print(
            f"{p.ticker:<7}{p.stories:>8}{len(p.planned):>11}{p.count('up_to_date'):>11}"
            f"{p.count('reprocess'):>10}{p.count('cached'):>8}{p.count('call'):>6}"
        )
    estimated = plan.estimated
    cost = plan.estimated_cost
    print(
        f"\n{plan.calls} calls, at most {estimated.input_tokens:,} input and "
        f"{estimated.output_tokens:,} output tokens: at most "
        + (f"${cost:.4f}" if cost is not None else "an unknown cost (no price known)")
        + f" ({_typical(plan)})"
    )


def _typical(plan: "DryRun") -> str:
    """The cost to expect, at the output measured on earlier runs."""
    from talk_is_cheap.guidance.llm import Usage
    from talk_is_cheap.statements.extract import TYPICAL_OUTPUT_TOKENS

    if plan.pricing is None:
        return "no price known"
    usage = Usage(plan.estimated.input_tokens, TYPICAL_OUTPUT_TOKENS * plan.calls)
    return f"typically ${plan.pricing.cost(usage):.4f}"


# Words of an article quoted in tables and answers, at most (the article is linked).
QUOTE_WORDS = 40


def short_quote(text: str, words: int = QUOTE_WORDS) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


async def statements_command(
    ticker: str,
    checkable: bool,
    speaker: str | None,
    all_attributions: bool,
    summary: bool,
    limit: int | None,
) -> int:
    from talk_is_cheap.statements.queries import company_statements

    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            rows = await company_statements(
                session,
                None if ticker.lower() == "all" else ticker,
                speaker=speaker,
                management_only=not all_attributions and not summary,
                checkable_only=checkable,
            )
    finally:
        await engine.dispose()
    if summary:
        _print_summary(rows)
        return 0
    if not rows:
        print(f"{ticker.upper()}: no statements; run `talk-is-cheap extract statements`")
        return 0
    who = "All companies" if ticker.lower() == "all" else ticker.upper()
    kind = "every claim" if all_attributions else "management claims"
    print(
        f"{who}: {kind} reported in the news, newest first. Actuals as first reported. "
        "GUIDANCE compares the claim with formal guidance issued within 45 days.\n"
    )
    print(
        f"{'ID':>6}  {'DATE':<11}{'SPEAKER':<24}{'VENUE':<14}{'CLAIM':<44}{'TARGET':<9}"
        f"{'ACTUAL':<13}{'VERDICT':<16}GUIDANCE"
    )
    for found in rows[:limit] if limit else rows:
        _print_row(found)
    return 0


def _print_row(found: "StatementRow") -> None:
    from talk_is_cheap.statements.queries import target_label

    row, outcome = found.statement, found.outcome
    speaker = row.executive_name or row.speaker_name or row.attribution.replace("_", " ")
    if not row.management:
        speaker = f"{speaker} ({row.attribution.replace('_', ' ')})"
    date = row.statement_date.isoformat() + ("~" if row.date_from_article else " ")
    claim = (row.value_text and f"{row.metric or ''} {row.value_text}") or row.claim
    if row.claim_type == "directional":
        claim = f"{row.metric or ''} {row.direction}: {row.claim}"
    verdict = "-"
    actual = ""
    comparison = ""
    if outcome is not None:
        verdict = outcome.verdict.replace("_", " ")
        if outcome.plausibility == "flagged":
            verdict = {None: "REVIEW ", "ok": "", "wrong": "WRONG "}[outcome.review] + verdict
        elif outcome.review == "wrong":
            verdict = "WRONG " + verdict
        actual = _amount(outcome.actual, row.unit)
        comparison = outcome.guidance_comparison.replace("_", " ")
    print(
        f"{row.id:>6}  {date:<11}{speaker[:23]:<24}{row.venue[:13]:<14}"
        f"{' '.join(claim.split())[:43]:<44}{target_label(row) or '-':<9}{actual:<13}"
        f"{verdict[:15]:<16}{comparison}"
    )
    pad = " " * 8
    print(f'{pad}"{short_quote(row.quote)}"')
    if outcome is not None:
        notes = [n for n in (outcome.verdict_reason, outcome.guidance_note) if n]
        if outcome.plausibility == "flagged":
            notes.append(f"plausibility: {outcome.plausibility_note}")
        if notes:
            print(f"{pad}{'; '.join(notes)}")
        if outcome.context_url:
            print(f"{pad}filing context ({outcome.context_label}): {outcome.context_url}")
    print(f"{pad}reported by {found.publisher or 'unknown'}: {found.url}")


def _amount(value: Decimal | None, unit: str) -> str:
    if value is None:
        return ""
    if unit == "pct":
        return f"{value:.1f}%"
    if unit == "usd_per_share":
        return f"${value:.2f}"
    if unit == "usd":
        return f"${value / 10**9:,.2f}B" if abs(value) >= 10**9 else f"${value / 10**6:,.0f}M"
    return f"{value.normalize():f}"


def _print_summary(rows: Sequence["StatementRow"]) -> None:
    by_ticker: dict[str, list[StatementRow]] = defaultdict(list)
    for found in rows:
        by_ticker[found.statement.ticker].append(found)
    print(
        "Management claims in the news by company. Claims by type count every story; "
        "verdicts count each forecast once, however many outlets reported it. HIT RATE: met "
        "or confirmed, of checkable claims with a verdict (beat, met, missed, confirmed, "
        "contradicted). CONSISTENT: consistent with formal guidance, of claims compared with "
        "guidance. OTHERS: claims by analysts, unnamed sources, reporters and unverified "
        "executives (never scored).\n"
    )
    print(
        f"{'TICKER':<7}{'MGMT':>5}{'NUMERIC':>8}{'DIRECT.':>8}{'PLAN':>6}{'QUAL.':>6}"
        f"{'OTHERS':>7}  {'HIT RATE':<16}{'BEAT/MET/MISSED':<16}{'CONF/CONTRA':<12}"
        f"{'PENDING':>8}{'N/C':>5}{'REVIEW':>7}  CONSISTENT"
    )
    totals: dict[str, int] = defaultdict(int)
    for ticker in sorted(by_ticker):
        line = _summary_counts(by_ticker[ticker])
        for key, value in line.items():
            totals[key] += value
        _print_summary_line(ticker, line)
    if by_ticker:
        _print_summary_line("Total", totals)


def _summary_counts(rows: Sequence["StatementRow"]) -> dict[str, int]:
    from talk_is_cheap.statements.outcomes import counts_as as count
    from talk_is_cheap.statements.outcomes import same_claim_key

    line: dict[str, int] = {}
    seen: set[tuple[object, ...]] = set()
    for found in rows:
        row, outcome = found.statement, found.outcome
        if not row.management:
            line["others"] = line.get("others", 0) + 1
            continue
        line["management"] = line.get("management", 0) + 1
        line[row.claim_type] = line.get(row.claim_type, 0) + 1
        if outcome is None:
            continue
        if row.claim_type in ("numeric_forecast", "directional"):
            key = same_claim_key(row)
            if key in seen:
                continue  # the same forecast from another outlet
            seen.add(key)
        line[count(outcome)] = line.get(count(outcome), 0) + 1
        if count(outcome) in ("needs_review", "wrong"):
            continue
        line[f"guidance:{outcome.guidance_comparison}"] = (
            line.get(f"guidance:{outcome.guidance_comparison}", 0) + 1
        )
    return line


def _print_summary_line(ticker: str, c: Mapping[str, int]) -> None:
    decided = sum(c.get(v, 0) for v in ("beat", "met", "missed", "confirmed", "contradicted"))
    hits = c.get("met", 0) + c.get("confirmed", 0)
    rate = f"{hits}/{decided} ({hits / decided:.0%})" if decided else "-"
    compared = c.get("guidance:consistent", 0) + c.get("guidance:differs", 0)
    consistent = (
        f"{c.get('guidance:consistent', 0)}/{compared} "
        f"({c.get('guidance:consistent', 0) / compared:.0%})"
        if compared
        else "-"
    )
    print(
        f"{ticker:<7}{c.get('management', 0):>5}{c.get('numeric_forecast', 0):>8}"
        f"{c.get('directional', 0):>8}{c.get('plan', 0):>6}{c.get('qualitative', 0):>6}"
        f"{c.get('others', 0):>7}  {rate:<16}"
        f"{c.get('beat', 0)}/{c.get('met', 0)}/{c.get('missed', 0):<12}"
        f"{c.get('confirmed', 0)}/{c.get('contradicted', 0):<10}"
        f"{c.get('pending', 0):>8}{c.get('not_comparable', 0):>5}"
        f"{c.get('needs_review', 0) + c.get('wrong', 0):>7}  {consistent}"
    )


async def review_command(statement_id: str, decision: str) -> int:
    from talk_is_cheap.statements.review import review

    if not statement_id.isdigit():
        print(f"statements review takes a statement ID, got {statement_id!r}", file=sys.stderr)
        return 2
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session, session.begin():
            try:
                summary = await review(session, int(statement_id), decision)
            except LookupError as exc:
                print(exc, file=sys.stderr)
                return 1
    finally:
        await engine.dispose()
    print(summary)
    return 0


async def eval_command(labels_file: Path, model: str | None, dry: bool, details: bool) -> int:
    from talk_is_cheap.guidance.llm import (
        CacheOnlyModel,
        ExtractionModel,
        LangChainModel,
        LlmError,
        ResponseCache,
        build_chat_model,
        model_label,
        pricing_for,
    )
    from talk_is_cheap.spend import budget_line, command_budget
    from talk_is_cheap.statements.evaluate import (
        StoryScore,
        evaluate,
        labeled_inputs,
        load_labels,
        planned_calls,
    )
    from talk_is_cheap.statements.extract import MAX_OUTPUT_TOKENS, PROMPT_VERSION
    from talk_is_cheap.statements.schema import StatementExtraction

    settings = get_settings()
    universe = _universe()
    if universe is None:
        return 1
    try:
        labels = load_labels(labels_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {labels_file}: {exc}", file=sys.stderr)
        return 1
    label = model_label(settings, model)
    pricing = pricing_for(label, settings)
    if pricing is None:
        print(f"{label}: no price known; set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE")
        return 1
    cache = ResponseCache(settings.llm_cache_dir)
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            try:
                inputs = await labeled_inputs(
                    session, labels, {c.ticker: c for c in universe.companies}
                )
            except ValueError as exc:
                print(f"{labels_file}: {exc}", file=sys.stderr)
                return 1
        calls, worst = planned_calls(inputs, label, cache)
        budget = await command_budget(sessions, settings, pricing, "eval statements")
        claims = sum(len(i.label.claims) for i in inputs)
        print(
            f"{len(inputs)} labeled stories, {claims} labeled claims; model {label}: "
            f"{len(inputs) - calls} cached, {calls} calls, at most ${pricing.cost(worst):.4f}"
        )
        print(budget_line(budget), flush=True)
        if dry:
            return 0
        extractor: ExtractionModel[StatementExtraction]
        if calls == 0:
            extractor = CacheOnlyModel(label, StatementExtraction)
        else:
            try:
                chat = build_chat_model(settings, model, max_tokens=MAX_OUTPUT_TOKENS)
            except LlmError as exc:
                print(exc, file=sys.stderr)
                return 1
            extractor = LangChainModel(chat, label, StatementExtraction)

        def show(s: StoryScore) -> None:
            if not details:
                return
            problems = s.leaks + s.misses + s.extras + s.field_errors
            print(f"{s.id:<44} {'ok' if not problems else ''}")
            for problem in problems:
                print(f"    {problem}")

        try:
            score = await evaluate(inputs, extractor, cache, budget, on_story=show)
        except LlmError as exc:
            print(f"stopped: {exc}", file=sys.stderr)
            return 1
    finally:
        await engine.dispose()

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}"

    print(f"\nmodel {score.model}, prompt statement_extraction/{PROMPT_VERSION}")
    stories = score.stories
    true = sum(x.system_management_true for x in stories)
    covered = sum(x.system_management_covered for x in stories)
    system = sum(x.system_management for x in stories)
    labeled = sum(x.labeled_management for x in stories)
    found = sum(x.found_management for x in stories)
    attributed = sum(x.attributed_management for x in stories)
    print(
        f"attribution precision (management vs not): {pct(score.attribution_precision)} "
        f"({true} of {covered} labeled claims the system attributes to management)"
    )
    print(
        f"attribution recall: {pct(score.attribution_recall)} ({attributed} of {found} "
        "management claims found)"
    )
    print(f"non-management claims attributed to management (leaks): {score.leaks}")
    print(
        f"claim recall (labeled management claims found): {pct(score.claim_recall)} "
        f"({found} of {labeled})"
    )
    print(
        f"claim precision (management claims that are labeled): "
        f"{pct(score.claim_precision)} ({true} of {system})"
    )
    for name in ("claim_type", "metric", "period", "figures"):
        right, total = score.field_accuracy(name)
        print(f"{name} accuracy: {right}/{total}" + (f" ({right / total:.3f})" if total else ""))
    print(f"grounding drops: {score.dropped}")
    usage = score.usage
    print(
        f"{len(score.stories)} stories, {usage.input_tokens:,} input and "
        f"{usage.output_tokens:,} output tokens, ${score.cost:.4f} as made "
        f"(${budget.spent:.4f} spent now)"
    )
    print(budget_line(budget))
    return 0
