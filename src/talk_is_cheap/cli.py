"""Command-line entry point: `talk-is-cheap <command>`."""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from decimal import Decimal

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
from talk_is_cheap.universe import Universe, load_universe


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

    commands.add_parser("companies", help="list loaded companies")

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
    return 2  # unreachable: argparse rejects unknown commands


if __name__ == "__main__":
    raise SystemExit(main())
