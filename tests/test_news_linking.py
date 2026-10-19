from pathlib import Path

import pytest

from talk_is_cheap.news.linking import (
    CONFIDENCE_BARE_TICKER,
    CONFIDENCE_HEADLINE,
    CONFIDENCE_SUMMARY,
    CONFIDENCE_TICKER_NOTATION,
    HIGH_CONFIDENCE,
    CompanyMatcher,
    Link,
    combine,
)
from talk_is_cheap.universe import load_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATCHER = CompanyMatcher(load_universe(PROJECT_ROOT / "universe.yaml").companies)


def tickers(title: str, summary: str | None = None) -> set[str]:
    return {link.ticker for link in MATCHER.match(title, summary)}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "The $5 Trillion Club: Which Is the Better Stock to Own, Apple or Nvidia?",
            {"AAPL", "NVDA"},
        ),
        ("NVIDIA Launches Open Agent Safety Platform", {"NVDA"}),
        ("Nvidia's AI Financing Model Is Spreading to Rivals", {"NVDA"}),
        ("I left my 13-year career at Google to work in sustainability", {"GOOGL"}),
        ("Facebook parent faces new EU probe", {"META"}),
        ("Meta Platforms shares rise after earnings", {"META"}),
        ("What Advanced Micro Devices Buyers Were Betting On", {"AMD"}),
        ("Nvidia, AMD, Dell sink in global tech rout", {"NVDA", "AMD"}),
        ("Intel factory accident kills two workers in Israel", {"INTC"}),
        (
            "AI Cannibalization Is Hitting Salesforce and Adobe In Very Different Ways",
            {"CRM", "ADBE"},
        ),
        # market notation
        ("Oracle\u2019s AI Selloff Is Creating A Rare Setup (NYSE:ORCL)", {"ORCL"}),
        ("Shares Sold by TD Waterhouse Canada Inc. $INTC", {"INTC"}),
        ("Why (CRM) Could Rally", {"CRM"}),
        # a bare ticker that is not a word
        ("NVDA, TSLA lead premarket movers", {"NVDA", "TSLA"}),
    ],
)
def test_names_aliases_and_tickers_match(title: str, expected: set[str]) -> None:
    assert tickers(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        # lower case is never the company
        "How to bake an apple pie in 20 minutes",
        "A meta look at the season finale",
        "Army intel officers testify",
        # tickers and names inside other words
        "AMDOCS reports results",
        "Metaverse hype fades",
        "Intelligence agencies warn of AI threats",
        "Googled it: the best laptops",
        "Teslas are piling up in lots",
        # hyphenated compounds
        "Meta-analysis finds coffee is fine",
        # configured phrases where the name means something else
        "New York's Big Apple Circus returns",
        "Deforestation in the Amazon rainforest hits record",
        "The Brazilian Amazon lost 5% of its cover",
        "Nikola Tesla's lab becomes a museum",
        "The Oracle of Omaha is buying Japanese stocks",
        # tickers that are ordinary words only match in market notation
        "Best CRM software for small business",
        "META TAGS still matter for SEO",
        "Apple Hospitality REIT raises dividend",
        # boilerplate Google News scraped into a title
        "Add Us On Google Add as a preferred source on Google ATLANTA, Ga. - Police said",
    ],
)
def test_false_matches_are_avoided(title: str) -> None:
    assert tickers(title) == set()


def test_all_caps_headlines_skip_bare_tickers_but_keep_names() -> None:
    assert tickers("NVIDIA AND AMD SHARES SLIDE AS AI TRADE COOLS") == {"NVDA", "AMD"}
    assert tickers("CRM SOFTWARE: A BEGINNER GUIDE TO SALES TOOLS") == set()
    # Short names count in upper case only in all-caps headlines.
    assert tickers("META REPORTS RECORD AD REVENUE") == {"META"}


def test_evidence_sets_the_confidence() -> None:
    (headline,) = MATCHER.match("Nvidia beats estimates")
    (notation,) = MATCHER.match("Is NVDA a buy? (NASDAQ: NVDA)")
    (bare,) = MATCHER.match("NVDA slips premarket")
    (summary,) = MATCHER.match("Chip stocks slide", "Nvidia led the semiconductor index lower.")
    assert (headline.method, headline.confidence) == ("headline", CONFIDENCE_HEADLINE)
    assert (notation.method, notation.confidence) == ("ticker", CONFIDENCE_TICKER_NOTATION)
    assert (bare.method, bare.confidence) == ("ticker", CONFIDENCE_BARE_TICKER)
    assert (summary.method, summary.confidence) == ("summary", CONFIDENCE_SUMMARY)
    assert summary.confidence < HIGH_CONFIDENCE <= bare.confidence


