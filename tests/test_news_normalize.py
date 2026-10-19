from datetime import UTC, datetime

import pytest

from talk_is_cheap.news.normalize import (
    SUMMARY_MAX_CHARS,
    canonical_url,
    clean_summary,
    clean_title,
    domain,
    is_spam,
    parse_timestamp,
    plain_text,
    url_hash,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Yahoo, MarketWatch, Cisco and Google News tracking parameters
        (
            "https://www.fool.com/investing/2026/10/18/the-5-trillion-club/?.tsrc=rss",
            "https://www.fool.com/investing/2026/10/18/the-5-trillion-club/",
        ),
        (
            "https://www.marketwatch.com/story/retire-f1b6b085?mod=mw_rss_topstories",
            "https://www.marketwatch.com/story/retire-f1b6b085",
        ),
        (
            "https://newsroom.cisco.com/c/r/newsroom/en/us/a/y2026/m10/higher-ed.html?source=rss",
            "https://newsroom.cisco.com/c/r/newsroom/en/us/a/y2026/m10/higher-ed.html",
        ),
        (
            "https://news.google.com/rss/articles/CBMingFBVV95?oc=5",
            "https://news.google.com/rss/articles/CBMingFBVV95",
        ),
        # utm_*, fragments, default ports and host case; other parameters kept, sorted
        (
            "HTTPS://Example.COM:443/a?utm_source=x&id=7&utm_medium=rss&b=2#comments",
            "https://example.com/a?b=2&id=7",
        ),
        ("http://example.com:8080", "http://example.com:8080/"),
    ],
)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_tracking_variants_hash_alike() -> None:
    plain = canonical_url("https://www.reuters.com/markets/nvidia-2026-10-18/")
    tracked = canonical_url("https://www.reuters.com/markets/nvidia-2026-10-18/?utm_source=rss#x")
    assert url_hash(plain) == url_hash(tracked)
    assert len(url_hash(plain)) == 64


def test_domain_drops_www() -> None:
    assert domain("https://www.fool.com/investing/") == "fool.com"
    assert domain("https://finance.yahoo.com/news/x.html") == "finance.yahoo.com"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sun, 18 Oct 2026 20:20:00 +0000", datetime(2026, 10, 18, 20, 20, tzinfo=UTC)),
        ("Fri, 16 Oct 2026 17:10:49 GMT", datetime(2026, 10, 16, 17, 10, 49, tzinfo=UTC)),
        # AMD's investor-relations feed: two-digit year, US Eastern offset
        ("Mon, 12 Oct 26 09:00:00 -0400", datetime(2026, 10, 12, 13, 0, tzinfo=UTC)),
        # Atom (Apple Newsroom)
        ("2026-10-15T12:59:28.566Z", datetime(2026, 10, 15, 12, 59, 28, 566000, tzinfo=UTC)),
        ("2026-10-15T08:00:00-04:00", datetime(2026, 10, 15, 12, 0, tzinfo=UTC)),
        # No zone: taken as UTC
        ("2026-10-15T08:00:00", datetime(2026, 10, 15, 8, 0, tzinfo=UTC)),
        # GDELT seendate
        ("20261001T201500Z", datetime(2026, 10, 1, 20, 15, tzinfo=UTC)),
    ],
)
def test_parse_timestamp(text: str, expected: datetime) -> None:
    parsed = parse_timestamp(text)
    assert parsed == expected
    assert parsed is not None and parsed.utcoffset() is not None


@pytest.mark.parametrize("text", [None, "", "   ", "yesterday", "Sun, 99 Foo 2026"])
def test_unparseable_timestamps_are_none(text: str | None) -> None:
    assert parse_timestamp(text) is None


def test_plain_text_strips_html_and_entities() -> None:
    markup = (
        '<img src="x.jpg" width="450px" /><br />Notre Dame, Auburn &amp; George&nbsp;Mason'
        "<p>use <b>AI</b></p><script>alert(1)</script>"
    )
    assert plain_text(markup) == "Notre Dame, Auburn & George Mason use AI"


def test_plain_text_decodes_double_escaped_entities() -> None:
    assert plain_text("Microsoft&amp;nbsp;&amp;nbsp;Reuters") == "Microsoft Reuters"


def test_summary_is_capped_on_a_word_boundary() -> None:
    summary = clean_summary("word " * 200)
    assert summary is not None
    assert len(summary) <= SUMMARY_MAX_CHARS
    assert summary.endswith("word…")


def test_short_summary_is_kept_whole() -> None:
    assert clean_summary("<p>Apple and Nvidia hit $5 trillion.</p>") == (
        "Apple and Nvidia hit $5 trillion."
    )


def test_summary_that_repeats_the_title_is_dropped() -> None:
    # A Google News summary: a link to the title, then the publisher.
    google = (
        '<a href="https://news.google.com/rss/articles/CBM" target="_blank">Nvidia is no '
        'longer a PC gaming company. Get used to it</a>&nbsp;&nbsp;<font color="#6f6f6f">'
        "PCWorld</font>"
    )
    title = "Nvidia is no longer a PC gaming company. Get used to it"
    assert clean_summary(google, title) is None
    assert clean_summary("", title) is None


