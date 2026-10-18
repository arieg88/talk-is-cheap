"""Section finding and tagging, on synthetic text and trimmed real SEC filings."""

from pathlib import Path

import pytest

from talk_is_cheap.filings.html_text import ConvertedText, convert_html, html_to_text
from talk_is_cheap.filings.sections import (
    Section,
    SectionType,
    exhibit_sections,
    has_guidance,
    periodic_report_sections,
    press_release_sections,
    tag_heading,
    untagged_sections,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return html_to_text((FIXTURES / name).read_text(encoding="utf-8"))


def fixture(name: str) -> ConvertedText:
    return convert_html((FIXTURES / name).read_text(encoding="utf-8"))


def by_type(sections: list[Section]) -> dict[SectionType, list[Section]]:
    found: dict[SectionType, list[Section]] = {}
    for section in sections:
        found.setdefault(section.section_type, []).append(section)
    return found


def assert_offsets_cover_text(sections: list[Section], text: str) -> None:
    for section in sections:
        body = section.text(text)
        assert body == text[section.char_start : section.char_end]
        assert body == body.strip()
        first_title = section.title.split(" / ")[0]
        assert first_title in body.split("\n", 1)[0]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Outlook", SectionType.OUTLOOK),
        ("Business Outlook", SectionType.OUTLOOK),
        ("Financial Guidance", SectionType.OUTLOOK),
        ("Q4 FY 2026 Guidance:", SectionType.OUTLOOK),
        ("Financial Targets", SectionType.OUTLOOK),
        ("What We Expect", SectionType.OUTLOOK),
        ("Looking Forward", SectionType.OUTLOOK),
        ("Forward Looking Guidance", SectionType.OUTLOOK),
        ("Forward-Looking Statements", SectionType.OTHER),
        ("Forward Looking Statements, Non-GAAP Information", SectionType.OTHER),
        ("Cautionary Note Regarding Forward Looking Statements", SectionType.OTHER),
        ("Financial Highlights", SectionType.RESULTS),
        ("Q2 Fiscal 2027 Summary", SectionType.RESULTS),
        ("CONDENSED CONSOLIDATED STATEMENTS OF INCOME", SectionType.RESULTS),
        ("Adobe Reports Record Revenue; Raises Targets", SectionType.RESULTS),
        ("About NVIDIA", SectionType.OTHER),
        ("Conference Call and Webcast Information", SectionType.OTHER),
    ],
)
def test_press_release_heading_tags(title: str, expected: SectionType) -> None:
    assert tag_heading(title, default=SectionType.OTHER) is expected


def test_exhibit_headings_are_commentary_unless_outlook() -> None:
    assert tag_heading("Revenue", default=SectionType.COMMENTARY) is SectionType.COMMENTARY
    assert tag_heading("Outlook", default=SectionType.COMMENTARY) is SectionType.OUTLOOK


def test_real_press_release_sections() -> None:
    text = fixture_text("nvda_press_release.htm")
    sections = press_release_sections(text)
    found = by_type(sections)

    (outlook,) = found[SectionType.OUTLOOK]
    body = outlook.text(text)
    assert body.startswith("## Outlook\n\nNVIDIA\u2019s outlook for the third quarter")
    assert "Revenue is expected to be $108.0 billion, plus or minus 2%." in body
    assert "## Highlights" not in body
    assert [s.title for s in found[SectionType.RESULTS]][:2] == [
        "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027",
        "Q2 Fiscal 2027 Summary",
    ]
    assert "About NVIDIA" in [s.title for s in found[SectionType.OTHER]]
    assert sections == sorted(sections, key=lambda s: s.char_start)
    assert_offsets_cover_text(sections, text)


def test_headings_with_nothing_below_join_the_next_section() -> None:
    text = "## NVIDIA CORPORATION\n\n## CONDENSED STATEMENTS OF INCOME\n\n| a | b |"
    (section,) = press_release_sections(text)
    assert section.title == "NVIDIA CORPORATION / CONDENSED STATEMENTS OF INCOME"
    assert (section.section_type, section.char_start) == (SectionType.RESULTS, 0)


def test_period_headings_continue_an_outlook() -> None:
    text = (
        "## Business Outlook\n\nWe expect growth.\n\n"
        "## Fourth Quarter Fiscal 2026\n\nRevenue of $10 billion.\n\n"
        "## Full Year 2026\n\nRevenue of $40 billion.\n\n"
        "## About Us\n\nWe make chips."
    )
    outlook, about = press_release_sections(text)
    assert outlook.section_type is SectionType.OUTLOOK
    assert outlook.text(text).endswith("Revenue of $40 billion.")
    assert (about.section_type, about.title) == (SectionType.OTHER, "About Us")


