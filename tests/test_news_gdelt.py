import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from talk_is_cheap.news.adapters import FeedError
from talk_is_cheap.news.gdelt import (
    API_URL,
    MAX_QUERY_CHARS,
    MAX_RECORDS,
    MAX_WINDOW_SECONDS,
    STOP_AFTER_REFUSALS,
    WINDOW,
    GdeltBackfill,
    GdeltRefused,
    Pacer,
    clean_gdelt_title,
    windows,
)
from talk_is_cheap.news.records import TimePrecision
from talk_is_cheap.universe import UniverseCompany

GOOGLE = UniverseCompany(ticker="GOOGL", name="Alphabet", aliases=["Google"])
NVIDIA = UniverseCompany(ticker="NVDA", name="Nvidia")
QUERY = '"Nvidia" sourcelang:english'

# Shaped like a real artlist answer (October 2026).
ARTICLE = {
    "url": "https://www.reuters.com/technology/nvidia-buyback-2026-10-01/",
    "url_mobile": "",
    "title": "Nvidia Authorizes a Record $150 Billion in Stock Buybacks . The Real Prize Is "
    "Where the Rest of Its Cash Is Going . ",
    "seendate": "20261001T201500Z",
    "socialimage": "",
    "domain": "reuters.com",
    "language": "English",
    "sourcecountry": "United States",
}


def answer(*articles: dict[str, str]) -> str:
    return json.dumps({"articles": list(articles)})


def test_query_names_domains_and_language() -> None:
    source = GdeltBackfill(["reuters.com", "www.cnbc.com"])
    assert source.queries(GOOGLE) == [
        '("Alphabet" OR "Google") (domainis:reuters.com OR domainis:cnbc.com) sourcelang:english'
    ]
    assert GdeltBackfill([]).queries(NVIDIA) == ['"Nvidia" sourcelang:english']
    assert GdeltBackfill(["reuters.com"]).queries(NVIDIA) == [
        '"Nvidia" domainis:reuters.com sourcelang:english'
    ]


def test_long_domain_lists_are_split_into_short_queries() -> None:
    """GDELT rejected one query with all 28 configured domains as too long."""
    domains = [f"publisher{n}.com" for n in range(28)]
    queries = GdeltBackfill(domains).queries(GOOGLE)
    assert len(queries) > 1
    assert all(len(q) <= MAX_QUERY_CHARS for q in queries)
    found = [d for q in queries for d in re.findall(r"domainis:(\S+?)[ )]", q)]
    assert found == domains


def test_parse_keeps_wanted_domains_and_marks_seen_time() -> None:
    source = GdeltBackfill(["reuters.com", "yahoo.com"])
    other = dict(ARTICLE, url="https://spam.example/x", domain="spam.example")
    french = dict(ARTICLE, url="https://www.reuters.com/fr/x", language="French")
    sub = dict(ARTICLE, url="https://finance.yahoo.com/news/x", domain="finance.yahoo.com")
    items, raw = source.parse(answer(ARTICLE, other, french, sub))
    assert raw == 4
    assert [i.link for i in items] == [ARTICLE["url"], sub["url"]]
    first = items[0]
    assert first.title == (
        "Nvidia Authorizes a Record $150 Billion in Stock Buybacks. The Real Prize Is Where "
        "the Rest of Its Cash Is Going."
    )
    assert (first.published, first.publisher) == ("20261001T201500Z", "reuters.com")
    assert first.time_precision is TimePrecision.SEEN


def test_parse_accepts_empty_answers_and_rejects_error_text() -> None:
    source = GdeltBackfill([])
    assert source.parse("") == ([], 0)
    assert source.parse("{}") == ([], 0)
    with pytest.raises(FeedError, match="too short"):
        source.parse("The specified phrase is too short.")


def test_gdelt_titles_lose_spaces_around_punctuation() -> None:
    assert clean_gdelt_title("Apple ( AAPL ) shares rise 3 % , analysts say . ") == (
        "Apple (AAPL) shares rise 3%, analysts say."
    )


def test_windows_cover_the_range_in_weeks() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + 2 * WINDOW + timedelta(days=3)
    found = windows(start, end)
    assert [w[0] for w in found] == [start, start + WINDOW, start + 2 * WINDOW]
    assert found[-1][1] == end
    assert windows(end, start) == []


def _fast_source(domains: list[str]) -> GdeltBackfill:
    return GdeltBackfill(domains, pacer=Pacer(interval=0))


