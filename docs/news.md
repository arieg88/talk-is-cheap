# News

How `talk-is-cheap ingest news` collects headlines, how articles are deduplicated and
linked to companies, and what each source returned when it was checked.

Polling and backfill store metadata only: headline, the feed's own summary, link,
publisher and time. The body of one article per linked story is fetched separately, for
statement extraction only (section 8).

## 1. How it works

1. **Feeds.** `feeds.yaml` lists the feeds; each entry names an adapter `type`
   (`news/adapters.py`). Per-company entries (`companies: all`) expand to one feed per
   company in `universe.yaml`, with `{ticker}` and `{query}` filled in. `{query}` is the
   company's name and aliases, quoted and joined by `OR`.
2. **Fetch.** All feeds are fetched concurrently with httpx, at most two at a time per
   host, with a 20-second timeout and the `NEWS_USER_AGENT` User-Agent. A feed's stored
   ETag and Last-Modified are sent back as `If-None-Match` and `If-Modified-Since`; a
   304 ends the poll for that feed. Otherwise the items are parsed and fingerprinted
   (link, title and time, order ignored). When the fingerprint matches the last one, the
   feed is reported as `not modified` and nothing is written.
3. **Normalize.** Spam is dropped and counted (section 3); every other item becomes an
   article (`news/normalize.py`, `news/store.py: normalize_item`).
4. **Deduplicate.** An article whose canonical URL is already stored is not stored
   again. The new feed is recorded in `article_feeds`, and its links are added. A new
   article joins the cluster of a near-duplicate headline published within 48 hours, or
   starts its own (`news/dedup.py`).
5. **Link.** Companies are found in the headline, summary and publisher, and from
   per-company feeds (`news/linking.py`). During a poll a link only gains confidence.
6. **Health.** Every poll updates the feed's row: last poll, last success, last new
   item, consecutive failures and the last error.

Each feed is stored in its own transaction. A feed that times out, answers an error,
sends malformed XML or fails while storing is recorded as a failure and the next feed
goes on.

A poll takes a Postgres advisory lock (`news/locks.py`); a poll that starts while another
holds it prints "another news poll is still running; skipped" and exits 0. A backfill
takes a second lock, so a poll and a backfill can run together; articles are inserted
with `ON CONFLICT DO NOTHING`, so the same article arriving from both is stored once.

`talk-is-cheap ingest news --reprocess` applies the current rules and `universe.yaml` to
every stored article in one transaction, holding both locks: it deletes spam, cleans
titles again, rebuilds the clusters in publish order and replaces every link
(`store.py: reprocess`). Run it after changing names, aliases or excluded phrases.

## 2. Sources

Checked live on 2026-10-18 and 2026-10-19.

### Per-company feeds

| Feed | Result | Validators | Notes |
| --- | --- | --- | --- |
| Yahoo Finance `feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}` | Works for all 15 tickers, 16-20 items each | None. `lastBuildDate` changes on every request | Links go to the publisher's site with `?.tsrc=rss`. No source element, so the publisher is the link's domain (`fool.com`, `finance.yahoo.com`). Many items are about other companies: the NVDA feed carried stories about Green Thumb, Coca-Cola and Nike. |
| Google News search `news.google.com/rss/search?q={query}` | Works for all 15, about 100 items each | None | Links are Google redirect URLs (`news.google.com/rss/articles/CBM...`), which cannot be resolved without fetching them, so the same story from Yahoo and Google has two URLs and is matched by headline. The publisher is the `<source>` element; titles end in " - Publisher", which is removed. The summary only repeats title and publisher, so it is dropped. The search ignores case and searches full text, so "Apple" also finds "apple festival" and "Intel" finds "intel officials". |

### Market-wide feeds

| Feed | Result | Validators | Notes |
| --- | --- | --- | --- |
| MarketWatch top stories `feeds.content.dowjones.io/public/rss/mw_topstories` | Works, 10 items | ETag; answers 304 | Mostly personal finance. `?mod=mw_rss_topstories` is removed. |
| CNBC earnings `cnbc.com/id/15839135/device/rss/rss.html` | Works, about 30 items over a month | None | Chosen over CNBC top news (id 100003114), which is general news. CNBC technology (id 19854910) also works. |
| `marketwatch.com/rss/topstories` | Redirects (301) | | Replaced by the Dow Jones feed above |
| MarketWatch MarketPulse, Real-time Headlines (`mw_marketpulse`, `mw_realtimeheadlines`) | Answer 200, but the newest items are from June-July 2025 | | Not used |
| WSJ Markets `feeds.a.dj.com/rss/RSSMarketsMain.xml` | Answers 200, unchanged since January 2025 | | Not used |