def test_release_without_guidance_has_no_outlook() -> None:
    text = (
        "Apple reports third quarter results\n\n## Financial Results\n\nRevenue grew."
        "\n\n## Forward-Looking Statements\n\nThis release contains forward-looking statements."
    )
    sections = press_release_sections(text)
    assert [s.section_type for s in sections] == [
        SectionType.OTHER,
        SectionType.RESULTS,
        SectionType.OTHER,
    ]
    assert sections[0].title == ""


def test_exhibit_sections() -> None:
    text = "CFO commentary\n\n## Revenue\n\nUp 10%.\n\n## Outlook\n\nFlat next quarter."
    assert [(s.section_type, s.title) for s in exhibit_sections(text)] == [
        (SectionType.COMMENTARY, ""),
        (SectionType.COMMENTARY, "Revenue"),
        (SectionType.OUTLOOK, "Outlook"),
    ]
    assert exhibit_sections("Plain text without headings.")[0].char_end == 28


def test_annual_report_items_skip_the_table_of_contents() -> None:
    text = fixture_text("aapl_10k.htm")
    found = by_type(periodic_report_sections(text, "10-K"))

    (risk,) = found[SectionType.RISK_FACTORS]
    body = risk.text(text)
    assert body.startswith("## Item 1A. Risk Factors")
    assert "Item 1B" not in body and "Item 2" not in body
    assert text[risk.char_end :].lstrip().startswith("## Item 1B. Unresolved Staff Comments")
    assert text.index("| Item 1A. | Risk Factors") < risk.char_start  # the contents table

    (mdna,) = found[SectionType.MDNA]
    assert mdna.title.startswith("Item 7. Management\u2019s Discussion and Analysis")
    assert text[mdna.char_end :].lstrip().startswith("## Item 7A.")
    assert_offsets_cover_text([risk, mdna], text)


def test_quarterly_report_with_running_page_headers() -> None:
    text = fixture_text("msft_10q.htm")
    found = by_type(periodic_report_sections(text, "10-Q"))

    (mdna,) = found[SectionType.MDNA]
    assert mdna.title.startswith("ITEM 2. MANAGEMENT\u2019S DISCUSSION AND ANALYSIS")
    assert "Industry Trends and Opportunities" in mdna.text(text)  # past a page break
    assert text[mdna.char_end :].lstrip().startswith("Item 3, 4")

    (risk,) = found[SectionType.RISK_FACTORS]
    assert risk.title == "ITEM 1A. RISK FACTORS"
    assert "ITEM 2. UNREGISTERED SALES" not in risk.text(text)


def test_titled_sections_without_item_numbers() -> None:
    converted = fixture("intc_10q.htm")
    text = converted.text
    found = by_type(periodic_report_sections(text, "10-Q", converted.pages))

    (mdna,) = found[SectionType.MDNA]
    body = mdna.text(text)
    assert body.startswith("# Management's Discussion and Analysis")
    assert "# Operating Segments Trends and Results" in body  # a banner inside MD&A
    assert "Table of Contents" not in body and "\nMD&A\n" not in body  # page furniture
    assert text[mdna.char_end :].lstrip().startswith("# Risk Factors and Other Key Information")

    (risk,) = found[SectionType.RISK_FACTORS]
    assert risk.text(text).startswith("# Risk Factors and Other Key Information\n\n## Risk Factors")
    assert text[risk.char_end :].lstrip().startswith("## Quantitative and Qualitative")


def test_page_footers_extend_a_titled_section() -> None:
    def page(body: str, footer: str) -> str:
        return (
            f"<p><b>Table of Contents</b></p>{body}<p>{footer}</p>"
            '<hr style="page-break-after:always">'
        )

    html = (
        "<html><body>"
        + page('<p style="font-size:18pt">Our Business</p><p>We make chips.</p>', "Business 1")
        + page("<p>We sell chips.</p>", "Business 2")
        + page(
            '<p style="font-size:18pt">Management\'s Discussion and Analysis</p>'
            "<p>Revenue grew.</p>",
            "MD&A 3",
        )
        + page("<p><b>Properties</b></p><p>We own fabs.</p>", "MD&A 4")
        + page("<p><b>Critical Accounting Estimates</b></p><p>Judgments.</p>", "MD&A 5")
        + page('<p style="font-size:18pt">Risk Factors</p><p>Demand may fall.</p>', "Risk 6")
        + "</body></html>"
    )
    converted = convert_html(html)
    sections = by_type(periodic_report_sections(converted.text, "10-K", converted.pages))
    (mdna,) = sections[SectionType.MDNA]
    assert mdna.text(converted.text).endswith("## Critical Accounting Estimates\n\nJudgments.")


