# Guidance

How `talk-is-cheap extract guidance` turns the outlook sections of earnings releases into
structured guidance, how each statement is matched with the result reported later, and
how well the extraction does on hand-labeled sections.

## 1. How it works

1. **Select documents.** Press releases and earnings exhibits (Nvidia's CFO commentary)
   with `outlook` sections that give guidance. Outlooks that only say guidance is given on
   the earnings call (Microsoft) are skipped (`filings/sections.py: has_guidance`).
   Apple, Alphabet, and Oracle before 2026 give no guidance in their releases; they are
   reported with zero rows, not as failures.
2. **One model call per document**, over all of its outlook sections, with the prompt in
   `prompts/guidance_extraction/v1.md`. The model is a LangChain chat model with
   structured output (`with_structured_output`, JSON-schema mode), so provider and model
   are configuration (`LLM_PROVIDER`, `LLM_MODEL`). The model returns each statement as
   written (`guidance/schema.py`): canonical metric and the company's words, segment,
   basis, measure, constant currency, the target period as named, the figure as written,
   qualitative text, conditions, and an exact quote. It does not compute.
3. **Post-process in code** (`guidance/items.py`):
   - figures: `numbers.py` reads the wording into low, high, point, plus-or-minus and
     unit (section 3);
   - periods: `periods.py` resolves the period from the release's fiscal quarter
     (section 4);
   - grounding: `grounding.py` finds the quote in the outlook sections, after
     normalizing quotes, dashes, whitespace, case and markup, and stores its character
     range in the document. Items whose quote is not found are dropped and logged;
   - condition scoping: a condition the model attached to an item is looked up in the
     text; when the sentence it is in, or else its line (paragraph, bullet or table
     row), holds other items' quotes but not this item's, it belongs to them and is
     dropped (`scope_conditions`). Conditions written apart from every item, or not
     found in the text, are kept;
   - duplicates within the document are merged.
4. **Rows per filing.** The items of all documents of an earnings filing are merged:
   the same statement in the press release and the CFO commentary, or in a text and a
   table, is one `guidance` row with several `guidance_sources`. Each filing's statements
   are their own rows, so a full-year outlook raised over three quarters is three rows.
5. **Verdicts** for every row of the company (`guidance/matching.py`, section 5).

Runs are incremental. A document already extracted with the same model, prompt version
and prompt text is skipped; a changed section, a new prompt version or another model
extracts it again and replaces its rows. `--missing-only` extracts only documents never
extracted and keeps those extracted with another model. When post-processing changes
(`items.py: PROCESSING_VERSION`), stored extractions are processed again from their
cached responses, without a model call. Extractions of documents that were re-parsed
away are removed. Verdicts are recomputed on every run, so `pending` rows get a verdict
once the results are filed.

## 2. What counts as guidance

These rules are in the prompt and were used to label the benchmark.

- A statement of what the company expects a financial measure of its own to be in a
  future period: revenue, margins, expenses, operating income, EPS, capital
  expenditures, tax rate, and other measures (segment revenue, ARR, remaining
  performance obligation, cash flow, share count, deliveries).
- One item per metric, basis, segment, measure, currency basis and period. "GAAP and
  non-GAAP gross margins are expected to be 74.9% and 75.0%, respectively, plus or minus
  50 basis points" is two items. A statement about several quarters ("in Q3 and Q4", "the
  second half of the year", "the remaining quarters of 2026") is one item per quarter.
- Growth stated next to a dollar range ("or to grow between 9% and 12%") is its own item,
  measured as year-over-year or sequential growth. So is the growth implied at the
  midpoint when the release states it.
- Targets assumed for the company's own measures in the period ("Targets assume non-GAAP
  operating margin of ~44.5%, ... and diluted share count of ~402 million") are items.
- Figures from GAAP-to-non-GAAP reconciliation tables count when not stated elsewhere
  (Cisco's GAAP gross and operating margins); the reconciling adjustments do not.
- Qualitative guidance about a measure in a period ("operating income this year that is
  above 2025 operating income") is an item without a figure.
- Not guidance: results already reported, earlier guidance quoted for comparison
  ("narrowed from our prior outlook of $125-145 billion"), product plans and timelines,
  and statements without a measure and a period.
- Conditions: assumptions, inclusions and exclusions, stated for the item or for all of
  the guidance ("excluding any discrete items", "absent any changes to our tax
  landscape", "not assuming any Data Center compute revenue from China"). The exchange
  rates a figure is based on ("based on F/X rates as of January 1, 2026") and long-term
  rates used to compute other guidance (Salesforce's long-term non-GAAP tax rate) are
  conditions, not items or a currency basis. `constant_currency` is only for figures
  stated in constant currency or F/X neutral.

## 3. Figures

Amounts are stored in dollars, per-share values in dollars per share, rates in percent
(74.0 means 74%).

| Wording | low | high | point | plus_minus |
| --- | --- | --- | --- | --- |
| "$108.0 billion, plus or minus 2%" | 105.84B | 110.16B | 108.0B | 2, percent |
| "approximately $13 billion, plus or minus $300 million" | 12.7B | 13.3B | 13B | 300M, absolute |
| "74.0%, plus or minus 50 basis points" | 73.5 | 74.5 | 74.0 | 0.5, absolute |
| "17.0%, plus or minus 1%" (a rate) | 16.0 | 18.0 | 17.0 | 1, absolute (points) |
| "$15.8-16.8 billion", "$9.7B - $10.5B", "between $1.83 and $1.91", any dash | range | range | | |
| "approximately $9.2 billion", "~44.5%" | | | point | |
| "at least $90 billion", "20%+" | bound | | | |
| "$(0.21)", "(79)%" | | | negative | |
| "in the mid-70% range", "slightly above 12%", "low double-digit" | qualitative | | | |

A percentage plus or minus a percentage is plus or minus percentage points; an amount
plus or minus a percentage is relative. A scale or sign written once in a range applies
to both ends ("$61-64 billion").

## 4. Periods

Years are fiscal years, labeled by the calendar year in which they end, as companies name
them. From the release's fiscal quarter:

- "third quarter of fiscal 2027", "Q3 FY27", "FY27 Q3", "Q4'25", "third quarter of 2026"
  name the quarter;
- "next quarter" and a quarter without a year ("the fourth quarter") are the next such
  quarter after the release's quarter; a fourth-quarter release's "first quarter" is in
  the next year;
- "fiscal 2027", "FY27", "2026" name the year; "full year" without a number is the year
  still under way after the release (for a fourth-quarter release, the new year);
- "the quarter ending December 31, 2026" is placed with the company's fiscal calendar;
- half years, nine months, "the rest of the year", and calendar years of companies whose
  fiscal year does not end in December are not resolved.

Tested with fiscal years ending in January (Nvidia, Salesforce), June (Microsoft),
September (Qualcomm) and December (AMD, Meta) (`tests/test_guidance_periods.py`).

## 5. Verdicts

| Verdict | Meaning |
| --- | --- |
| `beat` | the actual is above the guided range, above the bound of an "up to" figure, or above a point by more than its tolerance |
| `met` | within the range, at or above an "at least" figure, or within the tolerance of a point |
| `missed` | below the range, below an "at least" figure, or below a point by more than its tolerance |
| `pending` | the target period has not been reported yet |
| `not_comparable` | cannot be compared; the reason is stored |

**"Beat" is a direction, not a judgment.** It means the actual came in above the
guidance. For operating expenses and capital expenditures that is spending more than
guided, usually unfavorable. The signed difference (`actual - reference`) and its
percentage of the reference are stored for every compared row, so either reading is
possible. The reference is the point, else the midpoint of the range, else the bound.

Point estimates use named tolerances (`matching.py`): 0.5 percentage points for rates,
1% of the point for amounts and per-share values, and never less than half a cent per
share, the rounding of reported EPS.

**Plausibility check.** A guided value far from the company's own result for the
period before the target (the quarter before a quarterly target, the year before a
full-year one, as first reported and in the same measure) is more likely an extraction
error than guidance, so the row is flagged for review (`matching.py:
check_plausibility`). The verdict is kept, but flagged rows are left out of verdict
counts until reviewed:

| Rule | Applies to | Flagged when |
| --- | --- | --- |
| `ratio` | revenue, operating expenses | guided outside 50% to 200% of the prior period |
| `ratio` | capex, operating income | outside 33% to 300% (AI build-outs more than doubled capex in a year) |
| `ratio` | EPS | outside 25% to 400% (one-time items swing EPS) |
| `margin_points` | gross and operating margin | more than 15 pp from the prior period |
| `growth_points` | year-over-year and sequential growth | more than 30 pp from the prior period's growth |
| `share_points` | shares of revenue | more than 10 pp from the prior period |

The guided value is the point, else the middle of the range, else the bound. A ratio is
skipped, and recorded as skipped with the reason, when the prior value is zero,
negative or below $10 million ($0.10 a share), or the guided value is zero or negative;
any rule is skipped when the prior period is not in the stored history. Every comparable
or pending row records `passed`, `flagged` or `skipped`, the rule and the numbers.

`talk-is-cheap guidance review ID --ok` clears a flag; `--wrong` leaves the row out of
verdicts. Decisions are stored in `guidance_reviews` by filing and item key (a hash of
the statement's metric, basis, segment, measure, period, figures and unit), so they
survive rebuilding the filing's rows.

**Actuals** are the target period's value as first reported (the earliest filing in
`financial_values`), never a later restatement:

| Guided metric | Actual |
| --- | --- |
| revenue, operating income, capex, EPS | revenue, operating_income, capex, eps_diluted |
| operating expenses | operating_expenses: `us-gaap:OperatingExpenses`, else `CostsAndExpenses` (Meta's "total expenses") |
| gross margin, operating margin | gross profit or operating income over revenue, all first reported, in percent; compared in percentage points |
| year-over-year or sequential growth | the first-reported values of both periods |
| share of revenue (Salesforce capex) | the metric over first-reported revenue |
| Meta capex | capex plus principal payments on finance leases, as Meta defines its guidance |

Full-year guidance uses the annual value. Guidance without a GAAP or non-GAAP label (most
revenue, capex, Amazon's operating income, Netflix's operating margin) is compared with
the GAAP actual and flagged `basis_assumed`, since SEC rules require non-GAAP measures to
be identified as such.

**Not comparable**, with the reason stored: qualitative guidance; metric `other`; tax
rates; non-GAAP guidance (XBRL actuals are GAAP); segment guidance (actuals are company
totals); constant-currency guidance; an unresolved period; a unit that does not fit the
metric; growth of a margin; a reported period without the metric (Netflix tags no
operating expenses); and per-share guidance issued before a stock split and reported
after it. Splits are in `guidance/splits.yaml`, checked against the split ratio each
company reports in XBRL and the text of its filings: Nvidia 10-for-1 (June 10, 2024) and
Netflix 10-for-1 (November 17, 2025).

## 6. Cost control

- **Ledger.** Every LLM call that reaches the provider (extraction, the benchmark, and
  any later command) is recorded in `llm_spend` as it returns: command, model, prompt
  version, tokens and cost, including calls whose answer could not be used. Cached
  responses cost nothing and are not recorded. `talk-is-cheap spend` shows the total
  against the budget, and spending per command and per model.
- **Total budget.** `LLM_BUDGET_USD` (default $2.50) caps everything the ledger counts.
  A call whose estimated cost could take the total over it is refused before it is made,
  with a message showing what was spent and what remains; the run keeps what it did
  before. Spending from before the ledger is recorded as notes that do not count: $5.22
  on 2026-10-19 (Opus 5.5 $4.06, Sonnet 5.5 $1.08, Haiku 5.5 $0.07, 186 calls), taken
  from the response cache.
- **Per run.** A run also stops before a call that could take it over `LLM_COST_CAP`
  (default $10). Calls in flight hold their estimate, so parallel calls cannot overrun
  either cap together.
- Every response is cached in `LLM_CACHE_DIR` by model (with effort), prompt version and
  a hash of the prompt. Re-runs, the benchmark and tests cost nothing; tests use a fake
  model and never call a real one.
- `--dry-run` prints the calls a run would make, the estimated tokens, the most it could
  cost, and the budget left. Every run prints its tokens and cost and the budget left.

Estimates (`llm.py`), measured on the 24 labeled documents: input is 1,258 tokens of
instructions and output schema plus one token per 2.64 characters of prompt (estimated as
1,500 plus one per 2.4); output was at most 1.57 times the input (estimated as 1,500 plus
1.6 times). The estimate is an upper bound: actual output was 1,400 tokens a document for
Sonnet and Opus.

## 7. Extraction benchmark

`evals/guidance/labels.jsonl`: 24 documents (32 outlook sections) from 12 companies,
issued from January 2024 to September 2026, labeled by reading the text, never from
model output. 198 statements: 91 ranges, 15 plus-or-minus figures, 72 points, 2 bounds
and 18 qualitative items; 59 non-GAAP, 18 segment, 56 full-year, 49 growth and 18
constant-currency items; 116 with conditions. One document (Tesla, Q2 2026) has no
guidance. Labels follow section 2; after review, 6 were changed (an exchange-rate basis
recorded as a condition, a long-term tax rate made a condition of non-GAAP EPS, a
duplicate removed, two statements about several quarters split into one item per
quarter) and conditions were added to 113.

`talk-is-cheap eval guidance` pairs extracted items with labels (same metric, basis,
measure and currency basis, and overlapping quote, period or figures) and reports:
precision, recall and F1 overall and separately for numeric and qualitative items;
figures exact (low, high, point and unit all equal, of matched pairs); period accuracy;
conditions captured (of labels with conditions) and spurious conditions (of labels
without); tokens and cost per document. For metric `other` a segment may stand in for
the metric's name ("energy storage" deployments), so segment presence is not compared.

Results, all models at effort `low`, the production prompt (`v1`) and post-processing
version 2, measured 2026-10-19:

| Model | F1 all | F1 numeric | F1 qualitative | Figures exact | Periods | Conditions captured | Spurious conditions | Output tokens/doc | $/doc |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **claude-haiku-5-5** (default) | 0.965 | 0.981 | 0.800 | 1.000 | 0.995 | 0.85 | 0.17 | 2,110 | 0.0015 |
| claude-sonnet-5-5 | 0.967 | 0.983 | 0.792 | 0.973 | 0.984 | 0.87 | 0.16 | 1,405 | 0.0226 |
| claude-opus-5-5 | 0.990 | 1.000 | 0.889 | 0.979 | 0.990 | 0.82 | 0.05 | 1,404 | 0.0451 |

An earlier run, whose prompts named companies by their SEC names ("NVIDIA CORP") instead
of `universe.yaml` names, gave F1 0.940, 0.982 and 0.987 and numeric F1 0.962, 0.986
and 1.000: run-to-run variation is a few points for Haiku and Sonnet.

By accuracy, Opus 5.5 is best: the best overall F1 in both runs, every numeric
statement found with the right basis both times, and the fewest conditions on the wrong
item. **The default is `claude-haiku-5-5`**, chosen for the project's budget
(`LLM_BUDGET_USD` $2.50): it gets every figure exact and 98% of numeric statements
(F1 0.981) at one thirtieth of Opus's cost, $0.0015 a document; its weaker points are
qualitative items (F1 0.80) and extra items. Opus stays available with `--model
claude-opus-5-5`.

Condition scoping (section 1), applied to the cached responses at no cost, cut
conditions on the wrong item from 13% to 5% for Opus and from 21% to 16% for Sonnet.
Haiku's stayed at 17%: its extra conditions are mostly loose text ("Full year 2026
guidance is unchanged", "calculated on GAAP revenue") rather than another item's, and
in two Meta documents it turned the exchange-rate assumption into an item of its own,
which then keeps the condition.

What the remaining errors are:

- Share counts under a "(millions)" table heading are read as 954 rather than 954
  million (Salesforce). They are metric `other`, never compared.
- "Exit the year with non-GAAP gross margins in the mid-70% range" is resolved to the
  full year by every model; the label says the last quarter. Qualitative, never compared.
- Not every statement about several quarters is split ("the second half of the year").
- Conditions stated once for all of the guidance (Cisco's "this guidance does not
  include the effects of any future acquisitions...", Adobe's macroeconomic assumption)
  are often missed. A condition written in one bullet was attached to the others too
  (Nvidia's "not assuming any Data Center compute revenue from China" on margin and
  expense items) until condition scoping removed it. Conditions are shown, never used in
  verdicts.

## 8. Results on the 15 companies

Extracted on 2026-10-19: 135 documents, 66 with Opus 5.5 (before the default changed)
and 69 with Haiku 5.5; each row records its model. The Haiku run made 69 calls, 321,707
input and 209,447 output tokens, **$0.1369** from the ledger (the dry run's upper bound
was $0.37); the 66 Opus documents were re-processed from the cache at no cost.

| Company | Documents | Rows | beat | met | missed | pending | not comparable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ADBE | 11 | 205 | 35 | 3 | 2 | 12 | 153 |
| AMD | 11 | 47 | 21 | 5 | 0 | 3 | 18 |
| AMZN | 11 | 33 | 21 | 9 | 0 | 3 | 0 |
| CRM | 11 | 259 | 33 | 22 | 7 | 22 | 175 |
| CSCO | 11 | 155 | 41 | 10 | 6 | 6 | 92 |
| INTC | 11 | 78 | 13 | 7 | 8 | 3 | 47 |
| META | 11 | 72 | 12 | 14 | 3 | 7 | 36 |
| NFLX | 11 | 80 | 19 | 12 | 2 | 12 | 35 |
| NVDA | 22 | 102 | 13 | 9 | 8 | 3 | 69 |
| ORCL | 3 | 32 | 2 | 2 | 0 | 4 | 24 |
| QCOM | 11 | 59 | 8 | 10 | 0 | 2 | 39 |
| TSLA | 11 | 14 | 0 | 0 | 0 | 0 | 14 |
| **Total** | **135** | **1,136** | **218** | **103** | **36** | **77** | **702** |

Counts include the three rows flagged for review below (two beat in ADBE and INTC, one
beat in QCOM); `guidance` and `extract guidance` leave them out until reviewed.

AAPL, GOOGL and MSFT have releases but no outlook with guidance: 0 rows, as expected.
259 compared or pending rows have no stated basis and were compared as GAAP
(`basis_assumed`). Most not-comparable rows are non-GAAP, tax-rate, segment,
constant-currency or qualitative guidance.

**Dropped by grounding: 2 items**, both Netflix (FY2024 Q3 letter): the sentence "For
2025, based on F/X rates as of 9/30/24, we forecast revenue of $43B-$44B…" is split in
the converted text by a footnote block, so no contiguous quote exists.

**Spot checks** against the filings (all correct):

| Guidance | Guided | Filing | Verdict |
| --- | --- | --- | --- |
| NVDA revenue, FY2027 Q2 | $91.0B ±2% ($89.18-92.82B) | 10-Q: total revenue $96,221M | beat, +$5.22B |
| NVDA GAAP gross margin, FY2027 Q2 | 74.9% ±50 bp | 10-Q: 72,142 / 96,221 = 74.98% | met, +0.08 pp |
| NVDA GAAP operating expenses, FY2027 Q2 | ~$8.5B | 10-Q: total operating expenses $8,408M | missed, -1.1% (spent less) |
| AMD revenue, Q2 2026 | $11.2B ± $0.3B | 10-Q: net revenue $11,536M | beat |
| META capex incl. finance leases, 2025 | $70-72B | $72.215B; Meta's own figure: "$72.22 billion for the … full year 2025" | beat |

The largest differences were checked too: Intel's GAAP EPS of -$3.88 and gross margin of
15.0% (Q3 2024), Qualcomm's GAAP EPS of $6.88 (fiscal Q2 2026) and Salesforce's of
$4.29 (Q2 FY27) are what their 10-Qs report.

**Plausibility check** on the 434 comparable or pending rows:

| Rule | Passed | Flagged | Skipped |
| --- | --- | --- | --- |
| ratio | 242 | 2 | 20 (14 no prior period: EPS and revenue of early quarters; 6 Intel EPS at or below $0.10) |
| margin_points | 87 | 1 | 0 |
| growth_points | 77 | 0 | 3 (no prior period) |
| share_points | 2 | 0 | 0 |

The growth rule flags nothing; the largest gap it passes is 19.3 pp (AMD's seasonal
sequential growth), so 30 pp is kept. The three flagged rows, checked against the
filings:

| Row | Flag | Finding |
| --- | --- | --- |
| ADBE revenue FY2025, "~$200M" (FY2024 Q4 release) | 1% of FY2024 revenue | **Extraction error.** The sentence is "an approximate $200 million headwind to fiscal year 2025 revenue, as a result of the effect of foreign exchange"; Opus read it as revenue guidance. |
| INTC GAAP gross margin Q4 2024, 36.5% | +21.5 pp from Q3 2024's 15.0% | Genuine guidance; the prior quarter was depressed by impairments. Actual 39.2%. |
| QCOM GAAP EPS fiscal Q3 2026, $1.26-1.46 | 20% of Q2's $6.88 | Genuine guidance; Q2 included a one-time item. Actual $1.87. |

One of three flags is an error; the two false positives come from a one-time item in
the prior quarter, which one prior period cannot tell apart. No review decisions have
been recorded.
