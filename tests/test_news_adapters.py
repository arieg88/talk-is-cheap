"""Feed adapters on saved feeds (tests/fixtures/news, trimmed from the real feeds), and
their HTTP behaviour against mocked responses."""

import re
from pathlib import Path

import httpx
import pytest
import respx

from talk_is_cheap.news.adapters import (
    FeedEntry,
    FeedError,
    GoogleNewsFeed,
    PressReleaseFeed,
    RssFeed,
    YahooFinanceFeed,
    build_feeds,
    company_query,
    load_feeds_file,
)
from talk_is_cheap.news.records import Validators
from talk_is_cheap.universe import UniverseCompany, load_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "news"
UNIVERSE = load_universe(PROJECT_ROOT / "universe.yaml")
URL = "https://feeds.example.test/rss"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_yahoo_items_take_the_publisher_from_the_link() -> None:
    items = YahooFinanceFeed("yahoo_finance:NVDA", URL, "NVDA").parse(fixture("yahoo_nvda.xml"))
    assert len(items) == 4
    first = items[0]
    assert first.title == "The $5 Trillion Club: Which Is the Better Stock to Own, Apple or Nvidia?"
    assert first.publisher == "fool.com"
    assert first.published == "Sun, 18 Oct 2026 20:20:00 +0000"
    assert first.link.endswith("/?.tsrc=rss")
    assert first.summary == "Apple and Nvidia have become the world's first $5 trillion stocks."


def test_google_news_items_take_the_publisher_from_source() -> None:
    items = GoogleNewsFeed("google_news:NVDA", URL, "NVDA").parse(fixture("google_news_nvidia.xml"))
    assert [i.publisher for i in items] == ["TechPowerUp", "Tom's Hardware", "PCWorld"]
    assert items[2].title == "Nvidia is no longer a PC gaming company. Get used to it - PCWorld"
    assert all(i.link.startswith("https://news.google.com/rss/articles/") for i in items)


def test_market_feed_uses_the_configured_publisher() -> None:
    items = RssFeed("rss:mw", URL, publisher="MarketWatch").parse(
        fixture("marketwatch_top_stories.xml")
    )
    assert len(items) == 2
    assert {i.publisher for i in items} == {"MarketWatch"}
    assert items[0].title.startswith("My wife says we can\u2019t afford to retire")


def test_atom_press_releases_parse() -> None:
    items = PressReleaseFeed("press_releases:AAPL", URL, "AAPL").parse(
        fixture("apple_newsroom.atom")
    )
    assert items[0].title == "Open up to new possibilities with iPhone Duo"
    assert items[0].published == "2026-10-15T12:59:28.566Z"
    assert items[0].summary is not None and "pre-order iPhone Duo" in items[0].summary
    assert items[0].publisher == "Apple Newsroom"  # the feed's own title


def test_rss_with_two_digit_years_parses() -> None:
    items = PressReleaseFeed("press_releases:AMD", URL, "AMD").parse(
        fixture("amd_press_releases.xml")
    )
    assert items[0].published == "Mon, 12 Oct 26 09:00:00 -0400"
    assert items[0].publisher == "Advanced Micro Devices, Inc. (AMD) Press Releases"


@pytest.mark.parametrize(
    "body",
    [b"<rss><channel><item><title>Broken", b"<html><body>Not a feed</body></html>", b""],
)
def test_malformed_feeds_raise(body: bytes) -> None:
    with pytest.raises(FeedError):
        RssFeed("rss:x", URL).parse(body)


def test_company_query_quotes_every_name() -> None:
    company = UniverseCompany(ticker="GOOGL", name="Alphabet", aliases=["Google"])
    assert company_query(company) == '"Alphabet" OR "Google"'


def test_project_feeds_file_builds_every_feed() -> None:
    feeds = build_feeds(load_feeds_file(PROJECT_ROOT / "feeds.yaml").feeds, UNIVERSE.companies)
    keys = {f.key for f in feeds}
    assert {"yahoo_finance:NVDA", "google_news:GOOGL", "rss:marketwatch-top-stories"} <= keys
    google = next(f for f in feeds if f.key == "google_news:GOOGL")
    assert "q=%22Alphabet%22+OR+%22Google%22" in google.url
    yahoo = next(f for f in feeds if f.key == "yahoo_finance:NVDA")
    assert "s=NVDA" in yahoo.url and yahoo.ticker == "NVDA"