### Company newsrooms and investor relations

| Company | Feed | Result |
| --- | --- | --- |
| Nvidia | `nvidianews.nvidia.com/releases.xml` | Works, 20 items, ETag and Last-Modified |
| Apple | `apple.com/newsroom/rss-feed.rss` | Works (Atom), 20 items, Last-Modified |
| Microsoft | `news.microsoft.com/source/feed/` | Works, 10 items, ETag. `news.microsoft.com/feed/` answers but stopped in May 2025 |
| AMD | `ir.amd.com/news-events/press-releases/rss` | Works, 10 items. Dates have two-digit years ("Mon, 12 Oct 26"); every response embeds its generation time |
| Meta | `about.fb.com/news/feed/` | Works, 10 items, ETag. `investor.atmeta.com` answers 403 |
| Salesforce | `salesforce.com/news/feed/` | Works, 10 items, ETag. `investor.salesforce.com` answers 403 |
| Cisco | `newsroom.cisco.com/...rssfeed.json?feed=all` | Works, 20 items. Items with the same time come in a different order on each request |
| Alphabet, Amazon, Netflix, Oracle, Qualcomm | `abc.xyz/investor/rss`, `ir.aboutamazon.com`, `ir.netflix.net`, `investor.oracle.com`, `investor.qualcomm.com` (`/rss/news-releases.xml`) | 403: the investor sites block automated clients |
| Intel, Tesla, Adobe | `intc.com`, `ir.tesla.com`, `adobe.com/investor-relations` | 404 or an HTML page; no feed found |

Companies without a feed are still covered by Yahoo and Google News; their own posts
reach Google News, where the publisher rule (section 5) links them.

### GDELT DOC 2.0 (backfill)

- **Endpoint:** `api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json`, no key.
- **How far back:** articles from January 2024 are returned (a backfill of the week of
  2024-01-01 for Nvidia stored them), so the full `history_start` range is searchable.
  Whether the archive reaches GDELT's announced January 2017 start was not tested.
- **Queries:** GDELT returns at most 250 articles per query (counted before local
  filtering) and answered "Your query was too short or too long" to one query naming all
  28 configured publisher domains (743 characters); queries of about 100 characters were
  answered. So the domains are sent in batches of at most 250 characters
  (`MAX_QUERY_CHARS`), four batches with the current list.
  - Companies flagged `gdelt_filter_publishers: true` in `universe.yaml` (Apple,
    Amazon, Alphabet, Meta, Tesla, Intel, Oracle, Adobe: names that are common words or
    heavily covered) are always searched with the domain batches: GDELT's search ignores
    case, so "Apple" or "Adobe" alone also returns the fruit and the building material.
  - Any other company is searched by name alone first, with publishers filtered
    locally: one request when the window is quiet. When that answer holds 250 articles,
    they are kept and the same window is searched again with the domain batches instead
    of being halved.
  - Only a domain-batch answer that still holds 250 articles is split in half, down to
    30 minutes (`MIN_WINDOW`, GDELT dates have 15-minute resolution); a part still full
    there is recorded as truncated and counted by `feeds --backfill`.
- **Connections:** the request right after each answer used to fail in 0.1 seconds with
  "Server disconnected without sending a response" (7 times in one 30-minute log): GDELT
  closes idle connections without saying so. Every request now asks for a new
  connection (`Connection: close`).
- **Dates:** `seendate` is when GDELT's crawler saw the article, not when it was
  published; such articles have `time_precision = 'seen'` and are marked `~` by
  `talk-is-cheap news`.
- **Titles** come with spaces around punctuation ("Buybacks . The Real Prize"), which are
  removed.
- **Errors in a query** (a phrase that is too short, unbalanced parentheses) come back as
  plain text with status 200 and fail that company's run at once.
