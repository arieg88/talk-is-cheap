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

`promises/frame.py`, `promises/framing.py`, prompts `prompts/promise_framing/v1.md`,
`v2.md` and `v3.md` (the current one).

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
| `related_companies` (v2) | other companies the claim names: partners, customers, makers ("Dell", "HP") | widens the resolver's searches |
| `comparison_basis` (v2) | `yoy`, `sequential`, `absolute` or none | picks a change's baseline (the period a year earlier for `yoy`); shown to the judge. Framings without it fall back to the quote's wording ("year over year") |
| `event` | one sentence: the company and the exact thing the quote names (product and model, partner, place), never a broader category, no purposes or comparisons | the rule's event, and its key terms (section 3) |

Due dates, from the statement date:

| Deadline | `due_basis` | `due_date` |
| --- | --- | --- |
| "in a year", said 2025-03-10 | `relative` | 2026-03-10 |
| "next month", "effective next month", "this month" (said 2024-05-22) | `relative` | the end of that month (2024-06-30, 2024-05-31) |
| "within weeks", "in the coming weeks" / "in the coming months" / "in the coming days" | `relative` | 8 weeks / 6 months / 2 weeks after the statement (conventions of prompt v2) |
| "this quarter", "the current quarter" / "next quarter" | `fiscal_period` | the end of the fiscal quarter under way / the next one |
| a stated launch, availability or effective date; "soon" / "in the near term" / "in the coming years" (prompt v3) | as written / `relative` | the date; 6 months / 12 months / 3 years after the statement |
| "by the end of next year", "later this year", "first half of 2026", "by June", "June 10", "next summer" | `explicit` | the end of that calendar year, half, quarter or month, the day, the end of the season |
| "next fiscal year", "fiscal 2026", "next quarter", "the fiscal fourth quarter" | `fiscal_period` | the end of that fiscal year or quarter on the company's calendar (Nvidia's fiscal 2025 ends 2025-01-31) |
| no time reference at all, or only "over time", "eventually", "in the long run" | `open_ended` | none (expires after 18 months, section 4) |

A month or season named without a year is the next one that ends on or after the
statement date. A fiscal quarter named without a year is, in code, the next one whose
results were not yet filed before the statement date: "the fiscal third quarter", said
by Apple on the day its fiscal Q3 2026 results came out (after the quarter ended), is
fiscal Q3 2026, not fiscal Q3 2027.

**Specificity** is decided in code: `specific` with a checkable target (a figure that
passed grounding, or a concrete event) and a deadline, `partial` with only one of them,
`vague` with neither. Only specific and partial claims are ever scored, and a partial
claim without a target (a deadline for "gross margin would fall") has nothing to judge, so
it is not resolved either.

**Claim types**, in code: a split, dividend or record date on a fixed date is a
`scheduled_action`. It is resolved like any claim, but left out of track records and
bold-claim figures: a company scheduling a corporate action is not a promise about its
business. A claim that restates a formal guidance row (same metric, target period and
figure, a low, high or point within 0.5%, issued within 120 days) is linked to that row
(`guidance_id`) and not resolved or scored again: it is scored with the guidance
(Qualcomm's $9.2 billion and Nvidia's $28 billion revenue outlooks among them).

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
version adds a promise row with the new version (`v3+5`: prompt v3, post-processing 5)
and keeps the old one, no longer `current`, with its verdicts; every verdict records the
rule version and a hash of the rule it judged. A claim keeps the prompt version it was
framed with: a new post-processing version re-processes its cached answer at no cost,
while a new prompt reaches only the claims picked with `--promises` (or every claim with
`--upgrade`). New claims get the current prompt.

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
`prompts/promise_judge/v3.md`. A LangGraph graph per claim, 4 claims at a time:

1. **Plan** (model, structured output): from the rule, the metric's fiscal periods to
   fetch, up to three searches of later filings, up to two of later news, and whether to
   read the company's later statements.
