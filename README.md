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

**News**

- Polls RSS and Atom feeds with one command: Yahoo Finance and Google News per company, MarketWatch and CNBC market-wide, and the newsrooms of Nvidia, Apple, Microsoft, AMD, Meta, Salesforce and Cisco (`feeds.yaml`). Each kind of source is an adapter; adding a feed is an entry in `feeds.yaml`.
- Stores metadata only: headline, the feed's summary (at most 500 characters), canonical link, publisher and publish time in UTC. Full articles are never downloaded.
- Stores each article once per canonical URL (tracking parameters removed) and records every feed that supplied it. Near-duplicate headlines, the same story from several outlets, form one cluster.
- Links articles to companies by name, alias and ticker in the headline and summary, by publisher, and by the company's own feeds, with a method and confidence per link. Per-company feeds alone are weak evidence, since Yahoo's carry stories about other companies and Google News searches ignore case.
- Tracks each feed's health: last success, last new item, consecutive failures, and a status (ok, stale after 72 hours without new items, failing).
- Backfills past news from the GDELT DOC 2.0 API by date range, resumably.
- Drops spam (styled Unicode letters, live-stream bait) and counts it per feed.
- Polls are safe to repeat and to overlap: unchanged feeds are skipped through conditional requests or an unchanged item fingerprint, one failing feed does not stop the others, and a poll that starts while another runs exits.

What each source returned when checked, and every normalization, clustering and linking rule, are in [docs/news.md](docs/news.md).

**Search**

- Makes filing text and news searchable for the agent: `search()` (`retrieval/search.py`) and `talk-is-cheap search` find the passages that answer a question, filtered by company, fiscal period range, date range, source, document type and section type. Every result is citable: the sec.gov URL and character range of the filing text, or the article's link, with its period or publish date and its section or publisher.
- Chunks each section on its own, on paragraph boundaries, at up to 450 tokens of the embedding model's tokenizer with about 15% overlap. Tables stay whole when they fit; longer ones are split by rows, each piece repeating the header rows. A news article is one chunk, headline and summary. No chunk exceeds the model's 512-token input.
- Embeds each chunk with a short context header ("NVDA · Nvidia · FY2027 Q2 · press release · outlook") using a local model (BAAI/bge-small-en-v1.5 through fastembed and ONNX Runtime, no torch, no API key).
- Four modes: keyword (Postgres full text), vector (pgvector HNSW), hybrid (Reciprocal Rank Fusion of both) and rerank (hybrid reordered by a local cross-encoder), the default. Measured on 49 labeled questions in `evals/retrieval/questions.jsonl`: searching by company and period, rerank puts a correct passage in the top 10 for 94% of them (hybrid 86%) at about 1.4 seconds a search on CPU (hybrid 60 ms). Results and choices are in [docs/retrieval.md](docs/retrieval.md).
- Indexing is incremental: only new or changed text is embedded, chunks of re-parsed or deleted documents and articles are removed, and changing the embedding model re-embeds everything. News added by polling or backfill is indexed by running `talk-is-cheap index` again.

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
- Network access to `www.sec.gov` and `data.sec.gov` for loading filings, and to the news feeds in `feeds.yaml` and `api.gdeltproject.org` for news
- Network access to `huggingface.co` once, for the search models (about 150 MB, kept in `MODEL_CACHE_DIR`); search and indexing then run offline

## Quick start

