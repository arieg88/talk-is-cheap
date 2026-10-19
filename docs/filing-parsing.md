# Filing text parsing

How `talk-is-cheap ingest filings` turns SEC filings into sectioned, citable text, and
the rules it applies because real filings do not follow one layout.

## 1. How it works

1. **Select filings.** From the `filings` table, each company's 10-Qs, 10-Ks and 8-Ks
   whose items include 2.02. Amendments are not parsed
   (`filings/documents.py: is_parsed_form`). A filing already parsed with the current
   `PARSER_VERSION` is skipped (`filings/ingest.py`).
2. **Fetch.** edgartools downloads the full submission once and caches it. The adapter
   keeps the primary document and the EX-99 exhibits that are HTML, each with its SEC
   document type and sec.gov URL (`edgar/source.py: EdgartoolsSource.documents`,
   `filing_documents`).
3. **Pick documents.** For a 10-Q or 10-K, the primary document. For an item 2.02 8-K,
   every EX-99 exhibit, classified as press release, earnings exhibit, deliveries report
   or other exhibit (`filings/documents.py: parse_filing`).
4. **HTML to text.** Markdown-flavoured text: `#` headings, `**bold**`, markdown tables,
   hidden XBRL and scripts removed, whitespace normalised
   (`filings/html_text.py: convert_html`). The function is pure, so the same HTML always
   gives the same text and `content_hash`.
5. **Remove page furniture.** The document is split into pages at its page breaks, and
   running headers, footers and page numbers are removed. What was removed is kept per
   page (`Page.furniture`).
6. **Split and tag sections.** Releases and exhibits are split on headings. 10-Qs and
   10-Ks are split on item headings into `mdna` and `risk_factors`
   (`filings/sections.py`).
7. **Assign the fiscal period.** A 10-Q or 10-K is placed by its period of report, so a
   10-K is fiscal Q4. A release is placed in the quarter it reports
   (`filings/periods.py: release_period`), and its exhibits share that quarter.
8. **Store.** One `documents` row per document, holding its full text, and one
   `sections` row per section. Each section has `char_start` and `char_end` into
   `documents.text`, so it can be cited by URL and character range. Each company is
   written in one transaction. Raising `PARSER_VERSION` makes the next run re-parse
   every stored document.

## 2. Adjustments

Each entry covers what real filings do, the general rule, where the rule lives, the test
that covers it, and its known limits.

### Item extraction is our own, not edgartools'
- **Filings:** Intel's 10-Ks and 10-Qs title their sections ("Management's Discussion
  and Analysis") instead of numbering them, and edgartools' item extraction finds no
  MD&A in them. Its sections also carry no offsets into the document text, and its text
  drops bold, which the risk-factor headings depend on.
- **Rule:** edgartools only fetches filings and lists their attachments. Items are split
  from our own converted text.
- **Where:** `edgar/source.py` (module docstring), `filings/sections.py:
  periodic_report_sections`.
- **Test:** `test_sections.py::test_titled_sections_without_item_numbers` (trimmed Intel
  10-Q fixture).
- **Limits:** we maintain item matching ourselves.

### Item headings in the table of contents, page headers and cross references
- **Filings:** every 10-K lists "Item 1A. Risk Factors" in its table of contents, and
  Microsoft repeats "PART I / Item 1A" on every page.
- **Rule:**
  - Table lines are skipped.
  - Repeats of one item with no other item between them are a single section, titled by
    the first heading that names the item.
  - Of the remaining candidates the longest wins, since a contents entry runs only to
    the next entry.
  - A PART heading sets the part but is not a boundary.
- **Where:** `sections.py: _item_headings`, `_collapse_repeats`, `_longest_item`.
- **Tests:** `test_annual_report_items_skip_the_table_of_contents` (AAPL 10-K),
  `test_quarterly_report_with_running_page_headers` (MSFT 10-Q),
  `test_item_headings_are_matched_tolerantly`, `test_quarterly_part_ii_item_2_is_not_mdna`.