def test_title_loses_the_publisher_suffix() -> None:
    assert clean_title("Nvidia is no longer a PC gaming company - PCWorld", "PCWorld") == (
        "Nvidia is no longer a PC gaming company"
    )
    assert clean_title("Q3 results | Reuters", "Reuters") == "Q3 results"
    # Only the publisher's own name is removed.
    assert clean_title("Apple - The Verge", "Reuters") == "Apple - The Verge"
    assert clean_title("Tom&#x2019;s pick", None) == "Tom\u2019s pick"


def test_a_url_given_as_publisher_becomes_its_domain() -> None:
    from talk_is_cheap.news.records import RawItem
    from talk_is_cheap.news.store import normalize_item

    fetched = datetime(2026, 10, 18, tzinfo=UTC)
    item = RawItem(title="t", link="https://x.test/a", publisher="https://www.tech-insider.org")
    article = normalize_item(item, fetched)
    assert article is not None and article.publisher == "tech-insider.org"


# Titles that manual testing found still ending with their publisher.
@pytest.mark.parametrize(
    ("title", "publisher", "expected"),
    [
        (
            "OpenAI accuses former employee of leaking intel on AI device to Apple | CNN Business",
            "CNN",
            "OpenAI accuses former employee of leaking intel on AI device to Apple",
        ),
        (
            "Qualcomm denies rumors about Snapdragon 8 Elite Extreme Gen 6 overheating - "
            "GSMArena.com news",
            "GSMArena.com",
            "Qualcomm denies rumors about Snapdragon 8 Elite Extreme Gen 6 overheating",
        ),
        (
            "Adobe brings interactive image and PDF editing to ChatGPT plugin - CHOSUNBIZ",
            "Chosunbiz",
            "Adobe brings interactive image and PDF editing to ChatGPT plugin",
        ),
        (
            "NVIDIA Run:ai Is Now Open Source as Part of NVIDIA DSX AI Factory Platform | "
            "NVIDIA Technical Blog",
            "NVIDIA Developer",
            "NVIDIA Run:ai Is Now Open Source as Part of NVIDIA DSX AI Factory Platform",
        ),
        (
            "Pre-order our most powerful Surface devices ever | Microsoft Devices Blog",
            "Windows Blog",
            "Pre-order our most powerful Surface devices ever",
        ),
        (
            "Microsoft pulls out of data center agreement with Nscale - The Telegraph",
            "marketscreener.com",
            "Microsoft pulls out of data center agreement with Nscale",
        ),
        ("Qualcomm wins verdict - chosunbiz.com", "chosunbiz.com", "Qualcomm wins verdict"),
        (
            "Microsoft faces strategic market status designation - Computing UK",
            "Computing UK",
            "Microsoft faces strategic market status designation",
        ),
        (
            "Microsoft Pulls Out of UK Data Center Deal With Nscale - www.marketscreener.com",
            "www.marketscreener.com",
            "Microsoft Pulls Out of UK Data Center Deal With Nscale",
        ),
        (
            "Tesla Turns Roadster Stunt Into a Flame-Grilled Joke - LBB - Little Black Book",
            "LBB - Little Black Book",
            "Tesla Turns Roadster Stunt Into a Flame-Grilled Joke",
        ),
    ],
)
def test_publisher_suffixes_are_removed(title: str, publisher: str, expected: str) -> None:
    assert clean_title(title, publisher) == expected


@pytest.mark.parametrize(
    ("title", "publisher"),
    [
        ("Meta's Petal Subsea Cable Is A World First - Here's Why", "BGR"),
        ("Advanced Micro Devices (NASDAQ:AMD) Stock Falls 6% - Here's Why", "MarketBeat"),
        ("Oracle Stock Tumbles: What's Going On? - Oracle (NYSE:ORCL)", "Benzinga"),
        ("Before - Microsoft's latest custom chip: Maia 300", "Microsoft Source"),
        ("Qualcomm Technology Explained | EduSeries", "TechNewsWorld"),
        ("Apple results - Apple shares jump after earnings", "Apple"),
    ],
)
def test_real_title_text_is_kept(title: str, publisher: str) -> None:
    assert clean_title(title, publisher) == title


@pytest.mark.parametrize(
    "title",
    [
        "++[Netflix-Watch]TV!] Houston Texans vs Jacksonville Jaguars NFL Game "
        "\U0001d40b\U0001d408\U0001d415\U0001d404 \U0001d412\U0001d42d\U0001d411\U0001d41e"
        "\U0001d400\U0001d426 \uff26\uff52\uff45\uff45 On 18 October 2026",
        "Texans vs Jaguars Live Stream Free Online HD",
        "Reddit Free Livestream: Chiefs vs Bills",
        "\uff37\uff41\uff54\uff43\uff48 Lions game tonight",
    ],
)
def test_spam_titles_are_detected(title: str) -> None:
    assert is_spam(title)


@pytest.mark.parametrize(
    "title",
    [
        "Netflix will live stream Christmas Day NFL games",
        "Amazon Prime Video adds free ad-supported tier",
        "Toyota\uff08\u30c8\u30e8\u30bf\uff09 shares rise",  # full-width parentheses in CJK text
        "Is NVIDIA [NVDA] a buy?",
    ],
)
def test_real_titles_are_not_spam(title: str) -> None:
    assert not is_spam(title)
