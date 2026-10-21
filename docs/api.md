# HTTP API

`talk-is-cheap serve` serves the agent and the stored data over HTTP, so the web UI
([ui.md](ui.md)) and other clients can use the project without the CLI: a streaming
chat, and typed JSON endpoints for promises, track records, guidance, financials and
coverage. It is a thin layer over
the same query functions the commands use (`financials.py`, `guidance/queries.py`,
`promises/queries.py`, `promises/track.py`, `companies.py`) and the same agent graph as
`ask`. The OpenAPI document is at `/docs` (and `/openapi.json`), with every response
model and the chat stream's event models.

Code: `src/talk_is_cheap/api/` (`app.py` the factory and lifespan, `chat.py` the stream,
`routes.py` the data endpoints, `schemas.py` the models, `security.py` keys and rate
limits, `observability.py` request ids, logs and the request log).

## 1. Running it

Locally, against the database in `DATABASE_URL`:

```bash
make migrate                    # the API needs migration 0016 (request_log, feedback)
make api                        # 127.0.0.1:8080, reloads on code changes
LLM_PROVIDER=fake make api      # the same with the scripted model: chat costs nothing
uv run talk-is-cheap serve --host 0.0.0.0 --port 8080   # without reload
```

In Docker:

```bash
docker compose up --build       # db, then migrate (alembic upgrade head), then api
curl localhost:8080/readyz
docker compose run --rm api talk-is-cheap ingest news   # the same image runs any command
```

The `Dockerfile` installs the locked dependencies with uv in a builder stage and copies
the virtual environment into a slim runtime image that runs as the non-root user `app`.
Its health check calls `/readyz`. Compose reads `.env` (if there is one) for every
setting but `DATABASE_URL`, which points at the `db` service; the search models, SEC,
news and LLM caches live in the `cache` volume, so the models download once (about
150 MB, on the first start). `API_PORT` (default 8080) is the host port.

At startup the lifespan handler opens the database pool and the checkpointer (a
connection pool of `DATABASE_POOL_SIZE`), loads the search models and compiles the
agent's graph once. Each chat request runs that graph with its own budget, evidence log
and tool log, passed as the run's LangGraph `context` (`agent/graph.py`). Without an LLM
key the API still starts: chat answers `model_unavailable` and everything else works.

The API only reads the data tables, so it runs while ingestion and extraction jobs write
to them. It writes `request_log`, `feedback`, `agent_runs`, `llm_spend` and the
checkpointer's tables.

## 2. Auth, limits and budgets

**API keys.** Clients send `X-API-Key` with one of the comma-separated `API_KEYS`. A
missing or wrong key gets 401. Keys are compared in constant time and identified in logs
and tables by `key_` and 12 hex digits of their SHA-256, never by the key. `/healthz`,
`/readyz`, `/docs`, `/redoc` and `/openapi.json` are open. With no keys configured the
API refuses to start when `ENVIRONMENT=production` (`serve` exits with status 2) and lets
every request in, as `anonymous`, in `local` and `test`. `LLM_PROVIDER=fake` is refused
in production too.

**Rate limits.** Per key and per minute (a sliding window): `RATE_LIMIT_CHAT_PER_MINUTE`
(20) chat questions and `RATE_LIMIT_PER_MINUTE` (120) other requests, counted apart. Over
a limit: 429 with `Retry-After` in seconds. Counts are kept in the process's memory, so
each API process (and each restart) counts on its own.

**Budgets.** Before a chat question makes any model call, three caps are checked:

| Cap | Setting | What counts |
| --- | --- | --- |
| this key, per UTC day | `DAILY_LLM_BUDGET_USD` (0.25) | `llm_spend` rows of command `api chat` with the key's id since midnight UTC |
| all keys, per UTC day | `DAILY_LLM_BUDGET_TOTAL_USD` (1.00) | every `api chat` row since midnight UTC |
| the project | `LLM_BUDGET_USD` | the whole ledger, as for every command |