- **Limits:** an item heading that only appears inside a table is not found.

### Titled sections (Intel)
- **Filings:** Intel's MD&A is a series of 18pt banners. Its pages carry footers naming
  the section ("MD&A", "Risk Factors"). Its 10-K places "Properties" on a page inside
  MD&A.
- **Rule:**
  - Text set at 13.5pt or more is a heading (`#` at 16pt or more).
  - A titled section runs from the heading that names it to the next heading that is a
    standard 10-K/10-Q section title.
  - When page footers show the section continuing, it is extended through the last page
    whose removed footer names it.
- **Where:** `html_text.py: _render_paragraph`, `sections.py: _longest_titled`,
  `_STANDARD_TITLE`.
- **Tests:** `test_titled_sections_without_item_numbers`,
  `test_page_footers_extend_a_titled_section`.
- **Limits:** depends on the list of standard titles and on footer names (MD&A and Risk
  Factors only).

### Page headers, footers and page numbers
- **Filings:**
  - Apple's 10-K pages end with "Apple Inc. | 2025 Form 10-K | 5".
  - Microsoft's pages start with "PART I" / "Item 2" and end with a page number.
  - Intel's pages start with "Table of Contents" and end with "MD&A" and a number.
  - Left in place, these break sections across pages, and they would make unchanged
    risk factors look reworded from one year to the next.
- **Rule:** pages come from `page-break-before/after` styles, which every 10-Q and 10-K
  in the universe has. A block among the first or last three of a page, at most 120
  characters and not a table, is furniture when:
  - it is a page number ("47", "Page 5", "F-12", "ii"), or
  - its text, digits ignored, recurs in the same place on at least three consecutive
    pages.
  Headers and footers are tracked separately. Intel's "Risk Factors" footer would
  otherwise remove the "Risk Factors" title that opens the section's first page. On a
  short page, the top half can hold headers and the bottom half footers. A document of
  one page keeps everything.
- **Where:** `html_text.py: _remove_furniture`, `_edges`, `_furniture_key`, `_PAGE_NUMBER`.
- **Tests:** `test_running_headers_footers_and_page_numbers_are_removed`,
  `test_repeats_on_pages_that_are_not_consecutive_are_content`,
  `test_a_single_page_keeps_its_numbers`,
  `test_a_page_title_that_matches_a_running_footer_is_kept`.
- **Current data:** no stored document matches `| <year> Form 10-K | <n>`, and no 10-Q
  or 10-K contains a standalone page-number line.
- **Limits:** a header that appears on fewer than three pages stays in the text.
  Microsoft's "Item 3, 4" header, which sits on the single page that Items 3 and 4
  share, is one example; it falls outside every section.

### Whitespace and zero-width spaces
- **Filings:** filing HTML breaks sentences across source lines, separates inline spans
  with only a newline, and uses `&nbsp;`, thin and narrow no-break spaces. AMD's slide
  exhibits contain `&#8203;` (a zero-width space).
- **Rule:** any whitespace in or between text nodes becomes one space, never nothing. A
  zero-width space becomes a space, because it separates words. Joiners and soft hyphens
  are dropped, because they sit inside words. Words split by markup ("Com<span>pany")
  stay whole.
- **Where:** `html_text.py: _clean`, `_render_paragraph`.
- **Test:** `test_whitespace_between_text_nodes_is_one_space` (12 cases shaped like AAPL
  and MSFT filings).

### `<br>`
- **Rule:** a single `<br>` is a space inside the paragraph, so a sentence broken by a
  line break stays one sentence. Two `<br>` in a row end the paragraph.
- **Where:** `html_text.py: _Converter.line_break`.
- **Test:** `test_whitespace_between_text_nodes_is_one_space` (`<br>` cases).
- **Limits:** lists separated only by single `<br>` merge into one paragraph.

### Stacked headings, and sections that do not overlap
- **Filings:** a heading directly above another, such as "Consolidated Statements of
  Operations" above "(in millions, except per share data)". 689 stored sections have
  such joined titles.
