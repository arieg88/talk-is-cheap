# News

Where the news comes from: `talk-is-cheap ingest news` (RSS polling and the GDELT
backfill), `ingest archives` (past articles of interview-heavy publishers from Common
Crawl and the Wayback Machine) and `ingest finnhub` (Finnhub company news); how articles
are deduplicated and linked to companies; and what each source returned when it was
checked. Every source feeds the same store, deduplication and linking, and the same
article text, statements and promises downstream.

Polling and backfill store metadata only: headline, the feed's own summary, link,
publisher and time. The body of one article per linked story is fetched separately, for
statement extraction only (section 8); archived articles arrive with their body
(section 9).

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

### Technology and business press, and press release wires

Checked live on 2026-10-21; every feed below returned items on its first poll.

| Feed | Items on the first poll |
| --- | --- |
| TechCrunch `techcrunch.com/feed/` | 20 |
| The Verge `theverge.com/rss/index.xml` | 10 |
| Ars Technica `feeds.arstechnica.com/arstechnica/index` | 20 |
| The Register `theregister.com/headlines.atom` | 50 |
| Tom's Hardware `tomshardware.com/feeds/all` | 50 |
| Fortune `fortune.com/feed/` | 10 |
| Business Wire technology `feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==` | 96 |
| GlobeNewswire semiconductors `globenewswire.com/RssFeed/industry/9576-Semiconductors/...` | 20 |
| PR Newswire technology `prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss` | 20 |

No wire offers a per-company feed that can be found without an account: GlobeNewswire's
organization feeds need an id that only its script-rendered pages show
(`RssFeed/organization/NVIDIA` answers 400), its keyword feed (`RssFeed/keyword/NVIDIA`)
returns partners' releases that mention a name, not the issuer's, and Business Wire and PR
Newswire publish feeds by industry and subject only. So the wires' technology feeds are
polled, and releases are linked by the names in their headlines and summaries. Business
Wire's home feed (`?rss=G1QFDERJXkJeEFtRXg==`) answered with no items and is not used.

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

Backfill sources (`gdelt:<ticker>`, `finnhub:<ticker>`, `commoncrawl:<ticker>`,
`wayback:<ticker>`) are not polled feeds and are left out of this table and its totals.
`talk-is-cheap feeds --backfill` shows them: windows fetched (GDELT and Finnhub), the
first and last of them, articles, windows still over the record limit, last success and
last error.

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

`talk-is-cheap ingest article-text [--tickers NVDA] [--limit 100] [--recovered-only |
--finnhub [--dry-run] | --common-crawl] [--slow]` (`news/article_text.py`) fetches the
text of one article per story (dedup cluster) linked to a company with confidence 0.5 or
more, into `article_texts`, for the claims executives make in the news
([statements.md](statements.md)). It is a separate command and module: polling,
backfill, clustering and linking are unchanged.

- **Which article:** a direct link before a Google News link (Google's robots.txt
  forbids following its redirects), then the longest summary, then the earliest; a story
  whose article is unavailable for good moves on to its next article. An article's link
  is its `publisher_url` when one is known: a Google News story matched on Finnhub, or a
  Finnhub link already resolved (section 10). `--recovered-only` fetches only the
  matched Google News stories; `--finnhub` only Finnhub stories that pass the pre-filter
  of section 10, oldest first.