2. **Gather** (code): the plan as at most 6 tool calls, every one limited to evidence
   dated after the statement, 3 results per search: the metric's periods (always with the
   period the due date closes), the company's filings, news and later statements. Then
   each related company the claim names gets a search of its own, with its own 5 results,
   on the rule's event: its filings when it is covered, news about it otherwise (AMD's
   documents for Microsoft's MI300X claim).
3. **Judge** (model, structured output): no verdict. One finding per gathered item:
   `satisfies` (the item shows the rule met: a filing or release reporting the thing as
   having happened, revenue from the product, a completed launch, in full; never a plan,
   a schedule or a repeat of the announcement), `partly` (only part met), `contradicts`,
   `walks_back` or `irrelevant` (including another or a later product). The judge reads
   whole passages (the resolver's `search_filings` keeps all of each passage, the agent's
   keeps 700 characters); of a text longer than 1,600 characters, the windows of 300
   characters around each key term of the rule, joined. For a satisfying
   item, the exact text that says when it happened. For numeric targets, *where* the
   measured number is (an evidence id and the exact span), never the number itself: a
   target with "at least" or "more than" and no measured value cannot be determined. At
   most three sentences of reasoning.
4. **Measure** (code): each source through `cite_value` (spans) or as reported, then
   `compute` (a single value is a sum of one; several are added, or divided), so the
   measured value is always a derived item that cites its inputs.
5. **Decide** (code, `promises/checks.py: derive`). For a numeric target, code judges the
   measured items itself: they satisfy the rule when the measured value reaches the
   required one (per-share targets split-adjusted: a $0.10 dividend set before a 10-for-1
   split is $0.01 after it, from the stock-split table guidance matching uses), show it
   partly met between the baseline and the target, and contradict it otherwise. Then: an
   item satisfies the rule: `delivered`, or `delivered_late` when the earliest delivery is
   late (below); else an open-ended claim is `too_early`, or `expired_unproven` 18 months
   after the statement; else an item walks the claim back: `walked_back`; else, once the
   grace period has ended, one shows it partly met: `partly_delivered`, one contradicts
   it: `not_delivered`; else `too_early` while the due date and grace period have not
   passed, `cannot_determine` after. Silence is not failure. A claim revised by a later
   statement no later than the report of its due period (the end of the grace period) and
   delivered after its due date is `delivered_late` with `revised_before_due`;
   `walked_back` is only for a claim revised and never delivered: code tests satisfying
evidence first, and a claim with any is never `walked_back`, `too_early` or
`expired_unproven` (an invariant `check` enforces).
6. **Check** (code), before anything is stored. Cited items are those not found
   irrelevant, and the measured sources. A failed check makes the verdict
   `cannot_determine` and is recorded by name:

