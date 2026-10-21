"""Statements in the news, without a database or a model: speakers matched to the
roster, the candidate filter, post-processing of the model's claims, directional
outcomes and the benchmark's scoring."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.guidance.periods import FiscalPeriod
from talk_is_cheap.statements.candidates import passes_filter
from talk_is_cheap.statements.evaluate import LabeledClaim, LabeledStory, score_story
from talk_is_cheap.statements.items import SourceText, process
from talk_is_cheap.statements.outcomes import observed_direction, previous_period
from talk_is_cheap.statements.roster import match_executive, normalize_name
from talk_is_cheap.statements.schema import ExtractedClaim, StatementExtraction
from talk_is_cheap.universe import Executive, UniverseCompany

NVIDIA = UniverseCompany(
    ticker="NVDA",
    name="Nvidia",
    executives=[
        Executive(name="Jen-Hsun Huang", role="Chief Executive Officer", aliases=["Jensen Huang"]),
        Executive(name="Colette Kress", role="Chief Financial Officer"),
    ],
)
INTEL = UniverseCompany(
    ticker="INTC",
    name="Intel",
    executives=[
        Executive(name="Pat Gelsinger", role="Chief Executive Officer", end=date(2024, 12, 1)),
        Executive(name="David Zinsner", role="Chief Financial Officer", aliases=["Dave Zinsner"]),
        Executive(
            name="David Zinsner",
            role="Interim Co-Chief Executive Officer",
            aliases=["Dave Zinsner"],
            start=date(2024, 12, 1),
            end=date(2025, 3, 17),
        ),
        Executive(
            name="Michelle Johnston Holthaus",
            role="Interim Co-Chief Executive Officer",
            start=date(2024, 12, 1),
            end=date(2025, 3, 17),
        ),
        Executive(name="Lip-Bu Tan", role="Chief Executive Officer", start=date(2025, 3, 18)),
    ],
)
# Nvidia's fiscal year ends in late January.
NVDA_CALENDAR = FiscalCalendar.parse("0126")


def test_names_are_compared_without_titles_initials_and_punctuation() -> None:
    assert normalize_name("Dr. Lisa T. Su") == "lisa su"
    assert normalize_name("Colette M. Kress") == "colette kress"
    assert normalize_name("Jen-Hsun Huang") == "jen hsun huang"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Jensen Huang", "Jen-Hsun Huang"),  # an alias
        ("Jen-Hsun Huang", "Jen-Hsun Huang"),
        ("Colette M. Kress", "Colette Kress"),
        ("Kress", "Colette Kress"),  # a surname, unique on the roster
        ("Ian Buck", None),  # an Nvidia vice president, not on the roster
        ("Tim Cook", None),
        (None, None),
    ],
)
def test_speakers_match_the_roster(name: str | None, expected: str | None) -> None:
    found = match_executive(name, None, NVIDIA, date(2025, 5, 28))
    assert (found.name if found else None) == expected


def test_a_speaker_named_only_by_a_bare_title() -> None:
    day = date(2025, 5, 28)
    cfo = match_executive(None, "CFO", NVIDIA, day)
    assert cfo is not None and cfo.name == "Colette Kress"
    assert match_executive(None, "Nvidia's chief executive", NVIDIA, day) is not None
    assert match_executive(None, "Arm CEO", NVIDIA, day) is None
    assert match_executive(None, "a vice president", NVIDIA, day) is None
    # Two chief executives that day (interim co-CEOs): no single match.
    assert match_executive(None, "CEO", INTEL, date(2024, 12, 15)) is None
    tan = match_executive(None, "CEO", INTEL, date(2025, 4, 1))
    assert tan is not None and tan.name == "Lip-Bu Tan"


def test_a_speaker_counts_only_while_in_the_role() -> None:
    before = match_executive("Pat Gelsinger", "CEO", INTEL, date(2024, 10, 31))
    after = match_executive("Pat Gelsinger", "former CEO", INTEL, date(2025, 1, 30))
    assert before is not None and before.role == "Chief Executive Officer"
    assert after is None
    assert match_executive("Lip-Bu Tan", "CEO", INTEL, date(2025, 3, 1)) is None


def test_one_person_in_two_roles_gets_the_closer_one() -> None:
    day = date(2025, 1, 30)
    as_ceo = match_executive("David Zinsner", "interim co-CEO", INTEL, day)
    as_cfo = match_executive("Dave Zinsner", "chief financial officer", INTEL, day)
    assert as_ceo is not None and as_ceo.role == "Interim Co-Chief Executive Officer"
    assert as_cfo is not None and as_cfo.role == "Chief Financial Officer"


def test_the_filter_needs_a_speaker_an_attribution_and_a_forward_marker_together() -> None:
    quoted = "Nvidia sold more chips.\n\n“We expect demand to exceed supply,” Colette Kress said."
    company = "Nvidia said it plans to double capacity next year."
    no_forward = "“Demand was strong,” Jensen Huang said."
    no_speaker = "Analysts said Nvidia will grow revenue next year."
    apart = "Jensen Huang spoke at the conference.\n\nA.\n\nB.\n\nAnalysts said sales will grow."
    assert passes_filter(quoted, NVIDIA)
    assert passes_filter(company, NVIDIA)
    assert not passes_filter(no_forward, NVIDIA)
    assert not passes_filter(no_speaker, NVIDIA)
    assert not passes_filter(apart, NVIDIA)


ARTICLE = (
    "Nvidia expects revenue of about $28 billion in the current quarter, the company said. "
    "“Demand will exceed supply well into next year,” Jensen Huang said on Wednesday. "
    "Analysts at Bank of America expect revenue of $30 billion. "
    "Ian Buck, a vice president, said the company will ship Blackwell later this year."
)


def claim(**values: Any) -> ExtractedClaim:
    base: dict[str, Any] = {
        "attribution": "company_statement",
        "is_direct_quote": False,
        "venue": "unknown",
        "claim_type": "qualitative",
        "claim": "a claim",
    }
    return ExtractedClaim(**{**base, **values})


def test_claims_are_grounded_attributed_and_resolved() -> None:
    answer = StatementExtraction(
        claims=[
            claim(
                quote="Nvidia expects revenue of about $28 billion in the current quarter",
                claim_type="numeric_forecast",
                metric="revenue",
                period_text="the current quarter",
                value_text="about $28 billion",
            ),
            claim(
                quote="“Demand will exceed supply well into next year,” Jensen Huang said",
                attribution="named_executive",
                speaker_name="Jensen Huang",
                speaker_role="CEO",
                is_direct_quote=True,
                statement_date="2024-05-22",
            ),
            claim(
                quote="Analysts at Bank of America expect revenue of $30 billion.",
                attribution="analyst",
                claim_type="numeric_forecast",
                metric="revenue",
                value_text="$30 billion",
            ),
            claim(
                quote="Ian Buck, a vice president, said the company will ship Blackwell",
                attribution="named_executive",
                speaker_name="Ian Buck",
                claim_type="plan",
            ),
            claim(quote="Nvidia will triple revenue, the CEO said.", claim_type="directional"),
        ]
    )
    source = SourceText(1, ARTICLE, date(2024, 5, 23))
    claims, dropped = process(source, answer, NVIDIA, NVDA_CALENDAR)

    assert [d.reason for d in dropped] == ["quote not found in the article text"]
    company, huang, analyst, buck = claims
    assert company.management and company.attribution == "company_statement"
    # The last quarter ended before May 23, 2024 is fiscal 2025 Q1; the current is Q2.
    assert company.target == FiscalPeriod(2025, 2)
    assert company.point == Decimal("28000000000") and company.unit == "usd"
    assert company.date_from_article and company.statement_date == date(2024, 5, 23)
    assert huang.management and huang.executive_name == "Jen-Hsun Huang"
    assert huang.statement_date == date(2024, 5, 22) and not huang.date_from_article
    assert not analyst.management
    assert buck.attribution == "unverified_executive" and not buck.management
    assert ARTICLE[company.char_start : company.char_end] == company.quote


def test_a_forecast_without_a_readable_figure_is_not_numeric() -> None:
    answer = StatementExtraction(
        claims=[
            claim(
                quote="Nvidia expects revenue of about $28 billion",
                claim_type="numeric_forecast",
                metric="revenue",
                value_text="strong double-digit growth",
                direction="increase",
            ),
            claim(
                quote="the company said",
                claim_type="numeric_forecast",
                metric="revenue",
                value_text="a lot",
            ),
        ]
    )
    claims, _ = process(SourceText(1, ARTICLE, date(2024, 5, 23)), answer, NVIDIA, NVDA_CALENDAR)
    assert [c.claim_type for c in claims] == ["directional", "qualitative"]
    assert all(c.low is None and c.point is None for c in claims)


def test_a_statement_date_after_the_article_or_far_before_it_is_not_used() -> None:
    answer = StatementExtraction(
        claims=[
            claim(quote="Nvidia expects revenue", statement_date="2024-06-30"),
            claim(quote="the company said", statement_date="2021-01-01"),
        ]
    )
    claims, _ = process(SourceText(1, ARTICLE, date(2024, 5, 23)), answer, NVIDIA, NVDA_CALENDAR)
    assert all(c.statement_date == date(2024, 5, 23) and c.date_from_article for c in claims)


def test_claim_keys_survive_re_extraction() -> None:
    answer = StatementExtraction(claims=[claim(quote="Nvidia expects revenue of about")])
    source = SourceText(1, ARTICLE, date(2024, 5, 23))
    first, _ = process(source, answer, NVIDIA, NVDA_CALENDAR)
    again, _ = process(source, answer, NVIDIA, NVDA_CALENDAR)
    assert first[0].key() == again[0].key()
    assert type(first[0]).from_json(first[0].to_json()) == first[0]


def test_directions_use_the_point_tolerance() -> None:
    assert observed_direction(Decimal(100), Decimal(102), "usd") == "increase"
    assert observed_direction(Decimal(100), Decimal("100.5"), "usd") == "flat"
    assert observed_direction(Decimal(50), Decimal("49.6"), "pct") == "flat"
    assert observed_direction(Decimal(50), Decimal("49.4"), "pct") == "decrease"
    assert previous_period((2026, 1)) == (2025, 4)
    assert previous_period((2026, None)) == (2025, None)


def test_scoring_counts_leaks_misses_and_fields() -> None:
    answer = StatementExtraction(
        claims=[
            claim(
                quote="Nvidia expects revenue of about $28 billion in the current quarter",
                claim_type="numeric_forecast",
                metric="revenue",
                period_text="the current quarter",
                value_text="about $28 billion",
            ),
            # The analyst's figure, wrongly read as the company's.
            claim(quote="Analysts at Bank of America expect revenue of $30 billion."),
        ]
    )
    claims, _ = process(SourceText(1, ARTICLE, date(2024, 5, 23)), answer, NVIDIA, NVDA_CALENDAR)
    labels = [
        LabeledClaim(
            quote="Nvidia expects revenue of about $28 billion",
            management=True,
            attribution="company_statement",
            metric="revenue",
            target="FY2025Q2",
            point=Decimal("28000000000"),
            span=(0, 43),
        ),
        LabeledClaim(
            quote="Demand will exceed supply well into next year",
            management=True,
            attribution="named_executive",
            span=(ARTICLE.index("Demand"), ARTICLE.index("Demand") + 45),
        ),
        LabeledClaim(
            quote="Analysts at Bank of America expect revenue of $30 billion.",
            management=False,
            attribution="analyst",
            span=(ARTICLE.index("Analysts"), ARTICLE.index("Analysts") + 58),
        ),
    ]
    score = score_story(LabeledStory("s", "NVDA", "u", labels), claims)
    assert score.labeled_management == 2 and score.found_management == 1
    assert score.system_management == 2 and score.system_management_true == 1
    assert score.system_management_covered == 2  # attribution precision 1/2
    assert len(score.leaks) == 1 and "analyst" in score.leaks[0]
    assert score.fields == {"metric": [True], "period": [True], "figures": [True]}


def test_two_claims_quoting_one_sentence_go_to_the_labels_of_their_figures() -> None:
    sentence = (
        "Revenue could increase by 68% in fiscal 2028, which is almost in line with the "
        "company's guidance of 70% growth."
    )
    answer = StatementExtraction(
        claims=[
            claim(quote=sentence, attribution="analyst", claim_type="numeric_forecast",
                  metric="revenue", value_text="68%"),
            claim(quote=sentence, claim_type="numeric_forecast", metric="revenue",
                  value_text="70% growth"),
        ]
    )  # fmt: skip
    claims, _ = process(SourceText(1, sentence, date(2026, 10, 1)), answer, NVIDIA, NVDA_CALENDAR)
    analyst_end = sentence.index(",")
    company_start = sentence.index("which")
    labels = [
        LabeledClaim(sentence[:analyst_end], False, "analyst", span=(0, analyst_end)),
        LabeledClaim(
            sentence[company_start:], True, "company_statement", span=(company_start, len(sentence))
        ),
    ]
    score = score_story(LabeledStory("s", "NVDA", "u", labels), claims)
    assert score.leaks == [] and score.attributed_management == 1


def test_stories_quoting_an_executive_on_the_future_come_first_then_older_then_thin() -> None:
    from datetime import UTC, datetime

    from talk_is_cheap.statements.candidates import (
        Candidate,
        StoryText,
        quotes_executive_forward,
    )
    from talk_is_cheap.statements.extract import PlannedStory, priority
    from talk_is_cheap.universe import Executive, UniverseCompany

    company = UniverseCompany(
        ticker="TICA", name="Ticacorp", executives=[Executive(name="Ann Lee", role="CEO")]
    )
    quoted = 'Ann Lee spoke on Tuesday.\n"We will double output next year," she said.'
    reported = "Ann Lee said demand was strong. Ticacorp expects more orders next year."
    assert quotes_executive_forward(quoted, company)
    assert not quotes_executive_forward(reported, company)  # forward-looking, not quoted
    assert not quotes_executive_forward('"We will grow," a spokesman said.', company)

    def planned(text: str, day: date, article_id: int) -> PlannedStory:
        story = StoryText(
            article_id, article_id, "t", text, "article", None, "u",
            datetime(day.year, day.month, day.day, tzinfo=UTC),
        )  # fmt: skip
        return PlannedStory(Candidate("TICA", story), "p", "h", "call", "m")

    today = date(2026, 10, 21)
    stories = {
        "quoted, recent": planned(quoted, date(2026, 10, 1), 1),
        "reported, old": planned(reported, date(2025, 1, 5), 2),
        "reported, recent": planned(reported, date(2026, 9, 1), 3),
    }
    thin = sorted(stories, key=lambda k: priority(stories[k], company, 3, today))
    assert thin == ["quoted, recent", "reported, old", "reported, recent"]
    # Coverage only breaks ties between stories otherwise alike.
    covered = priority(stories["reported, old"], company, 40, today)
    assert priority(stories["reported, old"], company, 3, today) < covered
    assert covered < priority(stories["reported, recent"], company, 3, today)
