"""Choosing, dating and splitting a filing's documents."""

from datetime import date
from pathlib import Path

from talk_is_cheap.edgar.fiscal import FiscalCalendar, FiscalQuarter
from talk_is_cheap.edgar.records import FilingDocument, FilingRecord
from talk_is_cheap.filings.documents import DocType, is_parsed_form, parse_filing
from talk_is_cheap.filings.sections import SectionType

FIXTURES = Path(__file__).parent / "fixtures"
NVIDIA = FiscalCalendar.parse("0126")


def filing(
    form: str, filed: date, *, items: str | None = None, report: date | None = None
) -> FilingRecord:
    return FilingRecord(
        accession_no="0001045810-26-000073",
        cik=1045810,
        form=form,
        filing_date=filed,
        report_date=report,
        acceptance_datetime=None,
        primary_document="nvda-main.htm",
        items=items,
        is_xbrl=True,
    )


def document(
    name: str, document_type: str, html: str, description: str | None = None
) -> FilingDocument:
    return FilingDocument(
        accession_no="0001045810-26-000073",
        document_name=name,
        document_type=document_type,
        description=description,
        url=f"https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/{name}",
        html=html,
    )


RELEASE = (FIXTURES / "nvda_press_release.htm").read_text(encoding="utf-8")
COMMENTARY = (
    "<p><b>CFO Commentary</b></p><p>Revenue for the second quarter was $96.2 billion. GAAP "
    "gross margin was 75.0%; non-GAAP gross margin was 75.0%. Operating expenses rose.</p>"
    "<p><b>Outlook</b></p><p>Revenue next quarter is expected to be $108.0 billion.</p>"
)
UNRELATED = "<p><b>Leadership Update</b></p><p>Our chief executive will retire next year.</p>"
DELIVERIES = (
    "<p><b>Exhibit 99.1</b></p><p><b>Tesla Third Quarter 2026 Production, Deliveries &amp; "
    "Deployments</b></p><p>In the third quarter, we delivered over 486,000 vehicles.</p>"
)


def test_earnings_8k_documents_share_the_reported_quarter() -> None:
    earnings = filing("8-K", date(2026, 8, 26), items="2.02,9.01")
    parsed = parse_filing(
        earnings,
        [
            document("cfo.htm", "EX-99.2", COMMENTARY),
            document("q2fy27pr.htm", "EX-99.1", RELEASE),
            document("nvda-main.htm", "8-K", "<p>Item 2.02</p>"),
        ],
        NVIDIA,
        {},
    )
    release, commentary = parsed
    assert (release.document_name, release.doc_type, release.exhibit) == (
        "q2fy27pr.htm",
        DocType.PRESS_RELEASE,
        "EX-99.1",
    )
    assert (release.quarter, release.period_end) == (FiscalQuarter(2027, 2), date(2026, 7, 26))
    assert SectionType.OUTLOOK in {s.section_type for s in release.sections}
    assert release.url.endswith("/q2fy27pr.htm")
    assert len(release.content_hash) == 64

    assert commentary.doc_type is DocType.EARNINGS_EXHIBIT
    assert commentary.quarter == release.quarter
    assert [s.section_type for s in commentary.sections] == [
        SectionType.COMMENTARY,
        SectionType.OUTLOOK,
    ]


def test_release_described_as_press_release_is_preferred() -> None:
    parsed = parse_filing(
        filing("8-K", date(2026, 8, 26), items="2.02"),
        [
            document("slides.htm", "EX-99.1", COMMENTARY),
            document("release.htm", "EX-99.2", "<p>News</p>", "Q2 PRESS RELEASE"),
        ],
        NVIDIA,
        {},
    )
    assert [(d.document_name, d.doc_type) for d in parsed] == [
        ("slides.htm", DocType.EARNINGS_EXHIBIT),
        ("release.htm", DocType.PRESS_RELEASE),
    ]