@respx.mock
async def test_fetch_window_reports_a_full_answer() -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, text=answer(*[ARTICLE] * MAX_RECORDS))
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        items, full = await _fast_source(["reuters.com"]).fetch_window(
            client, QUERY, start, start + WINDOW
        )
    assert full and len(items) == MAX_RECORDS
    params = route.calls.last.request.url.params
    assert params["startdatetime"] == "20240101000000"
    assert params["enddatetime"] == "20240108000000"
    assert params["mode"] == "artlist" and params["format"] == "json"
    assert params["maxrecords"] == str(MAX_RECORDS)


@respx.mock
async def test_429_slows_down_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []

    async def no_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr("talk_is_cheap.news.gdelt.asyncio.sleep", no_sleep)
    respx.get(API_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(429), httpx.Response(200, text="")]
    )
    source = _fast_source([])
    source.pacer.interval = 5.0
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        items, full = await source.fetch_window(client, QUERY, start, start + WINDOW)
    assert (items, full) == ([], False)
    assert source.pacer.interval == 20.0
    assert [p for p in pauses if p >= 30] == [30.0, 60.0]


@respx.mock
async def test_other_errors_raise() -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(500, text="oops"))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        with pytest.raises(FeedError, match="HTTP 500"):
            await _fast_source([]).fetch_window(client, QUERY, start, start + WINDOW)


@respx.mock
async def test_timeouts_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("talk_is_cheap.news.gdelt.asyncio.sleep", no_sleep)
    respx.get(API_URL).mock(
        side_effect=[
            httpx.ConnectTimeout("handshake"),
            # seen from GDELT: "Server disconnected without sending a response."
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.Response(200, text=answer(ARTICLE)),
        ]
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        items, full = await _fast_source([]).fetch_window(client, QUERY, start, start + WINDOW)
    assert len(items) == 1 and not full


@respx.mock
async def test_persistent_timeouts_fail_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("talk_is_cheap.news.gdelt.asyncio.sleep", no_sleep)
    respx.get(API_URL).mock(side_effect=httpx.ConnectTimeout("handshake"))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        with pytest.raises(FeedError, match="ConnectTimeout: handshake; gave up on this window"):
            await _fast_source([]).fetch_window(client, QUERY, start, start + WINDOW)


REFUSAL = "Please limit requests to one every 5 seconds or contact us for larger queries."


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr("talk_is_cheap.news.gdelt.asyncio.sleep", sleep)
    return pauses


@respx.mock
async def test_refusals_in_a_row_stop_the_backfill(no_sleep: list[float]) -> None:
    """GDELT refused this machine even on the first request; three refusals in a row stop."""
    route = respx.get(API_URL).mock(return_value=httpx.Response(429, text=REFUSAL))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        with pytest.raises(GdeltRefused, match="Please limit requests") as refused:
            await _fast_source([]).fetch_window(client, QUERY, start, start + WINDOW)
    assert route.call_count == STOP_AFTER_REFUSALS == 3
    assert "3 times in a row" in str(refused.value)
    assert sum(no_sleep) <= MAX_WINDOW_SECONDS


@respx.mock
async def test_a_refusal_with_status_200_counts(no_sleep: list[float]) -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(200, text=REFUSAL))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        with pytest.raises(GdeltRefused):
            await _fast_source([]).fetch_window(client, QUERY, start, start + WINDOW)


@respx.mock
async def test_refusals_count_across_windows_until_an_answer(no_sleep: list[float]) -> None:
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(429, text=REFUSAL),
            httpx.Response(200, text=""),  # window 1 answered: the count resets
            httpx.Response(429, text=REFUSAL),
            httpx.Response(429, text=REFUSAL),
            httpx.Response(200, text=""),
        ]
    )
    source = _fast_source([])
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        await source.fetch_window(client, QUERY, start, start + WINDOW)
        assert source.refusals == 0
        await source.fetch_window(client, QUERY, start + WINDOW, start + 2 * WINDOW)
    assert source.refusals == 0


@respx.mock
async def test_a_window_is_not_retried_past_its_time_limit(
    no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0, 0.0, 0.0, MAX_WINDOW_SECONDS, MAX_WINDOW_SECONDS, MAX_WINDOW_SECONDS])
    monkeypatch.setattr("talk_is_cheap.news.gdelt.time.monotonic", lambda: next(clock, 1e9))
    route = respx.get(API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as client:
        with pytest.raises(FeedError, match="gave up"):
            await _fast_source([]).fetch_window(client, QUERY, start, start + WINDOW)
    assert route.call_count < 3