def test_ticker_selection_drops_market_feeds_and_other_companies() -> None:
    entries = load_feeds_file(PROJECT_ROOT / "feeds.yaml").feeds
    nvda = UNIVERSE.select_news(["NVDA"])
    keys = {f.key for f in build_feeds(entries, nvda, company_feeds_only=True)}
    assert keys == {"yahoo_finance:NVDA", "google_news:NVDA", "press_releases:NVDA"}


def test_unknown_feed_types_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown feed type"):
        build_feeds([FeedEntry(type="telex", name="x", url=URL)], UNIVERSE.companies)


def test_an_entry_needs_exactly_one_scope() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        FeedEntry(type="rss", url=URL)
    with pytest.raises(ValueError, match="exactly one"):
        FeedEntry(type="rss", url=URL, name="x", ticker="NVDA")


# HTTP behaviour


@respx.mock
async def test_fetch_sends_validators_and_reads_a_304() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))
    feed = RssFeed("rss:x", URL)
    async with httpx.AsyncClient() as client:
        result = await feed.fetch(client, Validators(etag='W/"abc"', last_modified="Sun, x"))
    assert result.not_modified and result.items == []
    assert result.validators.etag == 'W/"abc"'
    request = route.calls.last.request
    assert request.headers["If-None-Match"] == 'W/"abc"'
    assert request.headers["If-Modified-Since"] == "Sun, x"


@respx.mock
async def test_fetch_returns_items_and_new_validators() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=fixture("marketwatch_top_stories.xml"),
            headers={"ETag": '"v2"', "Last-Modified": "Mon, 19 Oct 2026 00:00:00 GMT"},
        )
    )
    async with httpx.AsyncClient() as client:
        result = await RssFeed("rss:x", URL).fetch(client, Validators())
    assert not result.not_modified and len(result.items) == 2
    assert result.validators.etag == '"v2"'
    assert result.validators.last_modified == "Mon, 19 Oct 2026 00:00:00 GMT"
    assert result.validators.items_hash is not None


@respx.mock
async def test_unchanged_items_count_as_not_modified() -> None:
    """Most feeds send no validators, and Yahoo stamps each response with a new
    lastBuildDate; the same items in any order are not processed again."""
    body = fixture("yahoo_nvda.xml")
    rebuilt = body.replace(
        b"<channel>", b"<channel><lastBuildDate>Mon, 19 Oct 2026</lastBuildDate>"
    )
    items = re.findall(rb"<item>.*?</item>", body, re.S)
    reordered = body.replace(items[0], b"@@").replace(items[1], items[0]).replace(b"@@", items[1])
    changed = body.replace(b"History Says Nvidia", b"History Shows Nvidia")
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, content=b) for b in (body, rebuilt, reordered, changed)
    ]
    feed = YahooFinanceFeed("yahoo_finance:NVDA", URL, "NVDA")
    async with httpx.AsyncClient() as client:
        first = await feed.fetch(client, Validators())
        second = await feed.fetch(client, first.validators)
        third = await feed.fetch(client, second.validators)
        fourth = await feed.fetch(client, third.validators)
    assert len(first.items) == 4
    assert second.not_modified and second.items == []
    assert third.not_modified
    assert not fourth.not_modified and len(fourth.items) == 4


@respx.mock
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(403), "HTTP 403"),
        (httpx.Response(200, content=b"<rss><channel><item><title>x"), "malformed"),
    ],
)
async def test_fetch_failures_raise_feed_errors(response: httpx.Response, message: str) -> None:
    respx.get(URL).mock(return_value=response)
    async with httpx.AsyncClient() as client:
        with pytest.raises(FeedError, match=message):
            await RssFeed("rss:x", URL).fetch(client, Validators())


@respx.mock
async def test_timeouts_raise_feed_errors() -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(FeedError, match="ReadTimeout"):
            await RssFeed("rss:x", URL).fetch(client, Validators())