```bash
cp .env.example .env   # then set EDGAR_IDENTITY to your name and email
make install           # create .venv and install dependencies
make db                # start Postgres + pgvector and wait until healthy
make migrate           # apply migrations
make ingest            # load all companies from SEC EDGAR
uv run talk-is-cheap ingest news   # one poll of the news feeds
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
| `talk-is-cheap ingest news [--tickers NVDA,AMD]` | One poll of every feed in `feeds.yaml`, one summary line per feed: items, new articles, how many joined an existing story, duplicates and spam dropped, or `not modified`. With `--tickers`, only those companies' feeds. Exits quietly if another poll is still running. |
| `talk-is-cheap ingest news --backfill --around-earnings [--days-before 1] [--days-after 3] [--tickers NVDA]` | Past news from GDELT for the days around each stored earnings release, newest first (recommended). Prints the number of windows and an estimate first, then every request and one line per window. Safe to stop at any point; re-running resumes. See "Long backfills" below. |
| `talk-is-cheap ingest news --backfill --since 2024-01-01 [--until 2024-07-01] [--tickers NVDA]` | The same for every week from a start date |
| `talk-is-cheap ingest news --reprocess` | Apply the current rules and `universe.yaml` to every stored article: delete spam, clean titles, rebuild near-duplicate clusters and company links. Run it after editing names, aliases or excluded phrases. |
| `talk-is-cheap feeds [--backfill]` | Health of the polled feeds: status, last success, last new item, consecutive failures, articles, last error. With `--backfill`, the backfill sources instead: weeks fetched, the range they cover, articles and errors. |
| `talk-is-cheap news NVDA [--since 2026-10-01] [--around-earnings FY2027Q2] [--min-confidence 0.5] [--limit 50]` | A company's news, newest first, one line per story with time (UTC), publisher and headline, and the other publishers of the same story. `--around-earnings` shows the 48 hours after that quarter's earnings release was filed (needs `ingest edgar` and `ingest filings`). Times marked `~` are when GDELT saw the article, not its publish time. |
| `talk-is-cheap index [--tickers NVDA] [--rebuild]` | Chunk and embed filing sections and news for search, one line per company and one for news: chunks added, removed, unchanged, texts embedded, seconds. Only new or changed text is embedded; re-run it after ingesting filings or news. With `--tickers`, those companies' filings and the news linked to them. `--rebuild` drops the selected chunks and their vectors and embeds them again. |
| `talk-is-cheap search "query" [--ticker NVDA] [--periods FY2026Q1..FY2027Q2] [--since 2026-09-01] [--until 2026-10-19] [--sources filing,news] [--types outlook,mdna] [--doc-types press_release,10k] [--k 8] [--mode keyword\|vector\|hybrid\|rerank]` | Ranked passages with their citations, by `rerank` unless `--mode` says otherwise (`hybrid` is about 25 times faster and finds a little less). `--periods` takes quarters, ranges and whole years (`FY2026`) and only matches filings; `--since`/`--until` match news publish dates and filing dates. For news, `--ticker` counts only company links of confidence 0.5 or more. |
| `talk-is-cheap eval retrieval [--modes hybrid,rerank] [--filtered] [--details]` | Recall@5, recall@10, MRR and latency of each search mode, with and without the context header, on `evals/retrieval/questions.jsonl`, for all questions and for `lexical` and `paraphrase` ones. `--filtered` also filters each question by its own period or news dates. The first run embeds every chunk's text without its header too (about as long as the first `index`). |
| `talk-is-cheap db-check` | Database connectivity, pgvector and migration status |

Run `make` with no arguments to list the `make` shortcuts. `make ingest` runs `ingest edgar`, `ingest filings` and then `index`. `make index` indexes new filing text and news; `make eval-retrieval` runs the retrieval benchmark.

## Adding companies or history

Add tickers to `universe.yaml`, or move `history_start` earlier, then run `talk-is-cheap ingest edgar`. Only US domestic filers work: companies that file 20-F and 6-K reports have no 10-Q filings. Metrics and the XBRL concepts that report them are configured in `src/talk_is_cheap/edgar/metrics.yaml`.

News needs a `name` per company, the name the press uses, and takes optional `aliases` and `exclude_phrases`:

```yaml
  - ticker: GOOGL
    name: Alphabet
    aliases: [Google]
  - ticker: AVGO
    name: Broadcom
    news_only: true     # collect news now; filings are not loaded until this is removed
