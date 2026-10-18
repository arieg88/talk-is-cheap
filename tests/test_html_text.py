"""HTML to structured text, on small synthetic snippets and a trimmed real press release."""

from pathlib import Path

import pytest

from talk_is_cheap.filings.html_text import content_hash, convert_html, html_to_text

FIXTURES = Path(__file__).parent / "fixtures"


def convert(body: str) -> str:
    return html_to_text(f"<html><body>{body}</body></html>")


def test_heading_tags_become_hash_lines() -> None:
    assert convert("<h1>Annual Report</h1><h3>Overview</h3><p>Text.</p>") == (
        "# Annual Report\n\n### Overview\n\nText."
    )


def test_short_bold_paragraphs_become_headings() -> None:
    text = convert(
        '<div><span style="font-weight:700">Outlook</span></div>'
        '<div><span style="font-weight:400">Revenue is expected to grow.</span></div>'
    )
    assert text == "## Outlook\n\nRevenue is expected to grow."


def test_large_and_underlined_text_become_headings() -> None:
    text = convert(
        '<div><span style="font-size:18pt">Risk Factors</span></div>'
        '<div><span style="font-size:14pt">Overview</span></div>'
        '<div><span style="text-decoration:underline">Business Outlook</span></div>'
        "<p><u>Segment Results</u></p>"
    )
    assert text == "# Risk Factors\n\n## Overview\n\n## Business Outlook\n\n## Segment Results"


def test_bold_sentences_and_inline_bold_stay_bold() -> None:
    text = convert(
        "<p><b>Competition could adversely impact our market share.</b></p>"
        "<p>Revenue was <b>$5.0 billion</b>, up <strong>12%</strong>.</p>"
    )
    assert text == (
        "**Competition could adversely impact our market share.**\n\n"
        "Revenue was **$5.0 billion**, up **12%**."
    )


def test_tables_become_markdown_with_numbers_joined() -> None:
    text = convert(
        "<table>"
        '<tr><td></td><td colspan="3">Q2 FY27</td><td></td><td colspan="2">Q2 FY26</td></tr>'
        "<tr><td>Revenue</td><td>$</td><td>96,221</td><td></td><td></td><td>$</td>"
        "<td>46,743</td></tr>"
        "<tr><td>Gross margin</td><td></td><td>75.0</td><td>%</td><td></td><td></td>"
        "<td>72.4%</td></tr>"
        "<tr><td>Net income (loss)</td><td></td><td>(1|2</td><td>)</td><td></td><td></td>"
        "<td>5</td></tr>"
        "</table>"
    )
    assert text.splitlines() == [
        "|  | Q2 FY27 | Q2 FY26 |",
        "| --- | --- | --- |",
        "| Revenue | $96,221 | $46,743 |",
        "| Gross margin | 75.0% | 72.4% |",
        "| Net income (loss) | (1\\|2) | 5 |",
    ]


def test_layout_tables_become_paragraphs() -> None:
    text = convert(
        "<table><tr><td><b>Item 1A.</b></td><td><b>Risk Factors</b></td></tr></table>"
        "<table><tr><td>&#8226;</td><td>First point</td></tr>"
        "<tr><td>&#8226;</td><td>Second point</td></tr></table>"
        '<table><tr><td><span style="font-size:18pt">MD&amp;A</span></td></tr></table>'
    )
    assert text == "## Item 1A. Risk Factors\n\n- First point\n\n- Second point\n\n# MD&A"


def test_hidden_elements_scripts_and_xbrl_header_are_removed() -> None:
    text = html_to_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        "<html><head><title>Doc</title><style>p {color: red}</style></head><body>"
        '<div style="display:none"><ix:header><ix:hidden>dei:Hidden</ix:hidden></ix:header></div>'
        "<script>alert(1)</script><!-- a comment -->"
        "<p hidden>not shown</p>"
        '<p>Revenue of <ix:nonFraction name="us-gaap:Revenues">5</ix:nonFraction>\n'
        "billion&#160;&#160; dol\u00adlars\u2060.</p>"
        "</body></html>"
    )
    assert text == "Revenue of 5 billion dollars."


def test_bullets_are_list_items() -> None:
    text = convert("<div>&#8226; Revenue up 10%</div><p>&#9679;<b>Bold</b> point</p>")
    assert text == "- Revenue up 10%\n\n- **Bold** point"


def test_letter_spaced_slide_titles_become_headings() -> None:
    text = convert("<div>O U T L O O K Volume We are focused on capacity.</div>")
    assert text == "## OUTLOOK\n\nVolume We are focused on capacity."
    assert convert("<div>P H O T O S &amp; C H A R T S 12</div>") == "## PHOTOS&CHARTS"


def test_conversion_is_deterministic() -> None:
    html = (FIXTURES / "nvda_press_release.htm").read_text(encoding="utf-8")
    first, second = html_to_text(html), html_to_text(html.encode("utf-8"))
    assert first == second
    assert content_hash(first) == content_hash(second)
    assert len(content_hash(first)) == 64
    assert html_to_text("   ") == ""


