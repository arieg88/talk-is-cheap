# Talk Is Cheap

*Management talks. Filings keep score.*

An AI analyst for public companies that compares what management says with what the company reports, using SEC EDGAR filings and public news.

## Current state

The project foundation is in place:

- A Python 3.12 package (`talk_is_cheap`) managed with [uv](https://docs.astral.sh/uv/).
- Typed settings loaded from environment variables or a `.env` file, validated at startup.
- PostgreSQL 16 with the [pgvector](https://github.com/pgvector/pgvector) extension, run locally with Docker Compose.
- Async database access with SQLAlchemy 2 and the psycopg 3 driver.
- Alembic migrations; the current schema enables pgvector.
- A `talk-is-cheap` command-line tool with a `db-check` command.
- Unit tests, plus integration tests against a real database.
- Strict type checking (mypy) and linting and formatting (ruff).
- GitHub Actions CI that runs lint, type checks, migrations and all tests against Postgres + pgvector.

## Requirements

- Python 3.12 or newer
- uv
- Docker with Docker Compose

## Quick start

```bash
cp .env.example .env   # optional: defaults already match docker-compose.yml
make install           # create .venv and install dependencies
make db                # start Postgres + pgvector and wait until healthy
make migrate           # apply migrations
make db-check          # confirm the database is ready
make check             # lint, type check and run all tests
```

`make db-check` prints:

```
postgres:  16.x
pgvector:  0.x.x
migration: 0001
ready:     yes
```

Run `make` with no arguments to list every command.

## Configuration

All settings are optional environment variables. See `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `local` | `local`, `test` or `production` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `DATABASE_URL` | local Docker database | Must use the `postgresql+psycopg://` driver prefix |
| `DATABASE_ECHO` | `false` | Log every SQL statement |
| `DATABASE_POOL_SIZE` | `5` | Connection pool size, 1 to 50 |

## Project layout

```
talk-is-cheap/
├── src/talk_is_cheap/
│   ├── config.py        # typed settings
│   ├── cli.py           # talk-is-cheap command
│   └── db/
│       ├── base.py      # declarative base with constraint naming rules
│       ├── session.py   # async engine and session factories
│       └── health.py    # database readiness check
├── alembic/             # migrations
├── tests/
│   ├── test_config.py
│   ├── test_cli.py
│   └── integration/     # tests that use a real database
├── docker-compose.yml
├── Makefile
└── .github/workflows/ci.yml
```

## Testing

```bash
make test        # all tests; integration tests are skipped if no database is reachable
make test-unit   # only tests that need no database
```

Set `REQUIRE_DATABASE=1` to make integration tests fail, rather than skip, when the database is unreachable. CI sets it.