```

RSS feeds only carry recent items and their history cannot be fetched later, so list a company with `news_only: true` as soon as its news matters. `ingest edgar` and `ingest filings` skip news-only companies.

## Long backfills

RSS feeds only carry news from the day polling starts; GDELT fills in the past. As of 2026-10-19 the GDELT API is overloaded: it refuses most requests intermittently (from every network tested), every answer takes about 10 seconds, and a refused request is retried 10 seconds later, never more than one request per 5 seconds. A week that fails 30 times in a row is skipped and left for the next run.

Recommended: backfill only the days around each earnings release (one day before through three days after the filing date, newest quarters first), the news the analysis compares with filings. Run it unattended with a log:

```bash
nohup uv run talk-is-cheap ingest news --backfill --around-earnings >> .cache/backfill.log 2>&1 &
tail -f .cache/backfill.log          # every request with its response time, one line per window
talk-is-cheap feeds --backfill       # windows fetched, first and last, per company
```

It first prints how many windows are left and an estimate: for all 15 companies since 2024, 165 windows and 10 to 18 hours (measured 2026-10-19). `--since 2024-01-01` backfills every week instead, which takes weeks for the busiest companies. Load filings first (`make ingest`); earnings windows come from the stored press releases.

Stopping is safe at any point: press Ctrl+C, or `kill` the process (`pkill -f "ingest news --backfill"`). It prints a one-line summary (windows fetched, average time per window, new articles), keeps every finished window and every article already stored, and running the same command again resumes with the first unfinished or skipped window. Only one backfill runs at a time; scheduled polls keep running alongside it.

## Scheduled news polling

`ingest news` is a one-shot command; run it every 30 minutes. `scripts/poll-news.sh` runs one poll from wherever the project is checked out and appends a timestamp and the poll's output to `.cache/news-poll.log`. It finds `uv` on the `PATH` or in `~/.local/bin` (set `UV` to override). A poll that starts while the previous one is still running logs "previous poll still running; skipped" and exits; the command also takes a database lock, so overlapping runs never write at the same time, even when started some other way.

### Windows with WSL: Task Scheduler

Use Windows Task Scheduler rather than cron inside WSL: WSL shuts its distribution down when nothing is using it, and cron stops with it, so cron jobs silently stop running. Task Scheduler starts the distribution for each run.

1. Make Docker Desktop start with Windows: Docker Desktop > Settings > General > "Start Docker Desktop when you sign in to your computer". The database container has `restart: unless-stopped`, so it comes back with Docker.
2. Find your distribution name (`wsl.exe -l -v`) and the script's path (`realpath scripts/poll-news.sh` in the project).
3. In a Windows Command Prompt, create the task:

   ```bat
   schtasks /Create /TN "talk-is-cheap news" /SC MINUTE /MO 30 /TR "wsl.exe -d <distro> -- <path to>/scripts/poll-news.sh"
   ```

4. By default the task runs only while you are signed in, and a console window flashes on each run. To avoid the window, open the task in Task Scheduler > Properties and choose "Run whether user is logged on or not" (it asks for your Windows password), or add `/RU "%USERNAME%" /RP *` to the command above. Docker Desktop only runs while you are signed in, so polls made while signed out fail with "database: unreachable" in the log; that is expected.

Check that it works:

- Task Scheduler shows the task's "Last Run Result" as `0x0` (`0x1` means a feed or the database failed; see the log). If the result is not `0x0` and the log has no new line, `wsl.exe` could not start in the mode chosen in step 4; switch back to "Run only when user is logged on".
- `tail .cache/news-poll.log` shows a `---` timestamp every 30 minutes, followed by one line per feed and a `done:` summary.
- `talk-is-cheap feeds` shows "LAST SUCCESS" under 30 minutes for every feed.

### Always-on Linux: cron

```cron
*/30 * * * * /path/to/talk-is-cheap/scripts/poll-news.sh
```

The log grows by a few kilobytes per run; truncate or rotate `.cache/news-poll.log` from time to time.

## Filing layouts handled

Section finding uses general rules; these are the layouts in the current universe that shaped them.

The full reference, with the filing behind each rule, its tests and its limits, is [docs/filing-parsing.md](docs/filing-parsing.md).

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
| `UNIVERSE_FILE` | `universe.yaml` | Companies, filing forms, history start date, and the names news is matched by |
| `FEEDS_FILE` | `feeds.yaml` | News feeds and backfill sources |
| `NEWS_USER_AGENT` | `talk-is-cheap/0.1 (news headline collector; metadata only)` | User-Agent sent to news feeds and GDELT |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model for search embeddings. Changing it re-embeds everything on the next `index`. |
| `RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | fastembed cross-encoder for `--mode rerank` |
| `MODEL_CACHE_DIR` | `.cache/fastembed` | Where the models are downloaded |
| `DATABASE_URL` | local Docker database | Must use the `postgresql+psycopg://` driver prefix |
| `TEST_DATABASE_URL` | `talkischeap_test` on the local Docker server | Database the tests drop and recreate; the name must end in `_test`. Read from the shell only, not from `.env`. |
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
| `feeds` | One row per news feed or backfill source (`yahoo_finance:NVDA`, `rss:cnbc-earnings`, `gdelt:NVDA`): URL, ETag, Last-Modified, item fingerprint, last poll, last success, last new item, consecutive failures, last error |
| `articles` | One row per canonical URL: headline, summary, publisher, `published_at` (UTC) with its `time_precision` (`published`, `seen` by GDELT, or `fetched` when the feed gave none), and `cluster_id`, shared by near-duplicate headlines |
| `article_feeds` | Which feeds supplied each article, and when each first did |
| `article_companies` | Article-company links by ticker (news-only companies have no `companies` row), with `method` (`issuer`, `ticker`, `headline`, `summary`, `feed`) and `confidence` (0-1) |
| `backfill_windows` | Date windows fetched from GDELT per company, so backfill can resume |
| `chunks` | Search passages: source (`filing` or `news`), document, section or article, tickers, fiscal year and quarter (filings), publish time (news; filing date for filings), document and section type, context header, text, character range, token count, content hash of header and text, hash of the text alone, and a generated English `tsvector` with a GIN index |
| `embeddings` | One vector per embedded text and model, keyed by model name and content hash and shared by every chunk with that text. Each model has its own partial HNSW index (cosine), so vectors of different models are never searched together. |

