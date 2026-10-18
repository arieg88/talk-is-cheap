"""Command-line entry point: `talk-is-cheap <command>`."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from decimal import Decimal

from talk_is_cheap import __version__
from talk_is_cheap.config import get_settings
from talk_is_cheap.db import check_database, create_engine, create_session_factory
from talk_is_cheap.edgar.ingest import CompanyIngestResult, ingest_edgar
from talk_is_cheap.edgar.source import EdgartoolsSource, configure_edgartools
from talk_is_cheap.financials import (
    QuarterFinancials,
    Vintage,
    company_summaries,
    find_company,
    quarterly_financials,
    restatements,
)
from talk_is_cheap.universe import load_universe


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


async def _ingest_edgar(tickers: Sequence[str] | None) -> int:
    settings = get_settings()
    try:
        universe = load_universe(settings.universe_file)
        universe.select(tickers)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return 1
    if settings.edgar_identity is None:
        print(
            "EDGAR_IDENTITY is not set. The SEC requires a name and contact email, "
            "e.g. EDGAR_IDENTITY='Jane Doe jane@example.com'",
            file=sys.stderr,
        )
        return 1
    configure_edgartools(settings.edgar_identity, settings.edgar_data_dir)
    engine = create_engine(settings)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talk-is-cheap")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("db-check", help="check database connectivity, pgvector and migrations")

    ingest = commands.add_parser("ingest", help="load data from external sources")
    sources = ingest.add_subparsers(dest="source", required=True)
    edgar = sources.add_parser("edgar", help="companies, filings and financials from SEC EDGAR")
    edgar.add_argument(
        "--tickers",
        type=lambda text: [t for t in text.split(",") if t.strip()],
        help="comma-separated tickers from universe.yaml (default: all)",
    )

    commands.add_parser("companies", help="list loaded companies")

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
    match args.command:
        case "db-check":
            return asyncio.run(_db_check())
        case "ingest":
            return asyncio.run(_ingest_edgar(args.tickers))
        case "companies":
            return asyncio.run(_companies())
        case "financials":
            return asyncio.run(_financials(args.ticker, args.vintage, args.quarters))
        case "restatements":
            return asyncio.run(_restatements(args.ticker))
    return 2  # unreachable: argparse rejects unknown commands


if __name__ == "__main__":
    raise SystemExit(main())
