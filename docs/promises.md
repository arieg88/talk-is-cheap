# Promises

How the promise tracker holds management to what it says in public: every management
claim in the news ([statements.md](statements.md)) is framed into something resolvable,
judged once it is due from evidence dated after it, and counted into a track record per
executive and per company.

```
statements (management claims)
   │  promises frame: one model call per claim, then code
   ▼
promises: subject, target, baseline, due date, a fixed resolution rule, specificity, bold
   │  promises resolve: plan → gather (tools) → judge → code checks
   ▼
promise_verdicts: verdict, measured value, cited evidence, checks, rule version
   │  reviews win
   ▼
track records: per executive, per company
```

## 1. Framing

`promises/frame.py`, `promises/framing.py`, prompt `prompts/promise_framing/v1.md`.

**Which claims.** Every management claim in `statements` (a verified executive, a
spokesperson or a company statement) except numeric forecasts that already have a
verdict (beat, met or missed against the reported result: those count in track records
as they are) and claims reviewed as extraction errors. Articles are not extracted again:
the model reads the quote, its paragraph (at most 700 characters), the article and
statement dates, the speaker and the company's fiscal calendar. When one quote holds
several claims ("$400 million in the fourth quarter ... more than $2 billion in 2024"),
the prompt also names the claim to frame, from the statement's own summary; without it
the claims shared one prompt, and so one cached answer.

**What the model returns, and what code does with it.** The model reads and classifies;
it never computes.

| Field | From the model | In code |
| --- | --- | --- |
| `subject` | a few words | kept |
| `metric` | a reported metric, or none | only the guidance vocabulary's company-wide metrics |
| `target_kind`, `target_value`, `unit`, `direction` | multiple, absolute, change, threshold, event or none, and the figure | the figure is kept only when it is written in the quote or paragraph ("double" is 2, "profitable" and "break even" are 0); otherwise it is dropped with a note |
| `baseline_*` | only when the article states it | kept only when its number is written in the text (`article`); otherwise the metric's value, as first reported, for the latest period of the target's kind (a quarter, or a year) filed before the statement date (`actuals`); for a change stated year over year ("revenue up 9% to 11% year over year"), the same period a year before the target; else `unknown`. No stored series exists for operating figures (deliveries, users, cities), so theirs is `unknown` unless the article states it |
| deadline | the phrase and its structure: "in N months", "end of year Y", "fiscal year +1", "next quarter", a month, a day, a season | `promises/deadlines.py` computes `due_date` and `due_basis` from the statement date and the fiscal calendar |
| `new_market_or_product` | true or false | used for `bold` only when no numbers can decide it |

Due dates, from the statement date:

| Deadline | `due_basis` | `due_date` |
| --- | --- | --- |
| "in a year", said 2025-03-10 | `relative` | 2026-03-10 |
| "by the end of next year", "later this year", "first half of 2026", "by June", "June 10", "next summer" | `explicit` | the end of that calendar year, half, quarter or month, the day, the end of the season |
| "next fiscal year", "fiscal 2026", "next quarter", "the fiscal fourth quarter" | `fiscal_period` | the end of that fiscal year or quarter on the company's calendar (Nvidia's fiscal 2025 ends 2025-01-31) |
| none, "soon", "over time", "eventually", "within the next few years" | `open_ended` | none |

A quarter, month or season named without a year is the next one ending on or after the
statement date.

**Specificity** is decided in code: `specific` with a checkable target (a figure that
passed grounding, or a concrete event) and a deadline, `partial` with only one of them,
`vague` with neither. Only specific and partial claims are ever scored, and a partial
claim without a target (a deadline for "gross margin would fall") has nothing to judge, so
it is not resolved either.

**Bold**, in code wherever numbers decide it: a multiple of 1.5 or more, a change of more
than 50%, an absolute target more than 1.5 times a stored baseline (or under two thirds
of it, for a ceiling), or turning a loss into a profit. Otherwise a claim about a new
market or product is bold. `bold_basis` records why.

