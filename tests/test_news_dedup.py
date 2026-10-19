"""Near-duplicate headlines, tested on real pairs from Yahoo Finance and Google News
(October 2026). The scores in comments are the similarity before the threshold."""

from datetime import UTC, datetime, timedelta

import pytest

from talk_is_cheap.news.dedup import (
    CLUSTER_WINDOW,
    NEAR_DUPLICATE_THRESHOLD,
    Clusterer,
    headline_key,
    is_near_duplicate,
    similarity,
)

SAME_STORY = [
    # 99: British and American spelling (Investing.com, Reuters)
    (
        "Microsoft pulls out of UK data centre deal with Nscale, Telegraph reports",
        "Microsoft pulls out of UK data center deal with Nscale, Telegraph reports",
    ),
    # 98: curly apostrophes, one added word (PBS, WCCB Charlotte)
    (
        "Tesla's new Roadster car can thrust itself in the air. Don't expect to drive it "
        "anytime soon",
        "Tesla\u2019s new Roadster car can thrust itself in the air. But don\u2019t expect to "
        "drive it anytime soon",
    ),
    # wire credit appended by an aggregator
    (
        "Microsoft pulls out of UK data center deal with Nscale, Telegraph reports",
        "Microsoft pulls out of UK data center deal with Nscale, Telegraph reports By Reuters",
    ),
    # 86: a rewrite of the Reuters headline (Yahoo Finance, Reuters)
    (
        "US Appeals Court Overturns Class Action Lawsuit Over Tesla Self-Driving Claims",
        "US appeals court blocks class actions over Tesla's self-driving claims",
    ),
    # same headline, different capitalization
    (
        "Meta's $18 billion teen-safety deal is about to hit earnings",
        "Meta\u2019s $18 Billion Teen-Safety Deal Is About To Hit Earnings",
    ),
]

DIFFERENT_STORIES = [
    # 83: one template, two countries (NVIDIA Blog)
    (
        "NVIDIA Announces New Investments in France\u2019s Startups",
        "NVIDIA Announces New Investments in the UK Startup Ecosystem",
    ),
    # 78: one template, different amounts and places (NVIDIA Newsroom)
    (
        "NVIDIA Commits $250 Million to Advance UK Agentic Safety and Security Research Over "
        "the Next Five Years",
        "NVIDIA Commits $1 Billion to Advance US Science Over the Next Five Years",
    ),
    # 73: same column format, different question (Yahoo Finance)
    (
        "Nvidia vs. AMD: Which Business Is More Exposed to a Data-Center Slowdown?",
        "Nvidia vs. AMD: Which AI Business Has More Room for Margins to Fall?",
    ),
    # 72: different partners (NVIDIA Newsroom)
    (
        "Mercedes-Benz and NVIDIA Expand Strategic Partnership to Advance Safe Highly "
        "Automated Driving in the Next Decade",
        "Volvo Cars and NVIDIA Expand Partnership to Advance Safe Automated Driving",
    ),
    # 71: listicles from one publisher
    ("3 Founder Led Stocks With Up To 31% ROE", "3 Quality Stocks With ROE Over 30%"),
    # short headlines must match exactly
    ("Nvidia stock falls", "Nvidia stock rises"),
]


@pytest.mark.parametrize(("a", "b"), SAME_STORY)
def test_same_story_pairs_are_near_duplicates(a: str, b: str) -> None:
    assert is_near_duplicate(a, b)
    assert is_near_duplicate(b, a)


@pytest.mark.parametrize(("a", "b"), DIFFERENT_STORIES)
def test_different_story_pairs_are_kept_apart(a: str, b: str) -> None:
    assert not is_near_duplicate(a, b)


def test_threshold_sits_between_the_real_pairs() -> None:
    """The closest different-story pair scores below the threshold, the loosest
    same-story rewrite above it."""
    loosest_same = similarity(headline_key(SAME_STORY[3][0]), headline_key(SAME_STORY[3][1]))
    closest_different = similarity(
        headline_key(DIFFERENT_STORIES[0][0]), headline_key(DIFFERENT_STORIES[0][1])
    )
    assert closest_different < NEAR_DUPLICATE_THRESHOLD <= loosest_same


def test_headline_key_normalizes_punctuation_and_credits() -> None:
    assert headline_key("Nvidia\u2019s Q3: Beats!  By Reuters") == "nvidia q3 beats"


def test_clusterer_joins_within_the_window_only() -> None:
    noon = datetime(2026, 10, 18, 12, tzinfo=UTC)
    clusterer = Clusterer()
    clusterer.add(1, SAME_STORY[0][0], noon)
    clusterer.add(2, "Apple unveils iPhone Duo with two screens", noon)

    assert clusterer.find(SAME_STORY[0][1], noon + timedelta(hours=5)) == 1
    assert clusterer.find(SAME_STORY[0][1], noon - CLUSTER_WINDOW + timedelta(minutes=1)) == 1
    assert clusterer.find(SAME_STORY[0][1], noon + CLUSTER_WINDOW + timedelta(hours=1)) is None
    assert clusterer.find(DIFFERENT_STORIES[0][0], noon) is None


def test_clusterer_picks_the_most_similar_cluster() -> None:
    noon = datetime(2026, 10, 18, 12, tzinfo=UTC)
    clusterer = Clusterer()
    clusterer.add(1, "US appeals court blocks class actions over Tesla's self-driving claims", noon)
    clusterer.add(
        2, "US Appeals Court Overturns Class Action Lawsuit Over Tesla Self-Driving Claims", noon
    )
    found = clusterer.find("US Appeals Court Overturns Class Action Over Tesla Self-Driving", noon)
    assert found == 2


def test_headlines_naming_different_companies_never_merge() -> None:
    """Manual testing found these two merged: they score 90 but are two stories."""
    noon = datetime(2026, 10, 18, 12, tzinfo=UTC)
    adobe = "Should You Buy Adobe Stock For Its Cash?"
    qualcomm = "Should You Buy Qualcomm Stock For Its Cash?"
    assert similarity(headline_key(adobe), headline_key(qualcomm)) >= NEAR_DUPLICATE_THRESHOLD
    clusterer = Clusterer()
    clusterer.add(1, adobe, noon, frozenset({"ADBE"}))
    assert clusterer.find(qualcomm, noon, frozenset({"QCOM"})) is None
    # The same companies still cluster by similarity, and an exact title always does.
    assert (
        clusterer.find("Should You Buy Adobe Stock for Its Cash Pile?", noon, frozenset({"ADBE"}))
        == 1
    )
    assert clusterer.find(adobe, noon, frozenset()) == 1


def test_clusterer_keeps_members_in_time_order() -> None:
    noon = datetime(2026, 10, 18, 12, tzinfo=UTC)
    clusterer = Clusterer()
    for hours, cluster in ((100, 3), (0, 1), (50, 2)):
        clusterer.add(cluster, SAME_STORY[0][0], noon + timedelta(hours=hours))
    assert clusterer.find(SAME_STORY[0][1], noon + timedelta(hours=60)) == 2
    assert clusterer.find(SAME_STORY[0][1], noon + timedelta(hours=200)) is None
