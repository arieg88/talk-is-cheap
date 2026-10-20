# Statements in the news

How `talk-is-cheap extract statements` finds forward-looking claims executives made in
public, as reported in the news (interviews, investor conferences, press statements,
posts, earnings-call remarks quoted by reporters), who made each one, and how it compares
with what the company later reported and with its formal guidance.

## 1. Data

Measured on the development database on 2026-10-20: 3,688 articles from January 2024 to
October 2026, 2,695 stories (dedup clusters) linked to a company with confidence 0.5 or
more, of 2,371 distinct clusters.

**Feeds store too little text.** An article row holds its headline (74 characters on
average) and, for 11% of articles, the feed's summary:

| Source | Articles | With a summary | Summary length (mean / median) |
| --- | --- | --- | --- |
| Google News | 1,939 | 0 | - |
| GDELT | 1,318 | 0 | - |
| Yahoo Finance | 291 | 291 | 224 / 161 |
| Company newsrooms | 100 | 85 | 216 / 171 |
| Other RSS (CNBC, MarketWatch) | 40 | 40 | 109 / 108 |

On that text, the filter of section 3 keeps 6 stories, and only 2 of them hold an
attributed forward-looking claim ("Elon Musk said Tesla's long-awaited Roadster will
enter production in 2028"). Executives' quotes are in article bodies, so bodies are
fetched for linked stories (section 2).

**Article text** (`talk-is-cheap ingest article-text`, `news/article_text.py`): one
article per linked story, the best one: a direct link before a Google News link, then
the longest summary, then the earliest. Results for all 2,371 linked stories (810
requests, 302 seconds):

| Outcome | Stories |
| --- | --- |
| Text stored | 538 |
| Google News link: its robots.txt disallows following the redirect (the link is the only one in the story) | 1,560 |
| HTTP 403 (Benzinga 63, Forbes 45, Seeking Alpha 30, TheStreet 13, ...) | 159 |
| Publisher's robots.txt disallows (Reuters, MarketBeat, Investor's Business Daily, Bloomberg, WSJ, NYT, ...) | 90 |
| No article text (a teaser, a paywall or a script-rendered page) | 12 |
| HTTP 404, 402, timeout | 13 |

Stored bodies: GDELT articles 2,918 characters (median), Yahoo Finance 2,902, company
newsrooms 6,088. Most of the text comes from Yahoo Finance, The Motley Fool, CNBC, Tom's
Hardware, The Verge and company newsrooms (Apple, Nvidia, Cisco); 66% of stories have
only a Google News link, which can never be followed.