**The resolution rule is written now and never rewritten.** One sentence built in code
from the fields above, with the target, the baseline and the due date in it: "True if
Tesla's vehicle production reaches at least 2 x 410,831 vehicles (last quarter), that is
821,662 vehicles, in any quarter ending on or before 2026-03-10." Fixing the rule when
the claim is framed, before any outcome is known, keeps the judgment honest: the bar
cannot move once results are in. Framing again with a new prompt or post-processing
version adds a promise row with the new version (`v1+4`: prompt v1, post-processing 4)
and keeps the old one, no longer `current`, with its verdicts; every verdict records the
rule version and a hash of the rule it judged.

**Identity.** A promise is keyed by a hash of its claim (article, company and the
statement's item key), so it survives re-extraction, which replaces `statements` rows.
Claims that make the same promise (the same speaker, subject, target and due date, one
statement reported by several outlets) share a `group_key`: they are resolved once and
counted once.

**Structured output.** The answer schema (`FramingAnswer`) has no nullable fields and no
descriptions: every field is required in the schema, "not given" is an empty string, 0,
-1 or "none", and the prompt explains each field. A first version with nullable fields
and descriptions was refused by the provider ("Schema is too complex"); a version with
optional fields let the model leave some out.

## 2. Outcome evidence

Every piece of evidence is dated after the statement: the resolver asks for it that way
(`date_from` the day after the statement), and the checks refuse any cited item dated on
or before it.

| Evidence | Source | Tool |
| --- | --- | --- |
| Financial actuals | XBRL values as first reported | `get_actuals` |
| Operating figures: deliveries, production, users, subscribers, segment revenue, non-GAAP figures | earnings releases, shareholder letters, Tesla's deliveries reports, 10-Q and 10-K text, already indexed | `search_filings` (now with `date_from`/`date_to` and deliveries reports), then `cite_value` |
| What happened: launches, delays, cancellations, revised targets | later filings and press releases, later news | `search_filings`, `search_news` (`date_from`/`date_to`) |
| Whether the claim was walked back or restated | the company's later statements | `get_statements` (`date_from`/`date_to`) |

**`cite_value`** (`agent/tools.py`) gives a number from text the same grounding as a
reported value: it takes the evidence id of a passage, headline or quote and the exact
span holding the number; code checks the span is in the item's text (with the guidance
grounding's normalization), that it holds exactly one number, reads it and its unit
("$96.2 billion", "20%", "836,000 vehicles") and returns a new evidence item that cites
its source and carries its date. `compute` can then use it. Every evidence item now has a
date (`agent/evidence.py: evidence_date`): a filing's filing date, an article's publish
date, a later statement's article; a computed item takes the latest of its inputs'.

## 3. Resolver

`promises/resolve.py`, prompts `prompts/promise_plan/v1.md` and
`prompts/promise_judge/v1.md`. A LangGraph graph per claim:

1. **Plan** (model, structured output): from the rule, the metric's fiscal periods to
   fetch, up to three searches of later filings, up to two of later news, and whether to
   read the company's later statements.
2. **Gather** (code): the plan as at most 6 tool calls, every one limited to evidence
   dated after the statement, 3 results per search.
3. **Judge** (model, structured output): `verdict`, the evidence ids it rests on, the
   earliest proof of delivery, at most three sentences of reasoning, a confidence, and,
   for numeric targets, *where* the measured number is (an evidence id and the exact
   span), never the number itself.
4. **Measure** (code): each source through `cite_value` (spans) or as reported, then
   `compute` (a single value is a sum of one; several are added, or divided), so the
   measured value is always a derived item that cites its inputs.
5. **Check** (code, `promises/checks.py`), before anything is stored. A failed check
   makes the verdict `cannot_determine` and is recorded by name:

| Check | Fails when |
| --- | --- |
| `evidence_exist` | a cited id is not among the gathered items |
| `evidence_after_statement` | a cited item is dated on or before the statement, or has no date |
| `numbers_grounded` | a number in the reasoning is in no gathered item, no measured value and not in the rule or quote (the agent verifier's number rules, against all the evidence, as the verifier checks a draft) |
| `measured_from_tools` | a numeric target is judged without a measured value from `cite_value` or `compute` |
| `measured_meets_target` | `delivered` or `delivered_late` with a measured value short of the required one, or `not_delivered` with one that reaches it |
| `due_period_reported` | `not_delivered` before a 10-Q or 10-K covering the due period exists: silence is not failure |
| `partly_between` | `partly_delivered` with a measured value not between the baseline and the target; for targets without a number, without the judge's stated reason |
| `walked_back_cited` | `walked_back` citing no later statement, filing or news |
| `not_too_early` | `too_early` after the due date and its grace period have passed |

**Timing.** `delivered` needs its first proof dated on or before the end of the grace
period; a later proof makes it `delivered_late` (and `days_late` is counted from the due
date); a `delivered_late` whose proof is on time is `delivered`. The grace period is
`PROMISE_GRACE_DAYS` days after the due date, or, by default, until the first 10-Q or
10-K covering the due period (the fiscal quarter that holds the due date; a date up to 10
days after a quarter's nominal end belongs to it, so December 31 is the fourth quarter of
AMD's 52/53-week year ending December 26). The 10-Q or 10-K is used rather than the earnings release
because XBRL actuals come from it, a few days after the release: ending the grace period
at the release would make every metric-based delivery late.

**When.** `promises resolve` (`--due-only`, the default) takes the specific and partial
claims with a target whose grace period has ended. Open-ended claims are checked once a
10-Q or 10-K has been filed since the statement, for delivery only: any other verdict
leaves them open, as `too_early`. `too_early` claims are checked again by later runs.
Every call goes through the response cache, keyed by the prompt (which holds the rule and
the evidence) and its version, so a run with no new evidence makes no paid call; each
verdict also stores an evidence key (rule version and hash, prompt versions and texts,
and the gathered evidence), so the run reports it as unchanged.

**Review.** `talk-is-cheap promises review ID --verdict V [--note TEXT]`, like guidance
review: the decision is stored by the claim's hash (`promise_reviews`), so it survives
re-extraction, re-framing and re-resolution, and a reviewed verdict always wins, in the
tables, in track records and in the agent's answers.

## 4. Verdicts

| Verdict | Meaning |
| --- | --- |
| `delivered` | the rule was met, with its first proof on or before the end of the grace period |
| `delivered_late` | met, but first shown after the grace period |
| `partly_delivered` | a numeric target moved from the baseline toward the target without reaching it, or part of an event happened (the judge says what) |
| `not_delivered` | the due period was reported and the rule was not met |
| `walked_back` | a later statement by the company withdrew, delayed or lowered the claim |
| `too_early` | not due yet, or an open-ended claim not delivered yet |
| `cannot_determine` | the evidence does not say, or a code check failed |

## 5. Track records

`talk-is-cheap promises track-record [--by executive|company] [--ticker T]
[--specific-only]` (`promises/track.py`):

- only `specific` and `partial` claims count; `vague` ones are counted apart and never
  scored;
- each promise counts once, however many outlets reported it, with the verdict that
  counts (a review's first);
- numeric forecasts already scored against reported results count too, once each: beat
  or met is `delivered`, missed is `not_delivered`;
- rates are of resolved claims (delivered, delivered late, partly delivered, not
  delivered, walked back): the on-time rate (delivered), median days late of late
  deliveries, the share walked back, the share of bold claims and their hit rate
  (delivered on time or late);
- a group with fewer than 5 resolved claims prints "too few to judge" instead of rates.

Company statements and spokespeople form one group per company ("NVDA (company)").

## 6. Commands and the agent

| Command | What it does |
| --- | --- |
| `promises frame [--tickers] [--limit N] [--dry-run] [--missing-only] [--model]` | Frame claims that have no promise of the current version; prints the calls, the most it can cost and the budget first |
| `promises resolve [--tickers] [--limit N] [--dry-run] [--due-only/--no-due-only] [--model]` | Resolve due claims (one per group), one line per claim, then per company: verdicts, failed checks, tokens, cost |
| `promises NVDA [--verdict V] [--open] [--bold]` (or `all`) | Date, speaker, claim, due date, verdict, measured value against the target, and the rule |
| `promises show ID` | The claim, the rule, the baseline, every cited item with its date and link, the measured value, the checks, the reasoning and any review |
| `promises review ID --verdict V [--note]` | Record a reviewer's verdict |
| `promises track-record [--by executive\|company] [--ticker] [--specific-only]` | Track records |
| `eval promises [--dry-run] [--details]` (`make eval-promises`) | Framing accuracy and verdict agreement on `evals/promises/` |

The agent has `get_promises(ticker?, speaker?, verdict?, due_before?, due_after?)`, which
returns each claim with its quote, speaker, date, publisher and article link, rule, due
date, verdict and measured value against the target, and its outcome evidence as items of
their own (each with its filing or article URL); and `get_track_record(ticker?,
speaker?)`, whose items cite the promises they were computed from. The router sends
promise questions ("Did Musk deliver on...", "Which executive's promises hold up best?")
to the guidance-check path by rule. See [agent.md](agent.md).

## 7. Results on the 15 companies

Measured on the development database on 2026-10-20 (framing version `v1+4`).

**Claims.** 357 management claims are stored; 346 are framed (9 numeric forecasts already
have a verdict, 2 claims were reviewed as extraction errors). Their statement dates: 3
from 2023, 72 from 2024, 271 from 2026. **Most of the news history is recent**: the
stored news starts in 2024 and is thin before 2026 (GDELT backfill around earnings
windows, RSS since 2026-10-18), so most claims are not due yet. Everything here is
incremental: older claims added by the backfill and new article extraction are framed
and resolved by the next runs, and re-runs cost nothing when nothing changed.

| Specificity | Claims | Distinct promises | Bold |
| --- | --- | --- | --- |
| specific | 118 | 107 | 36 |
| partial | 79 | 78 | 17 |
| vague | 149 | 143 | 18 |

Specific and partial claims by target and deadline: events 94 (41 with an explicit date,
44 open-ended), absolute targets 61, changes 19, one multiple; 22 have a deadline but no
checkable target.

**Due now:** 59 promises (one per group of identical claims): 22 with a deadline whose
grace period has ended, 37 open-ended ones with a 10-Q or 10-K filed since. Nvidia has
29 of them, AMD 12, Apple 5, Amazon 4, Qualcomm 3, Microsoft, Intel and Alphabet 2 each;
Salesforce, Oracle, Adobe, Netflix, Cisco, Meta and Tesla have none.

| Verdict | Claims | Promises |
| --- | --- | --- |
| delivered | 19 | 15 |
| delivered_late | 2 | 2 |
| too_early | 28 | 28 |
| cannot_determine | 14 | 14 |
| not_delivered, partly_delivered, walked_back | 0 | 0 |

Track records: only Nvidia's company statements have 5 resolved claims (17, all
delivered on time, counting the three revenue forecasts already scored); every other
group is "too few to judge". By company, Nvidia: 19 resolved, 89% on time, 2 late (202
days late, median), bold claims 26% (all kept).

**Example** (`talk-is-cheap promises show 1180`):

```
Promise 1180 (NVDA), version v1+4
  company statement, 2024-05-23, reported by fortune.com: https://fortune.com/2024/05/23/nvidias-landmark-performance-tech-ceo-capitalism/
  "Nvidia said it expects sales in its second fiscal quarter to hit $28 billion"
  subject: quarterly revenue; specificity: specific; bold: False (1.27 times the baseline (FY2024Q4))
  due: 2024-07-31 (fiscal_period, "second fiscal quarter")
  baseline: 22103000000 (FY2024Q4, actuals)
  rule: True if Nvidia's quarterly revenue reaches at least $28 billion in any quarter ending on or before 2024-07-31.

  verdict: delivered, judged against rule version v1+4, resolved 2026-10-20
  measured: $30.04 billion against $28 billion
  first proof: 2024-08-28; on time until 2024-08-28
  checks:
    pass evidence_exist
    pass evidence_after_statement
    pass numbers_grounded
    pass measured_from_tools
    pass measured_meets_target: measured 30040000000.0 against required 28000000000.000000
    note on_time: first proof 2024-08-28, grace period ends 2024-08-28
  evidence cited:
    E1   2024-08-28 get_actuals    revenue FY2025Q2: $30,040M
                                    https://www.sec.gov/Archives/edgar/data/1045810/000104581024000264/nvda-20240728.htm
    measured: $30,040M from E1
```

The baseline is fiscal Q4 2024: the Q1 fiscal 2025 10-Q was filed on 2024-05-29, after
the statement.

## 8. Measurement

`make eval-promises` (`ARGS=--dry-run` prints the calls) on two label files drafted by
Claude (the coding assistant) from the stored data and marked for the project owner to
check: `evals/promises/framing_labels.jsonl` (30 claims framed by hand: due date, target,
baseline, specificity) and `evals/promises/resolution_labels.jsonl` (30 due claims
resolved by hand from evidence stored in the database, not only what the resolver
gathered).

| Measure | Haiku 5.5, prompts v1, post-processing 4 |
| --- | --- |
| Framing: due date | 27/30 (0.900) |
| Framing: target (kind and value) | 25/30 (0.833) |
| Framing: baseline (source and value) | 25/30 (0.833) |
| Framing: specificity | 24/30 (0.800) |
| Verdict agreement | 21/30 (0.700) |
| **False "delivered" rate** (of claims the system calls delivered or late, those the labels do not) | **1/12 (0.083)** |
| False "not delivered" rate | 0/0: no label and no verdict is "not delivered" |
| `cannot_determine` rate | 9/30 (0.300) |

**The label set is lopsided.** The 30 due claims hold 17 delivered, 1 delivered late, 5
cannot determine and 7 too early, and no claim that was clearly not delivered: the due
claims are mostly 2024 corporate actions and product launches that happened (Nvidia's
split, dividend and Blackwell), and several labels are the same event reported by
different outlets. The intended minimum of 5 not delivered and 3 late could not be met
from stored data, so the false "not delivered" rate is not measured yet.

Framing errors: the model reads some concrete events as no target (an iCloud+ upgrade
tier, an announced split, 1,000 job cuts, "will share more at Adobe MAX in November");
it takes a prior estimate or another figure in the paragraph for the baseline (Amazon's
"up from its prior estimate of $200 billion", figures in Oracle's and Qualcomm's
paragraphs), since code checks only that the number is written; it missed Cisco's
"guiding to $72.2B to $73.4B in fiscal 2027" entirely; "effective next month" got no
deadline; and Nvidia's "throughout 2024 (fiscal year 2025)" was read as the fiscal year
(the label says the calendar year; both are defensible).

Resolution errors (the 9 disagreements):

| Claim | System | Label | Why |
| --- | --- | --- | --- |
| Dell and HP laptops with Nvidia's new GPUs (Jan 2024) | delivered | too early | **The false "delivered".** The framed event says "Nvidia's new PC GPUs"; the judge accepted 2026 RTX Spark laptops, a later product. The judge prompt now requires the product the quote names, but the framed rule is too loose |
| Blackwell running in data centers by fiscal Q4 2025 | delivered late | delivered | The 10-K on the grace-end date shows $11.0 billion of Blackwell revenue; the judge picked a later filing as its first proof |
| The split "takes effect next month" | too early | delivered | Framed without its deadline (open-ended), and the judge did not credit the split |
| Microsoft offering MI300X clusters on Azure | too early | delivered | The evidence is in AMD's release; searches are limited to the claim's company |
| Blackwell available in data centers; Blackwell boosting revenue later in 2024 | cannot determine | delivered | The resolver did not gather the 10-K that shows it |
| AMD's $2 billion of AI revenue in 2024 | cannot determine | delivered | The release saying "more than $5 billion of AMD Instinct accelerator revenue" was not gathered |
| Nvidia's dividend raised to $0.10 | cannot determine | delivered | The target is pre-split; the filings give $0.01 after the 10-for-1 split |
| Intel spinning off its programmable chip business | too early | delivered | Intel sold 51% of Altera and deconsolidated it; the judge read a partial sale as not a spin-off (defensible) |

**Spot checks** of 10 verdicts against their evidence (`promises show`), chosen across
verdicts:

| Promise | Verdict | Finding |
| --- | --- | --- |
| Nvidia fiscal Q2 2025 revenue of $28 billion | delivered | Right: $30,040M in the 10-Q filed 2024-08-28, on time |
| Nvidia's split effective by 2024-06-22 | delivered | Right |
| Nvidia's raised dividend paid June 28 | delivered | Right |
| Nvidia phasing out the RTX 4070 Ti | delivered | Right (Tom's Hardware, 2024-01-09) |
| Nvidia shipping Blackwell by the end of 2024 | delivered | **Verdict right, proof wrong**: the cited first proof is the August 2024 10-Q's shipping schedule, not a shipment |
| Dell and HP laptops with Nvidia's new GPU | delivered | **Wrong** (above) |
| Blackwell running in data centers by fiscal Q4 | delivered late | **Wrong**: on time |
| The split "takes effect next month" | too early | **Wrong**: framing lost the deadline |
| Microsoft MI300X clusters on Azure | too early | **Wrong**: evidence outside Microsoft's own documents |
| Apple's fiscal Q3 2026 revenue up 9% to 11% year over year | cannot determine | **Wrong**: first resolved as not delivered against the prior quarter (a sequential baseline for a year-over-year claim), fixed in code (post-processing 3: a year-over-year change is measured from the same period a year earlier); now a cited item dated before the statement fails `evidence_after_statement` |

Fixes made after these checks, all applied from the cache: the month-and-year rule in the
number check (it read "January 2024" as "January 20" and a stray "24"); the number check
against all gathered evidence, as the agent verifier does; the grace period of
52/53-week years (AMD's December 31 deadline was placed in the next quarter); year-over-
year baselines; claims sharing one quote got one prompt and so one cached answer.

## 9. Cost

All through the ledger and the budget, with an estimate and the budget left printed
before every run. Haiku 5.5 at effort `low`.

| Run | Calls | Input / output tokens | Cost |
| --- | --- | --- | --- |
| Framing: trials, all 346 claims, 5 retries at a higher output limit, 6 claims sharing a quote | 359 | 1,260,072 / 243,869 | $0.2480 |
| Resolution: NVDA trials, all due claims, retries, re-runs after fixes (mostly from the cache) | 132 | 538,906 / 36,970 | $0.0724 |
| Benchmark | 2 | 8,532 / 309 | $0.0010 |

A framing call reads about 3,400 tokens (the prompt is most of it: the schema carries no
descriptions) and writes about 700, $0.0007 a claim; a resolution reads about 8,000
(plan and judge, with up to 21 evidence items) and writes about 400, about $0.001 a
claim. Re-runs with no new evidence made no call: the last full resolution re-run of 59
claims cost $0.0036, for the 3 claims whose rule had changed.

## 10. Known limitations

- **News history depth.** Claims exist only for stories in stored news; most are from
  2026 and not due. Older claims arrive with the GDELT backfill and article extraction.
- **Google News links.** 66% of linked stories have only a Google News link, which cannot
  be followed ([statements.md](statements.md)), so most of what executives said is never
  read, and track records cover the few companies whose news can be fetched (Nvidia
  above all).
- **Claims no free source reports.** Operating figures that are only in text (units,
  users, cities) have no stored series, so their baselines are unknown unless the article
  states them; segment revenue, non-GAAP figures, product launches and corporate actions
  are judged from filing text and headlines, and "cannot determine" is common when the
  outcome was announced somewhere not stored (a conference, another company's release,
  a paywalled outlet).
- **Evidence search.** Searches are limited to the claim's company: a partner's release
  that shows the outcome (AMD's, for Microsoft's Azure offering) is not found. Three
  results per search miss passages ranked lower.
- **Timing uses publication dates.** A delivery's date is the date the evidence became
  public, not the date it says the event happened, so a late filing can make an on-time
  delivery look late; the judge picks the first proof, and sometimes picks a later one.
- **Loose events.** An event framed broadly ("Nvidia's new PC GPUs") can be met by a
  later, different product; the judge prompt asks for the product the quote names.
- **Stock splits.** Per-share targets set before a split are not adjusted (Nvidia's
  $0.10 dividend, $6.22 EPS).
- **Vague claims** (43%) are never scored, by design.