def test_real_press_release_keeps_structure() -> None:
    text = html_to_text((FIXTURES / "nvda_press_release.htm").read_text(encoding="utf-8"))
    assert text.startswith("## NVIDIA Announces Financial Results for Second Quarter Fiscal 2027")
    assert "\n## Outlook\n" in text
    assert "- Revenue is expected to be $108.0 billion, plus or minus 2%." in text
    assert "| Revenue | $96,221 | $81,615 | $46,743 | 18% | 106% |" in text
    assert "**SANTA CLARA, Calif." in text


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Text nodes broken across source lines, as Workiva and Donnelley write them.
        ("<p>harm the\nCompany\u2019s business</p>", "harm the Company\u2019s business"),
        ("<p>in whole or in part\r\nby factors</p>", "in whole or in part by factors"),
        # Inline elements separated only by a line break or indentation.
        ("<p><span>could result</span>\n<span>in losses</span></p>", "could result in losses"),
        ("<p><span>more</span>\n    <span>severe</span></p>", "more severe"),
        ('<p><font style="x">part of</font>\t<font>the</font> year</p>', "part of the year"),
        # Spaces that are not ASCII spaces.
        ("<p>cost&#160;of&#160;revenue</p>", "cost of revenue"),
        ("<p>such\u202fas\u2009these\u3000items</p>", "such as these items"),
        ("<p>such&#8203;as</p>", "such as"),
        # A line break inside a sentence is a space; two end the paragraph.
        ("<p>the Company<br>expects</p>", "the Company expects"),
        ("<p>first<br/>\n<br/>second</p>", "first\n\nsecond"),
        # Bold and plain runs keep the space between them.
        ("<p>see <b>Item 1A</b>\nof this report</p>", "see **Item 1A** of this report"),
        # Words split by markup stay whole.
        ("<p>Com<span>pany</span> and $<span>5</span></p>", "Company and $5"),
    ],
)
def test_whitespace_between_text_nodes_is_one_space(body: str, expected: str) -> None:
    assert convert(body) == expected


def test_text_that_reads_as_markup_is_escaped() -> None:
    assert convert("<p>About us.</p><p>###</p><p>| not a table</p>") == (
        "About us.\n\n\\###\n\n\\| not a table"
    )


PAGE_BREAK = '<hr style="page-break-after:always">'


def page(number: int, body: str, header: str = "") -> str:
    return f"{header}{body}<p>Acme Inc. | 2025 Form 10-K | {number}</p><p>{number}</p>{PAGE_BREAK}"


def test_running_headers_footers_and_page_numbers_are_removed() -> None:
    header = "<p><b>PART I</b></p><p>Item 1A</p>"
    html = (
        "<html><body>"
        + page(1, "<p><b>Item 1A. Risk Factors</b></p><p>Demand may fall.</p>", header)
        + page(2, "<p>Supply may fail.</p><p>None.</p>", header)
        + page(3, "<p>Prices may drop.</p>", header)
        + page(4, "<p><b>Item 1B. Unresolved Staff Comments</b></p><p>None.</p>")
        + "</body></html>"
    )
    converted = convert_html(html)
    assert converted.text == (
        "## Item 1A. Risk Factors\n\nDemand may fall.\n\nSupply may fail.\n\nNone."
        "\n\nPrices may drop.\n\n## Item 1B. Unresolved Staff Comments\n\nNone."
    )

    first, second, _, last = converted.pages
    assert first.furniture == ("PART I", "Item 1A", "Acme Inc. | 2025 Form 10-K | 1", "1")
    assert converted.text[first.start : first.end] == "## Item 1A. Risk Factors\n\nDemand may fall."
    assert converted.text[second.start : second.end] == "Supply may fail.\n\nNone."
    assert last.furniture == ("Acme Inc. | 2025 Form 10-K | 4", "4")


def test_repeats_on_pages_that_are_not_consecutive_are_content() -> None:
    pages = [
        "<p>Item 3. Legal Proceedings</p><p>None.</p>",
        "<p>Discussion continues at length.</p>",
        "<p>Item 4. Mine Safety Disclosures</p><p>None.</p>",
        "<p>More discussion.</p>",
        "<p>Item 9. Changes in Accountants</p><p>None.</p>",
    ]
    html = "<html><body>" + PAGE_BREAK.join(pages) + "</body></html>"
    assert convert_html(html).text.count("None.") == 3


def test_a_single_page_keeps_its_numbers() -> None:
    assert convert("<p>Fiscal year</p><p>2026</p>") == "Fiscal year\n\n2026"
    assert convert_html("<p>One page</p>").pages[0].furniture == ()


def test_a_page_title_that_matches_a_running_footer_is_kept() -> None:
    pages = [
        "<p>We make chips.</p><p>We sell chips.</p><p>Our Business</p><p>5</p>",
        '<p style="font-size:18pt">Risk Factors</p><p>Demand may fall.</p>'
        "<p>Rates may rise.</p><p>Risk Factors</p><p>6</p>",
        "<p>Supply may fail.</p><p>Costs may grow.</p><p>Risk Factors</p><p>7</p>",
        "<p>Prices may drop.</p><p>Rivals may win.</p><p>Risk Factors</p><p>8</p>",
    ]
    html = "<html><body>" + PAGE_BREAK.join(pages) + "</body></html>"
    text = convert_html(html).text
    assert "We sell chips.\n\nOur Business\n\n# Risk Factors\n\nDemand may fall." in text
    assert text.count("Risk Factors") == 1