When one is spent, the stream's only event is an `error` with code `budget_exceeded` and
status 402, and no model is called. Otherwise the run's cost cap is what is left of the
smallest (and `LLM_COST_CAP`), and the agent's budget refuses any call whose largest
possible cost would exceed it, so a question stops before it can overspend, with the same
error. Every paid call is recorded in the ledger as soon as it returns, under the command
`api chat` with the key's id (`talk-is-cheap spend` lists it). The fake model costs
nothing and is not recorded.

**Size limits.** A question has 1 to 2,000 characters (422 otherwise); a thread holds at
most 30 questions (`thread_full`). One thread answers one question at a time
(`thread_busy` while another runs).

## 3. Endpoints

All list endpoints take `limit` (1 to 200, default 50) and `cursor`, and return
`next_cursor`, null on the last page. A cursor is an opaque offset into the list, so a
page read while a data job writes may repeat or miss a row at its edge. Unknown tickers
and ids are 404 (`{"detail": "NOPE is not loaded"}`); invalid parameters 422; a bad
cursor 400. Decimal figures are JSON strings, so no precision is lost.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/chat` | A question, optionally with a `thread_id`; Server-Sent Events (section 4) |
| `GET /v1/threads/{thread_id}` | The conversation from the checkpointer: questions and answers, each with its sources |
| `GET /v1/companies` | Loaded companies with coverage: latest quarter, guidance rows, linked articles, management claims, due claims |
| `GET /v1/companies/{ticker}/promises` | Promises, newest statement first. Filters: `verdict`, `speaker` (the roster name or an alias), `due_before`, `due_after`, `open`, `bold`, `include_scheduled` |
| `GET /v1/promises/{id}` | One promise as `promises show` prints it |
| `GET /v1/track-record` | `by=executive\|company\|all` (`all`: one record of every claim selected), `ticker`, `specific_only` |
| `GET /v1/companies/{ticker}/guidance` | Guidance rows with actuals, verdicts and review status; `metric`, `flagged` |
| `GET /v1/companies/{ticker}/financials` | `vintage=first\|latest` (default `first`), `quarters` (default 8) |
| `POST /v1/feedback` | Thumbs up or down on a chat answer, by `request_id`, with an optional comment |
| `GET /healthz` | Liveness |
| `GET /readyz` | The database answers, has pgvector, and is at this build's newest migration; 503 otherwise |

The examples below are from the stored data on 2026-10-21 (long fields cut).

**Companies.**

```bash
curl -H "X-API-Key: $KEY" "localhost:8080/v1/companies?limit=2"
```
```json
{"items": [
  {"ticker": "AAPL", "cik": 320193, "name": "Apple Inc.", "fiscal_year_end": "0926",
   "filings": 37, "latest_quarter": "FY2026 Q3", "guidance_rows": 0,
   "linked_articles": 5896, "management_claims": 74, "due_claims": 4},
  {"ticker": "ADBE", "...": "..."}],
 "next_cursor": "eyJvZmZzZXQiOiAyfQ=="}
```

`linked_articles` counts links of confidence 0.5 or more; `due_claims` counts promise
groups whose due date and grace period have passed, by the resolver's own rule
(`promises/resolve.py: plan_resolution`).

**Promises.**

```bash
curl -H "X-API-Key: $KEY" \
  "localhost:8080/v1/companies/NVDA/promises?speaker=Jensen%20Huang&due_before=2026-06-30&limit=1"
```
```json
{"items": [{
  "id": 1583, "ticker": "NVDA",
  "quote": "Huang added that demand for both platforms will outstrip supply well into next year, …",
  "speaker": "Jen-Hsun Huang", "role": "President and Chief Executive Officer",
  "date": "2024-05-23", "publisher": "finance.yahoo.com",
  "url": "https://finance.yahoo.com/news/demand-is-just-so-strong-nvidia-ceo-jensen-huang-tells-yahoo-finance-supply-cant-keep-up-012653951.html",
  "subject": "demand for Hopper and Blackwell platforms versus supply",
  "claim_type": "promise", "specificity": "partial", "bold": false,
  "rule": "Not resolvable: the claim states no checkable target (demand for Hopper and Blackwell platforms versus supply).",
  "due_date": "2025-12-31", "due_basis": "explicit",
  "verdict": null, "reviewed": false, "scored": true, "guidance_id": null,
  "measured": null, "target": null, "revised_before_due": false, "evidence": []}],
 "next_cursor": null}
