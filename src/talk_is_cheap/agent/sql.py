"""`run_sql`: one read-only SELECT over an allowlist of tables, enforced in code.

The statement is parsed with sqlglot (Postgres dialect) and rejected unless it is a
single SELECT (or a UNION, INTERSECT or EXCEPT of SELECTs) that:

- writes nothing anywhere in its tree: no INSERT, UPDATE, DELETE or MERGE (also not in a
  WITH clause), no SELECT INTO, no row locks (FOR UPDATE), no DDL, COPY, SET or other
  command;
- reads only tables in ALLOWED_TABLES, without a schema other than `public` (so no
  `pg_catalog` or `information_schema`), plus its own WITH names;
- calls only functions in ALLOWED_FUNCTIONS (aggregates, arithmetic, string and date
  functions). Anything sqlglot does not know, `pg_sleep`, `pg_read_file`,
  `set_config`, `dblink` and `lo_import` among them, is rejected, as are
  set-returning functions such as `generate_series`.

What runs is the statement as sqlglot read it, regenerated, so what was checked is what
the database sees. It runs in a transaction that is READ ONLY with a statement timeout
of STATEMENT_TIMEOUT_SECONDS, and at most MAX_ROWS rows are returned.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlglot import exp
from sqlglot.errors import ParseError

# sqlglot logs a warning for statements it reads as an opaque command; they are rejected.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_ROWS = 200
STATEMENT_TIMEOUT_SECONDS = 5.0
MAX_CELL_CHARS = 300

ALLOWED_TABLES = frozenset(
    {
        "companies",
        "filings",
        "financial_values",
        "documents",
        "sections",
        "guidance",
        "guidance_sources",
        "guidance_reviews",
        "stock_splits",
        "articles",
        "article_companies",
        "statements",
        "statement_outcomes",
    }
)

# sqlglot expression classes of the functions a query may call.
ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates and window functions
        "Count",
        "Sum",
        "Avg",
        "Min",
        "Max",
        "Stddev",
        "StddevPop",
        "StddevSamp",
        "Variance",
        "VariancePop",
        "Median",
        "PercentileCont",
        "PercentileDisc",
        "ArrayAgg",
        "GroupConcat",
        "RowNumber",
        "Rank",
        "DenseRank",
        "Lag",
        "Lead",
        "FirstValue",
        "LastValue",
        "NthValue",
        "Ntile",
        "CumeDist",
        "PercentRank",
        "CountIf",
        "LogicalAnd",
        "LogicalOr",
        # arithmetic
        "Abs",
        "Round",
        "Trunc",
        "Floor",
        "Ceil",
        "Sqrt",
        "Pow",
        "Ln",
        "Log",
        "Exp",
        "Sign",
        "Mod",
        "Greatest",
        "Least",
        "SafeDivide",
        # conditionals and casts
        "Coalesce",
        "Nullif",
        "If",
        "Case",
        "Cast",
        "TryCast",
        # strings
        "Lower",
        "Upper",
        "Length",
        "Substring",
        "Concat",
        "ConcatWs",
        "Trim",
        "Left",
        "Right",
        "Replace",
        "StrPosition",
        "Initcap",
        "Lpad",
        "Rpad",
        "RegexpLike",
        "Split",
        "SplitPart",
        # dates
        "Extract",
        "TimestampTrunc",
        "DateTrunc",
        "TimeToStr",
        "CurrentDate",
        "CurrentTimestamp",
        "DateAdd",
        "DateSub",
        "DateDiff",
        "ToChar",
    }
)

_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Into,
    exp.Lock,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.TruncateTable,
)


class SqlRejected(ValueError):
    """The statement breaks a rule; the message says which."""


@dataclass(frozen=True, slots=True)
class SqlResult:
    sql: str  # the statement that ran
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool  # more than MAX_ROWS rows


def validate(statement: str) -> str:
    """The statement as it will run, or SqlRejected with the rule it breaks."""
    try:
        parsed = [p for p in sqlglot.parse(statement, read="postgres") if p is not None]
    except ParseError as exc:
        raise SqlRejected(f"not valid SQL: {str(exc).splitlines()[0]}") from exc
    if len(parsed) != 1:
        raise SqlRejected("exactly one statement is allowed; no ';'-chained statements")
    tree = parsed[0]
    if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
        raise SqlRejected(f"only SELECT is allowed, not {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, _WRITES):
            raise SqlRejected(f"{node.key.upper()} is not allowed: the query must only read")
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            raise SqlRejected(f"function {table.this.sql_name()} in FROM is not allowed")
        name = table.name.lower()
        schema = table.db.lower()
        if table.catalog or (schema and schema != "public"):
            raise SqlRejected(f"table {table.sql()} is not allowed: only public tables")
        if name in ctes and not schema:
            continue
        if name not in ALLOWED_TABLES:
            raise SqlRejected(
                f"table {name} is not allowed; tables: {', '.join(sorted(ALLOWED_TABLES))}"
            )
    for function in tree.find_all(exp.Func):
        if isinstance(function, exp.Anonymous):
            raise SqlRejected(f"function {function.name} is not allowed")
        if type(function).__name__ not in ALLOWED_FUNCTIONS:
            raise SqlRejected(f"function {function.sql_name()} is not allowed")
    return tree.sql(dialect="postgres")


async def execute_read_only(
    connection: AsyncConnection,
    statement: str,
    *,
    timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS,
    max_rows: int = MAX_ROWS,
) -> SqlResult:
    """Run an already validated statement in a READ ONLY transaction with a statement
    timeout. The transaction is rolled back, whatever the statement did."""
    async with connection.begin() as transaction:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        await connection.execute(
            text(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
        )
        result = await connection.execute(text(statement))
        columns = list(result.keys())
        fetched = result.fetchmany(max_rows + 1)
        await transaction.rollback()
    rows = [[_cell(value) for value in row] for row in fetched[:max_rows]]
    return SqlResult(statement, columns, rows, truncated=len(fetched) > max_rows)


async def run_sql(
    connection: AsyncConnection,
    statement: str,
    *,
    timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS,
) -> SqlResult:
    """Validate and run one SELECT. Raises SqlRejected for a statement that breaks a
    rule, and the database's error for one that fails (a timeout among them)."""
    return await execute_read_only(connection, validate(statement), timeout_seconds=timeout_seconds)


def _cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    if isinstance(value, list | tuple):
        return [_cell(v) for v in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)