| Check | Fails when |
| --- | --- |
| `evidence_exist` | a cited id is not among the gathered items |
| `evidence_after_statement` | a cited item is dated before the statement, or has no date; one of the statement's own day counts only when its timestamp (the filing's acceptance) is later than the statement's article, or it reports a period that ended after the statement |
| `key_terms` | a key term of the rule is in none of the cited items' text: a product, model, partner or place, that is a capitalized word or a model number ("MI300X", "4070") of the rule's claim, other than the company's own names, months, years, figures and period labels. "Dell and HP release laptops containing the new GPUs" needs Dell, HP and GPUs in the evidence |
| `numbers_grounded` | a number in the reasoning is in no gathered item, no measured value and not in the rule or quote (the agent verifier's number rules, against all the evidence, as the verifier checks a draft) |
| `measured_from_tools` | a numeric target is judged without a measured value from `cite_value` or `compute` |
| `measured_meets_target` | `delivered` or `delivered_late` with a measured value short of the required one, or `not_delivered` with one that reaches it |
| `partly_between` | `partly_delivered` with a measured value not between the baseline and the target; for targets without a number, without the judge's stated reason |

The judge's findings are stored with the verdict (`promise_verdicts.assessments`), and
`model_verdict` is what they give before the checks.

**Timing.** The date of delivery is, for each satisfying item, the event date it states
(the judge's span, found in the item's text and read in code like `cite_value`: "effective
June 7, 2024"; a month alone is its last day), else its publication date; the earliest
counts. A stated date is compared with the due date itself; a publication date with the
end of the grace period, since reports come after the fact. A stated date on or before
the statement is no proof (an announcement repeated). Days late count from the due date.
The grace period is
`PROMISE_GRACE_DAYS` days after the due date, or, by default, until the first 10-Q or
10-K covering the due period (the fiscal quarter that holds the due date; a date up to 10
days after a quarter's nominal end belongs to it, so December 31 is the fourth quarter of
AMD's 52/53-week year ending December 26). The 10-Q or 10-K is used rather than the earnings release
because XBRL actuals come from it, a few days after the release: ending the grace period
at the release would make every metric-based delivery late.

**When.** `promises resolve` (`--due-only`, the default) takes the specific and partial
claims with a target whose grace period has ended, except those linked to formal
guidance, and those not due yet once a 10-Q or 10-K has been filed since the statement:
satisfying evidence resolves a claim whenever it appears, before its due date too, and a
claim is `too_early` only while nothing satisfies it. Open-ended claims are checked once a 10-Q or 10-K has been filed since the
statement, for delivery only: until 18 months after the statement any other verdict
leaves them open, as `too_early`, and then they are `expired_unproven`. `too_early`
claims are checked again by later runs.
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
| `delivered` | the rule was met on time: the event date the evidence states is on or before the due date, or, without one, the evidence was published by the end of the grace period |
| `delivered_late` | met, but late; with `revised_before_due` when the company revised the claim before its due period was reported |
| `partly_delivered` | a numeric target moved from the baseline toward the target without reaching it, or part of an event happened (the judge says what) |
| `not_delivered` | the due period was reported and the rule was not met |
| `walked_back` | a later statement by the company withdrew, delayed or lowered the claim, and it was never delivered |
| `too_early` | the due date and grace period have not passed, or an open-ended claim not shown delivered within 18 months of the statement |
| `expired_unproven` | an open-ended claim not shown delivered 18 months after the statement |
| `cannot_determine` | the evidence does not say, or a code check failed |

## 5. Track records

`talk-is-cheap promises track-record [--by executive|company] [--ticker T]
[--specific-only]` (`promises/track.py`):

- only `specific` and `partial` claims count; `vague` ones are counted apart and never
  scored; scheduled actions and claims restating formal guidance are left out, from the
  counts and the bold-claim figures alike;
- `expired_unproven` claims have a column of their own (EXPIRED): never hidden, never
  counted as resolved;
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
| `promises frame [--tickers] [--limit N] [--dry-run] [--missing-only] [--promises ID,ID] [--model]` | Frame claims that have no promise of the current version (`--promises`: only the claims of these promises); prints the calls, the most it can cost and the budget first |
| `promises resolve [--tickers] [--limit N] [--dry-run] [--due-only/--no-due-only] [--model]` | Resolve due claims (one per group, 4 at a time), one line per claim, then per company: verdicts, failed checks, tokens, cost, seconds a claim |
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

Measured on the development database on 2026-10-20 and 21. Claims keep the prompt they
were framed with: 258 with v1, 37 with v2 and 51 with v3 (`v3+5`: the open-ended ones,
re-framed to date their implied deadlines), all with post-processing 5.

**Claims.** 357 management claims are stored; 346 are framed (9 numeric forecasts already
have a verdict, 2 claims were reviewed as extraction errors). Their statement dates: 3
from 2023, 72 from 2024, 271 from 2026. **Most of the news history is recent**: the
stored news starts in 2024 and is thin before 2026 (GDELT backfill around earnings
windows, RSS since 2026-10-18), so most claims are not due yet. Everything here is
incremental: older claims added by the backfill and new article extraction are framed
and resolved by the next runs, and re-runs cost nothing when nothing changed.

| Specificity | Claims |
| --- | --- |
| specific | 130 |
| partial | 61 |
| vague | 155 |

12 claims are scheduled actions (Nvidia's June 2024 split and dividend dates), and 19
restate formal guidance and are scored with it (Nvidia's $28 billion and $108 billion,
Qualcomm's $9.2 billion, Salesforce's, Adobe's and Netflix's outlooks among them).

**Due now:** 43 promises (one per group of identical claims), against 52 before the
guidance links. Re-framing with v3 dated 11 of the 51 claims it re-framed; 36 have no time reference.

| Verdict | Claims | Promises |
| --- | --- | --- |
| delivered | 13 | 6 |
| delivered_late | 1 | 1 |
| walked_back | 3 | 3 |
| too_early | 14 | 14 |
| expired_unproven | 3 | 3 |
| cannot_determine | 16 | 16 |
| not_delivered, partly_delivered | 0 | 0 |

Track records by executive: only Nvidia's company statements have 5 resolved claims (6:
4 delivered, 1 late, 1 walked back; 67% on time, 50% bold, 2 expired unproven); every
other group, every named executive included, is "too few to judge".

**Example** (`talk-is-cheap promises show 1426`, the reasoning left out):

```
Promise 1426 (NVDA), version v2+4
  company statement, 2024-05-23, reported by fortune.com: https://fortune.com/2024/05/23/nvidias-landmark-performance-tech-ceo-capitalism/
  "Nvidia said it expects sales in its second fiscal quarter to hit $28 billion"
  subject: second-quarter sales; specificity: specific; bold: False (1.27 times the baseline (FY2024Q4))
  due: 2024-07-31 (fiscal_period, "second fiscal quarter")
  baseline: 22103000000 (FY2024Q4, actuals)
  rule: True if Nvidia's second-quarter sales reaches at least $28 billion in any quarter ending on or before 2024-07-31.

  verdict: delivered, judged against rule version v2+4, resolved 2026-10-20
  measured: $30.04 billion against $28 billion
  first proof: 2024-08-28; on time until 2024-08-28

  judge's findings (18 items irrelevant):
    E1   contradicts: FY2025Q2 (ending 2024-07-28) revenue reported as $30,040M, which reached the $28B threshold; this is a reported value for the quarter in the rule window, so it satisfies the rule

  checks:
    pass evidence_exist
    pass evidence_after_statement
    pass key_terms
    pass numbers_grounded
    pass measured_from_tools
    pass measured_meets_target: measured 30040000000.0 against required 28000000000.000000
    note on_time: earliest satisfying evidence 2024-08-28, on time until 2024-08-28

  evidence cited:
    E1   2024-08-28 get_actuals    revenue FY2025Q2: $30,040M
                                    https://www.sec.gov/Archives/edgar/data/1045810/000104581024000264/nvda-20240728.htm
    measured: $30,040M from E1

  gathered: 19 items
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

The labels were reviewed by the project owner on 2026-10-20: duplicates of Nvidia's June
2024 split and dividend were removed (one of each kept), a restatement of formal guidance
removed (it is scored in the guidance table), five verdicts changed (an article repeating
an announcement is no proof; three claims whose deadline passed are `cannot_determine`;
Blackwell's July 2024 shipping is `delivered_late` with `revised_before_due`), and 7 due
claims added. The stored due claims hold no claim that was clearly not delivered, so
none could be added; the set is 12 delivered, 1 late, 12 cannot determine, 5 too early.

| Measure | Judge v1 | Judge v2 | Judge v3, framing v3, owner-reviewed labels |
| --- | --- | --- | --- |
| Framing: due date | 27/30 | 29/30 | 27/30 |
| Framing: target | 25/30 | 27/30 | 25/30 |
| Framing: baseline | 25/30 | 26/30 | 25/30 |
| Framing: specificity | 24/30 | 26/30 | 24/30 |
| Verdict agreement | 21/30 | 18/30 | 18/30 |
| **False "delivered" rate** | 1/12 | 0/9 | **0/5** |
| False "not delivered" rate | 0/0 | 0/0 | 0/0 |
| `cannot_determine` rate | 9/30 | 10/30 | 14/30 |
| `revised_before_due` flag | - | - | 1/1 |

The label sets differ between columns (the third is the reviewed one); the framing
figures fall in the third because two labeled claims are framed with prompt v3 now and a
third label follows the new deadline rule.

Framing errors left: events the model reads as no target (an iCloud+ upgrade tier, an
announced split, 1,000 job cuts, "will share more at Adobe MAX"); a figure in the
paragraph taken for the baseline (Amazon's "prior estimate of $200 billion", Oracle's,
Qualcomm's); Cisco's "guiding to $72.2B to $73.4B in fiscal 2027" missed; Nvidia's
"throughout 2024 (fiscal year 2025)" read as the fiscal year; and three Nvidia claims the
owner dates by their launch (nvda-4, 5, 98) that v3 still frames without a deadline, so
they expire instead of being `cannot_determine`.

**Spot checks** (`promises show`) of 25 verdicts: the same 20, and 5 scheduled actions
and expired claims.

| Promise | Verdict | Finding |
| --- | --- | --- |
| Nvidia fiscal Q2 2025 revenue of $28 billion | linked to guidance | Right: it restates the release's guidance, scored in the guidance table |
| Qualcomm's fiscal Q3 2026 revenue guidance of $9.2 billion | linked to guidance | Right (it was refused as same-day evidence before) |
| Apple's fiscal Q3 revenue up 9% to 11% year over year | delivered | Right: now dated fiscal Q3 2026; $109,417M against $94,036M |
| Nvidia's split completed by 2024-06-07 | delivered (scheduled action) | Right, on its stated date, June 7 |
| Nvidia's split "takes effect next month" | delivered (scheduled action) | Right |
| Split-adjusted trading from June 10 | delivered (scheduled action) | Right |
| Nvidia's raised dividend paid June 28 | delivered (scheduled action) | Right, event date June 28 |
| RTX-class gaming in thin Windows laptops | delivered | Right |
| Intel's Core Ultra in more than 230 AI PCs | cannot determine | Right |
| Amazon acquiring Globalstar | too early | Right |
| Apple's automatic proofreading in more apps | too early | Right (open-ended, within 18 months) |
| Dell and HP laptops with Nvidia's new GPUs | cannot determine | Right, as the owner labels it: `key_terms` refuses the 2026 laptops |
| Nvidia phasing out the RTX 4070 Ti | expired unproven | Right in substance: the only evidence repeats the announcement; the owner's label is `cannot_determine` (framing gives no deadline) |
| Nvidia's dividend raised to at least $0.10 by June 28 | cannot determine | **Error** (framing): the rule asks for the rate "in any quarter ending on or before 2024-06-28", and the judge sets the June 2024 filing aside as about a later quarter; split adjustment would have counted $0.01 |
| Nvidia shipping Blackwell by July 2024 | walked back, revised before due | **Error**: the owner's label is `delivered_late`; the judge still calls the 10-K with Blackwell revenue "partly", so nothing satisfies |
| Nvidia shipping Blackwell by the end of 2024 | walked back | **Error**: same cause; delivered |
| Blackwell in data centers by fiscal Q4 2025 | walked back | **Error**: same cause; delivered |
| Blackwell launch boosting revenue later in 2024 | delivered late | **Error**: same cause; the first item found satisfying is a 2025 filing |
| Microsoft's MI300X clusters on Azure | expired unproven | **Error**: AMD's July 2024 release is now gathered (its own search), but its Azure sentence lies beyond the 500 characters the judge reads of a passage |
| Intel spinning off its programmable chip business | too early | **Error**: framed with "within the next few years" (3 years, to 2027-01-08); the owner labels the Altera deconsolidation as delivery |
| AMD's $2 billion of AI revenue in 2024 | cannot determine | **Error**: the release with "more than $5 billion of AMD Instinct accelerator revenue" is not gathered |
| Scheduled: Nvidia executes its 10-for-1 split by June 30 | delivered | Right, June 7 |
| Scheduled: the raised $0.10 dividend paid June 28 | delivered | Right: $0.01 after the split |
| Scheduled: split-adjusted trading from June 10 (two more reports) | delivered | Right |
| Expired: Nvidia's RTX 4070 Super Founders Edition | expired unproven | Right in substance: no stored evidence of it |

8 errors in 25, none a false "delivered". Four share one cause the judge prompt v3 did
not fix: a filing reporting Blackwell revenue is still called "partly" (and, for claims
whose ramp moved, its earlier delay "walks back"), so the claims fall to `walked_back`.

**Speed.** Before: one claim at a time, about 40 seconds a claim on the first full run
(16 claims in about 10 minutes), of which 4.9 seconds were evidence gathering (measured
from the cache on 8 claims) and the rest the plan and judge calls. After: 4 claims at a
time and the search models in worker threads: 6.7 seconds a claim of wall time for
Nvidia's 23 claims with paid calls (each claim takes 15 to 23 seconds on its own), 4 to 6
seconds a claim from the cache. A shallower rerank does not keep the same evidence (24
claims: 17 identical at depth 16, 10 at 12, 6 at 10, against depth 20), so it stays at 20.

## 9. Cost

All through the ledger and the budget, with an estimate and the budget left printed
before every run. Haiku 5.5 at effort `low`.

| Run | Calls | Cost |
| --- | --- | --- |
| Framing v1: trials, all 346 claims, retries at a higher output limit, claims sharing a quote | 359 | $0.2480 |
| Framing v2: the 64 claims its changes affect | 64 | $0.0529 |
| Resolution, judge v1: NVDA trials, all due claims, retries, re-runs after fixes | 132 | $0.0724 |
| Resolution, judge v2: all 52 due claims, 2 retries at a higher output limit | 90 | $0.0764 |
| Framing v3: the 51 open-ended claims, to date implied deadlines | 51 | $0.0445 |
| Resolution, judge v3: the 43 due claims | 60 | $0.0716 |
| Benchmark (two runs framed their labels with a newer prompt; it now uses each claim's own) | 34 | $0.0277 |

A framing call reads about 3,400 tokens with prompt v1 and 4,100 with v2 (the prompt is
most of it: the schema carries no descriptions) and writes about 700 to 800, $0.0007 to
$0.0008 a claim. A resolution reads about 8,000 tokens (plan and judge, with up to 21
evidence items) and writes about 1,300 with judge v2 (a finding per item), about $0.0015
a claim. Re-runs with no new evidence make no call: the last re-run of all 52 due claims
cost nothing, every response coming from the cache.

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
- **Evidence search.** Searches cover the claim's company and up to two related
  companies it names; three results per search (five per related company) miss
  passages ranked lower.
- **Timing uses publication dates.** A delivery's date is the date the earliest
  satisfying evidence became public, not the date it says the event happened, so a late
  filing can make an on-time delivery look late.
- **"Partly" for what happened.** The judge often calls a filing that reports the
  outcome (Blackwell revenue in the 10-K) "partly" rather than "satisfies", which gives
  `partly_delivered` or a late delivery; three of the 20 spot-check errors.
- **Key terms are words.** The check needs each capitalized word or model number of the
  rule in the cited evidence: a rule naming a product less exactly than the evidence
  ("the new GPUs" against "RTX Spark") fails, and synonyms ("ten-for-one") do not count.
  It turns weak evidence into `cannot_determine`, never into a verdict.
- **Same-day evidence.** A result filed on the statement's own date (guidance restated on
  results day) is not after the statement and is refused.
- **Framing versions.** Claims framed with prompt v1 keep their v1 promise until framed
  again; only the 64 claims prompt v2 changes were re-framed.
- **Stock splits.** Per-share targets set before a split are not adjusted (Nvidia's
  $0.10 dividend, $6.22 EPS).
- **Vague claims** (43%) are never scored, by design.
