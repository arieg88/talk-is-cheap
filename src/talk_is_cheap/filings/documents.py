"""Turn a filing's HTML documents into parsed documents: text, fiscal period and sections.

Which documents are kept:

- Earnings 8-K (item 2.02): every EX-99 exhibit. The press release is the exhibit
  described as a press release, else the lowest-numbered one (EX-99.1). The others are
  `earnings_exhibit`s (CFO commentary, prepared remarks, slides, supplemental tables)
  when they discuss financial results, and `other_exhibit`s when they do not (an
  announcement that happens to share the 8-K): earnings exhibits use financial-results
  terms (revenue, net income, GAAP, quarter, ...) at least twice per 1,000 characters,
  unrelated ones hardly at all.
- An item 2.02 release whose headline is about production or deliveries (Tesla's
  quarterly "Production, Deliveries & Deployments") is a `deliveries_report`, not an
  earnings release; its exhibits are kept with it.
- 10-Q and 10-K: the primary document.

A 10-Q or 10-K belongs to the fiscal quarter its period of report ends in, so a 10-K is
the year's fourth quarter. An earnings release belongs to the quarter it reports
(`periods.release_period`), and its exhibits share that quarter. Its period end is the
date the release states, else the period of report of that quarter's 10-Q or 10-K, else
the quarter's nominal end.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.edgar.records import FilingDocument, FilingRecord
from talk_is_cheap.filings.html_text import content_hash, convert_html, html_to_text
from talk_is_cheap.filings.periods import release_period
from talk_is_cheap.filings.sections import (
    Section,
    exhibit_sections,
    periodic_report_sections,
    press_release_sections,
    untagged_sections,
)

PERIODIC_FORMS = frozenset({"10-Q", "10-K"})
EARNINGS_ITEM = "2.02"


class DocType(StrEnum):
    """Document types, in the order `show` prefers them."""

    PRESS_RELEASE = "press_release"
    EARNINGS_EXHIBIT = "earnings_exhibit"
    TEN_Q = "10q"
    TEN_K = "10k"
    DELIVERIES_REPORT = "deliveries_report"
    OTHER_EXHIBIT = "other_exhibit"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    document_name: str
    doc_type: DocType
    exhibit: str | None
    url: str
    quarter: FiscalQuarter
    period_end: date
    text: str
    sections: list[Section]

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


def is_earnings_release(filing: FilingRecord) -> bool:
    items = {item.strip() for item in (filing.items or "").split(",")}
    return filing.form == "8-K" and EARNINGS_ITEM in items


def is_parsed_form(filing: FilingRecord) -> bool:
    """10-Qs, 10-Ks and earnings 8-Ks; amendments are not parsed."""
    return filing.form in PERIODIC_FORMS or is_earnings_release(filing)


def parse_filing(
    filing: FilingRecord,
    documents: Sequence[FilingDocument],
    fiscal_calendar: FiscalCalendar,
    report_dates: Mapping[FiscalQuarter, date],
) -> list[ParsedDocument]:
    """Parse the documents of one filing. `report_dates` maps the company's fiscal quarters
    to the period of report of their 10-Q or 10-K."""
    if filing.form in PERIODIC_FORMS:
        return _parse_periodic(filing, documents, fiscal_calendar)
    if is_earnings_release(filing):
        return _parse_release(filing, documents, fiscal_calendar, report_dates)
    return []


def _parse_periodic(
    filing: FilingRecord, documents: Sequence[FilingDocument], fiscal_calendar: FiscalCalendar
) -> list[ParsedDocument]:
    primary = next((d for d in documents if not d.document_type.startswith("EX-")), None)
    if primary is None:
        return []
    period_end = filing.report_date
    if period_end is None:
        quarter = fiscal_calendar.quarter_ended_before(filing.filing_date)
        period_end = fiscal_calendar.quarter_end(quarter)
    converted = convert_html(primary.html)
    text = converted.text
    return [
        ParsedDocument(
            document_name=primary.document_name,
            doc_type=DocType.TEN_K if filing.form == "10-K" else DocType.TEN_Q,
            exhibit=None,
            url=primary.url,
            quarter=fiscal_calendar.quarter_of(period_end),
            period_end=period_end,
            text=text,
            sections=periodic_report_sections(text, filing.form, converted.pages),
        )
    ]


_EXHIBIT_NUMBER = re.compile(r"EX-99(?:\.(\d+))?", re.IGNORECASE)
_DELIVERIES_HEADLINE = re.compile(r"\b(?:production|deliveries)\b", re.IGNORECASE)
_EXHIBIT_LABEL = re.compile(r"exhibit\s+99(?:\.\d+)?", re.IGNORECASE)
_FINANCIAL_TERMS = re.compile(
    r"\b(?:revenues?|net income|net loss|earnings per share|EPS|gross margin"
    r"|operating (?:income|margin|expenses)|non-GAAP|GAAP|cash flows?|quarter)\b",
    re.IGNORECASE,
)
EARNINGS_TERMS_PER_1000_CHARS = 2.0
_MIN_EARNINGS_TERMS = 5


def headline(text: str) -> str:
    """The first heading of a document that is not just its exhibit label."""
    for line in text.split("\n"):
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title and not _EXHIBIT_LABEL.fullmatch(title):
                return title
    return ""


def is_deliveries_report(release_text: str) -> bool:
    return bool(_DELIVERIES_HEADLINE.search(headline(release_text)))


def discusses_results(text: str) -> bool:
    """Whether an exhibit discusses financial results, by how densely it uses their terms."""
    terms = len(_FINANCIAL_TERMS.findall(text))
    return terms >= _MIN_EARNINGS_TERMS and terms * 1000 >= EARNINGS_TERMS_PER_1000_CHARS * len(
        text
    )


def _exhibit_order(document: FilingDocument) -> tuple[int, str]:
    match = _EXHIBIT_NUMBER.match(document.document_type)
    number = int(match.group(1)) if match and match.group(1) else 0
    return number, document.document_name


def _parse_release(
    filing: FilingRecord,
    documents: Sequence[FilingDocument],
    fiscal_calendar: FiscalCalendar,
    report_dates: Mapping[FiscalQuarter, date],
) -> list[ParsedDocument]:
    exhibits = sorted(
        (d for d in documents if d.document_type.startswith("EX-99")), key=_exhibit_order
    )
    if not exhibits:
        return []
    release = next(
        (d for d in exhibits if "press release" in (d.description or "").lower()), exhibits[0]
    )
    texts = {d.document_name: html_to_text(d.html) for d in exhibits}
    quarter, stated_end = release_period(
        texts[release.document_name], filing.filing_date, fiscal_calendar
    )
    period_end = stated_end or report_dates.get(quarter) or fiscal_calendar.quarter_end(quarter)

    deliveries = is_deliveries_report(texts[release.document_name])
    parsed = []
    for document in exhibits:
        text = texts[document.document_name]
        if deliveries:
            doc_type = DocType.DELIVERIES_REPORT if document is release else DocType.OTHER_EXHIBIT
        elif document is release:
            doc_type = DocType.PRESS_RELEASE
        elif discusses_results(text):
            doc_type = DocType.EARNINGS_EXHIBIT
        else:
            doc_type = DocType.OTHER_EXHIBIT
        parsed.append(
            ParsedDocument(
                document_name=document.document_name,
                doc_type=doc_type,
                exhibit=document.document_type,
                url=document.url,
                quarter=quarter,
                period_end=period_end,
                text=text,
                sections=_SECTIONS[doc_type](text),
            )
        )
    return parsed


_SECTIONS = {
    DocType.PRESS_RELEASE: press_release_sections,
    DocType.EARNINGS_EXHIBIT: exhibit_sections,
    DocType.DELIVERIES_REPORT: untagged_sections,
    DocType.OTHER_EXHIBIT: untagged_sections,
}