def test_release_period_end_comes_from_the_quarters_report() -> None:
    (release,) = parse_filing(
        filing("8-K", date(2026, 8, 26), items="2.02"),
        [document("pr.htm", "EX-99.1", "<p>Results for Q2 FY27.</p>")],
        NVIDIA,
        {FiscalQuarter(2027, 2): date(2026, 7, 26)},
    )
    assert (release.quarter, release.period_end) == (FiscalQuarter(2027, 2), date(2026, 7, 26))

    (release,) = parse_filing(
        filing("8-K", date(2026, 8, 26), items="2.02"),
        [document("pr.htm", "EX-99.1", "<p>Results.</p>")],
        NVIDIA,
        {},
    )
    assert release.period_end == NVIDIA.quarter_end(FiscalQuarter(2027, 2))


def test_quarterly_and_annual_reports() -> None:
    html = (FIXTURES / "aapl_10k.htm").read_text(encoding="utf-8")
    (annual,) = parse_filing(
        filing("10-K", date(2026, 2, 25), report=date(2026, 1, 25)),
        [document("nvda-main.htm", "10-K", html), document("ex21.htm", "EX-99.1", "<p>x</p>")],
        NVIDIA,
        {},
    )
    assert (annual.doc_type, annual.exhibit) == (DocType.TEN_K, None)
    assert (annual.quarter, annual.period_end) == (FiscalQuarter(2026, 4), date(2026, 1, 25))
    assert [s.section_type for s in annual.sections] == [
        SectionType.RISK_FACTORS,
        SectionType.MDNA,
    ]

    (quarterly,) = parse_filing(
        filing("10-Q", date(2026, 8, 26), report=date(2026, 7, 26)),
        [document("nvda-main.htm", "10-Q", "<p>Item 2. MD&amp;A</p>")],
        NVIDIA,
        {},
    )
    assert (quarterly.doc_type, quarterly.quarter) == (DocType.TEN_Q, FiscalQuarter(2027, 2))


def test_which_filings_are_parsed() -> None:
    assert is_parsed_form(filing("10-Q", date(2026, 8, 26)))
    assert is_parsed_form(filing("10-K", date(2026, 2, 25)))
    assert is_parsed_form(filing("8-K", date(2026, 8, 26), items="2.02, 9.01"))
    assert not is_parsed_form(filing("8-K", date(2026, 8, 26), items="5.02"))
    assert not is_parsed_form(filing("10-K/A", date(2026, 2, 25)))
    assert not is_parsed_form(filing("8-K/A", date(2026, 8, 26), items="2.02"))
    assert parse_filing(filing("8-K", date(2026, 8, 26), items="7.01"), [], NVIDIA, {}) == []
    assert parse_filing(filing("8-K", date(2026, 8, 26), items="2.02"), [], NVIDIA, {}) == []


def test_exhibits_unrelated_to_earnings_are_other_exhibits() -> None:
    parsed = parse_filing(
        filing("8-K", date(2026, 8, 26), items="2.02"),
        [
            document("pr.htm", "EX-99.1", RELEASE),
            document("cfo.htm", "EX-99.2", COMMENTARY),
            document("ceo.htm", "EX-99.3", UNRELATED),
            document("blank.htm", "EX-99.4", ""),
        ],
        NVIDIA,
        {},
    )
    assert [(d.document_name, d.doc_type) for d in parsed] == [
        ("pr.htm", DocType.PRESS_RELEASE),
        ("cfo.htm", DocType.EARNINGS_EXHIBIT),
        ("ceo.htm", DocType.OTHER_EXHIBIT),
        ("blank.htm", DocType.OTHER_EXHIBIT),
    ]
    assert {s.section_type for s in parsed[2].sections} == {SectionType.OTHER}


def test_deliveries_reports_are_not_earnings_releases() -> None:
    tesla = FiscalCalendar.parse("1231")
    report, exhibit = parse_filing(
        filing("8-K", date(2026, 10, 2), items="2.02,9.01"),
        [document("pd.htm", "EX-99.1", DELIVERIES), document("x.htm", "EX-99.2", COMMENTARY)],
        tesla,
        {},
    )
    assert (report.doc_type, report.quarter) == (DocType.DELIVERIES_REPORT, FiscalQuarter(2026, 3))
    assert exhibit.doc_type is DocType.OTHER_EXHIBIT
    assert {s.section_type for d in (report, exhibit) for s in d.sections} == {SectionType.OTHER}