```

`verdict` is the one that counts: a review's, else the resolver's, null when not
resolved. `scored` is false for vague claims and for claims that restate formal guidance
(scored with the guidance). `measured` and `target` are `{value, unit, display}`
(`"display": "$108 billion"`), null for event claims. Scheduled actions (splits,
dividends) are left out unless `include_scheduled=true`. `role` is the roster's role for
the speaker on the statement date.

**One promise** (`GET /v1/promises/1812`): everything above, plus `version`, `current`,
the full `claim`, `deadline_text`, `bold_basis`, `baseline`, framing `notes`, the
`review` and the `resolution`:

```json
{"resolution": {
  "verdict": "delivered", "model_verdict": "delivered", "rule_version": "v3+5",
  "resolved_at": "2026-10-20T20:51:11.588017Z", "reasoning": "Multiple newsroom and news items report RTX Spark…",
  "proof_date": "2026-08-26", "proof_date_source": "published", "grace_end": null,
  "days_late": null, "revised_before_due": false, "resolved_with": null,
  "measured_from": [],
  "findings": [{"evidence_id": "E1", "finding": "satisfies", "note": "Press release states …"}, "…"],
  "checks": [{"check": "evidence_exist", "passed": true, "detail": null}, "…",
             {"check": "on_time", "passed": null,
              "detail": "delivered 2026-08-26 (published), on time until unknown"}],
  "evidence": [{"id": "E1", "date": "2026-08-26", "tool": "search_filings",
                "provenance": "company_press_release",
                "url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/q2fy27pr.htm",
                "description": "…"}],
  "gathered": 26}}
```

Findings the judge marked irrelevant are left out, as `promises show` does; `passed` is
null for a check that is a note. Each piece of evidence has its `provenance`
(`sec_filing`, `company_press_release` or `news_report`), as citations do.

**Track records.**

```bash
curl -H "X-API-Key: $KEY" "localhost:8080/v1/track-record?by=executive&ticker=NVDA"
```
```json
{"min_resolved": 5, "items": [
  {"key": "NVDA (company)", "claims": 27, "resolved": 6,
   "by_verdict": {"delivered": 4, "delivered_late": 1, "walked_back": 1, "too_early": 3,
                  "cannot_determine": 6, "expired_unproven": 2},
   "expired": 2, "vague": 25, "too_few_to_judge": false,
   "rates": {"on_time_rate": 0.667, "walked_back_share": 0.167, "bold_share": 0.5,
             "bold_hit_rate": 0.667, "median_days_late": 239.0},
   "promise_ids": [1635, 1628, 1612, "…"]},
  {"key": "Jen-Hsun Huang (NVDA)", "claims": 8, "resolved": 2,
   "by_verdict": {"too_early": 1, "walked_back": 2}, "too_few_to_judge": true,
   "rates": null, "…": "…"}],
 "next_cursor": null}