- **Rule:** a heading with nothing under it joins the next one ("A / B"). The section
  starts at the first heading's own position, so sections never overlap. Headings at
  the very end of a document are kept as a final section.
- **Where:** `sections.py: _heading_sections`.
- **Tests:** `test_headings_with_nothing_below_join_the_next_section`,
  `test_headings_at_the_end_are_kept`.
- **Current data:** no two sections overlap, and every section's text equals its
  character range.

### "###" at the end of releases (Nvidia)
- **Filings:** press releases end with the journalists' "###". As text it would read as
  a markdown heading.
- **Rule:** a paragraph that starts with `#` or `|` but is not a heading or a table is
  escaped with a backslash (`\###`). 40 stored documents (Adobe, Salesforce, Nvidia)
  contain an escaped `\###`.
- **Where:** `html_text.py: _render_paragraph`.
- **Test:** `test_text_that_reads_as_markup_is_escaped`.

### Release headings and tags
- **Filings:**
  - Outlooks are titled "Outlook", "Business Outlook", "Guidance" or "Financial
    Targets".
  - Safe-harbor sections ("Forward-Looking Statements") are not outlooks; Salesforce's
    "Forward Looking Guidance" is one.
  - Headlines such as Salesforce's "Salesforce Reports Record First Quarter Fiscal 2026
    Results / Exceeds Guidance…" report results; 6 stored headline sections match both
    kinds of words.
  - Period subheadings ("Fourth Quarter Fiscal 2026") subdivide an outlook.
- **Rule:**
  - A heading with "reports/announces/delivers/posts" is `results`.
  - Otherwise outlook words make it `outlook`, unless it says forward-looking
    statements, safe harbor or cautionary.
  - Otherwise results words make it `results`, and anything else is `other`.
  - A period-only heading continues the outlook above it.
- **Where:** `sections.py: tag_heading`, `_heading_sections`.
- **Tests:** `test_press_release_heading_tags`, `test_period_headings_continue_an_outlook`.

### Tables tagged as results by content
- **Filings:** financial tables sit under headings that do not say "results". Examples:
  Cisco's "GROSS MARGIN PERCENTAGE BY SEGMENT", Amazon's "Segment Information",
  Microsoft's "MICROSOFT CORPORATION", and Intel's statements under "About Intel".
- **Rule:** a release section that would be `other` is `results` when it has at least 3
  table rows, table rows make up at least half of its lines, and at least 30% of its
  cells are figures. 313 stored release sections are `results` by this rule (Cisco 88,
  Alphabet 47, Amazon 44, …).
- **Where:** `sections.py: _mostly_figure_tables`.
- **Test:** `test_sections_of_figure_tables_are_results` (a contacts table stays
  `other`).
- **Current data:** 31,868 of the 33,740 table rows in releases are in `results`
  sections, 1,500 in `outlook` and 372 in `other`. 154 of 165 releases contain markdown
  tables. The 11 without are Tesla's earnings decks, which are slide images with a text
  layer and no HTML tables.
- **Limits:** text that shares a section with such tables ("About Intel") is tagged
  `results` along with them.

### Table cells
- **Filings:** SEC tables put "$", "(", ")" and "%" in their own cells and use colspans
  for headers. Rows that show "$" use two cells (`$` and the value), while the other rows
  use one spanning cell; Apple's income statement is an example.
- **Rule:**
  - Fragment cells are joined to their number.
  - A number in a spanning cell goes to the right end of the span.
  - Neighbouring value columns that never both hold a value are merged; the label
    column is never merged.
  - Layout tables (one row, one column, or bullet beside text) become paragraphs.
- **Where:** `html_text.py: _table_rows`, `_join_fragments`, `_is_layout`.
- **Tests:** `test_tables_become_markdown_with_numbers_joined`,
  `test_layout_tables_become_paragraphs`.