def test_per_ticker_feed_alone_is_weak() -> None:
    """Yahoo's NVDA feed carried a story about Green Thumb (cannabis); it stays a weak link."""
    text = MATCHER.match(
        "President Trump's Push to Reschedule Marijuana Hit a Snag. Is Green Thumb Still a Buy?",
        "Green Thumb's financial strength could outweigh Washington's regulatory delays.",
    )
    (link,) = combine(text, [Link("NVDA", "feed", 0.3)])
    assert (link.ticker, link.method, link.confidence) == ("NVDA", "feed", 0.3)
    assert link.confidence < HIGH_CONFIDENCE


def test_feed_agreement_raises_a_text_match_once() -> None:
    text = MATCHER.match("Chip stocks slide", "Nvidia led the index lower.")
    combined = combine(text, [Link("NVDA", "feed", 0.3), Link("NVDA", "feed", 0.5)])
    assert combined == [Link("NVDA", "summary", 0.7)]


def test_issuer_feed_wins_and_other_companies_keep_text_links() -> None:
    text = MATCHER.match("Adobe and NVIDIA Expand Collaboration to Speed Agentic Creation")
    combined = combine(text, [Link("NVDA", "issuer", 1.0)])
    assert combined == [Link("NVDA", "issuer", 1.0), Link("ADBE", "headline", 0.9)]


@pytest.mark.parametrize(
    ("publisher", "expected"),
    [
        ("Netflix", {"NFLX"}),
        ("Cisco Talos Blog", {"CSCO"}),
        ("Google DeepMind", {"GOOGL"}),
        ("NVIDIA Newsroom", {"NVDA"}),
        ("AMD", {"AMD"}),
        # a student newspaper, a local paper, a domain
        ("The Oracle", set()),
        ("Apple Valley News Now", set()),
        ("blog.google", set()),
        ("Netflixer Weekly", set()),
    ],
)
def test_a_publisher_named_like_the_company_is_the_issuer(
    publisher: str, expected: set[str]
) -> None:
    links = MATCHER.link("App Store Indie Games Week kicks off today", None, publisher)
    assert {link.ticker for link in links} == expected
    assert all((link.method, link.confidence) == ("issuer", 0.9) for link in links)


def test_excluded_phrases_cancel_a_feed_only_link() -> None:
    feed = [Link("CSCO", "feed", 0.3)]
    title = "Get to know your Nevada Secretary of State candidate: Cisco Aguilar"
    assert MATCHER.link(title, None, "KOLO 8 News Now", feed) == []
    # A real mention next to the excluded phrase still counts.
    both = "Cisco Aguilar visits Cisco headquarters"
    assert MATCHER.link(both, None, None, feed) == [Link("CSCO", "headline", 1.0)]
    # An issuer feed is the company itself, whatever the text says.
    assert MATCHER.link(title, None, None, [Link("CSCO", "issuer", 1.0)]) == [
        Link("CSCO", "issuer", 1.0)
    ]


# Headlines that manual testing found unlinked: a hyphen is a boundary for names.
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("What a New Nvidia-Backed AI Platform Means for Honeywell Stock", {"NVDA"}),
        ("Weekly Recap: MBG-Nvidia Level 4 AI partnership", {"NVDA"}),
        ("Scaleway to build AMD-powered AI supercomputer in Sweden", {"AMD"}),
        ("All-AMD Ryzen 9800X3D and Radeon RX 9070 XT PC", {"AMD"}),
        ("Arm-Qualcomm legal fight ends in limited verdict", {"QCOM"}),
    ],
)
def test_hyphenated_names_link(title: str, expected: set[str]) -> None:
    links = MATCHER.match(title)
    assert {link.ticker for link in links} == expected
    assert all(
        (link.method, link.confidence) == ("headline", CONFIDENCE_HEADLINE) for link in links
    )


def test_a_former_connection_links_at_lower_confidence() -> None:
    (former,) = MATCHER.match("Ex-Microsoft exec joins OpenAI rival as CEO")
    assert (former.ticker, former.method, former.confidence) == ("MSFT", "headline", 0.6)
    # A current mention elsewhere in the headline wins.
    (both,) = MATCHER.match("Ex-Microsoft exec says Microsoft is losing the AI race")
    assert both.confidence == CONFIDENCE_HEADLINE


def test_tickers_stay_strict_around_hyphens() -> None:
    assert tickers("NVDA-linked ETFs slide") == set()
    assert tickers("Meta-analysis finds coffee is fine") == set()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Found among stored articles when hyphens became boundaries.
        ("Predicting trail-making test performance using 7-Tesla structural cerebral MRI", set()),
        ("New Meta-Model Standardizes Children's Lung Sound-Wave Tests", set()),
        ("Prediction market odds on Tesla-SpaceX merger are above 50% for early 2027", {"TSLA"}),
        ("NVIDIA B200 Price Doubles as Tencent-Oracle Ink $7B Deal", {"NVDA", "ORCL"}),
        ("Cisco-backed chiplet interconnect upstart in line for acquisition", {"CSCO"}),
    ],
)
def test_hyphen_compounds_from_stored_articles(title: str, expected: set[str]) -> None:
    assert tickers(title) == expected
