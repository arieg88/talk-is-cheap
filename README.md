# Talk Is Cheap

*Management talks. Filings keep score.*

An AI analyst for public companies that compares what management says with what the company reports, using SEC EDGAR filings and public news.

## Current state

**Financial data from SEC EDGAR**

- Loads 15 companies (listed in `universe.yaml`) from SEC EDGAR through [edgartools](https://github.com/dgunning/edgartools): company details, the filing index (10-K, 10-Q, 8-K and amendments) and XBRL financial facts.
- Normalizes facts into quarterly and annual values for nine metrics: revenue, cost of revenue, gross profit, operating income, net income, diluted EPS, R&D, operating cash flow and capex (`metrics.yaml`).
- Keeps every version of every value, one per filing that reported it, so numbers can be read **as first reported** or **as latest reported**, and restatements can be listed.
- Handles each company's fiscal calendar, including 52/53-week years and years that end in January, June or September.
- Fills quarters that filings don't report directly: Q4 from the annual total minus nine months, and cash-flow quarters from year-to-date totals. Each uses only the values public on that filing's date. Per-share values are never derived this way.
- Re-runs are safe: filings are upserted, raw facts are insert-only, and normalized values are rebuilt per company.

**Foundation**

- Python 3.12 package (`talk_is_cheap`) managed with [uv](https://docs.astral.sh/uv/).
- Typed settings from environment variables or a `.env` file.
- PostgreSQL 16 with [pgvector](https://github.com/pgvector/pgvector), run locally with Docker Compose; async SQLAlchemy 2 with psycopg 3; Alembic migrations.
- Unit tests, plus integration tests against a real database; strict mypy; ruff.
- GitHub Actions CI running lint, type checks, migrations and all tests against Postgres + pgvector.

## Requirements

- Python 3.12 or newer
- uv
- Docker with Docker Compose
- Network access to `www.sec.gov` and `data.sec.gov` for loading data

## Quick start

```bash
cp .env.example .env   # then set EDGAR_IDENTITY to your name and email
make install           # create .venv and install dependencies
make db                # start Postgres + pgvector and wait until healthy
make migrate           # apply migrations
make ingest            # load all companies from SEC EDGAR
make check             # lint, type check and run all tests
```

The SEC requires every automated request to identify a person or organization with a contact email, so `EDGAR_IDENTITY` must be set, for example `EDGAR_IDENTITY="Jane Doe jane@example.com"`. edgartools caches SEC responses in `EDGAR_DATA_DIR`, so repeated runs are fast.

## Commands

| Command | What it does |
| --- | --- |
| `talk-is-cheap ingest edgar [--tickers AAPL,MSFT]` | Load companies from SEC EDGAR (default: all in `universe.yaml`). One company failing does not stop the others. |
| `talk-is-cheap companies` | List loaded companies with filing counts and latest quarter |
| `talk-is-cheap financials NVDA [--vintage first\|latest] [--quarters 8]` | Quarterly revenue, margins, net income, EPS, operating cash flow and capex. Derived quarters are marked `*`. |
| `talk-is-cheap restatements NVDA` | Quarterly values that changed after they were first reported |
| `talk-is-cheap db-check` | Database connectivity, pgvector and migration status |

Run `make` with no arguments to list the `make` shortcuts.

## Adding companies or history

Add tickers to `universe.yaml`, or move `history_start` earlier, then run `talk-is-cheap ingest edgar`. Only US domestic filers work: companies that file 20-F and 6-K reports have no 10-Q filings. Metrics and the XBRL concepts that report them are configured in `src/talk_is_cheap/edgar/metrics.yaml`.

## Configuration

All settings are environment variables. See `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `EDGAR_IDENTITY` | unset | Your name and contact email, sent to the SEC. Required by `ingest`. |
| `EDGAR_DATA_DIR` | `.cache/edgar` | Where edgartools caches SEC responses |
| `UNIVERSE_FILE` | `universe.yaml` | Companies, filing forms and history start date |
| `DATABASE_URL` | local Docker database | Must use the `postgresql+psycopg://` driver prefix |
| `DATABASE_ECHO` | `false` | Log every SQL statement |
| `DATABASE_POOL_SIZE` | `5` | Connection pool size, 1 to 50 |
| `ENVIRONMENT` | `local` | `local`, `test` or `production` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## Data model

| Table | Contents |
| --- | --- |
| `companies` | CIK, ticker, name, fiscal year end, industry |
| `filings` | One row per filing: form, filing date, report date, 8-K items, primary document |
| `xbrl_facts` | Raw XBRL values for the configured concepts, one row per filing that reported them; never overwritten |
| `financial_values` | Normalized metric values per fiscal quarter or year, one row per filing version, with how each was derived |

## Project layout

```
talk-is-cheap/
├── universe.yaml            # companies, forms, history start
├── src/talk_is_cheap/
│   ├── config.py            # typed settings
│   ├── cli.py               # talk-is-cheap command
│   ├── universe.py          # universe.yaml loader
│   ├── financials.py        # first/latest quarterly values, restatements
│   ├── edgar/
│   │   ├── source.py        # edgartools adapter → plain records
│   │   ├── records.py       # CompanyInfo, FilingRecord, RawFact
│   │   ├── fiscal.py        # fiscal years, quarters, period lengths
│   │   ├── normalize.py     # raw facts → versioned metric values
│   │   ├── metrics.yaml     # metrics and their XBRL concepts
│   │   └── ingest.py        # load and store, per company
│   └── db/
│       ├── models.py        # companies, filings, xbrl_facts, financial_values
│       ├── base.py, session.py, health.py
├── alembic/                 # migrations
├── tests/                   # unit tests; integration/ uses a real database
├── docker-compose.yml
├── Makefile
└── .github/workflows/ci.yml
```

## Testing

```bash
make test        # all tests; integration tests are skipped if no database is reachable
make test-unit   # only tests that need no database
```

Tests never call the SEC. The edgartools adapter is tested against edgartools' own parser with SEC-shaped sample data, and ingestion is tested end to end against a real database with an in-memory source. Set `REQUIRE_DATABASE=1` to make integration tests fail, rather than skip, when the database is unreachable. CI sets it.