### Releases are dated after the quarter they report
- **Filings:** an 8-K's report date is its release date. Releases were filed 9 to 38
  days after their quarter ended (median 28), and none before it ended.
- **Rule:**
  - A release reports the most recent fiscal quarter whose nominal end precedes its
    filing date.
  - The quarter named first in the opening prose confirms that quarter, or moves it to
    the quarter before for a late release. Tables are skipped, since their headers name
    comparison quarters.
  - The period end is the date the release states, else the period of report of that
    quarter's 10-Q or 10-K, else the nominal end.
- **Current data:** 155 of 165 releases name the same quarter as the date gives, and
  none names a different one. The 10 that name no quarter are Netflix letters that say
  "Q2" without a year, so the date decides. 75 state their quarter's end date.
- **Where:** `periods.py: named_quarter`, `release_period`; `edgar/fiscal.py:
  quarter_ended_before`; `documents.py: _parse_release`.
- **Tests:** `test_periods.py` (all), and
  `test_fiscal.py::test_quarter_ended_before_a_release` (January, June and September
  year ends).

### Tesla's deliveries reports under item 2.02
- **Filings:** Tesla files its quarterly production report as an item 2.02 8-K, for
  example 0001628280-26-064366: "Tesla Third Quarter 2026 Production, Deliveries &
  Deployments".
- **Rule:** an item 2.02 release whose headline mentions production or deliveries is a
  `deliveries_report`. Its other exhibits become `other_exhibit`, and all their sections
  are `other`. 12 are stored, filed from 2024-01-02 to 2026-10-02.
- **Where:** `documents.py: is_deliveries_report`, `headline`.
- **Tests:** `test_filing_documents.py::test_deliveries_reports_are_not_earnings_releases`,
  `integration/test_filings_ingest.py::test_deliveries_reports_are_not_releases`.
- **Limits:** a real earnings release headlined about deliveries would be misread.

### Outlook sections without guidance (Microsoft)
- **Filings:** Microsoft's "Business Outlook" says only that "Microsoft will provide
  forward-looking guidance … on its earnings conference call and webcast."
- **Rule:** the section is kept as `outlook`. `has_guidance` is false when it has no
  body, or when it defers guidance to the call (or declines to give it) and states no
  figure. `coverage` reports such quarters as "no guidance". Of 199 stored outlook
  sections, exactly 11 have no guidance, all of them Microsoft's.