## Project layout

```
talk-is-cheap/
├── universe.yaml            # companies, forms, history start, news names
├── feeds.yaml               # news feeds and backfill sources
├── src/talk_is_cheap/
│   ├── config.py            # typed settings
│   ├── cli.py               # talk-is-cheap command
│   ├── universe.py          # universe.yaml loader
│   ├── financials.py        # first/latest quarterly values, restatements
│   ├── news/
│   │   ├── adapters.py      # feed adapters (Yahoo, Google News, RSS, press releases)
│   │   ├── gdelt.py         # GDELT backfill: windows, splitting, pacing, resume
│   │   ├── normalize.py     # canonical URLs, timestamps, titles and summaries
│   │   ├── dedup.py         # near-duplicate headline clusters
│   │   ├── linking.py       # article-company links, method and confidence
│   │   ├── store.py         # articles, feeds, clusters and links in the database
│   │   ├── poll.py          # one poll of every feed
│   │   ├── locks.py         # database locks against overlapping runs
│   │   └── queries.py       # company news, earnings window, feed health
│   ├── retrieval/
│   │   ├── chunking.py      # sections and articles → chunks, offsets, token limits
│   │   ├── encoders.py      # embedding model, reranker and tokenizer (fastembed)
│   │   ├── index.py         # incremental indexing, context headers, vector indexes
│   │   ├── search.py        # filters, keyword, vector, hybrid (RRF) and rerank search
│   │   └── evaluate.py      # retrieval benchmark: recall@k and MRR
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
│       ├── models.py        # filings and financial data, documents, sections, news tables
│       ├── base.py, session.py, health.py
├── scripts/poll-news.sh     # one scheduled news poll, with log and overlap guard
├── alembic/                 # migrations
├── evals/retrieval/         # labeled questions for the retrieval benchmark
├── tests/                   # unit tests; integration/ uses a real database
│   └── fixtures/            # trimmed real SEC filings; news/ holds trimmed real feeds
├── docker-compose.yml
├── Makefile
└── .github/workflows/ci.yml
```

## Testing

```bash
make test        # all tests; integration tests are skipped if no database is reachable
make test-unit   # only tests that need no database
```

Integration tests never touch the database in `DATABASE_URL`. Each test run drops, recreates and migrates its own database, `talkischeap_test` on the local Docker server, so tests start from an empty database, as in CI. Set `TEST_DATABASE_URL` in the shell to use another one; its name must end in `_test`. Tests read neither `.env` nor the application's variables from the shell: they run with the defaults, and tests that need a setting set it themselves.

Tests never call the SEC or a news site. The edgartools adapter is tested against edgartools' own parser with SEC-shaped sample data, and ingestion is tested end to end against a real database with an in-memory source. Text conversion and section finding are tested on small fixtures trimmed from real SEC filings (public domain). News adapters are tested on feeds trimmed from the real ones; HTTP behaviour (304, 404, timeouts, malformed XML, GDELT's 429) is mocked with respx; clustering thresholds and linking rules are tested on real headline pairs, including false-match cases; polling, backfill and the news commands run end to end against the database with mocked feeds; and chunking, indexing and search run without downloading a model, using a word-count tokenizer, a bag-of-words embedder and a word-overlap reranker (`tests/retrieval_fakes.py`). Set `REQUIRE_DATABASE=1` to make integration tests fail, rather than skip, when the database is unreachable. CI sets it.
