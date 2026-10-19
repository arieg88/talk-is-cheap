"""Command-line entry point: `talk-is-cheap <command>`."""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
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


def _run_backfill(tickers: Sequence[str] | None, since: date, until: date | None) -> int:
    """Run a backfill that stops cleanly on Ctrl+C or `kill` (SIGTERM), with a summary.

    Every answered part is already stored and every finished week recorded, so stopping
    loses at most the week in progress, and the same command resumes there.
    """
    seen: list[BackfillProgress] = []
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        return asyncio.run(_backfill_news(tickers, since, until, seen))
    except KeyboardInterrupt:
        print(
            f"\n{_stamp()} stopped: {_backfill_summary(seen)}. "
            "Running the same command again resumes where it stopped.",
            flush=True,
        )
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _backfill_summary(seen: Sequence["BackfillProgress"]) -> str:
    if not seen:
        return "no week finished in this run"
    latest = {p.ticker: p for p in seen}
    last = seen[-1]
    new = sum(p.new_articles for p in latest.values())
    text = f"{last.fetched} weeks fetched in {_duration(last.elapsed.total_seconds())}"
    if last.fetched:
        text += f" (average {_duration(last.elapsed.total_seconds() / last.fetched)} per week)"
    started = f"{len(latest)} {'company' if len(latest) == 1 else 'companies'} started"
    return f"{text}, {new} new articles, {started}"


async def _backfill_news(
    tickers: Sequence[str] | None,
    since: date,
    until: date | None,
    seen: list["BackfillProgress"],
) -> int:
    from talk_is_cheap.news.adapters import load_feeds_file
    from talk_is_cheap.news.gdelt import GdeltBackfill, backfill
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
    start = datetime.combine(since, time(), tzinfo=UTC)
    end = datetime.combine(until, time(), tzinfo=UTC) if until else datetime.now(UTC)
    if start >= end:
        print("--since must be before --until", file=sys.stderr)
        return 1

    def report(p: "BackfillProgress") -> None:
        seen.append(p)
        eta = "-" if p.remaining is None else _duration(p.remaining.total_seconds())
        line = (
            f"{_stamp()} {p.ticker:<6} {p.windows_done}/{p.windows_total} weeks, "
            f"{p.new_articles} new, {p.requests} requests, about {eta} left"
        )
        if p.skipped:
            line += f", {p.skipped} weeks skipped"
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
            async with new_client(get_settings().news_user_agent) as client:
                results = await backfill(
                    create_session_factory(engine),
                    GdeltBackfill.from_entry(
                        entries[0],
                        on_attempt=lambda a: print(
                            f"{_stamp()} GDELT {a.label} {a.start:%Y-%m-%d %H:%M} to "
                            f"{a.end:%Y-%m-%d %H:%M} attempt {a.number}: {a.outcome} "
                            f"in {a.seconds:.1f}s",
                            flush=True,
                        ),
                    ),
                    companies,
                    CompanyMatcher([c for c in universe.companies if c.name]),
                    client,
                    since=start,
                    until=end,
                    on_progress=report,
                )
    finally:
        await engine.dispose()
    skipped = sum(r.skipped for r in results)
    if skipped:
        print(
            f"\n{_stamp()} The GDELT API kept refusing requests for {skipped} weeks, which "
            "were skipped. Running the same command later fetches them and resumes where "
            "it stopped.",
            file=sys.stderr,
        )
    print(
        f"{_stamp()} done: {sum(r.error is None for r in results)}/{len(companies)} "
        f"companies, {_backfill_summary(seen)}",
        flush=True,
    )
    return 0 if len(results) == len(companies) and all(r.error is None for r in results) else 1


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
    start = datetime.combine(since, time(), tzinfo=UTC) if since else None
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


_TICKERS_HELP = "comma-separated tickers from universe.yaml (default: all)"


def _tickers(text: str) -> list[str]:
    return [t for t in text.split(",") if t.strip()]


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
    news.add_argument("--since", type=date.fromisoformat, help="backfill start, e.g. 2024-01-01")
    news.add_argument("--until", type=date.fromisoformat, help="backfill end (default: now)")

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
            if args.since is None:
                print("--backfill needs --since, e.g. --since 2024-01-01", file=sys.stderr)
                return 2
            return _run_backfill(args.tickers, args.since, args.until)
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