- **Where:** `sections.py: has_guidance`; `filings/coverage.py`.
- **Test:** `test_outlooks_that_give_guidance`.
- **Limits:** qualitative guidance without figures (Tesla's) counts as guidance, unless
  it also defers to the call.

### EX-99 exhibits unrelated to earnings
- **Filings:**
  - Adobe: a $25 billion buyback announcement (0000796343-24-000057, EX-99.2), and a
    leadership update with a contact sheet (0000796343-26-000048, EX-99.2/99.3).
  - Microsoft: "The next chapter of the Microsoft–OpenAI partnership" and an empty
    exhibit (0001193125-25-256310, EX-99.2/99.3).
- **Rule:** a non-release exhibit is an `earnings_exhibit` only when it uses
  financial-results terms at least 5 times and at least 2 per 1,000 characters; anything
  else is an `other_exhibit`.
  - The 34 earnings exhibits come from AMD (slides), Amazon (non-GAAP notes), Nvidia
    (CFO commentary) and Intel (recast financials).
  - The 5 other exhibits are the ones listed above.
- **Where:** `documents.py: discusses_results`.
- **Test:** `test_exhibits_unrelated_to_earnings_are_other_exhibits`.

### Several documents in one quarter
- **Filings:** Nvidia files a CFO commentary with every release, and it has its own
  outlook in all 11 quarters. Tesla's quarters have both a deliveries report and an
  earnings release (11 quarters).
- **Rule:** `show` prints every section of the requested type from the preferred
  document: press release, then earnings exhibit, 10-Q, 10-K, deliveries report, other
  exhibit; the latest filing first. It lists the other documents that have the section.
  `coverage` counts releases per 8-K. No quarter currently has two press releases.
- **Where:** `filings/coverage.py: find_sections`, `_DOC_ORDER`; `cli.py: _show`.
- **Test:** `integration/test_filings_ingest.py::test_cli_coverage_and_show`.

### Other conversions
- **Headings:** a short paragraph that is entirely bold, underlined (Intel's release
  headings) or large is a heading, if it is at most 120 characters and does not end like
  a sentence. Tests: `test_short_bold_paragraphs_become_headings`,
  `test_large_and_underlined_text_become_headings`.
- **Slide titles:** letter-spaced slide titles in Tesla's decks ("O U T L O O K")
  become headings. Test: `test_letter_spaced_slide_titles_become_headings`.

## 3. Document and section types

| doc_type | Meaning | Counted in coverage |
| --- | --- | --- |
| `press_release` | The earnings release of an item 2.02 8-K: the exhibit described as a press release, else the lowest-numbered EX-99 | Yes, as "press release" |
| `earnings_exhibit` | Another EX-99 on an earnings 8-K that discusses results | Its outlook counts toward the quarter's outlook |
| `10q`, `10k` | Primary document of a 10-Q or 10-K | Yes, for MD&A and risk factors |
| `deliveries_report` | An item 2.02 release about production or deliveries | No; listed as "not counted as releases" |
| `other_exhibit` | An EX-99 unrelated to earnings, or any exhibit of a deliveries report | No |

| section_type | Meaning |
| --- | --- |
| `outlook` | Guidance; "no guidance" in `coverage` when it only defers to the call |
| `results` | Headline, results, highlights, financial tables |
| `other` | Anything else in a release; every section of deliveries reports and other exhibits |
| `commentary` | Sections of earnings exhibits that are not outlooks |
| `mdna` | 10-K Item 7, 10-Q Part I Item 2 |
| `risk_factors` | 10-K Item 1A, 10-Q Part II Item 1A |

## 4. Current coverage

Reproduce with `uv run talk-is-cheap coverage` (all 382 documents at parser version 2):

| Section | Found | Of |
| --- | --- | --- |
| press release | 165/165 | earnings 8-Ks (100%) |
| outlook | 124/165 | quarters with a release (75.2%) |
| outlook (no guidance) | 11/165 | quarters with a release (6.7%) |
| MD&A | 166/166 | 10-Qs and 10-Ks (100%) |
| risk factors | 43/43 | 10-Ks (100%) |
| risk factors | 123/123 | 10-Qs (100%) |

12 deliveries reports are not counted as releases.

Known real gaps:
- Apple (11 quarters), Alphabet (11) and Oracle (8, all before 2026) give no guidance in
  their releases.
- Microsoft's 11 outlooks only point to the earnings call.
- Tesla FY2026Q3 has only a deliveries report so far.

## 5. Adding a company

1. Add the ticker to `universe.yaml`, then run `talk-is-cheap ingest edgar --tickers NEW`
   and `talk-is-cheap ingest filings --tickers NEW`.
2. Run `talk-is-cheap coverage --tickers NEW`. Every quarter should show a release, MD&A
   and risk factors; an outlook may legitimately be missing.
3. Spot-check with `show`:
   - `show NEW latest outlook` should hold the guidance.
   - `show NEW <quarter> risk_factors` should start at Item 1A and stop before the next
     item.
   - `show NEW <quarter> mdna | tail` should end in prose, with no page numbers or
     headers.
   - `show NEW <quarter> results | grep "|"` should print the financial tables.
4. If something is wrong, find the layout in the HTML and look for a general rule.
   Before adding one, check the rule against every company. Save a trimmed copy of the
   real filing under `tests/fixtures/` (SEC filings are public domain), with a comment
   naming its source URL, and add a test against it. Then raise `PARSER_VERSION` in
   `filings/ingest.py` so stored documents are re-parsed, and add the layout to the
   README's "Filing layouts handled" table and to this document.
