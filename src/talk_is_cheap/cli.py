"""Command-line entry point: `talk-is-cheap <command>`."""

import argparse
import asyncio
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as day_time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from talk_is_cheap import __version__
from talk_is_cheap.config import get_settings
from talk_is_cheap.db import check_database, create_engine, create_session_factory
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.edgar.ingest import CompanyIngestResult, ingest_edgar
from talk_is_cheap.edgar.source import EdgartoolsSource, configure_edgartools
from talk_is_cheap.filings.coverage import QuarterCoverage, coverage, find_sections
from talk_is_cheap.filings.ingest import FilingsIngestResult, ingest_filings
from talk_is_cheap.filings.sections import SectionType
from talk_is_cheap.financials import (
    QuarterFinancials,
    Vintage,
    company_summaries,
    find_company,
    quarterly_financials,
    restatements,
)
from talk_is_cheap.universe import Universe, UniverseCompany, load_universe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from talk_is_cheap.db.models import Guidance
    from talk_is_cheap.guidance.extract import DryRun
    from talk_is_cheap.guidance.llm import Budget, Pricing
    from talk_is_cheap.news.gdelt import BackfillProgress
    from talk_is_cheap.news.queries import BackfillSource


async def _db_check() -> int:
    engine = create_engine(get_settings())
    try:
        status = await check_database(engine)
    except Exception as exc:  # report any connection failure as a clean CLI error
        print(f"database: unreachable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    print(f"postgres:  {status.server_version}")
    print(f"pgvector:  {status.vector_extension_version or 'not installed'}")
    print(f"migration: {status.migration_revision or 'none applied'}")
    print(f"ready:     {'yes' if status.ready else 'no'}")
    return 0 if status.ready else 1


def _edgar_universe(tickers: Sequence[str] | None) -> Universe | None:
    """The universe, with edgartools configured; None after printing why it cannot run."""
    settings = get_settings()
    try:
        universe = load_universe(settings.universe_file)
        universe.select(tickers)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return None
    if settings.edgar_identity is None:
        print(
            "EDGAR_IDENTITY is not set. The SEC requires a name and contact email, "
            "e.g. EDGAR_IDENTITY='Jane Doe jane@example.com'",
            file=sys.stderr,
        )
        return None
    configure_edgartools(settings.edgar_identity, settings.edgar_data_dir)
    return universe


async def _ingest_edgar(tickers: Sequence[str] | None) -> int:
    universe = _edgar_universe(tickers)
    if universe is None:
        return 1
    engine = create_engine(get_settings())

    def report(result: CompanyIngestResult) -> None:
        if result.ok:
            print(
                f"{result.ticker:<6} {result.name}: {result.filings} filings, "
                f"{result.new_facts} new facts, {result.financial_values} financial values"
            )
        else:
            print(f"{result.ticker:<6} FAILED: {result.error}", file=sys.stderr)

    try:
        results = await ingest_edgar(
            EdgartoolsSource(),
            create_session_factory(engine),
            universe,
            tickers=tickers,
            on_result=report,
        )
    finally:
        await engine.dispose()
    print(f"done: {sum(r.ok for r in results)}/{len(results)} companies loaded")
    return 0 if all(r.ok for r in results) else 1


async def _ingest_filings(tickers: Sequence[str] | None) -> int:
    universe = _edgar_universe(tickers)
    if universe is None:
        return 1
    engine = create_engine(get_settings())

    def report(result: FilingsIngestResult) -> None:
        if result.error is not None:
            print(f"{result.ticker:<6} FAILED: {result.error}", file=sys.stderr)
            return
        line = (
            f"{result.ticker:<6} {result.name}: {result.filings} filings, {result.parsed} parsed, "
            f"{result.skipped} up to date, {result.documents} documents, "
            f"{result.sections} sections"
        )
        if result.failed_filings:
            line += f", {len(result.failed_filings)} failed ({', '.join(result.failed_filings)})"
        print(line, file=sys.stdout if result.ok else sys.stderr)

    try:
        results = await ingest_filings(
            EdgartoolsSource(),
            create_session_factory(engine),
            universe,
            tickers=tickers,
            on_result=report,
        )
    finally:
        await engine.dispose()
    print(f"done: {sum(r.ok for r in results)}/{len(results)} companies parsed")
    return 0 if all(r.ok for r in results) else 1


async def _coverage(tickers: Sequence[str] | None) -> int:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            rows = await coverage(session, tickers)
    finally:
        await engine.dispose()
    if not rows:
        print("no filings loaded; run `talk-is-cheap ingest edgar`")
        return 0

    print(f"{'TICKER':<7}{'QUARTER':<11}{'RELEASE':<9}{'OUTLOOK':<13}{'FORM':<6}{'MD&A':<6}RISK")
    for row in rows:
        release = "-" if not row.earnings_8ks else _found(row.press_releases > 0)
        if not row.press_releases:
            outlook = "-"
        elif row.outlook_without_guidance:
            outlook = "no guidance"
        else:
            outlook = _found(row.outlook)
        form = row.periodic_form
        mdna = "-" if form is None else _found(row.mdna)
        risk = "-" if form is None else _found(row.risk_factors)
        print(
            f"{row.ticker:<7}{row.quarter.label:<11}{release:<9}{outlook:<13}{form or '-':<6}"
            f"{mdna:<6}{risk}"
        )

    print()
    print(f"{'SECTION':<23}{'FOUND':>9}  {'OF':<25}COVERAGE")
    for label, found, total, of in _coverage_totals(rows):
        share = f"{found / total * 100:.1f}%" if total else "-"
        print(f"{label:<23}{f'{found}/{total}':>9}  {of:<25}{share}")
    deliveries = sum(r.deliveries_reports for r in rows)
    if deliveries:
        print(f"\nnot counted as releases: {deliveries} deliveries reports filed under item 2.02")
    return 0


def _found(present: bool) -> str:
    return "yes" if present else "no"


def _coverage_totals(rows: Sequence[QuarterCoverage]) -> list[tuple[str, int, int, str]]:
    releases = [r for r in rows if r.press_releases]
    tenks = [r for r in rows if r.periodic_form == "10-K"]
    tenqs = [r for r in rows if r.periodic_form == "10-Q"]
    return [
        (
            "press release",
            sum(min(r.press_releases, r.earnings_8ks) for r in rows),
            sum(r.earnings_8ks for r in rows),
            "earnings 8-Ks",
        ),
        ("outlook", sum(r.outlook for r in releases), len(releases), "quarters with a release"),
        (
            "outlook (no guidance)",
            sum(r.outlook_without_guidance for r in releases),
            len(releases),
            "quarters with a release",
        ),
        ("MD&A", sum(r.mdna for r in tenks + tenqs), len(tenks) + len(tenqs), "10-Qs and 10-Ks"),
        ("risk factors", sum(r.risk_factors for r in tenks), len(tenks), "10-Ks"),
        ("risk factors", sum(r.risk_factors for r in tenqs), len(tenqs), "10-Qs"),
    ]


async def _show(ticker: str, period: str, section_type: str) -> int:
    quarter = None
    if period.lower() != "latest":
        try:
            quarter = FiscalQuarter.parse(period)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            company = await find_company(session, ticker)
            if company is None:
                print(f"{ticker.upper()} is not loaded", file=sys.stderr)
                return 1
            found = await find_sections(session, company.cik, section_type, quarter)
    finally:
        await engine.dispose()

    if not found:
        label = quarter.label if quarter else "any quarter"
        print(f"{company.ticker} {label}: no {section_type} section", file=sys.stderr)
        return 1
    # Every section of this type in the preferred document; others are listed.
    first = found[0]
    shown = [s for s in found if (s.document_name, s.url) == (first.document_name, first.url)]
    elsewhere = sorted(
        {f"{s.exhibit or s.doc_type} {s.document_name}" for s in found}
        - {f"{first.exhibit or first.doc_type} {first.document_name}"}
    )
    print(
        f"{first.ticker} {first.quarter.label} {first.section_type}: "
        f"{first.exhibit or first.doc_type} {first.document_name}, filed "
        f"{first.filed_at.isoformat()}, period ended {first.period_end.isoformat()}"
    )
    print(first.url)
    if elsewhere:
        print(f"also in this quarter: {', '.join(elsewhere)}")
    for section in shown:
        print(
            f"\n--- {section.title or '(untitled)'} (characters {section.char_start}-"
            f"{section.char_end})\n"
        )
        print(section.text)
    return 0


async def _companies() -> int:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            summaries = await company_summaries(session)
    finally:
        await engine.dispose()
    if not summaries:
        print("no companies loaded; run `talk-is-cheap ingest edgar`")
        return 0
    print(f"{'TICKER':<7}{'CIK':>8}  {'FYE':<5}{'FILINGS':>8}  {'LATEST':<11}NAME")
    for s in summaries:
        print(
            f"{s.ticker:<7}{s.cik:>8}  {s.fiscal_year_end or '-':<5}{s.filings:>8}  "
            f"{s.latest_quarter or '-':<11}{s.name}"
        )
    return 0


async def _financials(ticker: str, vintage: Vintage, quarters: int) -> int:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            company = await find_company(session, ticker)
            if company is None:
                print(f"{ticker.upper()} is not loaded", file=sys.stderr)
                return 1
            rows = await quarterly_financials(
                session, company.cik, vintage=vintage, quarters=quarters
            )
    finally:
        await engine.dispose()

    label = "as first reported" if vintage == "first" else "latest reported"
    print(f"{company.name} ({company.ticker}), quarterly, {label}. USD millions except EPS.")
    print("* = derived from year-to-date totals\n")
    header = (
        f"{'QUARTER':<12}{'ENDED':<12}{'REVENUE':>11}{'GROSS %':>9}{'OP %':>8}"
        f"{'NET INC':>11}{'EPS':>8}{'OP CF':>11}{'CAPEX':>10}"
    )
    print(header)
    for row in rows:
        print(
            f"{row.label:<12}{row.period_end.isoformat():<12}"
            f"{_millions(row, 'revenue'):>11}{_percent(row.gross_margin):>9}"
            f"{_percent(row.operating_margin):>8}{_millions(row, 'net_income'):>11}"
            f"{_per_share(row, 'eps_diluted'):>8}{_millions(row, 'operating_cash_flow'):>11}"
            f"{_millions(row, 'capex'):>10}"
        )
    return 0


async def _restatements(ticker: str) -> int:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            company = await find_company(session, ticker)
            if company is None:
                print(f"{ticker.upper()} is not loaded", file=sys.stderr)
                return 1
            changes = await restatements(session, company.cik)
    finally:
        await engine.dispose()

    if not changes:
        print(f"{company.ticker}: no restated quarterly values")
        return 0
    print(f"{company.name} ({company.ticker}): values changed after first being reported\n")
    print(f"{'QUARTER':<12}{'METRIC':<26}{'FIRST':>14}{'LATEST':>14}{'CHANGE':>9}  LATEST FILING")
    for c in changes:
        change = "" if c.change is None else f"{c.change * 100:+.1f}%"
        print(
            f"{f'FY{c.fiscal_year} Q{c.fiscal_quarter}':<12}{c.metric:<26}"
            f"{_plain(c.first.value, c.first.unit):>14}{_plain(c.latest.value, c.latest.unit):>14}"
            f"{change:>9}  {c.latest.form} filed {c.latest.filed.isoformat()}"
        )
    return 0


def _millions(row: QuarterFinancials, metric: str) -> str:
    found = row.metrics.get(metric)
    if found is None:
        return "-"
    text = f"{found.value / Decimal(1_000_000):,.0f}"
    return text + ("*" if found.derived else " ")


def _per_share(row: QuarterFinancials, metric: str) -> str:
    value = row.get(metric)
    return "-" if value is None else f"{value:.2f}"


def _percent(value: Decimal | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}"


def _plain(value: Decimal, unit: str) -> str:
    if unit == "USD":
        return f"{value / Decimal(1_000_000):,.1f}M"
    return f"{value:.2f}"


# News modules import httpx, feedparser and rapidfuzz, so commands import them when run,
# keeping CLI startup fast.


def _news_config(
    tickers: Sequence[str] | None,
) -> tuple[Universe, list[UniverseCompany], Path] | None:
    settings = get_settings()
    try:
        universe = load_universe(settings.universe_file)
        companies = universe.select_news(tickers)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return None
    return universe, companies, settings.feeds_file


async def _ingest_news(tickers: Sequence[str] | None) -> int:
    from talk_is_cheap.news.adapters import build_feeds, load_feeds_file
    from talk_is_cheap.news.linking import CompanyMatcher
    from talk_is_cheap.news.locks import POLL_LOCK, advisory_locks
    from talk_is_cheap.news.poll import FeedPollResult, new_client, poll_feeds

    config = _news_config(tickers)
    if config is None:
        return 1
    universe, companies, feeds_file = config
    try:
        feeds = build_feeds(
            load_feeds_file(feeds_file).feeds, companies, company_feeds_only=bool(tickers)
        )
    except (OSError, ValueError) as exc:
        print(f"cannot use {feeds_file}: {exc}", file=sys.stderr)
        return 1

    def report(r: FeedPollResult) -> None:
        if r.error is not None:
            print(f"{r.key:<34} FAILED: {r.error}", file=sys.stderr)
        elif r.not_modified:
            print(f"{r.key:<34} not modified")
        else:
            print(
                f"{r.key:<34} {r.items:>4} items, {r.new:>3} new ({r.clustered} joined a "
                f"story), {r.duplicates:>3} duplicates, {r.spam} spam"
            )

    engine = create_engine(get_settings())
    try:
        if not await _database_reachable(engine):
            return 1
        async with advisory_locks(engine, POLL_LOCK) as locked:
            if not locked:
                # A scheduled poll overlapping a slow one: not an error.
                print("another news poll is still running; skipped")
                return 0
            async with new_client(get_settings().news_user_agent) as client:
                results = await poll_feeds(
                    create_session_factory(engine),
                    feeds,
                    # Link to every company in the universe, not only the polled ones.
                    CompanyMatcher([c for c in universe.companies if c.name]),
                    client,
                    on_result=report,
                )
    finally:
        await engine.dispose()
    print(
        f"done: {sum(r.ok for r in results)}/{len(results)} feeds ok, "
        f"{sum(r.not_modified for r in results)} not modified, "
        f"{sum(r.new for r in results)} new articles, {sum(r.spam for r in results)} spam dropped"
    )
    return 0 if all(r.ok for r in results) else 1


async def _database_reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # report any connection failure as a clean CLI error
        print(f"database: unreachable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return False
    return True


@dataclass(frozen=True, slots=True)
class _BackfillRange:
    """Which windows to backfill: weeks from `since`, or the days around each earnings
    release."""

    since: date | None = None
    until: date | None = None
    days_before: int | None = None  # set for earnings windows
    days_after: int | None = None


def _run_backfill(tickers: Sequence[str] | None, span: _BackfillRange) -> int:
    """Run a backfill that stops cleanly on Ctrl+C or `kill` (SIGTERM), with a summary.

    Every answered part is already stored and every finished window recorded, so
    stopping loses at most the window in progress, and the same command resumes there.
    """
    seen: list[BackfillProgress] = []
    # `kill` stops it like Ctrl+C; signal handlers can only be set from the main thread.
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler) if main_thread else None
    try:
        return asyncio.run(_backfill_news(tickers, span, seen))
    except KeyboardInterrupt:
        print(
            f"\n{_stamp()} stopped: {_backfill_summary(seen)}. "
            "Running the same command again resumes where it stopped.",
            flush=True,
        )
        return 130
    finally:
        if main_thread:
            signal.signal(signal.SIGTERM, previous)


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _backfill_summary(seen: Sequence["BackfillProgress"]) -> str:
    if not seen:
        return "no window finished in this run"
    latest = {p.ticker: p for p in seen}
    last = seen[-1]
    new = sum(p.new_articles for p in latest.values())
    text = f"{last.fetched} windows fetched in {_duration(last.elapsed.total_seconds())}"
    if last.fetched:
        average = _duration(last.elapsed.total_seconds() / last.fetched)
        text += f" (average {average} per window)"
    started = f"{len(latest)} {'company' if len(latest) == 1 else 'companies'} started"
    return f"{text}, {new} new articles, {started}"


async def _backfill_news(
    tickers: Sequence[str] | None, span: _BackfillRange, seen: list["BackfillProgress"]
) -> int:
    from talk_is_cheap.news.adapters import load_feeds_file
    from talk_is_cheap.news.gdelt import (
        GdeltBackfill,
        backfill,
        earnings_windows,
        estimate,
        plan_backfill,
        polling_started,
        weekly_windows,
    )
    from talk_is_cheap.news.linking import CompanyMatcher
    from talk_is_cheap.news.locks import BACKFILL_LOCK, advisory_locks
    from talk_is_cheap.news.poll import new_client

    config = _news_config(tickers)
    if config is None:
        return 1
    universe, companies, feeds_file = config
    try:
        entries = [e for e in load_feeds_file(feeds_file).backfill if e.type == GdeltBackfill.type]
    except (OSError, ValueError) as exc:
        print(f"cannot use {feeds_file}: {exc}", file=sys.stderr)
        return 1
    if not entries:
        print(f"no gdelt backfill source in {feeds_file}", file=sys.stderr)
        return 1
    source = GdeltBackfill.from_entry(
        entries[0],
        on_attempt=lambda a: print(
            f"{_stamp()} GDELT {a.label} {a.start:%Y-%m-%d %H:%M} to {a.end:%Y-%m-%d %H:%M} "
            f"attempt {a.number}: {a.outcome} in {a.seconds:.1f}s",
            flush=True,
        ),
    )

    def report(p: "BackfillProgress") -> None:
        seen.append(p)
        eta = "-" if p.remaining is None else _duration(p.remaining.total_seconds())
        line = (
            f"{_stamp()} {p.ticker:<6} {p.windows_done}/{p.windows_total} windows, "
            f"{p.new_articles} new, {p.requests} requests, about {eta} left"
        )
        if p.skipped:
            line += f", {p.skipped} windows skipped"
        if p.error is not None:
            print(f"{line}; FAILED: {p.error}", file=sys.stderr, flush=True)
        else:
            print(line + (" (done)" if p.finished else ""), flush=True)

    engine = create_engine(get_settings())
    try:
        if not await _database_reachable(engine):
            return 1
        async with advisory_locks(engine, BACKFILL_LOCK) as locked:
            if not locked:
                print("another backfill or reprocessing run is in progress", file=sys.stderr)
                return 1
            sessions = create_session_factory(engine)
            not_after = None
            if span.days_before is not None and span.days_after is not None:
                async with sessions() as session:
                    windows = await earnings_windows(
                        session,
                        companies,
                        days_before=span.days_before,
                        days_after=span.days_after,
                    )
                    not_after = await polling_started(session)
                kind = "earnings windows"
            else:
                assert span.since is not None
                start = datetime.combine(span.since, day_time(), tzinfo=UTC)
                end = (
                    datetime.combine(span.until, day_time(), tzinfo=UTC)
                    if span.until
                    else datetime.now(UTC)
                )
                if start >= end:
                    print("--since must be before --until", file=sys.stderr)
                    return 1
                windows = [(c, s, e) for c in companies for s, e in weekly_windows(start, end)]
                kind = "weeks"
            planned = await plan_backfill(sessions, source, windows, not_after=not_after)
            requests, duration = estimate(source, planned)
            print(
                f"{_stamp()} {len(planned)} {kind} to fetch ({len(windows) - len(planned)} "
                f"already fetched{' or covered by RSS polling' if not_after else ''}), at "
                f"least {requests} requests, about {_duration(duration.total_seconds())} at "
                "the measured pace; busy windows take longer",
                flush=True,
            )
            async with new_client(get_settings().news_user_agent) as client:
                results = await backfill(
                    sessions,
                    source,
                    planned,
                    CompanyMatcher([c for c in universe.companies if c.name]),
                    client,
                    on_progress=report,
                )
    finally:
        await engine.dispose()
    skipped = sum(r.skipped for r in results)
    if skipped:
        print(
            f"\n{_stamp()} The GDELT API kept refusing requests for {skipped} windows, which "
            "were skipped. Running the same command later fetches them and resumes where "
            "it stopped.",
            file=sys.stderr,
        )
    failed = [r.ticker for r in results if r.error is not None]
    print(
        f"{_stamp()} done: {len(results) - len(failed)}/{len(results)} companies, "
        f"{_backfill_summary(seen)}",
        flush=True,
    )
    return 1 if failed or skipped else 0


async def _reprocess_news() -> int:
    from talk_is_cheap.news.adapters import feed_links
    from talk_is_cheap.news.gdelt import FEED_LINK, GdeltBackfill
    from talk_is_cheap.news.linking import CompanyMatcher
    from talk_is_cheap.news.locks import BACKFILL_LOCK, POLL_LOCK, advisory_locks
    from talk_is_cheap.news.store import reprocess

    config = _news_config(None)
    if config is None:
        return 1
    universe = config[0]
    engine = create_engine(get_settings())
    try:
        if not await _database_reachable(engine):
            return 1
        async with advisory_locks(engine, POLL_LOCK, BACKFILL_LOCK) as locked:
            if not locked:
                print("a news poll or backfill is running; try again later", file=sys.stderr)
                return 1
            async with create_session_factory(engine)() as session, session.begin():
                result = await reprocess(
                    session,
                    CompanyMatcher([c for c in universe.companies if c.name]),
                    feed_links() | {GdeltBackfill.type: FEED_LINK},
                )
    finally:
        await engine.dispose()
    print(
        f"done: {result.articles} articles, {result.spam_removed} spam removed, "
        f"{result.titles_changed} titles cleaned, clusters {result.clusters_before} -> "
        f"{result.clusters_after}, {result.links} links"
    )
    return 0


async def _feeds(backfill: bool) -> int:
    from talk_is_cheap.news.queries import backfill_progress, feed_health

    now = datetime.now(UTC)
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            if backfill:
                sources = await backfill_progress(session)
            else:
                rows = await feed_health(session, now)
    finally:
        await engine.dispose()
    if backfill:
        return _print_backfill(sources, now)
    if not rows:
        print("no feeds polled yet; run `talk-is-cheap ingest news`")
        return 0
    print(
        f"{'FEED':<34}{'STATUS':<9}{'LAST SUCCESS':<14}{'LAST NEW':<14}{'FAILS':>6}"
        f"{'ARTICLES':>10}  ERROR"
    )
    for r in rows:
        print(
            f"{r.key:<34}{r.status:<9}{_ago(r.last_success_at, now):<14}"
            f"{_ago(r.last_new_item_at, now):<14}{r.consecutive_failures:>6}{r.articles:>10}  "
            f"{(r.last_error or '')[:60]}"
        )
    counts = {s: sum(r.status == s for r in rows) for s in ("ok", "stale", "failing", "new")}
    print("\n" + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
    return 0


async def _news(
    ticker: str,
    since: date | None,
    around_earnings: str | None,
    min_confidence: Decimal,
    limit: int,
) -> int:
    from talk_is_cheap.news.queries import company_news, earnings_window

    config = _news_config([ticker])
    if config is None:
        return 1
    quarter = None
    if around_earnings is not None:
        try:
            quarter = FiscalQuarter.parse(around_earnings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    start = datetime.combine(since, day_time(), tzinfo=UTC) if since else None
    end = None
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            if quarter is not None:
                window = await earnings_window(session, ticker, quarter)
                if window is None:
                    print(
                        f"{ticker.upper()} {quarter.label}: no earnings release loaded "
                        "(run `talk-is-cheap ingest edgar` and `ingest filings`)",
                        file=sys.stderr,
                    )
                    return 1
                start, end = window
                print(
                    f"{ticker.upper()} {quarter.label}: earnings release filed "
                    f"{_utc(start)}; news until {_utc(end)}\n"
                )
            stories = await company_news(
                session,
                ticker,
                since=start,
                until=end,
                min_confidence=min_confidence,
                limit=None if quarter else limit,
            )
    finally:
        await engine.dispose()
    if not stories:
        print(f"{ticker.upper()}: no news stored for this period")
        return 0
    for story in stories:
        mark = "~" if story.time_precision != "published" else " "
        others = f"  (+{len(story.others)}: {', '.join(story.others[:3])}"
        others += ", ...)" if len(story.others) > 3 else ")"
        print(
            f"{_utc(story.published_at)}{mark} {(story.publisher or '?')[:22]:<23}"
            f"{story.title}{others if story.others else ''}"
        )
    if any(s.time_precision != "published" for s in stories):
        print("\n~ = time first seen by GDELT or by this system, not the publish time")
    return 0


def _print_backfill(sources: Sequence["BackfillSource"], now: datetime) -> int:
    if not sources:
        print("no backfill run yet; run `talk-is-cheap ingest news --backfill --since ...`")
        return 0
    print(
        f"{'SOURCE':<14}{'WEEKS':>6}  {'FIRST TO LAST WEEK':<23}{'ARTICLES':>9}{'TRUNC':>6}  "
        f"{'LAST SUCCESS':<14}{'FAILS':>5}  ERROR"
    )
    for b in sources:
        covered = "-"
        if b.first_window is not None and b.last_window is not None:
            covered = f"{b.first_window:%Y-%m-%d} to {b.last_window:%Y-%m-%d}"
        print(
            f"{b.key:<14}{b.windows:>6}  {covered:<23}{b.articles:>9}{b.truncated:>6}  "
            f"{_ago(b.last_success_at, now):<14}{b.consecutive_failures:>5}  "
            f"{(b.last_error or '')[:70]}"
        )
    return 0


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    return _duration((now - moment).total_seconds()) + " ago"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 10:
        return f"{minutes}m{int(seconds % 60):02d}s"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    return f"{hours // 24}d{hours % 24}h"


# Indexing and search load the embedding library and models only when run.


def _company_names() -> dict[str, str] | None:
    settings = get_settings()
    try:
        universe = load_universe(settings.universe_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return None
    return {c.ticker: c.name for c in universe.companies if c.name}


async def _index(tickers: Sequence[str] | None, rebuild: bool) -> int:
    from talk_is_cheap.retrieval.encoders import load_embedder
    from talk_is_cheap.retrieval.index import IndexResult, index

    names = _company_names()
    if names is None:
        return 1
    settings = get_settings()
    engine = create_engine(settings)

    def report(r: IndexResult) -> None:
        if r.error is not None:
            print(f"{r.name:<6} FAILED: {r.error}", file=sys.stderr, flush=True)
            return
        print(
            f"{r.name:<6} {r.added:>5} added, {r.removed:>5} removed, {r.unchanged:>5} "
            f"unchanged, {r.embedded:>5} embedded, {r.seconds:6.1f}s",
            flush=True,
        )

    try:
        if not await _database_reachable(engine):
            return 1
        async with create_session_factory(engine)() as session:
            loaded = set(await session.scalars(text("SELECT ticker FROM companies")))
        unknown = sorted({t.upper() for t in tickers or []} - loaded)
        if unknown:
            print(f"not loaded: {', '.join(unknown)}", file=sys.stderr)
            return 1
        embedder = load_embedder(settings)
        print(f"embedding model: {embedder.model_name}", flush=True)
        results = await index(
            engine,
            embedder,
            names,
            tickers=tickers,
            rebuild=rebuild,
            on_result=report,
            on_note=lambda note: print(note, flush=True),
        )
    finally:
        await engine.dispose()
    ok = [r for r in results if r.ok]
    print(
        f"done: {sum(r.added for r in ok)} added, {sum(r.removed for r in ok)} removed, "
        f"{sum(r.unchanged for r in ok)} unchanged, {sum(r.embedded for r in ok)} embedded, "
        f"largest chunk {max((r.largest for r in ok), default=0)} tokens, "
        f"{sum(r.seconds for r in results):.0f}s"
    )
    return 0 if len(ok) == len(results) else 1


async def _search(args: argparse.Namespace) -> int:
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker
    from talk_is_cheap.retrieval.search import Searcher, parse_periods

    periods = None
    if args.periods:
        try:
            periods = parse_periods(args.periods)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            searcher = Searcher(
                session, lambda: load_embedder(settings), lambda: load_reranker(settings)
            )
            started = time.monotonic()
            results = await searcher.search(
                args.query,
                tickers=args.ticker,
                periods=periods,
                since=args.since,
                until=args.until,
                sources=args.sources,
                doc_types=args.doc_types,
                section_types=args.types,
                k=args.k,
                mode=args.mode,
            )
            seconds = time.monotonic() - started
    finally:
        await engine.dispose()
    if not results:
        print("no passages found")
        return 0
    for rank, r in enumerate(results, start=1):
        if r.source == "news":
            where = f"{', '.join(r.tickers) or '-'} news {_utc(r.published_at)} {r.publisher or ''}"
        else:
            label = r.period.label if r.period else "-"
            where = (
                f"{', '.join(r.tickers)} {label} {r.doc_type} {r.section_type}: "
                f"{r.section_title or '(untitled)'}"
            )
        print(f"{rank:>2}. [{r.score:.4f}] {where}")
        print(f"    {r.citation}")
        body = " ".join(r.text.split())
        print(f"    {body[: args.width]}{'…' if len(body) > args.width else ''}\n")
    print(f"{len(results)} passages in {seconds:.2f}s ({args.mode})")
    return 0


async def _eval_retrieval(
    questions_file: Path, modes: Sequence[str] | None, filtered: bool, details: bool
) -> int:
    from talk_is_cheap.retrieval.encoders import load_embedder, load_reranker
    from talk_is_cheap.retrieval.evaluate import (
        CONFIGS,
        DEPTH,
        KINDS,
        Config,
        Score,
        evaluate,
        load_questions,
        prepare_text_only_vectors,
    )

    try:
        questions = load_questions(questions_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {questions_file}: {exc}", file=sys.stderr)
        return 1
    configs = [
        Config(c.mode, c.context_header, filtered)
        for c in CONFIGS
        if not modes or c.mode.value in modes
    ]
    settings = get_settings()
    engine = create_engine(settings)

    def report(score: Score) -> None:
        columns = "".join(
            f"{score.recall(5, kind):>7.3f}{score.recall(10, kind):>7.3f}{score.mrr(kind):>7.3f}"
            for kind in (None, *KINDS)
        )
        print(f"{score.config.label:<30}{columns}{score.mean_ms:>9.0f}", flush=True)

    try:
        if not await _database_reachable(engine):
            return 1
        sessions = create_session_factory(engine)
        embedder = load_embedder(settings)
        reranker = load_reranker(settings)
        if any(not c.context_header for c in configs):
            print("embedding chunk text without headers where not done yet ...", flush=True)
            made = await prepare_text_only_vectors(engine, sessions, embedder)
            print(f"{made} texts embedded", flush=True)
        counts = ", ".join(f"{sum(q.kind == k for q in questions)} {k}" for k in KINDS)
        print(
            f"{len(questions)} questions ({counts}), {embedder.model_name}, "
            f"reranker {reranker.model_name}\n"
        )
        groups = "".join(f"{label:<21}" for label in ("ALL", *(k.upper() for k in KINDS)))
        print(f"{'':<30}{groups}")
        print(
            f"{'CONFIGURATION':<30}" + f"{'R@5':>7}{'R@10':>7}{'MRR':>7}" * 3 + f"{'MS/QUERY':>9}"
        )
        scores = await evaluate(sessions, embedder, reranker, questions, configs, report)
    finally:
        await engine.dispose()
    if details:
        print(f"\nrank of the first correct passage (- = not in the top {DEPTH})\n")
        print(f"{'QUESTION':<34}" + "".join(f"{s.config.label[:10]:>11}" for s in scores))
        for question in questions:
            ranks = [s.ranks[question.id] for s in scores]
            print(f"{question.id[:33]:<34}" + "".join(f"{r or '-':>11}" for r in ranks))
    return 0


# Guidance extraction imports LangChain and a provider SDK only when a model is built.


async def _llm_budget(
    sessions: "async_sessionmaker[AsyncSession]", pricing: "Pricing", command: str
) -> "Budget":
    from talk_is_cheap.spend import command_budget

    return await command_budget(sessions, get_settings(), pricing, command)


def _budget_line(budget: "Budget") -> str:
    from talk_is_cheap.spend import budget_line

    return budget_line(budget)


async def _extract_guidance(
    tickers: Sequence[str] | None, model: str | None, dry: bool, missing_only: bool
) -> int:
    from talk_is_cheap.guidance.extract import (
        ExtractResult,
        company_ciks,
        dry_run,
        extract_guidance,
    )
    from talk_is_cheap.guidance.llm import (
        CacheOnlyModel,
        ExtractionModel,
        LlmError,
        Pricing,
        ResponseCache,
        build_model,
        model_label,
        pricing_for,
    )
    from talk_is_cheap.guidance.matching import load_split_config, sync_splits

    names = _company_names()
    if names is None:
        return 1
    settings = get_settings()
    label = model_label(settings, model)
    pricing = pricing_for(label, settings)
    cache = ResponseCache(settings.llm_cache_dir)
    engine = create_engine(settings)
    try:
        if not await _database_reachable(engine):
            return 1
        sessions = create_session_factory(engine)
        async with sessions() as session:
            try:
                ciks = await company_ciks(session, tickers)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
        plan = await dry_run(
            sessions, label, cache, pricing, ciks=list(ciks.values()), names=names,
            missing_only=missing_only,
        )  # fmt: skip
        if dry:
            _print_plan(plan, label, settings.llm_cost_cap)
            if pricing is not None:
                print(_budget_line(await _llm_budget(sessions, pricing, "extract guidance")))
            return 0
        if plan.calls and pricing is None:
            print(
                f"no price known for {label}: set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE "
                "(US dollars per million tokens)",
                file=sys.stderr,
            )
            return 1
        extractor: ExtractionModel
        if plan.calls == 0:
            extractor = CacheOnlyModel(label)
        else:
            try:
                extractor = build_model(settings, model)
            except LlmError as exc:
                print(f"{exc}; {plan.calls} documents need a model call", file=sys.stderr)
                return 1
        budget = await _llm_budget(
            sessions, pricing or Pricing(Decimal(0), Decimal(0)), "extract guidance"
        )
        async with sessions() as session, session.begin():
            await sync_splits(session, load_split_config())
        print(
            f"model {label}, {plan.calls} calls planned (estimated "
            f"${plan.estimated_cost or 0:.2f}), cost cap ${settings.llm_cost_cap}",
            flush=True,
        )

        def report(r: ExtractResult) -> None:
            if not r.documents:
                print(
                    f"{r.ticker:<6} {r.releases} releases, no outlook with guidance: "
                    f"{r.rows} rows (expected)",
                    flush=True,
                )
                return
            verdicts = ", ".join(
                f"{n} {v}"
                for v, n in sorted(r.verdicts.items())
                if v not in ("needs_review", "wrong")
            )
            if r.verdicts.get("needs_review") or r.verdicts.get("wrong"):
                verdicts += (
                    f"; {r.verdicts.get('needs_review', 0)} need review, "
                    f"{r.verdicts.get('wrong', 0)} reviewed as wrong"
                )
            reprocessed = f"{r.reprocessed} reprocessed, " if r.reprocessed else ""
            print(
                f"{r.ticker:<6} {r.documents} documents ({r.up_to_date} up to date, "
                f"{reprocessed}{r.cached} cached, {r.called} called), {r.items} items, {r.dropped} "
                f"dropped, {r.rows} rows ({verdicts or 'none'}), "
                f"{r.usage.input_tokens:,}/{r.usage.output_tokens:,} tokens, ${r.cost:.4f}, "
                f"{r.seconds:.0f}s",
                file=sys.stdout if r.ok else sys.stderr,
                flush=True,
            )
            for quote in r.dropped_quotes:
                print(f"       dropped (quote not found): {quote!r}", flush=True)
            for error in r.errors:
                print(f"       FAILED {error}", file=sys.stderr, flush=True)

        results = await extract_guidance(
            sessions, extractor, cache, budget, ciks=list(ciks.values()), names=names,
            missing_only=missing_only, on_result=report,
        )  # fmt: skip
    finally:
        await engine.dispose()
    rows = sum(r.rows for r in results)
    dropped = sum(r.dropped for r in results)
    print(
        f"done: {rows} guidance rows, {dropped} items dropped by grounding, {budget.calls} "
        f"model calls, {budget.usage.input_tokens:,} input and {budget.usage.output_tokens:,} "
        f"output tokens, ${budget.spent:.4f}"
    )
    print(_budget_line(budget))
    return 0 if all(r.ok for r in results) else 1


def _print_plan(plan: "DryRun", label: str, cap: Decimal) -> None:
    print(f"dry run: model {label}, no calls made\n")
    print(f"{'TICKER':<7}{'RELEASES':>9}{'DOCS':>6}{'UP TO DATE':>11}{'REPROCESS':>10}{'CACHED':>8}"
          f"{'CALLS':>7}{'EST. IN':>10}{'EST. OUT':>10}")  # fmt: skip
    for p in plan.plans:
        estimated = p.estimated
        print(
            f"{p.ticker:<7}{p.releases:>9}{len(p.documents):>6}{p.count('up_to_date'):>11}"
            f"{p.count('reprocess'):>10}{p.count('cached'):>8}{p.count('call'):>7}{estimated.input_tokens:>10,}"
            f"{estimated.output_tokens:>10,}"
        )
    total = plan.estimated
    cost = plan.estimated_cost
    print(
        f"\n{plan.calls} calls, about {total.input_tokens:,} input and at most "
        f"{total.output_tokens:,} output tokens, "
        + (f"estimated cost at most ${cost:.2f}" if cost is not None else "no price known")
        + f" (cost cap ${cap})"
    )


def _amount(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "pct":
        return f"{value.quantize(Decimal('0.01')).normalize():f}%"
    if unit == "usd_per_share":
        return f"${value:.2f}" if value >= 0 else f"-${-value:.2f}"
    if unit == "usd":
        sign = "-" if value < 0 else ""
        size = abs(value)
        if size >= 10**9:
            return f"{sign}${size / 10**9:,.3f}".rstrip("0").rstrip(".") + "B"
        return f"{sign}${size / 10**6:,.1f}M"
    return f"{value.normalize():,f}"


def _guided(row: "Guidance") -> str:
    if row.low is not None and row.high is not None:
        text = f"{_amount(row.low, row.unit)} to {_amount(row.high, row.unit)}"
    elif row.point is not None:
        text = f"~{_amount(row.point, row.unit)}"
    elif row.low is not None:
        text = f">= {_amount(row.low, row.unit)}"
    elif row.high is not None:
        text = f"<= {_amount(row.high, row.unit)}"
    else:
        return (row.qualitative or row.value_text or "")[:40]
    return text


def _difference(row: "Guidance") -> str:
    if row.difference is None:
        return ""
    if row.unit == "pct":
        return f"{row.difference:+.2f}pp"
    sign = "+" if row.difference >= 0 else "-"
    text = sign + _amount(abs(row.difference), row.unit)
    if row.percent_difference is not None:
        text += f" ({row.percent_difference:+.1f}%)"
    return text


def _metric_label(row: "Guidance") -> str:
    label = row.metric
    if row.segment:
        label += f" [{row.segment[:20]}]"
    if row.measure != "level":
        label += {"yoy_growth": " y/y", "sequential_growth": " q/q",
                  "share_of_revenue": " %rev"}[row.measure]  # fmt: skip
    if row.constant_currency:
        label += " cc"
    if row.basis != "unspecified":
        label += " (GAAP)" if row.basis == "gaap" else " (non-GAAP)"
    return label


async def _guidance(ticker: str, metric: str | None, limit: int | None, flagged_only: bool) -> int:
    from sqlalchemy import select

    from talk_is_cheap.db.models import Company
    from talk_is_cheap.guidance.matching import counts_in_verdicts, needs_review
    from talk_is_cheap.guidance.queries import company_guidance

    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            names = {
                cik: symbol
                for cik, symbol in (await session.execute(select(Company.cik, Company.ticker)))
            }
            company = None
            if ticker.lower() != "all":
                company = await find_company(session, ticker)
                if company is None:
                    print(f"{ticker.upper()} is not loaded", file=sys.stderr)
                    return 1
            rows = await company_guidance(
                session, company.cik if company else None, metric, flagged_only=flagged_only
            )
    finally:
        await engine.dispose()
    who = f"{company.name} ({company.ticker})" if company else "All companies"
    if not rows:
        print(f"{who}: no guidance rows" + (" flagged" if flagged_only else "")
              + "; run `talk-is-cheap extract guidance`" * (not flagged_only))  # fmt: skip
        return 0
    print(f"{who}: guidance, newest first. Actuals as first reported; pp = percentage "
          "points. REVIEW marks a guided value implausible against the company's prior "
          "period; such rows and rows reviewed as wrong are left out of the counts.\n")  # fmt: skip
    print(
        f"{'ID':>6}  {'' if company else 'TICKER  '}{'ISSUED':<10}{'TARGET':<10}{'METRIC':<34}"
        f"{'GUIDED':<26}{'ACTUAL':<13}{'VERDICT':<15}DIFFERENCE"
    )
    for found in rows[:limit] if limit else rows:
        row = found.guidance
        target = "-"
        if row.target_fiscal_year is not None:
            target = f"FY{row.target_fiscal_year}" + (
                f"Q{row.target_fiscal_quarter}" if row.target_fiscal_quarter else ""
            )
        verdict = row.verdict if row.verdict != "not_comparable" else "not comparable"
        mark = "REVIEW " if needs_review(row) else "WRONG  " if row.review == "wrong" else ""
        print(
            f"{row.id:>6}  {'' if company else f'{names.get(row.cik, "?"):<8}'}"
            f"{f'FY{row.fiscal_year}Q{row.fiscal_quarter}':<10}{target:<10}"
            f"{_metric_label(row)[:33]:<34}{_guided(row)[:25]:<26}"
            f"{_amount(row.actual, row.unit):<13}{mark}{verdict:<15}{_difference(row)}"
        )
        pad = " " * (8 if company else 16)
        if row.plausibility == "flagged":
            state = {None: "needs review", "ok": "reviewed: ok", "wrong": "reviewed: wrong"}
            print(f"{pad}{state[row.review]} ({row.plausibility_rule}): {row.plausibility_note}")
        elif row.review == "wrong":
            print(f"{pad}reviewed: wrong")
        notes = []
        if row.verdict in ("not_comparable", "pending") and row.verdict_reason:
            notes.append(row.verdict_reason)
        if row.basis_assumed:
            notes.append("basis not stated, compared as GAAP")
        if notes:
            print(f"{pad}{'; '.join(notes)}")
        if row.conditions:
            print(f"{pad}conditions: {row.conditions}")
        for source in found.sources[:1]:
            print(f"{pad}{source.url} chars {source.char_start}-{source.char_end}")
    counted = [found.guidance for found in rows if counts_in_verdicts(found.guidance)]
    tally = {v: sum(r.verdict == v for r in counted) for v in
             ("beat", "met", "missed", "pending", "not_comparable")}  # fmt: skip
    flagged = sum(needs_review(found.guidance) for found in rows)
    wrong = sum(found.guidance.review == "wrong" for found in rows)
    print(
        f"\n{len(rows)} rows: "
        + ", ".join(f"{n} {v.replace('_', ' ')}" for v, n in tally.items())
        + f"; {flagged} need review, {wrong} reviewed as wrong (not counted)"
    )
    return 0


async def _review_guidance(row_id: str, decision: str) -> int:
    from talk_is_cheap.guidance.review import review

    if not row_id.isdigit():
        print(f"guidance review takes a row ID, got {row_id!r}", file=sys.stderr)
        return 2
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session, session.begin():
            try:
                summary = await review(session, int(row_id), decision)
            except LookupError as exc:
                print(exc, file=sys.stderr)
                return 1
    finally:
        await engine.dispose()
    print(summary)
    return 0


async def _eval_guidance(labels_file: Path, models: Sequence[str] | None, details: bool) -> int:
    from talk_is_cheap.guidance.evaluate import KINDS, evaluate, labeled_documents, load_labels
    from talk_is_cheap.guidance.llm import (
        LlmError,
        ResponseCache,
        build_model,
        model_label,
        pricing_for,
    )

    try:
        labels = load_labels(labels_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {labels_file}: {exc}", file=sys.stderr)
        return 1
    names = _company_names()
    if names is None:
        return 1
    settings = get_settings()
    cache = ResponseCache(settings.llm_cache_dir)
    engine = create_engine(settings)
    scores = []
    try:
        if not await _database_reachable(engine):
            return 1
        try:
            documents = await labeled_documents(create_session_factory(engine), labels, names)
        except ValueError as exc:
            print(f"cannot use {labels_file}: {exc}", file=sys.stderr)
            return 1
        sections = sum(len(d.source.sections) for _, d, _ in documents)
        companies = len({label.ticker for label, _, _ in documents})
        print(
            f"{len(documents)} documents ({sections} outlook sections, {companies} companies), "
            f"{sum(len(label.items) for label, _, _ in documents)} labeled items\n"
        )
        groups = "".join(f"{g:<21}" for g in ("ALL", *(k.upper() for k in KINDS)))
        print(f"{'':<26}{groups}")
        print(f"{'MODEL':<26}" + f"{'P':>7}{'R':>7}{'F1':>7}" * 3
              + f"{'FIGURES':>8}{'PERIOD':>7}{'COND':>6}{'SPUR':>6}{'DROP':>5}{'ERR':>4}"
              f"{'IN/DOC':>8}{'OUT/DOC':>8}{'$/DOC':>8}")  # fmt: skip
        for name in models or [settings.llm_model]:
            label = model_label(settings, name)
            pricing = pricing_for(label, settings)
            if pricing is None:
                print(f"{label}: no price known; set LLM_INPUT_PRICE and LLM_OUTPUT_PRICE")
                return 1
            try:
                model = build_model(settings, name)
            except LlmError as exc:
                print(exc, file=sys.stderr)
                return 1
            budget = await _llm_budget(create_session_factory(engine), pricing, "eval guidance")
            score = await evaluate(documents, model, cache, budget)
            scores.append(score)
            n = max(len(score.documents), 1)
            columns = "".join(
                f"{score.precision(k):>7.3f}{score.recall(k):>7.3f}{score.f1(k):>7.3f}"
                for k in (None, *KINDS)
            )
            print(
                f"{label[:25]:<26}{columns}"
                f"{score.figures_exact:>8.3f}{score.period_accuracy:>7.3f}"
                f"{score.conditions_captured:>6.2f}{score.conditions_spurious:>6.2f}"
                f"{score.dropped:>5}{score.errors:>4}"
                f"{score.usage.input_tokens // n:>8,}{score.usage.output_tokens // n:>8,}"
                f"{score.cost / n:>8.4f}",
                flush=True,
            )
            if budget.calls:
                print(f"{'':<26}this run: {budget.calls} calls, ${budget.spent:.4f}")
                print(f"{'':<26}{_budget_line(budget)}")
    finally:
        await engine.dispose()
    if details:
        for score in scores:
            print(f"\n== {score.model}")
            for d in score.documents:
                wrong_pairs = [
                    (lab, got) for lab, got in d.matched
                    if (lab.target, lab.low, lab.high, lab.point, lab.unit)
                    != (got.target, got.low, got.high, got.point, got.unit)
                ]  # fmt: skip
                if not (d.missed or d.extra or d.error or wrong_pairs):
                    continue
                print(f"{d.id}: {len(d.matched)}/{d.labeled} matched, {len(d.extra)} extra"
                      + (f", ERROR {d.error}" if d.error else ""))  # fmt: skip
                for item in d.missed:
                    target = item.target.label if item.target else "-"
                    print(f"   missed {item.metric}/{item.basis}/{item.measure} {target}: "
                          f"{item.quote[:90]!r}")  # fmt: skip
                for extra in d.extra:
                    target = extra.target.label if extra.target else "-"
                    quote = extra.sources[0].quote[:90] if extra.sources else ""
                    print(f"   extra  {extra.metric}/{extra.basis}/{extra.measure} {target}: "
                          f"{quote!r}")  # fmt: skip
                for label_item, got in d.matched:
                    wrong = []
                    if label_item.target != got.target:
                        wrong.append(f"period {got.target} != {label_item.target}")
                    if (label_item.low, label_item.high, label_item.point, label_item.unit) != (
                        got.low, got.high, got.point, got.unit
                    ):  # fmt: skip
                        wrong.append(
                            f"figures {got.low}/{got.high}/{got.point} {got.unit} != "
                            f"{label_item.low}/{label_item.high}/{label_item.point} "
                            f"{label_item.unit}"
                        )
                    if wrong:
                        print(f"   wrong  {label_item.metric}/{label_item.basis}: "
                              + "; ".join(wrong))  # fmt: skip
    return 0


async def _spend() -> int:
    from talk_is_cheap.spend import spend_report

    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            report = await spend_report(session)
    finally:
        await engine.dispose()
    budget = settings.llm_budget_usd
    print(
        f"LLM spending: ${report.total:.4f} of ${budget:.2f} budget (LLM_BUDGET_USD), "
        f"${max(budget - report.total, Decimal(0)):.4f} remaining, {report.calls} calls\n"
    )
    for title, lines in (("COMMAND", report.by_command), ("MODEL", report.by_model)):
        print(f"{title:<28}{'CALLS':>7}{'INPUT':>12}{'OUTPUT':>12}{'COST':>11}")
        for line in lines:
            print(
                f"{line.key[:27]:<28}{line.calls:>7}{line.input_tokens:>12,}"
                f"{line.output_tokens:>12,}{f'${line.cost:.4f}':>11}"
            )
        if not lines:
            print("(no calls recorded)")
        print()
    if report.notes:
        print("Not counted toward the budget:")
        for note in report.notes:
            print(f"  {note.model}: ${note.cost:.4f} ({note.note})")
    return 0


_TICKERS_HELP = "comma-separated tickers from universe.yaml (default: all)"


def _tickers(text: str) -> list[str]:
    return [t for t in text.split(",") if t.strip()]


# Search mode `search` uses unless --mode is given, chosen by the retrieval benchmark
# (docs/retrieval.md).
DEFAULT_SEARCH_MODE = "rerank"
# guidance/schema.py: Metric, repeated so the CLI does not import pydantic models.
GUIDANCE_METRICS = ("revenue", "gross_margin", "operating_margin", "operating_expenses",
                    "operating_income", "eps", "capex", "tax_rate", "other")  # fmt: skip
DOC_TYPES = ("press_release", "earnings_exhibit", "10q", "10k", "deliveries_report",
             "other_exhibit", "news")  # fmt: skip


def _choices(*allowed: str) -> Callable[[str], list[str]]:
    def parse(text: str) -> list[str]:
        values = [v.strip() for v in text.split(",") if v.strip()]
        unknown = [v for v in values if v not in allowed]
        if unknown or not values:
            raise argparse.ArgumentTypeError(
                f"{', '.join(unknown) or text!r}: choose from {', '.join(allowed)}"
            )
        return values

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talk-is-cheap")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("db-check", help="check database connectivity, pgvector and migrations")

    ingest = commands.add_parser("ingest", help="load data from external sources")
    sources = ingest.add_subparsers(dest="source", required=True)
    edgar = sources.add_parser("edgar", help="companies, filings and financials from SEC EDGAR")
    edgar.add_argument("--tickers", type=_tickers, help=_TICKERS_HELP)
    filings = sources.add_parser(
        "filings", help="text and sections of loaded earnings releases, 10-Qs and 10-Ks"
    )
    filings.add_argument("--tickers", type=_tickers, help=_TICKERS_HELP)
    news = sources.add_parser(
        "news", help="one poll of the news feeds in feeds.yaml, or a GDELT backfill"
    )
    news.add_argument(
        "--tickers",
        type=_tickers,
        help="comma-separated tickers from universe.yaml (default: all, plus market feeds)",
    )
    news.add_argument(
        "--backfill", action="store_true", help="fetch past news from GDELT instead of polling"
    )
    news.add_argument(
        "--reprocess",
        action="store_true",
        help="apply the current rules and universe.yaml to stored articles: drop spam, "
        "clean titles, rebuild clusters and company links",
    )
    news.add_argument(
        "--since", type=date.fromisoformat, help="weekly backfill start, e.g. 2024-01-01"
    )
    news.add_argument("--until", type=date.fromisoformat, help="weekly backfill end (default: now)")
    news.add_argument(
        "--around-earnings",
        action="store_true",
        help="backfill the days around each stored earnings release (recommended)",
    )
    news.add_argument(
        "--days-before", type=int, default=1, help="earnings window: days before (default 1)"
    )
    news.add_argument(
        "--days-after", type=int, default=3, help="earnings window: days after (default 3)"
    )

    commands.add_parser("companies", help="list loaded companies")

    feeds = commands.add_parser("feeds", help="health of every polled news feed")
    feeds.add_argument(
        "--backfill", action="store_true", help="progress of backfill sources (GDELT) instead"
    )

    company_news = commands.add_parser(
        "news", help="a company's news, newest first, one line per story"
    )
    company_news.add_argument("ticker")
    company_news.add_argument("--since", type=date.fromisoformat, help="e.g. 2026-10-01")
    company_news.add_argument(
        "--around-earnings",
        metavar="QUARTER",
        help="the 48 hours after the quarter's earnings release was filed, e.g. FY2027Q2",
    )
    company_news.add_argument(
        "--min-confidence",
        type=Decimal,
        default=Decimal("0.5"),
        help="lowest link confidence shown (default 0.5; per-company feeds alone give 0.3)",
    )
    company_news.add_argument("--limit", type=int, default=50, help="stories shown (default 50)")

    indexer = commands.add_parser(
        "index", help="chunk and embed filing sections and news for search (incremental)"
    )
    indexer.add_argument("--tickers", type=_tickers, help="comma-separated tickers (default: all)")
    indexer.add_argument(
        "--rebuild", action="store_true", help="drop the selected chunks and embed them again"
    )

    searching = commands.add_parser("search", help="passages of filings and news for a question")
    searching.add_argument("query")
    searching.add_argument(
        "--ticker", type=_tickers, help="company, or comma-separated companies (e.g. NVDA)"
    )
    searching.add_argument(
        "--periods", help="fiscal quarters or ranges, e.g. FY2027Q2 or FY2026Q1..FY2027Q2"
    )
    searching.add_argument(
        "--since", type=date.fromisoformat, help="from this date, e.g. 2026-09-01"
    )
    searching.add_argument("--until", type=date.fromisoformat, help="through this date")
    searching.add_argument(
        "--sources", type=_choices("filing", "news"), help="filing, news or filing,news"
    )
    searching.add_argument(
        "--types",
        type=_choices(*(t.value for t in SectionType)),
        help="section types, e.g. outlook,mdna",
    )
    searching.add_argument(
        "--doc-types",
        type=_choices(*DOC_TYPES),
        help="document types, e.g. press_release,10k or news",
    )
    searching.add_argument("--k", type=int, default=8, help="passages shown (default 8)")
    searching.add_argument(
        "--mode",
        choices=["keyword", "vector", "hybrid", "rerank"],
        default=DEFAULT_SEARCH_MODE,
        help=f"(default {DEFAULT_SEARCH_MODE})",
    )
    searching.add_argument(
        "--width", type=int, default=600, help="characters of each passage shown (default 600)"
    )

    evaluation = commands.add_parser("eval", help="benchmarks")
    benchmarks = evaluation.add_subparsers(dest="benchmark", required=True)
    retrieval = benchmarks.add_parser(
        "retrieval", help="recall@5, recall@10 and MRR of each search mode on labeled questions"
    )
    retrieval.add_argument(
        "--questions",
        type=Path,
        default=Path("evals/retrieval/questions.jsonl"),
        help="labeled questions (default evals/retrieval/questions.jsonl)",
    )
    retrieval.add_argument(
        "--modes",
        type=_choices("keyword", "vector", "hybrid", "rerank"),
        help="modes to run (default: all, each with and without the context header)",
    )
    retrieval.add_argument(
        "--filtered",
        action="store_true",
        help="also filter each question by its own period (filings) or dates (news)",
    )
    retrieval.add_argument(
        "--details", action="store_true", help="also print each question's rank per mode"
    )

    guidance_eval = benchmarks.add_parser(
        "guidance",
        help="precision, recall, figures and periods of guidance extraction on labeled "
        "outlook sections",
    )
    guidance_eval.add_argument(
        "--labels",
        type=Path,
        default=Path("evals/guidance/labels.jsonl"),
        help="labeled documents (default evals/guidance/labels.jsonl)",
    )
    guidance_eval.add_argument(
        "--models",
        type=_tickers,
        help="comma-separated models of the configured provider (default LLM_MODEL)",
    )
    guidance_eval.add_argument(
        "--details", action="store_true", help="also list missed, extra and wrong items"
    )

    agent_eval = benchmarks.add_parser(
        "agent", help="the agent's smoke set: facts, citations, grounding and refusals"
    )
    agent_eval.add_argument(
        "--questions",
        type=Path,
        default=Path("evals/agent/smoke.jsonl"),
        help="smoke questions (default evals/agent/smoke.jsonl)",
    )
    agent_eval.add_argument(
        "--only", type=_tickers, help="comma-separated question ids (default: all)"
    )
    agent_eval.add_argument(
        "--model", help="model of the configured provider (default LLM_MODEL, Haiku 5.5)"
    )
    agent_eval.add_argument(
        "--dry-run",
        action="store_true",
        help="run everything with a scripted fake model; spends nothing",
    )
    agent_eval.add_argument(
        "--max-cost", type=Decimal, help="US dollars this run may spend (default LLM_COST_CAP)"
    )
    agent_eval.add_argument(
        "--results",
        type=Path,
        default=Path("evals/agent/results"),
        help="where the results JSON is written (default evals/agent/results)",
    )

    extract = commands.add_parser("extract", help="extract structured data with an LLM")
    extracted = extract.add_subparsers(dest="target", required=True)
    guidance_extract = extracted.add_parser(
        "guidance", help="guidance from the outlook sections of earnings releases (incremental)"
    )
    guidance_extract.add_argument("--tickers", type=_tickers, help=_TICKERS_HELP)
    guidance_extract.add_argument(
        "--model", help="model of the configured provider (default LLM_MODEL)"
    )
    guidance_extract.add_argument(
        "--missing-only",
        action="store_true",
        help="only documents never extracted; keep those extracted with another model",
    )
    guidance_extract.add_argument(
        "--dry-run",
        action="store_true",
        help="print the calls, tokens and cost a run would take, and make no call",
    )

    commands.add_parser(
        "spend", help="LLM spending recorded in the ledger, by command and model, and budget"
    )

    asking = commands.add_parser(
        "ask",
        help="answer a question about the companies from filings, guidance and news, with "
        "citations; without a question, an interactive chat on one thread",
    )
    asking.add_argument("question", nargs="?", help="the question (omit for a chat)")
    asking.add_argument("--thread", help="conversation id, to ask follow-ups (default: new)")
    asking.add_argument(
        "--model", help="model of the configured provider (default LLM_MODEL, Haiku 5.5)"
    )
    asking.add_argument(
        "--show-trace", action="store_true", help="print each node and tool call as it runs"
    )
    asking.add_argument(
        "--max-cost", type=Decimal, help="US dollars this run may spend (default LLM_COST_CAP)"
    )

    graph = commands.add_parser("agent-graph", help="write the agent's graph as Mermaid")
    graph.add_argument(
        "--output", type=Path, default=Path("docs/agent-graph.md"),
        help="(default docs/agent-graph.md)",
    )  # fmt: skip

    guided = commands.add_parser(
        "guidance",
        help="a company's guidance against reported results, newest first; or `guidance "
        "review ID --ok|--wrong`",
    )
    guided.add_argument("ticker", help="a ticker, `all`, or `review` followed by a row ID")
    guided.add_argument("row", nargs="?", help="with `review`: the row ID shown by `guidance`")
    guided.add_argument("--metric", choices=GUIDANCE_METRICS, help="only this metric")
    guided.add_argument("--limit", type=int, help="rows shown (default all)")
    guided.add_argument(
        "--flagged", action="store_true", help="only rows flagged by the plausibility check"
    )
    decision = guided.add_mutually_exclusive_group()
    decision.add_argument(
        "--ok", action="store_true", help="with `review`: the row is right; clear its flag"
    )
    decision.add_argument(
        "--wrong", action="store_true", help="with `review`: the row is wrong; leave it out"
    )

    covered = commands.add_parser(
        "coverage", help="documents and sections found per company and fiscal quarter"
    )
    covered.add_argument("--tickers", type=_tickers, help="comma-separated tickers (default: all)")

    show = commands.add_parser("show", help="print one section of a company's filing text")
    show.add_argument("ticker")
    show.add_argument("period", help="fiscal quarter, e.g. FY2026Q3, or 'latest'")
    show.add_argument("section", choices=[t.value for t in SectionType])

    financials = commands.add_parser("financials", help="quarterly financials for one company")
    financials.add_argument("ticker")
    financials.add_argument(
        "--vintage",
        choices=["first", "latest"],
        default="first",
        help="values as first reported (default) or as most recently reported",
    )
    financials.add_argument("--quarters", type=int, default=8)

    restated = commands.add_parser(
        "restatements", help="quarterly values that changed after first being reported"
    )
    restated.add_argument("ticker")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        status = _run(args)
        # Write what is still buffered now, so a reader that stopped early fails here and
        # not when Python flushes stdout at exit.
        sys.stdout.flush()
        return status
    except BrokenPipeError:
        # The reader stopped early (`show ... | head`). Python flushes stdout again at
        # exit; point it at devnull so that flush has nowhere to fail (see "Note on
        # SIGPIPE" in the signal module docs).
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1


def _run(args: argparse.Namespace) -> int:
    match args.command:
        case "db-check":
            return asyncio.run(_db_check())
        case "ingest" if args.source == "edgar":
            return asyncio.run(_ingest_edgar(args.tickers))
        case "ingest" if args.source == "news" and args.reprocess:
            if args.backfill or args.tickers or args.since or args.until:
                print("--reprocess takes no other options", file=sys.stderr)
                return 2
            return asyncio.run(_reprocess_news())
        case "ingest" if args.source == "news" and args.backfill:
            if (args.since is None) == (not args.around_earnings):
                print(
                    "--backfill needs either --since (e.g. --since 2024-01-01) or "
                    "--around-earnings",
                    file=sys.stderr,
                )
                return 2
            if args.around_earnings:
                if args.until or args.days_before < 0 or args.days_after < 0:
                    print(
                        "--around-earnings takes --days-before/--days-after of 0 or more and "
                        "no --until",
                        file=sys.stderr,
                    )
                    return 2
                span = _BackfillRange(days_before=args.days_before, days_after=args.days_after)
            else:
                span = _BackfillRange(since=args.since, until=args.until)
            return _run_backfill(args.tickers, span)
        case "ingest" if args.source == "news":
            if args.since or args.until:
                print("--since and --until only apply with --backfill", file=sys.stderr)
                return 2
            return asyncio.run(_ingest_news(args.tickers))
        case "ingest":
            return asyncio.run(_ingest_filings(args.tickers))
        case "companies":
            return asyncio.run(_companies())
        case "coverage":
            return asyncio.run(_coverage(args.tickers))
        case "show":
            return asyncio.run(_show(args.ticker, args.period, args.section))
        case "financials":
            return asyncio.run(_financials(args.ticker, args.vintage, args.quarters))
        case "restatements":
            return asyncio.run(_restatements(args.ticker))
        case "index":
            return asyncio.run(_index(args.tickers, args.rebuild))
        case "search":
            return asyncio.run(_search(args))
        case "ask":
            from talk_is_cheap.agent.commands import ask_command

            return asyncio.run(
                ask_command(args.question, args.thread, args.model, args.show_trace, args.max_cost)
            )
        case "agent-graph":
            from talk_is_cheap.agent.commands import agent_graph_command

            return agent_graph_command(args.output)
        case "eval" if args.benchmark == "agent":
            from talk_is_cheap.agent.smoke import smoke_command

            return asyncio.run(
                smoke_command(
                    args.questions, args.only, args.model, args.dry_run, args.max_cost,
                    args.results,
                )
            )  # fmt: skip
        case "eval" if args.benchmark == "guidance":
            return asyncio.run(_eval_guidance(args.labels, args.models, args.details))
        case "eval":
            return asyncio.run(
                _eval_retrieval(args.questions, args.modes, args.filtered, args.details)
            )
        case "extract":
            return asyncio.run(
                _extract_guidance(args.tickers, args.model, args.dry_run, args.missing_only)
            )
        case "spend":
            return asyncio.run(_spend())
        case "guidance" if args.ticker == "review":
            if args.row is None or not (args.ok or args.wrong):
                print("usage: talk-is-cheap guidance review ID --ok|--wrong", file=sys.stderr)
                return 2
            return asyncio.run(_review_guidance(args.row, "ok" if args.ok else "wrong"))
        case "guidance":
            if args.row is not None or args.ok or args.wrong:
                print("--ok, --wrong and a row ID only go with `guidance review`", file=sys.stderr)
                return 2
            return asyncio.run(_guidance(args.ticker, args.metric, args.limit, args.flagged))
        case "feeds":
            return asyncio.run(_feeds(args.backfill))
        case "news":
            return asyncio.run(
                _news(
                    args.ticker,
                    args.since,
                    args.around_earnings,
                    args.min_confidence,
                    args.limit,
                )
            )
    return 2  # unreachable: argparse rejects unknown commands


if __name__ == "__main__":
    raise SystemExit(main())