```

A group with fewer than `min_resolved` resolved claims has `too_few_to_judge: true` and
`rates: null`. `by=all` gives one record, keyed `all`, of every claim selected (every
company's, or one company's with `ticker`): what the web UI shows for "All companies"
([ui.md](ui.md)). The counting rules are those of `promises track-record`
([promises.md](promises.md)).

**Guidance.**

```bash
curl -H "X-API-Key: $KEY" "localhost:8080/v1/companies/ADBE/guidance?limit=200"
```
```json
{"id": 1642, "issued_period": "FY2024Q4", "issued_at": "2024-12-11", "target_period": "FY2025",
 "metric": "revenue", "basis": "unspecified", "measure": "level", "unit": "usd",
 "low": null, "high": null, "point": "200000000.000000", "actual": "23769000000.000000",
 "verdict": "beat", "mark": "WRONG", "counts_in_verdicts": false,
 "review_status": "reviewed: wrong", "plausibility": "flagged", "plausibility_rule": "ratio",
 "plausibility_note": "guided $200.0M is 1% of FY2024's $21.50B (plausible: 50% to 200%)",
 "sources": [{"url": "https://www.sec.gov/…/adbeex991q424.htm",
              "document_name": "adbeex991q424.htm", "section_title": "Financial Targets",
              "char_start": 4811, "char_end": 5018}]}
```

`mark` follows the `guidance` command: `REVIEW` for a row flagged by the plausibility
check and not reviewed, `WRONG` for a row reviewed as wrong. Neither counts in verdicts
(`counts_in_verdicts: false`); the stored verdict is still shown, as the command shows
it. `flagged=true` lists only flagged rows, reviewed or not.

**Financials.**

```bash
curl -H "X-API-Key: $KEY" "localhost:8080/v1/companies/NVDA/financials?quarters=1"
```
```json
{"ticker": "NVDA", "vintage": "first", "items": [
  {"fiscal_year": 2027, "fiscal_quarter": 2, "label": "FY2027 Q2",
   "period_end": "2026-07-26", "gross_margin": "0.749753", "operating_margin": "0.662371",
   "metrics": {"revenue": {"value": "96221000000.000000", "unit": "USD",
                           "filed": "2026-08-26", "accession_no": "0001045810-26-000075",
                           "form": "10-Q", "derived": false, "derivation": null}, "…": {}}}],
 "next_cursor": null}
```

Margins are fractions (0.749753 is 75.0%). A derived quarter (from year-to-date totals)
has `derived: true` and says how.

**Threads.** `GET /v1/threads/888a469d2b24`:

```json
{"thread_id": "888a469d2b24", "turns": [
  {"question": "What was NVDA's revenue last quarter?",
   "answer": "NVDA's revenue in FY2027Q2 was $96,221M [1].",
   "sources": [{"number": 1, "label": "NVDA 10-Q filed 2026-08-26",
                "url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000075/nvda-20260726.htm",
                "provenance": "sec_filing"}]},
  {"question": "And Nvidia's revenue now?", "answer": "…", "sources": ["…"]}]}
```

Only questions and answers are returned, not tool calls or the verifier's feedback. A
question that was stopped has the answer "The answer was stopped before it was
finished." A thread id has 1 to 64 letters, digits, `_`, `.`, `:` or `-`.

**Feedback.**

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"request_id": "6babef51777c415b83b5e3cf48bbb7be", "rating": "up", "comment": "clear"}' \
  localhost:8080/v1/feedback
```
```json
{"id": 1, "request_id": "6babef51777c415b83b5e3cf48bbb7be", "rating": "up"}
```

201 when saved; 404 when no chat request has that id. Stored in `feedback` with the key
id, for evals.

**Health.** `GET /healthz` is `{"status": "ok"}` while the process runs. `GET /readyz`:

```json
{"status": "ready", "database": true, "migration": "0016", "head": "0016", "detail": null}
```

503 with `"status": "not_ready"` and the reason when the database does not answer within
3 seconds, lacks pgvector, or is at another migration than the newest this build has.

## 4. Streaming chat

```bash
curl -N -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"question": "What was NVDA'"'"'s revenue last quarter?"}' localhost:8080/v1/chat
```

