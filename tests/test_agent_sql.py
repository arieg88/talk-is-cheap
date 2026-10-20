"""`run_sql` validation: one read-only SELECT over allowed tables and functions, and the
ways around it that are refused. The database-side guards (READ ONLY, the statement
timeout) are tested in tests/integration/test_agent.py."""

import pytest

from talk_is_cheap.agent.sql import SqlRejected, validate


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT ticker, name FROM companies ORDER BY ticker",
        "select c.ticker, count(*) from companies c join filings f on f.cik = c.cik group by 1",
        "SELECT metric, round(avg(value)::numeric, 2) FROM financial_values GROUP BY metric",
        "WITH latest AS (SELECT cik, max(filed) AS filed FROM financial_values GROUP BY cik) "
        "SELECT * FROM latest",
        "SELECT ticker FROM companies UNION SELECT ticker FROM article_companies",
        "SELECT verdict, count(*) FILTER (WHERE review IS NULL) FROM guidance GROUP BY verdict",
        "SELECT lag(value) OVER (ORDER BY fiscal_year) FROM public.financial_values",
        "SELECT extract(year FROM filing_date), date_trunc('month', filing_date) FROM filings",
        "SELECT * FROM companies WHERE ticker = 'a'';drop table x;--'",
        "SELECT 1 -- ; DROP TABLE companies",
    ],
)
def test_reads_are_allowed(statement: str) -> None:
    assert validate(statement).upper().startswith(("SELECT", "WITH"))


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("SELECT 1; DROP TABLE companies", "exactly one statement"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("DELETE FROM companies", "only SELECT"),
        ("UPDATE guidance SET verdict = 'beat'", "only SELECT"),
        ("INSERT INTO companies VALUES (1)", "only SELECT"),
        ("DROP TABLE companies", "only SELECT"),
        ("TRUNCATE companies", "only SELECT"),
        ("COPY companies TO '/tmp/x'", "only SELECT"),
        ("SET transaction_read_only = off", "only SELECT"),
        ("EXPLAIN ANALYZE DELETE FROM companies", "only SELECT"),
        ("WITH gone AS (DELETE FROM companies RETURNING *) SELECT * FROM gone", "DELETE"),
        ("SELECT * INTO copy_of FROM companies", "INTO"),
        ("SELECT * FROM companies FOR UPDATE", "LOCK"),
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ('SELECT "pg_sleep"(10)', "pg_sleep"),
        ("SELECT (SELECT pg_read_file('/etc/passwd'))", "pg_read_file"),
        ("SELECT set_config('transaction_read_only', 'off', true)", "set_config"),
        ("SELECT current_setting('statement_timeout')", "current_setting"),
        ("SELECT lo_import('/etc/passwd')", "lo_import"),
        ("SELECT dblink('host=x', 'DELETE FROM companies')", "dblink"),
        ("SELECT * FROM generate_series(1, 100000000)", "FROM is not allowed"),
        ("SELECT * FROM pg_catalog.pg_tables", "only public tables"),
        ("SELECT * FROM information_schema.tables", "only public tables"),
        ("SELECT * FROM pg_tables", "not allowed"),
        ("SELECT * FROM llm_spend", "not allowed"),
        ("SELECT * FROM agent_runs", "not allowed"),
        ("SELECT * FROM checkpoints", "not allowed"),
        ("SELECT * FROM other_db.public.companies", "only public tables"),
        ("SELEC * FROM companies", "not valid SQL|only SELECT"),
    ],
)
def test_everything_else_is_rejected(statement: str, reason: str) -> None:
    with pytest.raises(SqlRejected, match=reason):
        validate(statement)
