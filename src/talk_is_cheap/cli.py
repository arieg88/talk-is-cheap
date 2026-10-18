"""Command-line entry point: `talk-is-cheap <command>`."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from talk_is_cheap import __version__
from talk_is_cheap.config import get_settings
from talk_is_cheap.db import check_database, create_engine


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talk-is-cheap")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db-check", help="check database connectivity, pgvector and migrations")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db-check":
        return asyncio.run(_db_check())
    return 2  # unreachable: argparse rejects unknown commands


if __name__ == "__main__":
    raise SystemExit(main())