Fetching rules: robots.txt is read first for every host, including hosts a redirect
leads to, and obeyed for the user agent named by the first word of
`ARTICLE_USER_AGENT`; a host whose robots.txt cannot be read is not fetched. At most one
request per second per host (or the host's Crawl-delay), eight hosts at a time, 15-second
timeout. Pages are converted with trafilatura (precision mode, no comments or tables); a
text under 400 characters, or ending in a subscription prompt, is "no article text".
401, 402, 403, 404, 410, 451, robots.txt refusals and pages without text are final;
timeouts, connection errors and server errors are tried by the next run. Each result is
stored as it arrives (`article_texts`), so a run can be stopped and resumed; a story whose
best article is unavailable moves on to its next article.

The text is used for extraction and grounding only. Commands and answers quote at most
40 words of an article (`statements/commands.py: QUOTE_WORDS`) and link to it.

## 2. Which stories go to the model

A deterministic filter (`statements/candidates.py`). A story is a candidate for a company
when one of its articles is linked to the company with confidence 0.5 or more, and one
passage of its text (a paragraph and the next) does all three of:

1. names an executive on the company's roster (any name or alias), or the company
   speaking ("the company said", "a Nvidia spokesperson", "Nvidia said");
2. has a quotation mark or an attribution ("said", "says", "told", "according to", ...);
3. has a forward-looking marker ("expects", "will", "plans", "target", "outlook",
   "guidance", "forecast", "next year", "by 2027", ...).

The story's text is the longest text among its articles: a fetched body, else headline
and summary. Requiring the three in one passage, rather than anywhere in the text, cut
candidates from 337 to 232 after the first trial run: most of the dropped stories were
opinion pieces naming the CEO once and forecasting in the reporter's own voice.

| Company | Stories | Candidates | | Company | Stories | Candidates |
| --- | --- | --- | --- | --- | --- | --- |
| NVDA | 490 | 85 | | INTC | 154 | 13 |
| AAPL | 298 | 41 | | MSFT | 176 | 12 |
| AMD | 167 | 19 | | QCOM | 120 | 8 |
| AMZN | 179 | 16 | | GOOGL | 140 | 8 |
| CRM | 128 | 7 | | TSLA | 174 | 6 |
| NFLX | 145 | 4 | | CSCO | 124 | 4 |
| META | 172 | 3 | | ORCL, ADBE | 114, 114 | 3, 3 |
| **Total** | **2,695** | **232** | | | | |

Meta, Oracle, Adobe and Netflix have few candidates because almost all of their stories
are Google News links (section 1).

**Executive roster.** `universe.yaml` lists each company's executives with role, start
and end dates and the names the press uses (`aliases`: "Jensen Huang" for "Jen-Hsun
Huang"), each roster citing the 10-K, proxy statement (DEF 14A, read with edgartools) and
8-K item 5.02 filings it comes from. An omitted start is before 2024; an omitted end is
still in the role. A person whose role changed has one entry per role (Tim Cook: CEO
until 2026-08-31, executive chair from 2026-09-01). Adding a company means adding its
executives and nothing else.

## 3. Extraction

One call per candidate story and company, with the prompt in
`prompts/statement_extraction/v1.md`, through the guidance code's structured-output
model, cache, budget and ledger (`guidance/llm.py`, now generic over the output schema).
The model returns each claim as written (`statements/schema.py`): exact quote, speaker
name and role as written, attribution, direct quote or paraphrase, venue, statement date
if the article gives it, claim type (`numeric_forecast`, `directional`, `plan`,
`qualitative`), metric (the guidance vocabulary), period and figure as written, and
direction. Claims by analysts, unnamed sources and the reporter are extracted too, but
only when specific (a figure, a direction, a dated plan), so they can never be mistaken
for management's.

Then in code (`statements/items.py`):

- **grounding**: the quote must be found in the stored text (`guidance/grounding.py`);
  claims whose quote is not found are dropped and counted;
- **figures** with `guidance/numbers.py` and **periods** with `guidance/periods.py`, as if
  the statement were made in a release for the last fiscal quarter that ended before the
  statement date ("this quarter" and "next quarter" are the quarter under way, "this
  year" the fiscal year under way). "The September quarter" (Apple's wording) is the
  next fiscal quarter ending in that month. A numeric claim whose figure cannot be read
  becomes directional (with a direction) or qualitative;
- **statement date**: the date the model reads from the article, if it is a real date no
  later than the article and at most a year before; otherwise the article's date, marked
  `date_from_article`;
- **speakers** (`statements/roster.py`): see section 4.

Runs are incremental like guidance extraction: a story extracted with the same model,
prompt version and prompt text is skipped; a new post-processing version re-processes
stored responses from the cache; stories that stop being candidates lose their claims.
`--limit N` takes at most N stories needing work per company; `--missing-only` keeps
stories extracted with another model; `--dry-run` prints candidates, calls, tokens, the
most a run can cost and the typical cost.

## 4. Attribution rules

- `named_executive` claims are matched to the roster in code: a fuzzy name match
  (rapidfuzz token sort ratio 90 or more, after removing titles, middle initials and
  punctuation), against executives who held a role on the statement date; a surname
  alone matches when one executive in a role that day has it; a speaker named only by a
  bare title ("the CFO said") matches when exactly one executive has that title that day.
  Unmatched, the claim becomes `unverified_executive`: Nvidia's GeForce Now vice
  president, Salesforce's head of Tableau, Arm's CEO quoted in a Qualcomm story, and a
  former CEO quoted after leaving are all unverified.
- **Management claims** are those of a verified executive, a `company_spokesperson` or a
  `company_statement`. Only they are scored.
- `analyst`, `anonymous_source`, `reporter` and `unverified_executive` claims are stored
  (`talk-is-cheap statements NVDA --all-attributions`), never scored, and returned by the
  agent only with `management: false`.

## 5. Comparison rules

`statements/outcomes.py`, rebuilt for the company after every extraction and every review,
at no cost.

| Claim type | Outcome |
| --- | --- |
| `numeric_forecast` | The guidance rules (`guidance/matching.py: judge`, which now takes any `Forecast`): `beat`, `met`, `missed`, `pending`, `not_comparable` with the reason; the same tolerances, actuals as first reported, basis, segment and per-share rules, and the plausibility check, whose flagged claims need review before they count |
| `directional` | The target period's reported value against the period before it (quarter before quarter, year before year), as first reported: `confirmed`, `contradicted`, or `pending`; a change within the point tolerance (1% of an amount, 0.5 points of a rate) is flat. Not comparable: metric `other`, non-GAAP, a segment, the direction of a growth rate, an unresolved period |
| `plan`, `qualitative` | `not_checkable`. A plan gets, as context, the best passage (MD&A or risk factors) of the 10-Qs and 10-Ks filed within 200 days after it, from the filing search the agent uses (`statements/context.py`) |

Each numeric or directional management claim is then compared with formal guidance
for the same metric, measure, segment and target period, issued within 45 days before or
after the claim (a stated basis must agree; rows that need review or were reviewed as
wrong are left out): `consistent` when the claim's figure (point, else middle, else bound)
is inside the guided range (a guided point is widened by its tolerance), `differs`
otherwise, or `no_formal_guidance`. A directional claim reads the direction the guidance
implies against the prior period's reported value, or the sign of guided growth. The
guidance row's id is stored.

`talk-is-cheap statements review ID --ok|--wrong` works like guidance review: the
decision is stored by article, company and the claim's key (`statement_reviews`), so it
survives re-extraction. A claim reviewed as wrong has no verdict or comparison in counts
or in the agent's answers.

## 6. Measurement

`evals/statements/labels.jsonl`: 40 candidate stories, labeled by reading the stored
text (drafted by Claude, the coding assistant, and to be spot-checked; the file says so):
52 management claims, and 34 claims that must never be attributed to management:
analysts' and consensus figures, unnamed sources, reporters' predictions, executives not
on the roster, a new director, and other companies' guidance in a story linked to this
one (Salesforce's in an Adobe story, Qualcomm's in an Apple story, Arista's in a Cisco
story). 15 stories put an analyst's, consensus, unnamed or reporter's figure next to
management's own words, some in the same sentence; 17 have no management claim at all.

`make eval-statements` (`ARGS=--dry-run` prints the calls and cost) pairs claims with
labels by overlapping quotes (a claim whose figure is in the label's quote first, since
two claims often quote one sentence) and reports:

| Measure | Haiku 5.5, prompt v1 (2026-10-20) |
| --- | --- |
| **Attribution precision** (of claims the system attributes to management that labels cover, those labeled management) | **1.000** (47 of 47) |
| Attribution recall (labeled management claims found and attributed to management) | 1.000 (47 of 47) |
| Leaks (non-management claims attributed to management) | 0 of 34 |
| Claim recall (labeled management claims found) | 0.904 (47 of 52) |
| Claim precision (management claims that are labeled ones) | 0.758 (47 of 62) |
| Claim type / metric / period / figures | 27/28, 19/21, 8/9, 14/15 |
| Grounding drops | 1 |
| Cost of the 40 stories | 211,129 input and 56,940 output tokens, $0.0496 (from the cache: $0) |

What the remaining errors are:

- **Claim precision** counts every unlabeled management claim against the system. Of the
  15, most are statements the labels left out as present-tense or borderline ("management
  said customers are exceeding their commitments", "the company's plans to sell its
  custom TPU chips directly"), two are reporters restating a company plan, and none
  attributes someone else's claim to management.
- **Misses** (5): a plan in a long parenthetical sentence, an investor-day target, a
  plan stated "a few weeks ago", a CEO's "we are poised for our next wave of growth",
  and Microsoft's Copilot key announcement.
- **Fields**: "revenue growth of 9%-11% to $111.7 billion-$113.7 billion" came back as one
  figure that cannot be read (a growth rate and an amount are two claims); "the
  September quarter" was not resolved until the period rule of section 3 was added
  (period accuracy went from 7/9 to 8/9 by re-processing, at no cost).
- One leak in the first scoring came from the matcher, not the model: an analyst's 68% and
  the company's 70% quoted in one sentence were paired with each other's labels. Matching
  by figure first fixed it.

## 7. Results on the 15 companies

Extracted 2026-10-20: 232 stories, 656 claims, 357 by management (83 numeric, 48
directional, 132 plans, 94 qualitative), 299 by others (199 reporters, 80 analysts, 12
unnamed sources, 8 unverified executives). 5 claims dropped by grounding. Venue, of
management claims: unknown 189, earnings call 80, press release 58, conference 14, other
10, interview 2, social media 1. 183 of 357 management claims are dated by their
article (the article does not say when the statement was made). 92 of 132 plans have a
filing passage as context.

`talk-is-cheap statements all --summary` (verdicts count each forecast once, however
many outlets repeated it; Nvidia's $28 billion for fiscal Q2 2025 was reported by seven):

| Company | Mgmt | Numeric | Directional | Plan | Qualitative | Others | Beat/met/missed | Confirmed/contradicted | Pending | Not comparable | Consistent with guidance |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AAPL | 74 | 12 | 19 | 13 | 30 | 60 | 0/0/0 | 0/0 | 8 | 11 | - |
| ADBE | 6 | 4 | 0 | 1 | 1 | 1 | 0/0/0 | 0/0 | 3 | 1 | 4/4 |
| AMD | 45 | 8 | 3 | 23 | 11 | 19 | 0/0/0 | 0/0 | 2 | 8 | 3/3 |
| AMZN | 17 | 1 | 0 | 13 | 3 | 29 | 0/0/0 | 0/0 | 1 | 0 | - |
| CRM | 15 | 6 | 1 | 5 | 3 | 7 | 0/0/0 | 0/0 | 3 | 3 | 1/1 |
| CSCO | 5 | 3 | 1 | 0 | 1 | 2 | 0/0/0 | 0/0 | 1 | 3 | - |
| GOOGL | 17 | 4 | 1 | 10 | 2 | 18 | 0/0/0 | 0/0 | 1 | 4 | - |
| INTC | 12 | 6 | 0 | 3 | 3 | 21 | 0/0/0 | 0/0 | 2 | 4 | 2/2 |
| META | 1 | 0 | 0 | 1 | 0 | 1 | 0/0/0 | 0/0 | 0 | 0 | - |
| MSFT | 9 | 3 | 0 | 5 | 1 | 13 | 0/0/0 | 0/0 | 2 | 0 | - |
| NFLX | 4 | 2 | 0 | 2 | 0 | 1 | 0/0/0 | 0/0 | 2 | 0 | - |
| NVDA | 112 | 23 | 12 | 48 | 29 | 118 | 3/0/0 | 2/0 | 3 | 14 | 4/4 |
| ORCL | 6 | 2 | 1 | 1 | 2 | 1 | 0/0/0 | 0/0 | 3 | 0 | - |
| QCOM | 30 | 8 | 10 | 5 | 7 | 1 | 0/0/0 | 0/0 | 1 | 13 | 2/2 |
| TSLA | 4 | 1 | 0 | 2 | 1 | 7 | 0/0/0 | 0/0 | 1 | 0 | - |
| **Total** | **357** | **83** | **48** | **132** | **94** | **299** | **3/0/0** | **2/0** | **33** | **61** | **16/16** |

Four claims need review or were reviewed as wrong (not in the counts).

**Below expectations, and why:**

- **Few verdicts.** Only 5 distinct checkable claims have a verdict (all Nvidia: three
  revenue forecasts beaten, two increases confirmed); the hit rate (met or confirmed) is
  2 of 5. Most stored news is from 2026, so 33 forecasts are about periods not reported
  yet, and 61 are not comparable: a segment (Qualcomm's handset and data center targets,
  Nvidia's data center revenue), a metric not in XBRL (shipments, backlog, AI revenue),
  non-GAAP figures, or a period that cannot be resolved ("the quarter being reported
  Wednesday", "over the next 24 months").
- **Apple** has the most management claims after Nvidia but no verdict: its outlook is
  given only on the call as growth rates ("9% to 11%") for a quarter not reported yet,
  and Apple's earnings releases have no guidance to compare with.
- **Meta, Oracle, Adobe, Netflix and Tesla** have under ten management claims each: their
  stories are almost all Google News links, which cannot be followed (section 1).
- **Claims are mostly not numeric**: plans (37%) and qualitative claims (26%) are
  `not_checkable`.
- **The comparison with formal guidance** found no difference once two extraction errors
  were reviewed as wrong: Qualcomm's "fourth-quarter adjusted earnings below Wall Street
  expectations" and AMD's "somewhat slower growth in the third quarter" had been read as
  a decrease of EPS and of revenue.

**Spot checks** of 10 scored management claims against their articles (speaker, date,
figure, verdict):

| Claim | Article | Finding |
| --- | --- | --- |
| Nvidia FY2025 Q2 revenue "$28 billion, plus or minus 2%", beat ($30.04B), consistent | Yahoo Finance, May 21, 2024 | Figure and verdict right. **Date wrong by a day**: GDELT first saw the live-updated page before the results; the stored text includes the results. |
| Same, "about $28 billion, the company said Wednesday" | Fortune | Right, dated May 22 by the article's "Wednesday" |
| Same, "$28 billion plus or minus 2%" | Yahoo Finance | Right |
| Same, "$28 billion versus analyst estimates of $26.61 billion" | Yahoo Finance | Right; the analyst figure is a separate analyst claim |
| Same, "management expects revenue of $28 billion" | The Motley Fool | Right |
| Same, "beating the LSEG estimate of $26.61 billion" | CNBC | Right |
| Same, "forecast revenue of around $28 billion, ahead of the consensus" | The Motley Fool | Right |
| Nvidia FY2027 Q2 revenue "about $91 billion", beat ($96.22B), consistent | Yahoo Finance, June 5, 2026 | Right; dated by the article |
| Nvidia FY2025 revenue increase ("a revenue boost later in the year"), confirmed | Fortune | Right (company statement); "later in the year" is read as the fiscal year |
| Nvidia FY2024 revenue "roughly $59 billion", beat ($60.92B), flagged | The Motley Fool, January 11, 2024 | Figure and verdict right; it is the reporter restating November 2023 guidance, dated by the article and so marked. Flagged by the plausibility check (219% of fiscal 2023: Nvidia's revenue did more than double); not yet reviewed. |

## 8. Agent

`get_statements(ticker, metric?, period?, speaker?, management_only=True)` returns claims
with quote (at most 40 words), speaker, role, attribution, venue, date (and a note when
it is the article's), publisher and URL, figures, the outcome and the comparison with
formal guidance, with provenance `news_report`. The answer says "reported by
<publisher>" (added in code when missing), cites filings and guidance rows first for
figures they also give, and never presents an analyst's claim as the company's
(`docs/agent.md`).

## 9. Cost

All through the ledger and budget (`talk-is-cheap spend`); every run printed its estimate
and the remaining budget first.

| Run | Calls | Input / output tokens | Cost |
| --- | --- | --- | --- |
| Trials on NVDA, META, TSLA (`--limit`, three prompt and schema revisions) | 36 | 200,851 / 46,233 | $0.0432 |
| All companies (212 stories), then 10 stories retried with a 6,000-token output limit | 222 | 1,215,124 / 296,334 | $0.2697 |
| Re-processing (new post-processing versions) and the benchmark | 0 | from the cache | $0 |
| **Extraction** | **258** | **1,415,975 / 342,567** | **$0.3129** |

A call reads about 5,500 tokens (instructions and schema 1,200, the article up to 12,000
characters) and writes about 1,330; $0.0012 a story with Haiku 5.5. Ten long stories (many
analyst figures) first hit the 3,000-token output limit and failed; the limit is now
6,000 (`extract.py: MAX_OUTPUT_TOKENS`), which every story fits.

## 10. Known limitations

- **News coverage.** 66% of linked stories have only a Google News link, whose
  robots.txt forbids following it, and 12% more are refused (403, robots.txt, paywalls). Reuters, Bloomberg,
  WSJ, Barron's and Investor's Business Daily, where executives are quoted most, are not
  read. Stories exist only for days polled (since 2026-10-18) or backfilled from GDELT
  around earnings.
- **Paywalls and teasers.** Pages that serve a teaser are detected by length and a few
  subscription phrases; a teaser over 400 characters is stored as if it were the article.
- **Reporter paraphrase.** Most management claims (233 of 357) are company statements as
  a reporter words them, and 80% are paraphrase (284 of 357); a reporter restating guidance
  months later is a claim dated by the article. "Venue" is unknown for half of them.
- **Dates.** GDELT's time is when it saw the article; a page updated after that (live
  coverage) can date a statement a day early.
- **Duplicates.** The same statement reported by several outlets is one claim per story;
  the summary counts each forecast once, the table and the agent list every report.
- **Growth and amounts in one figure** ("9%-11% to $111.7 billion-$113.7 billion") are
  not read; "slower growth" can be read as a decrease. Reviews correct single claims.
- **Roster.** Executives below the officer level (vice presidents, unit heads) are not on
  the roster, so their claims are never management's; roles before 2024 are not listed.
