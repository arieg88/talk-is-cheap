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

**Filing text**

- Parses the earnings releases (8-Ks with item 2.02), 10-Qs and 10-Ks in the filing index into structured text: headings as `#` lines, bold kept as `**bold**`, tables as markdown tables, hidden XBRL and scripts removed. Whitespace and line breaks between words always become one space. Running page headers and footers ("Apple Inc. | 2025 Form 10-K | 5", "PART I / Item 2") and page numbers are removed, so the same text reads the same from year to year. The same HTML always gives the same text and content hash.
- Tags sections of each document by character range, so every section can be cited by URL and offset:
  - press releases: `outlook` (guidance), `results` (results, highlights and financial statement tables) and `other`, from their headings;
  - other EX-99 exhibits on earnings 8-Ks that discuss results (CFO commentary, slides, supplemental tables): `commentary` and `outlook`;
  - 10-Qs and 10-Ks: `mdna` (10-Q Part I Item 2, 10-K Item 7) and `risk_factors` (10-K Item 1A, 10-Q Part II Item 1A).
- Keeps apart what is filed under item 2.02 but is not an earnings release: production and deliveries reports (Tesla) are `deliveries_report`s, and exhibits unrelated to earnings that share the 8-K (an executive change, a buyback, a partnership post) are `other_exhibit`s. Their sections are all `other`, and neither counts as a release.
- Places each document in a fiscal quarter: 10-Qs and 10-Ks by their period of report (a 10-K is the year's fourth quarter), earnings releases by the most recent quarter that ended before they were filed, checked against the quarter the release names.
- Many companies give no guidance in their release; such a release has no `outlook` section. An outlook section that only says guidance is given on the earnings call is kept, and `coverage` reports it as "no guidance".
- Re-runs skip documents already parsed with the current parser version, and re-parse the rest.

On the current 15 companies: a press release for 165 of 165 earnings 8-Ks (plus 12 Tesla deliveries reports, not counted), MD&A in 166 of 166 10-Qs and 10-Ks, risk factors in 43 of 43 10-Ks and 123 of 123 10-Qs, and an outlook with guidance in 124 of 165 quarters with a release. Apple, Alphabet and Oracle (until 2026) give no guidance in their releases; Microsoft's 11 outlook sections only say guidance is given on the call. Tesla's earnings releases are slide decks without HTML tables, so they have no markdown tables.

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
| `talk-is-cheap ingest filings [--tickers AAPL,MSFT]` | Parse the text of loaded 10-Qs, 10-Ks and earnings 8-Ks into documents and sections. Run after `ingest edgar`. |
| `talk-is-cheap companies` | List loaded companies with filing counts and latest quarter |
| `talk-is-cheap coverage [--tickers NVDA]` | Per company and fiscal quarter: press release, outlook (or "no guidance"), MD&A and risk factors found, with totals |
| `talk-is-cheap show NVDA FY2026Q3 outlook` | Print every section of one type from the quarter's preferred document (press release, then other earnings exhibits, then the 10-Q or 10-K), with its URL and character ranges; other documents with that section are listed. The quarter can be `latest`; section types are `outlook`, `results`, `other`, `commentary`, `mdna` and `risk_factors`. |
| `talk-is-cheap financials NVDA [--vintage first\|latest] [--quarters 8]` | Quarterly revenue, margins, net income, EPS, operating cash flow and capex. Derived quarters are marked `*`. |
| `talk-is-cheap restatements NVDA` | Quarterly values that changed after they were first reported |
| `talk-is-cheap db-check` | Database connectivity, pgvector and migration status |

Run `make` with no arguments to list the `make` shortcuts. `make ingest` runs `ingest edgar` and then `ingest filings`.

## Adding companies or history

Add tickers to `universe.yaml`, or move `history_start` earlier, then run `talk-is-cheap ingest edgar`. Only US domestic filers work: companies that file 20-F and 6-K reports have no 10-Q filings. Metrics and the XBRL concepts that report them are configured in `src/talk_is_cheap/edgar/metrics.yaml`.

## Filing layouts handled

Section finding uses general rules; these are the layouts in the current universe that shaped them.

| Company | Layout | How it is handled |
| --- | --- | --- |
| Most filers | Headings are bold paragraphs, not `<h1>`-`<h6>` | A short, fully bold paragraph that does not end like a sentence is a heading |
| All 10-Ks and 10-Qs | Item headings appear in the table of contents and in cross references | Lines inside tables are skipped; of several headings for one item, the longest section wins |
| All 10-Ks and 10-Qs | Running headers, footers and page numbers on every page | Pages come from the filing's page breaks; a block among the first or last three of a page is removed when it is a page number, or recurs in the same place (top or bottom) on at least three consecutive pages |
| Apple | Footer "Apple Inc. \| 2025 Form 10-K \| 5" on every page | Removed as a running footer, digits ignored |
| Microsoft | "PART I / Item 1A" repeated as a header on every page | Removed as a running header; repeats of an item with no other item between them are one section |
| Intel | Sections titled ("Management's Discussion and Analysis"), not numbered, in 18pt banners; footers name the section ("MD&A 35", "Risk Factors"); underlined release headings | Large type and underlining mark headings; a titled section ends at the next standard section title, extended through the last page whose removed footer names it, since Intel puts Properties inside MD&A. Headers and footers are told apart, so the "Risk Factors" footer does not remove the "Risk Factors" title |
| Tesla | Production and deliveries reports are filed under item 2.02; the earnings release is a slide deck | A release whose headline is about production or deliveries is a `deliveries_report`; letter-spaced slide titles ("O U T L O O K") become headings |
| Adobe, Microsoft | Unrelated announcements filed as EX-99.2/99.3 with the earnings release (buyback, leadership change, OpenAI partnership) | Exhibits that hardly use financial-results terms are `other_exhibit`s |
| Nvidia | A press release ends with "###" | Text that would read as markup is escaped (`\###`) |
| Cisco, Alphabet, Amazon, Netflix and most others | Financial tables under headings that do not say "results" ("GROSS MARGIN PERCENTAGE BY SEGMENT", "Segment Information", "MICROSOFT CORPORATION", Intel's "About Intel") | A release section that is mostly tables of figures is `results` |
| Adobe, Salesforce | Headlines mention targets or guidance ("... Raises Targets") | Headings that report or announce are `results` |
| Netflix | Shareholder letter opens with a table of comparison quarters and says "Q2" without a year | Tables are not read for the release quarter; without a named quarter the filing date decides |
| Qualcomm, Tesla | "Fourth Quarter and Fiscal 2024", "Q4 and FY 2023" | Recognized as the release quarter |
| Apple, Alphabet, Oracle until 2026 | No guidance in the release | No `outlook` section |
| Microsoft | "Business Outlook" only says guidance is given on the call | The section is kept; `coverage` reports it as "no guidance" |

Spanning table cells are aligned the way filers lay them out: a number spanning the `$` and value columns goes to the value column, and neighbouring value columns that never both hold a value are merged.

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
| `documents` | One row per parsed document (`press_release`, `earnings_exhibit`, `10q`, `10k`, `deliveries_report` or `other_exhibit`): fiscal quarter, period end, filing date, sec.gov URL, converted text, content hash and parser version |
| `sections` | Tagged sections of a document: type, title, position, text and the character range of `documents.text` it covers |

## Project layout

```
talk-is-cheap/
├── universe.yaml            # companies, forms, history start
├── src/talk_is_cheap/
│   ├── config.py            # typed settings
│   ├── cli.py               # talk-is-cheap command
│   ├── universe.py          # universe.yaml loader
│   ├── financials.py        # first/latest quarterly values, restatements
│   ├── filings/
│   │   ├── html_text.py     # filing HTML → structured text
│   │   ├── sections.py      # press release tagging, 10-Q/10-K item splitting
│   │   ├── periods.py       # the quarter an earnings release reports
│   │   ├── documents.py     # which documents of a filing are kept, and their periods
│   │   ├── ingest.py        # parse and store, per company
│   │   └── coverage.py      # coverage table, section lookup
│   ├── edgar/
│   │   ├── source.py        # edgartools adapter → plain records
│   │   ├── records.py       # CompanyInfo, FilingRecord, FilingDocument, RawFact
│   │   ├── fiscal.py        # fiscal years, quarters, period lengths
│   │   ├── normalize.py     # raw facts → versioned metric values
│   │   ├── metrics.yaml     # metrics and their XBRL concepts
│   │   └── ingest.py        # load and store, per company
│   └── db/
│       ├── models.py        # companies, filings, xbrl_facts, financial_values, documents, sections
│       ├── base.py, session.py, health.py
├── alembic/                 # migrations
├── tests/                   # unit tests; integration/ uses a real database
│   └── fixtures/            # trimmed real SEC filings
├── docker-compose.yml
├── Makefile
└── .github/workflows/ci.yml
```

## Testing

```bash
make test        # all tests; integration tests are skipped if no database is reachable
make test-unit   # only tests that need no database
```

Tests never call the SEC. The edgartools adapter is tested against edgartools' own parser with SEC-shaped sample data, and ingestion is tested end to end against a real database with an in-memory source. Text conversion and section finding are tested on small fixtures trimmed from real SEC filings (public domain). Set `REQUIRE_DATABASE=1` to make integration tests fail, rather than skip, when the database is unreachable. CI sets it.