The response is `text/event-stream`. `X-Request-ID` and `X-Thread-ID` come in the
headers; pass the thread id back as `thread_id` for a follow-up ("And the quarter
before?"). Each event is `event: <name>` and one `data:` line of JSON; a comment line
(`: keep-alive`) is sent after 10 seconds without an event. The stream of that question
with the real model (Haiku 5.5), tokens shortened:

```
event: status
data: {"node":"router","step":"Reading the question…"}

event: status
data: {"node":"executor","step":"Deciding what to look up…"}

event: status
data: {"node":"tools","step":"Reading reported financials…"}

event: tool_start
data: {"call_id":"01a15eb0-…","name":"get_actuals","arguments":{"ticker":"NVDA","metrics":["revenue"],"periods":["latest"]}}

event: tool_end
data: {"call_id":"01a15eb0-…","name":"get_actuals","summary":"1 evidence item: E1","evidence":["E1"],"error":null}

event: status
data: {"node":"executor","step":"Deciding what to look up…"}

event: status
data: {"node":"verifier","step":"Checking the draft against the evidence…"}

event: status
data: {"node":"answer","step":"Writing the answer…"}

event: token
data: {"text":"Nvidia's "}

event: token
data: {"text":"revenue "}

(… one event per word …)

event: citation
data: {"id":1,"url":"https://www.sec.gov/Archives/edgar/data/1045810/000104581026000075/nvda-20260726.htm","title":"NVDA 10-Q filed 2026-08-26","provenance":"sec_filing","evidence":["E1"]}

event: done
data: {"request_id":"62f880694e6445eda1a541e67aabe8d3","thread_id":"c41e2db5657b","question_type":"lookup","input_tokens":14099,"output_tokens":360,"cost":"0.0015899","seconds":4.639,"revisions":0,"cache_hit":false,"verified":true,"tool_calls":1}
```

The answer, joined from the tokens: "Nvidia's revenue for its latest reported quarter,
FY2027Q2 (ended July 26, 2026), was $96.2 billion ($96,221M) [1]."

| Event | Data |
| --- | --- |
| `status` | `node` (router, planner, executor, tools, verifier, answer) and a readable `step`; for a tool, what it does ("Searching filings…") |
| `tool_start` | `call_id`, `name`, `arguments` |
| `tool_end` | `call_id`, `name`, `summary` ("2 evidence items: E4, E5", "error: …", "nothing found"), the citation ids it added, `error` |
| `token` | `text`: the next word of the answer and the space after it |
| `citation` | `id` (the `[n]` in the answer), `url`, `title`, `provenance` (`sec_filing`, `company_press_release`, `news_report`), the evidence ids behind it |
| `error` | `code`, `status` (the HTTP status it stands for), `message`, `request_id`; always the last event |
| `done` | `request_id`, `thread_id`, `question_type`, `input_tokens`, `output_tokens`, `cost` (paid calls only), `seconds`, `revisions`, `cache_hit` (every model call came from the cache; null when none was made), `verified`, `tool_calls`; the last event |

**Tokens carry only the verified answer.** The agent drafts, the verifier checks the
draft, and the answer node edits it; an edit that breaks the checks is dropped and code
adds notes and renders citations. Streaming the model's tokens as they are generated
would show text the reader may never get, so `token` events are sent once the answer
node has finished, word by word, followed by the `citation` events. `status`, `tool_start`
and `tool_end` show progress until then. An answer served from the response cache (the
same question on a new thread, the same day) streams the same way at no cost.

**Errors.** Errors before the stream (401, 422, 429) are ordinary JSON responses. Once
the stream has started its status is 200, and a question that does not finish ends with
an `error` event:

| Code | Status | When |
| --- | --- | --- |
| `budget_exceeded` | 402 | a cap is spent (no model was called), or the next call could exceed what is left |
| `thread_full` | 409 | the thread already holds 30 questions |
| `thread_busy` | 409 | another question on the thread is running |
| `timeout` | 504 | the question took longer than `CHAT_TIMEOUT_SECONDS` (60) |
| `model_unavailable` | 503 | no LLM key is configured |
| `model_error` | 502 | the provider failed or returned something unusable |
| `internal_error` | 500 | anything else (logged with its traceback) |

**A slow tool call does not end the question.** A tool call that takes longer than
`TOOL_TIMEOUT_SECONDS` (10) is stopped, and its result is an error the model reads like
any other tool failure: "search_filings timed out after 10 seconds; try a narrower query
or another tool". The stream shows it as a `tool_end` with that `error` (and
`"summary": "error: …"`), and the run goes on: the model can narrow the query, call
another tool or answer without that result. The call is logged in `agent_runs.tool_calls`
with its error. Only the limit on the whole question, `CHAT_TIMEOUT_SECONDS` (60), ends
the stream with an `error` event.