@pytest.mark.parametrize(
    ("outlook", "expected"),
    [
        (
            "## Business Outlook\n\nMicrosoft will provide forward-looking guidance in "
            "connection with this quarterly earnings announcement on its earnings conference "
            "call and webcast.",
            False,
        ),
        ("## Outlook\n\nThe company does not provide quarterly guidance.", False),
        ("## Outlook", False),
        ("## Outlook\n\n- Revenue is expected to be $108.0 billion, plus or minus 2%.", True),
        (
            "## OUTLOOK\n\nVolume We are focused on maximum capacity utilization at our factories.",
            True,
        ),
        (
            "## Guidance\n\nWe will discuss our outlook on the call. Revenue is expected to "
            "grow 10% to 12%.",
            True,
        ),
    ],
)
def test_outlooks_that_give_guidance(outlook: str, expected: bool) -> None:
    assert has_guidance(outlook) is expected


def test_sections_of_figure_tables_are_results() -> None:
    text = (
        "## About Us\n\nWe make chips.\n\n## MICROSOFT CORPORATION\n\n"
        "| (In millions) | 2026 | 2025 |\n| --- | --- | --- |\n| Revenue | $77,673 | $65,585 |\n"
        "| Net income | 27,747 | 24,108 |\n| Diluted EPS | $3.72 | $3.23 |\n\n"
        "## Contacts\n\n| Investor Relations | Media |\n| --- | --- |\n| Jane Doe | Joe Roe |\n"
        "| jane@example.com | joe@example.com |\n| Redmond | Seattle |"
    )
    assert [(s.title, s.section_type) for s in press_release_sections(text)] == [
        ("About Us", SectionType.OTHER),
        ("MICROSOFT CORPORATION", SectionType.RESULTS),
        ("Contacts", SectionType.OTHER),
    ]


def test_untagged_documents_have_only_other_sections() -> None:
    text = "## Tesla Q3 Production, Deliveries & Deployments\n\nWe delivered.\n\n## Outlook\n\nUp."
    assert {s.section_type for s in untagged_sections(text)} == {SectionType.OTHER}


def test_headings_at_the_end_are_kept() -> None:
    (section,) = press_release_sections("## Contacts\n\n## Investor Relations")
    assert section.title == "Contacts / Investor Relations"


@pytest.mark.parametrize(
    "heading",
    [
        "## Item 1A. Risk Factors",
        "## ITEM 1A. RISK FACTORS",
        "## Item 1A — Risk Factors",
        "**Item 1A:** Risk Factors",
        "## Item 1A.Risk Factors",
        "ITEM 1A",
        "## PART II. Item 1A. Risk Factors",
    ],
)
def test_item_headings_are_matched_tolerantly(heading: str) -> None:
    text = (
        "## PART II. OTHER INFORMATION\n\n## Item 1. Legal Proceedings\n\nNone.\n\n"
        f"{heading}\n\nOur risks are described in our Form 10-K.\n\n"
        "## Item 2. Unregistered Sales of Equity Securities\n\nNone."
    )
    (risk,) = periodic_report_sections(text, "10-Q")
    assert risk.section_type is SectionType.RISK_FACTORS
    assert risk.text(text).startswith(heading)
    assert risk.text(text).endswith("described in our Form 10-K.")


def test_quarterly_part_ii_item_2_is_not_mdna() -> None:
    text = (
        "## PART I\n\n## Item 1. Financial Statements\n\nTables.\n\n"
        "## Item 2. Management's Discussion and Analysis\n\nRevenue grew.\n\n"
        "## Item 3. Quantitative and Qualitative Disclosures\n\nRates.\n\n"
        "## PART II\n\n## Item 1A. Risk Factors\n\nNo changes.\n\n"
        "## Item 2. Unregistered Sales of Equity Securities and Use of Proceeds\n\n"
        + "Repurchases. "
        * 50
    )
    sections = periodic_report_sections(text, "10-Q")
    assert [(s.section_type, s.text(text).split("\n\n")[1]) for s in sections] == [
        (SectionType.MDNA, "Revenue grew."),
        (SectionType.RISK_FACTORS, "No changes."),
    ]


def test_missing_sections_are_absent() -> None:
    assert periodic_report_sections("## Item 1. Business\n\nWe sell things.", "10-K") == []