- **robots.txt** of every host (and of every host a redirect leads to) is read first and
  obeyed for the first word of `ARTICLE_USER_AGENT`. It is kept on disk under
  `NEWS_CACHE_DIR/robots` for a day (`ROBOTS_TTL`), so a URL it refuses (news.google.com,
  StockTwits, The Register, Yahoo Finance's disallowed paths) is skipped without any
  request; such refusals are counted apart and left out of the success rate.
- **Pacing:** at most one request per second per host (or its Crawl-delay; `--slow`: one
  every 10 seconds), every host in its own queue and hosts in parallel, at most 8
  requests at a time, 15-second timeout. A Finnhub link is resolved first (one request
  to finnhub.io, not followed) and its publisher URL stored on the article, so the
  article joins its publisher's queue.
- **Outcomes:** `ok` with the text, or `unavailable` with the reason.
  - Final: 401, 402, 403, 404, 410 and 451; robots.txt refusals; paywalls (subscription
    prompts, and Seeking Alpha's 403, which is its paywall: `paywall: HTTP 403
    (seekingalpha.com)`); pages without article text.
  - Tried by the next run: timeouts, connection and server errors, unreadable robots.txt
    files, and **interstitials**: a consent page a redirect leads to, or HTTP 202 with an
    empty body (Barchart's bot check). A host that answers 3 interstitials in a row
    (`PAUSE_AFTER`) is paused for the rest of the run while the others continue; its
    remaining articles are left unstored, for a later `--slow` run.
  - Each result is stored as it arrives, so a run can be stopped and resumed.
- **Summary:** per publisher: fetched, unavailable (by reason), refused by robots.txt,
  paused and skipped; the success rate of the articles requested; the paused hosts.
- **Common Crawl fallback** (`--common-crawl`, `news/cc_fallback.py`): instead of
  fetching live, looks up articles whose live page is gone (404, 410) or had no text in
  the Common Crawl index, by publisher URL, in the crawls that ran after the article was
  published, newest first, at most 3; the first successful HTML capture's text is stored
  with `reason = "text from Common Crawl <crawl>"`, and an article found nowhere is marked
  `no Common Crawl copy` and not looked up again. Refusals are never got around with a
  copy: robots.txt refusals, 401, 403 and 405 answers, paywalls and interstitials are
  never looked up. Common Crawl gets one request a second, with backoff and the disk
  cache. The Wayback Machine is not used.

**What failed, 2026-10-21.** Of 1,480 articles marked unavailable in two hours (Finnhub
links resolved to their publisher to count them):

| Domain | Total | robots.txt | HTTP 403 | No text | HTTP 404 | Paywall (402) | HTTP 405 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| news.google.com | 875 | 875 | | | | | |
| finance.yahoo.com | 227 | 126 | | 67 | 34 | | |
| benzinga.com | 112 | | 112 | | | | |
| thestreet.com | 44 | | 44 | | | | |
| stocktwits.com | 42 | 42 | | | | | |
| barchart.com | 40 | | | 40 | | | |
| seekingalpha.com | 34 | | 34 | | | | |
| cnbc.com | 17 | | | 17 | | | |
| investopedia.com | 12 | | | | | 12 | |
| theregister.com | 10 | 10 | | | | | |
| 23 other domains | 67 | 35 | 8 | 2 | 7 | 9 | 6 |

No host answered 429, 5xx or timed out. Yahoo's pages without text are headline lists
and one-line wire briefs (the article page itself loads; "consent" appears only in its
scripts), not a consent wall; Barchart's are HTTP 202 with an empty body, an
interstitial, and its stories are now paused and retried with `--slow`. 87 earlier
Seeking Alpha 403s were re-marked as its paywall and 67 earlier HTTP 202 pages as
interstitials.

## 9. Archived articles: Common Crawl and the Wayback Machine

`talk-is-cheap ingest archives [--publishers cnbc,fortune] [--tickers NVDA] [--since
2024-01-01] [--dry-run] [--limit N]` (`news/archives.py`, `news/archive_index.py`) finds
past articles of interview-heavy publishers in web archives, back to `history_start`,
and reads each from the publisher's live page: RSS only reaches back days, and Google
News links cannot be followed. The archives supply the URLs; the text comes from the
publisher under the article-text rules, and from the archive copy only when the live
page is gone.

**Publishers** are configured in `archives.yaml`: domain, one or more URL formats (a path
prefix, a regular expression an article's path must match, and one that reads the date
from the path). Each format was checked against real index rows on 2026-10-21:

| Publisher | URL format | Date in the URL |
| --- | --- | --- |
| CNBC | `/2024/05/22/nvidia-...-says.html` | day |
| Fortune | `/2024/05/23/slug/` (the `/article/slug/` pages are evergreen guides, left out) | day |
| Business Insider | `/nvidia-ceo-jensen-huang-says-ai-2024-5` | month |
| The Verge | `/2025/1/1/24333454/slug` until early 2025, then `/news/1000532/slug` | day, then none |
| TechCrunch | `/2024/05/03/slug/` | day |
| Ars Technica | `/ai/2025/01/slug/` (gadgets, ai, information-technology, tech-policy, cars) | month |
| Tom's Hardware | `/pc-components/gpus/slug` | none |
| The Register | `/2024/05/23/words_joined_by_underscores/` | day |
| MarketWatch | `/story/slug-04fb6eb1` (older slugs end in the date) | none (older: day) |
| Yahoo Finance | `/news/slug-125746289.html` | none |

Every publisher's URLs carry a slug, so none was dropped; the four without a date in
the URL take it from the page's metadata.

**Search.**

- *Common Crawl* (`index.commoncrawl.org`, crawls from `collinfo.json` whose dates reach
  `--since`), for publishers with `common_crawl: true`: one prefix query per crawl and
  publisher format (the path up to its first placeholder other than `{year}`, for the
  crawl's year and the year before), in pages of one index block, filtered on the server
  by status 200 and HTML. The index ignores URL filters (any `url`, `original` or
  `urlkey` filter answers "No Captures found"), so URLs are filtered locally; it cuts
  answers off at about 960 KB, mid-line, and such a page is kept up to its last whole
  line and reported. One probe per crawl skips a crawl that holds nothing of the domain.
  **It is off for every configured publisher** (below).
- *Wayback Machine* (`web.archive.org/cdx/search/cdx`): for each month that ended at
  least 7 days ago (`SETTLE_TIME`; later days are still being archived, and RSS and
  Finnhub cover them) and that Common Crawl holds no article URL of the publisher for (by
  URL date, else capture month; filtered or not), **one query per month**: of that
  month's prefix (`www.cnbc.com/2024/05/`) when the format has one; else of each company
  name at the start of the slug (`finance.yahoo.com/news/nvidia`), or of the whole
  section (`www.theverge.com/news/`), limited to the month's captures with `from` and
  `to` (`202405`, the whole month). A query of a whole year of CNBC did not answer
  within 4.5 minutes, and one of The Verge's `/news/` over 31 months never answered, so
  no query spans months.
- Each answer is kept small: the server filters by status (`statuscode:200`), HTML
  (`mimetype:text/html`) and a regular expression (a name and a claim word anywhere in
  the URL, any case), collapses repeats of a URL (`collapse=urlkey`), and returns only
  the timestamp and URL (`fl`).
- **A 504 splits the query.** A month answered with 504 (too large to scan in time) is
  not asked again: it is split at once into the prefix's index pages (`showNumPages`,
  asked once a run per prefix and never cached, since it grows; then `page=`, with the
  same `from`/`to`), each page recorded as it is stored. The month fails only when a
  page still answers 504, or when the prefix has a single page and so nothing smaller
  to ask; the next run resumes at the failed page. The page count is of the prefix's
  index blocks whatever the month, so a split month of a prefix of N pages takes N
  queries.
- Progress is recorded in `archive_searches` (publisher, source, crawl or month, prefix,
  pages, pages done, captures, kept, months found, complete) as each page's captures are
  stored; an interrupted search resumes at its first page not done, and a complete one
  is skipped. A search that fails every attempt stays incomplete, so the next run
  searches only the failed and unsearched months; a search recorded over a span of
  months ("2024-05..2026-09", from before searches were monthly) counts for each of its
  months. For each publisher the run prints its Wayback months **done** (every search
  of the month complete), **failed** (one left incomplete) and **left** (not searched
  yet), before searching and in a table after. Publishers marked `deferred` (Ars Technica, five sections searched month by
  month) are searched and downloaded after every other publisher's search and downloads.

**Why Common Crawl's search is off.** Measured from its cached answers on 2026-10-21,
after 25 to 52 searches per publisher (20 to 100 minutes each, mostly 504 retries):

| Publisher | Captures | Not an article path | Dated before 2024 | Rejected by the slug filter | Kept |
| --- | --- | --- | --- | --- | --- |
| CNBC | 864, all in one crawl (CC-MAIN-2024-10) | 116 | 522 | 222 | 4 |
| Fortune | 0: no 2024-2026 prefix has an index page in any crawl | | | | 0 |
| Business Insider | 74,740 (34 lines lost to the cutoff) | 27,773 | 44,065 | 2,859 | 19 URLs |
| The Verge | 1,245, all January-February 2024 | | | 1,230 | 13 |

The filter was not at fault: the rejected 2024+ URLs are stories with no company name
and claim word ("wayfair-layoffs-...", "hungarian-president-novak-resigns"), and no
www, trailing-slash, AMP, mobile or query-string mismatch was found (paths are matched;
hosts with or without www). CNBC's and Yahoo's robots.txt name CCBot, and Common Crawl
obeys them, so after early 2024 the crawls hold almost none of these sites' articles.
Wayback kept 1,137 CNBC and 675 Fortune URLs from the same months. Common Crawl stays
in use for reading WARC records of captures it holds and for the article-text fallback
(section 8).

**The slug filter** (`SlugFilter`), free, before any download. A URL is kept when it is
an article of a configured format, published on or after `--since` (by its URL date),
and its slug (the last path segment, split on `-`, `_` and `.`):

1. names a company of `universe.yaml` (ticker, name or alias) or one of its executives
   (any roster name or alias), as consecutive words; the last may carry an "s", since
   slugs drop the apostrophe ("nvidias", "apples");
2. contains a claim word from `archives.yaml`: says, said, interview, ceo, cfo, expects,
   plans, predicts, targets, will, forecast, promises, vows;
3. contains none of that company's `exclude_phrases` ("big-apple");
4. is not already stored as an article (canonical URL, query dropped).

One capture per URL is kept in `archive_captures`: a successful HTML capture, from
Common Crawl when it has one (a range request is cheaper than a page), else the
earliest. Each round prints the URLs kept per publisher, per company and per year
once its searches are done, then downloads what is pending; `--dry-run` searches,
prints them, and downloads nothing. While the round's Wayback searches are paused for a
block (below), the captures it has found so far are downloaded meanwhile, so a long
pause does not hold the downloads up.

**Download.** Each article is fetched from its **live publisher URL** through the
article-text rules (section 8): its User-Agent, robots.txt (kept on disk), one request a
second per domain, every publisher's domain in parallel. The archive copy is read only
when the live page is gone (404, 410) or has no text (`LIVE_GONE`) **and** robots.txt
allows the URL; a robots.txt refusal, a 401, 403 or 451, or a paywall is never got
around with a copy. A Common Crawl copy is one HTTP range request for its WARC record at
`data.commoncrawl.org` (no S3 access); a Wayback copy is the raw page
(`web.archive.org/web/<timestamp>id_/<url>`), at one request every three seconds. Where
the text came from is recorded: a live page's text has no note (as every article-text
fetch), an archive copy's `reason` is "text from the Wayback Machine capture of <date>"
(or Common Crawl), and `archive_captures.reason` records what the live page answered.
Timeouts, server errors and interstitials leave a capture `pending` for the next run.
The text is extracted with trafilatura, as in section 8, into `article_texts`. The
article is stored with its original publisher URL (never the archive's), the publication
time from the page's metadata
(`article:published_time`, JSON-LD `datePublished` and similar; a time later than the
capture is a modification time and is not used), else the URL's date
(`time_precision = 'date'`), else the capture time (`seen`); its title and description
from Open Graph tags; and its source as the feed `commoncrawl:<ticker>` or
`wayback:<ticker>` for each company the slug names. Storing goes through
`store.store_items`: the same normalization, deduplication (canonical URL, then
near-duplicate headlines) and linking as feed items. The feed link is weak (0.3), like a
per-company feed: the headline and description decide the link. Downloads run oldest
first within each domain.

**Polite client.** Every request carries `talk-is-cheap/<version> (research: reads
archived news for executives' statements; contact: <EDGAR_IDENTITY>)`, and every
answer is kept on disk under `NEWS_CACHE_DIR` (`news/http_cache.py`), so nothing is
fetched twice. Common Crawl gets at most one request a second; its 429 and 5xx answers,
dropped connections and answers that are not whole (a maintenance page sent with status
200, a page cut off mid-line) are retried after 5 seconds, doubling to 2 minutes, at
most 8 times; then the search is left for the next run.

The Wayback Machine is asked at the pace the Internet Archive asks clients to keep (the
limits of the `wayback` library): a CDX search every 2.5 seconds (80% of 30 a minute),
and up to 8 archived pages a second, read by a fetcher of their own. Each failure gets
its own wait (`WAYBACK_RETRY`, `news/http_cache.py: RetryPolicy`), always at least the
pace, and every wait is logged with its reason:

| Answer | Wait |
| --- | --- |
| 503 (and 500, 502, an answer that is not whole) | retried at once, then after 2, 4 and 8 seconds; then the month is left for the next run |
| 504 | not retried: the query is split (above) |
| 429 | once, after Retry-After, or 60 seconds without it |
| a connection timeout, or another dropped connection | retried at the normal pace, up to 4 times; then the month is left for the next run |
| 3 connection timeouts in a row | taken for a block: searches pause, and one `limit=1` CDX query is sent each minute; the first one answered ends the pause, and searches go on at the normal pace. The pause's length is logged. Archived page reads and live publisher downloads are not paused: the round downloads the captures found so far while it lasts. |

**Text use.** As for every article: extraction and grounding only; answers quote at most
40 words and link to the publisher's URL.

## 10. Finnhub company news

`talk-is-cheap ingest finnhub [--tickers NVDA] [--since 2025-11-01] [--dry-run]`
(`news/finnhub.py`) uses Finnhub's company-news API with a free key
(`FINNHUB_API_KEY`; without it the command prints one line and does nothing). **Finnhub's
free tier is licensed for personal use only**; this project uses it for research on its
owner's machine, and its items are not republished.

- **Requests:** one per ticker and window of 7 days (`finnhub.io/api/v1/company-news`),
  the key in the `X-Finnhub-Token` header, at most one every 1.1 seconds (the free tier
  allows 60 a minute), 429 retried with backoff. An answer holds at most 250 items,
  newest first; a full answer is split in half, down to one day, and a day still full is
  recorded as truncated. Windows are recorded in `backfill_windows` and answers cached,
  except windows ending within the last two days.
- **History depth**, measured at the start of every run by halving: the earliest day a
  three-day window still returns items. On 2026-10-21 it was 362 days (back to
  2025-10-24), which covers every Google-News-only story (all from 2026).
- **Links:** every item's `url` is a Finnhub redirect (`finnhub.io/api/news?id=...`) to
  the publisher; Finnhub's robots.txt allows it.

**Recovering Google News stories.** A story whose only links are Google News redirects
is matched to the Finnhub items listed for the same company, published within two days:
the best headline similarity (`dedup.similarity`, the better of rapidfuzz `ratio` and
`token_sort_ratio`, of normalized headlines) wins, and a match at or above
`MATCH_THRESHOLD` has its Finnhub link resolved (one request, the redirect not
followed). The publisher URL is stored on the Google News article (`publisher_url`),
with the score (`publisher_url_score`) and its source, and the article's earlier
"robots.txt disallows" result is cleared, so `ingest article-text` fetches the
publisher's page under its usual rules.

The threshold was checked by hand on 20 pairs, four per score band: all 16 at 80 or more
were the same story (often another outlet's version: a Bloomberg headline matched to
Benzinga's write-up, which is why text read from a publisher URL is cited with that
URL's site, `queries.ARTICLE_PUBLISHER`); 2 of the 4 between 70 and 80 were not ("What
to expect in the week ahead" against another week-ahead piece). `MATCH_THRESHOLD` is 85.
Result on 2026-10-21: of 2,494 stories whose only links were Google News redirects, 364
(14.6%) were matched to a publisher URL and 256 of those (70%) yielded text; 6 more gained
a direct link because a new Finnhub item joined their cluster. The rest are mostly
Reuters, Bloomberg, WSJ and similar outlets Finnhub's free lists do not carry.

**New stories.** Items that match no stored story and whose headline names a company of
the universe (a text link of 0.8 or more: a name, alias or ticker) are stored as the feed
`finnhub:<ticker>` (link 0.3) through the usual pipeline, under their Finnhub link, which
`ingest article-text` follows to the publisher. Per-ticker lists carry many stories
about other companies (Micron, SMCI and Berkshire Hathaway under NVDA), which are not
stored. The first run also stored items that named a company only in their summary; a
hand check of 20 random Finnhub-linked stories found 16 about the company they were
linked to, and two of the four misses were such summary-only links (a Fed preview naming
Nvidia, a podcast roundup naming Google). Those items, 7,966 single-article stories
that only Finnhub supplied, were removed and the rule tightened; a fresh sample of 20
then had 17 right. The three misses name the company in the headline but are about
another (a "top money news" roundup with Netflix layoffs, SpaceX overtaking Amazon and
Microsoft in market value, Micron's outlook citing Nvidia news).

**Which stories are read.** `talk-is-cheap ingest article-text --finnhub [--dry-run]
[--limit N]` (`finnhub.py: text_candidates`) fetches the text only of Finnhub stories
whose headline or summary names the linked company or one of its executives together
with an attribution or claim word, and none of its excluded phrases: the archives' slug
filter (section 9, the claim words of `archives.yaml`) applied to the feed text. Oldest
first, since older claims are more likely due. `--dry-run` prints how many stories pass
per company and fetches nothing. Of 20 random stories that pass, 18 are about the company
they are linked to (the misses: a BMW model compared with Tesla, an Asian markets
roundup naming Nvidia).

## 11. What each source contributes

`talk-is-cheap sources` (`sources.py`) counts articles and stories (dedup clusters) per
source: RSS (polled feeds: Yahoo Finance, Google News, market-wide feeds, newsrooms),
GDELT, Common Crawl, the Wayback Machine and Finnhub, by the feeds that supplied each
article; a Google News story matched on Finnhub counts for both RSS and Finnhub.
`talk-is-cheap sources --report` counts only what no other source also found, over the
stories whose articles all come from one source: articles linked to a company
(confidence 0.5 or more), stories with text, management claims, and due claims (one per
group of identical promises, due by the resolver's rule). It reads the database only;
re-run it after any ingest.

## 12. Resuming the jobs

Every job below is resumable: what it stored is kept, and running the same command again
continues where it stopped. Run them one at a time per service (the archive run's live
downloads share publisher domains with `ingest article-text`, so the two should not run
together; Common Crawl should have one client at a time).

| Job | Command | State when last stopped (2026-10-21) |
| --- | --- | --- |
| Archive search and downloads | `uv run talk-is-cheap ingest archives` (`--dry-run` to search and print the counts only) | Wayback search done for CNBC (31 months) and Fortune (33); Business Insider 31 of 32 name searches, The Verge 3 of 32; TechCrunch, Tom's Hardware, The Register, MarketWatch and Yahoo Finance not started; Ars Technica (deferred) last. 2,284 URLs found, all pending download |
| Finnhub story text | `uv run talk-is-cheap ingest article-text --finnhub` (`--dry-run` for the pre-filter counts) | 1,010 pre-filtered stories left to fetch, oldest first |
| Hosts paused after interstitials (Barchart) | `uv run talk-is-cheap ingest article-text --finnhub --slow` | Barchart's stories left unstored by the pause |
| Common Crawl text fallback | `uv run talk-is-cheap ingest article-text --common-crawl` | 1,352 articles to look up; the first run made 12 lookups (10 failed on Common Crawl's 504s and stay queued) |
| Finnhub news and matching | `uv run talk-is-cheap ingest finnhub` | Done through 2026-10-21; a re-run fetches only windows not recorded |
| What each source found | `uv run talk-is-cheap sources --report` | Read-only, any time |