**When a question stops early** (the question's timeout, a budget error, or the client
going away), the stream closes at once and the run is cancelled. A model call in flight
is not abandoned: it is left to finish (up to 60 seconds) in the background so its cost
is known, then counted and recorded in the ledger like any other call. The stopped run is
logged to `agent_runs` with an empty answer and its paid calls under `usage.stopped`, and
the thread's unanswered turn is closed with "The answer was stopped before it was
finished.", so the next question on the thread is read as a question of its own. The
thread takes no new question until this is done (`thread_busy`).

## 5. Logging

Every response carries `X-Request-ID` (32 hex digits), which is also the `done` event's
`request_id` and the key `feedback` takes. structlog writes one line per request with
`request_id`, `method`, `path`, `status`, `latency_ms`, `key_id` and, for chat, `outcome`:
JSON in production, coloured text elsewhere. Health checks and docs are logged at debug
level, so Docker's probe every 15 seconds stays out of the log.

Each request but health checks and the API docs is stored in `request_log`: request id,
time, method, path, status, latency in milliseconds and key id. A chat request adds how
it ended (`completed`, `budget_exceeded`, `timeout`, `disconnected`,
`thread_full`, `thread_busy`, `model_error`, `internal_error`), its thread, the
`agent_runs` row, and the model, tokens, cost and number of tool calls. A finished answer
is stored before its `done` event is sent, so feedback can follow at once.

```sql
-- what each key spent on chat today, and how questions ended
SELECT key_id, outcome, count(*), sum(cost) FROM request_log
WHERE path = '/v1/chat' AND started_at >= current_date GROUP BY 1, 2;
-- answers rated down, with the question
SELECT f.comment, r.question, r.answer FROM feedback f
JOIN request_log l USING (request_id) JOIN agent_runs r ON r.id = l.agent_run_id
WHERE f.rating = 'down';
```

## 6. Tests

`tests/integration/test_api.py` runs the app against the test database with the scripted
model (`agent/fake.py`), never a provider: each endpoint's shape, filters, pagination and
404s on seeded rows; the chat stream's event order, citations, follow-ups on one thread,
the 2,000-character and 30-question limits, a chat timeout (an `error` event, the
thread's turn closed), a slow tool call (an error the model reads; the answer follows), a
client
disconnecting mid-call (the stream ends at once; the call in flight is recorded once it
finishes), the three budgets (no model call), the ledger and `agent_runs` records; keys,
rate limits with `Retry-After`, production refusing to start without keys; the request
log and `X-Request-ID`; and the migration's round trip on a throwaway database.
`tests/test_api.py` covers keys, rate limit windows, settings, cursors and the mapping of
LangGraph's events, without a database.

## 7. Known limitations

- **Rate limits are per process**, in memory: several API processes behind a load
  balancer each allow the full rate, and a restart resets the counts.
- **Daily budgets are checked when a question starts.** Questions of one key running at
  the same time each see the spending recorded before they started, so together they
  can pass the daily cap by up to the cost of the questions in flight (a few tenths of a
  cent each with Haiku). `LLM_BUDGET_USD` is enforced the same way.
- **Cursors are offsets**: a page read while a data job inserts rows may repeat or skip
  a row at its edge.
- **Tokens arrive after verification**, not as the model writes them; a question takes as
  long to show its first word as the whole run takes (5 seconds for a lookup, 10 to 20
  for questions that need revisions).
- **One thread, one question at a time, in one process**: the `thread_busy` check is in
  memory, so two API processes could run questions on the same thread at once.