- **Refusals:** GDELT asks for at most one request every 5 seconds and answers others
  with HTTP 429 and "Please limit requests to one every 5 seconds". As of 2026-10-19 the
  API refuses most requests intermittently from every network tested, including a phone
  on a different network on its first request. Every answer, refusals included, takes
  about 10 seconds, so the service is overloaded and a 429 means "busy, retry", not a
  penalty: retrying the same query every 10 seconds was answered within 1 to 5 tries,
  for 2024 dates as well.
- **Retries:** after a refusal, a timeout, a dropped connection ("Server disconnected
  without sending a response") or a server error, the same request is retried 10
  seconds after the answer (`RETRY_PAUSE`), and requests never start closer than 5
  seconds apart (`MIN_INTERVAL`). Requests get 30 seconds to connect and 90 to answer
  (`TIMEOUT`). After 30 failures in a row (`MAX_FAILURES`, about 10 minutes) the window
  is skipped and left unrecorded, the run says the GDELT API kept refusing requests,
  and it moves on; running the same command again fetches the skipped windows. Every
  attempt is printed with its outcome and response time.
- **Speed:** measured on 2026-10-19 (each answer 10-21 seconds, about one request in
  four answered):

  | Window | Queries | Requests | Minutes | Articles kept |
  | --- | --- | --- | --- | --- |
  | Nvidia, week of 2024-01-08, name only then halving | before | 13 | 24.0 | 236 |
  | Nvidia, week of 2024-01-08, name then publisher batches | after | 5 | 8.2 | 268 |
  | Apple, week of 2026-06-01, name only then halving | before | 9+ | 29 for 16 of 168 hours | 21 |
  | Apple, week of 2026-06-01, publisher batches | after | 4 | 4.2 | 221 |
  | Nvidia earnings, 2024-05-21 to 05-26 | after | 5 | 7.4 | 371 |
  | Apple earnings, 2026-07-29 to 08-03 | after | 4 | 3.0 | 199 |

  Quiet companies take one request (Cisco, Qualcomm, Salesforce: 1.0-2.2 minutes a
  week). The earnings-window backfill of all 15 companies (165 windows since 2024) needs
  424 to about 730 requests, 10 to 18 hours of unattended running at 87 seconds per
  answered request (`SECONDS_PER_ANSWER`). A weekly backfill since 2024-01-01 is about
  2,190 windows, roughly ten times as many.

## 3. Normalization

| What | Rule | Where |
| --- | --- | --- |
| URL | Lower-case scheme and host, no fragment or default port, tracking parameters removed (`utm_*`, `mod`, `tsrc`, `.tsrc`, `source`, `oc`, `cmpid`, `ncid`, `fbclid`, `gclid`, `guccounter`, ...), remaining parameters sorted. The canonical URL is stored; its SHA-256 is the dedup key. | `normalize.py: canonical_url` |
| Time | RFC 822 (including two-digit years), ISO 8601 and GDELT's `20261001T201500Z`, all to UTC. A time without a zone is UTC. No time, or one more than an hour in the future: the fetch time, with `time_precision = 'fetched'`. | `normalize.py: parse_timestamp`, `store.py: normalize_item` |
| Title | HTML removed, entities decoded (twice, for feeds that escape twice), whitespace collapsed. A trailing " - X" or " \| X" is removed when X is the item's publisher, alone or with "news", "business", "blog" and the like ("\| CNN Business" from CNN, "- GSMArena.com news", "- CHOSUNBIZ" from chosunbiz.com), or a name in `KNOWN_PUBLISHERS` ("\| NVIDIA Technical Blog", "- The Telegraph"). Other trailing text stays ("- Here's Why", "- Oracle (NYSE:ORCL)"). | `normalize.py: clean_title` |
| Summary | The same, then capped at 500 characters on a word boundary with "…". Dropped when it only repeats the title. | `normalize.py: clean_summary` |
| Spam | Dropped before storing, and counted per feed in the poll summary: titles with mathematical alphanumerics ("𝐋𝐈𝐕𝐄") or full-width Latin letters ("Ｆｒｅｅ"), bracketed watch tags ("++[Netflix-Watch]TV!]"), or "live stream" together with "free", "online", "HD" or "Reddit". "Netflix will live stream NFL games" is kept. | `normalize.py: is_spam` |
| Publisher | The feed's `<source>`, the configured publisher, the feed's title, or the link's domain, by adapter. Some Google News sources are named by a URL ("https://tech-insider.org"); the domain is used. | `adapters.py`, `store.py: _publisher` |

## 4. Near-duplicate clustering

- **Rule:** two headlines are the same story when their normalized texts (lower case,
  punctuation and a trailing "By Reuters" removed) score at least
  `NEAR_DUPLICATE_THRESHOLD = 85` with rapidfuzz (the better of `ratio` and
  `token_sort_ratio`), contain the same numbers, and were published within
  `CLUSTER_WINDOW = 48 hours` of each other. Headlines under four words must be equal.
- **Different companies:** two different headlines are never merged by similarity unless
  they name the same companies (`CompanyMatcher.match` on the title). "Should You Buy
  Adobe Stock For Its Cash?" and "Should You Buy Qualcomm Stock For Its Cash?" score 90
  but are two stories. The same title from several publishers is always one story.
- **Threshold:** set on real pairs from the feeds above. Rewrites of one story scored
  86-99 ("US appeals court blocks class actions over Tesla's self-driving claims" /
  "US Appeals Court Overturns Class Action Lawsuit Over Tesla Self-Driving Claims": 86).
  The closest different stories scored 83 ("NVIDIA Announces New Investments in
  France's Startups" / "... in the UK Startup Ecosystem"). Templated releases that
  differ only in an amount ("Commits $250 Million" / "Commits $1 Billion") are kept
  apart by the numbers rule.
- **Where:** `dedup.py`, `store.py: named_companies`. **Tests:** `test_news_dedup.py`,
  five same-story and six different-story pairs, and the Adobe/Qualcomm pair.
- **Limits:** a story rewritten with different words ("Microsoft Scraps UK Data Center
  Deal" / "Microsoft pulls out of UK data center deal": 84) stays two clusters.
  Companies outside `universe.yaml` are not known, so two templated headlines about two
  such companies can still merge.

## 5. Company linking

Each link has a method and a confidence. `talk-is-cheap news` shows links of 0.5 and
above by default; `HIGH_CONFIDENCE` is 0.8.

| Evidence | Method | Confidence |
| --- | --- | --- |
| The company's own newsroom or IR feed | `issuer` | 1.0 |
| Ticker in market notation: `$NVDA`, `(NVDA)`, `NASDAQ: NVDA`, `NYSE:CRM` | `ticker` | 0.95 |
| Name or alias in the headline | `headline` | 0.9 |
| Name only after "ex-" ("Ex-Microsoft exec") | `headline` | 0.6 |
| A publisher whose name starts with the company's name ("Netflix", "Cisco Talos Blog", "Google DeepMind") | `issuer` | 0.9 |
| A bare upper-case ticker in the headline ("NVDA slips") | `ticker` | 0.8 |
| Name or alias in the summary only | `summary` | 0.6 |
| A per-company feed alone (Yahoo, Google News, GDELT) | `feed` | 0.3 |

A per-company feed that agrees with a mention adds 0.1, once.

Matching rules (`linking.py`):

- Names match in their configured spelling, Title Case or UPPER CASE, as whole words,
  never in lower case ("apple", "meta", "intel" are not companies), and not inside a
  domain or e-mail address. A hyphen is a word boundary: "Nvidia-Backed", "MBG-Nvidia",
  "AMD-powered", "All-AMD" and "Arm-Qualcomm" all link. Compounds where the name means
  something else are excluded phrases ("Meta-analysis", "Meta-model"), and a number
  joined to a name is a measure ("7-Tesla MRI"). Possessives match ("Nvidia's").
- Tickers stay strict: a bare ticker never matches next to a hyphen ("NVDA-linked").
- Upper case of a name shorter than five letters ("META") reads as an acronym and only
  counts in an all-caps headline.
- Bare tickers only count in mixed-case headlines and never for tickers that are words
  or acronyms (`COMMON_WORD_TICKERS`: CRM, META, ...).
- `exclude_phrases` per company in `universe.yaml` remove phrases where the name means
  something else ("Big Apple", "Apple Hospitality", "Amazon rainforest", "Nikola Tesla",
  "Oracle of Omaha", "Cisco Aguilar", a Nevada politician, and "Add Us On Google",
  boilerplate Google News scrapes into some titles). When the text contains one
  and does not otherwise name the company, per-company feeds do not link it either.
- A publisher must start with the name: the student paper "The Oracle" is not Oracle.

**Tests:** `test_news_linking.py`, including false-match cases for each rule.

**Limits:** a name that is also a common capitalized word at the start of a sentence
("Apple orchards report...") still matches unless it is an excluded phrase.

## 6. Feed health

`talk-is-cheap feeds` shows each feed's status:

- `failing`: the last poll failed (the failure count and last error are shown);
- `stale`: no new item for 72 hours (`STALE_AFTER`). Company newsrooms that post weekly
  are often stale;
- `new`: never fetched successfully;
- `ok`: otherwise.

Backfill sources (`gdelt:<ticker>`) are not polled feeds and are left out of this table
and its totals. `talk-is-cheap feeds --backfill` shows them: weeks fetched, the first
and last of them (with any gaps between), articles, weeks still over the record limit, last success and last error.

## 7. Backfill

Two kinds of window (`news/gdelt.py`):

- `talk-is-cheap ingest news --backfill --around-earnings [--days-before 1]
  [--days-after 3] [--tickers ...]` (recommended): a window around each earnings press
  release stored by `ingest filings` (deliveries reports are not releases), from the
  day before the filing date through three days after it, newest quarters first across
  all companies. Windows are cut where RSS polling began (the first successful poll,
  2026-10-18), since later news comes from the feeds, and dropped when windows already
  fetched cover them. Companies whose filings are not loaded have no windows.
- `talk-is-cheap ingest news --backfill --since 2024-01-01 [--until ...] [--tickers ...]`:
  every week from a start date, company by company.

Before fetching, the command prints how many windows are left, how many were already
fetched or covered by RSS polling, the fewest requests they need and how long that takes
at the measured pace; busy windows take more. Articles are kept from the publisher domains
listed under `backfill` in `feeds.yaml`, and each answered part is stored at once,
through the same normalization, deduplication and linking as feed items. A window is
recorded in `backfill_windows` when all of it is stored; a re-run skips recorded windows.
Windows that ended less than a day ago are fetched but not recorded, since GDELT is still
adding to them.

The run prints every request (part, attempt number, outcome, response time) and one
timestamped line per window (windows done, new articles, requests, time left), so it can
run unattended with output appended to a log (README, "Long backfills"). It is safe to
stop at any point: Ctrl+C or `kill` (SIGTERM) prints a summary of the run (windows
fetched, average time per window, new articles), and loses at most the window in
progress, whose stored articles are kept and found again as duplicates on the next run.
A window that fails 30 times in a row is skipped and fetched by the next run (section
2); a company whose window fails with another error gets no further windows in that run.

## 8. Article text

`talk-is-cheap ingest article-text [--tickers NVDA] [--limit 100]` (`news/article_text.py`)
fetches the text of one article per story (dedup cluster) linked to a company with
confidence 0.5 or more, into `article_texts`, for the claims executives make in the news
([statements.md](statements.md)). It is a separate command and module: polling, backfill,
clustering and linking are unchanged.

- **Which article:** a direct link before a Google News link (Google's robots.txt
  forbids following its redirects), then the longest summary, then the earliest; a story
  whose article is unavailable for good moves on to its next article.
- **Rules:** robots.txt of every host (and of every host a redirect leads to) is read
  first and obeyed for the first word of `ARTICLE_USER_AGENT`; at most one request per
  second per host, or its Crawl-delay; 15-second timeout; no retries within a run.
- **Outcomes:** `ok` with the text, or `unavailable` with the reason. 401, 402, 403, 404,
  410, 451, robots.txt refusals and pages without article text are final; timeouts,
  connection and server errors, and unreadable robots.txt files are tried by the next
  run. Each result is stored as it arrives, so a run can be stopped and resumed.
- **Summary:** one line per publisher: fetched, unavailable (by reason) and skipped
  (text stored, or nothing left to try).

Results on 2026-10-20 are in [statements.md](statements.md), section 1: text for 538 of
2,371 stories; 1,560 stories have only a Google News link.
