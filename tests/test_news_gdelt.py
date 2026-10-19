import itertools
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from talk_is_cheap.news.adapters import FeedError
from talk_is_cheap.news.gdelt import (
    API_URL,
    MAX_FAILURES,
    MAX_QUERY_CHARS,
    MAX_RECORDS,
    MIN_INTERVAL,
    RETRY_PAUSE,
    TIMEOUT,
    WINDOW,
    Attempt,
    GdeltBackfill,
    GdeltGaveUp,
    clean_gdelt_title,
    weekly_windows,
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


def test_query_names_the_company_in_english() -> None:
    source = GdeltBackfill(["reuters.com", "www.cnbc.com"])
    assert source.query(GOOGLE) == '("Alphabet" OR "Google") sourcelang:english'
    assert source.query(NVIDIA) == '"Nvidia" sourcelang:english'
    assert source.domain_queries(NVIDIA) == [
        '"Nvidia" (domainis:reuters.com OR domainis:cnbc.com) sourcelang:english'
    ]
    assert GdeltBackfill(["reuters.com"]).domain_queries(NVIDIA) == [
        '"Nvidia" domainis:reuters.com sourcelang:english'
    ]
    assert GdeltBackfill([]).domain_queries(NVIDIA) == []


def test_domain_batches_keep_queries_short() -> None:
    """GDELT rejected one query naming all 28 configured domains as too long."""
    domains = [f"publisher{n}.com" for n in range(28)]
    queries = GdeltBackfill(domains).domain_queries(GOOGLE)
    assert len(queries) > 1
    assert all(len(q) <= MAX_QUERY_CHARS for q in queries)
    assert [d for q in queries for d in re.findall(r"domainis:(\S+?)[ )]", q)] == domains


def test_flagged_companies_start_with_the_domain_batches() -> None:
    source = GdeltBackfill([f"publisher{n}.com" for n in range(28)])
    apple = UniverseCompany(ticker="AAPL", name="Apple", gdelt_filter_publishers=True)
    assert source.first_queries(NVIDIA) == [(source.query(NVIDIA), False)]
    assert source.first_queries(apple) == [(q, True) for q in source.domain_queries(apple)]
    # Without configured publishers there is nothing to filter by.
    assert GdeltBackfill([]).first_queries(apple) == [('"Apple" sourcelang:english', False)]


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
    found = weekly_windows(start, end)
    assert [w[0] for w in found] == [start, start + WINDOW, start + 2 * WINDOW]
    assert found[-1][1] == end
    assert weekly_windows(end, start) == []


class FakeClock:
    """time.monotonic and asyncio.sleep for gdelt.py: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("talk_is_cheap.news.gdelt.time.monotonic", fake.monotonic)
    monkeypatch.setattr("talk_is_cheap.news.gdelt.asyncio.sleep", fake.sleep)
    return fake


START = datetime(2024, 1, 1, tzinfo=UTC)
REFUSAL = "Please limit requests to one every 5 seconds or contact us for larger queries."


async def fetch(source: GdeltBackfill, clock: FakeClock) -> tuple[list[object], bool]:
    async with httpx.AsyncClient() as client:
        items, full = await source.fetch_window(client, QUERY, START, START + WINDOW, "NVDA")
    return list(items), full


@respx.mock
async def test_fetch_window_reports_a_full_answer(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, text=answer(*[ARTICLE] * MAX_RECORDS))
    )
    items, full = await fetch(GdeltBackfill(["reuters.com"]), clock)
    assert full and len(items) == MAX_RECORDS == 250
    request = route.calls.last.request
    # A new connection per request: GDELT closes idle connections without saying so.
    assert request.headers["Connection"] == "close"
    params = request.url.params
    assert params["query"] == QUERY
    assert params["startdatetime"] == "20240101000000"
    assert params["enddatetime"] == "20240108000000"
    assert params["mode"] == "artlist" and params["format"] == "json"
    assert params["maxrecords"] == "250"


@respx.mock
async def test_full_answers_count_before_local_filtering(clock: FakeClock) -> None:
    """Local publisher filtering must not hide that GDELT's answer was full."""
    other = dict(ARTICLE, url="https://spam.example/x", domain="spam.example")
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, text=answer(ARTICLE, *[other] * (MAX_RECORDS - 1)))
    )
    items, full = await fetch(GdeltBackfill(["reuters.com"]), clock)
    assert full and len(items) == 1


@respx.mock
async def test_refusals_are_retried_after_a_fixed_pause(clock: FakeClock) -> None:
    """As on 2026-10-19: busy (429) a few times, then answered."""
    route = respx.get(API_URL).mock(
        side_effect=[httpx.Response(429, text=REFUSAL)] * 3 + [httpx.Response(200, text="")]
    )
    attempts: list[Attempt] = []
    assert await fetch(GdeltBackfill([], on_attempt=attempts.append), clock) == ([], False)
    assert route.call_count == 4
    assert clock.pauses == [RETRY_PAUSE] * 3
    assert [(a.label, a.number, a.outcome) for a in attempts] == [
        ("NVDA", 1, "refused (HTTP 429)"),
        ("NVDA", 2, "refused (HTTP 429)"),
        ("NVDA", 3, "refused (HTTP 429)"),
        ("NVDA", 4, "0 articles"),
    ]


@respx.mock
async def test_attempts_report_their_response_time(clock: FakeClock) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += 10.5  # GDELT takes about 10 seconds, even to refuse
        return httpx.Response(200, text=answer(ARTICLE))

    respx.get(API_URL).mock(side_effect=slow)
    attempts: list[Attempt] = []
    await fetch(GdeltBackfill([], on_attempt=attempts.append), clock)
    (attempt,) = attempts
    assert attempt.seconds == 10.5 and attempt.outcome == "1 articles"


@respx.mock
async def test_requests_are_never_closer_than_the_minimum_interval(clock: FakeClock) -> None:
    sent: list[float] = []
    responses = [httpx.Response(429, text=REFUSAL)] * 4 + [httpx.Response(200, text="")]

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        return responses.pop(0)

    respx.get(API_URL).mock(side_effect=respond)
    await fetch(GdeltBackfill([]), clock)
    assert all(b - a >= MIN_INTERVAL for a, b in itertools.pairwise(sent))


@respx.mock
async def test_a_window_gives_up_after_max_failures_in_a_row(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(return_value=httpx.Response(429, text=REFUSAL))
    with pytest.raises(GdeltGaveUp) as gave_up:
        await fetch(GdeltBackfill([]), clock)
    assert route.call_count == MAX_FAILURES == 30
    assert "kept refusing requests" in str(gave_up.value)
    assert "30 failed attempts in a row" in str(gave_up.value)


@respx.mock
async def test_a_refusal_with_status_200_counts(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        side_effect=[httpx.Response(200, text=REFUSAL), httpx.Response(200, text="")]
    )
    assert await fetch(GdeltBackfill([]), clock) == ([], False)
    assert clock.pauses[-1] == RETRY_PAUSE


@respx.mock
async def test_timeouts_dropped_connections_and_server_errors_are_retried(
    clock: FakeClock,
) -> None:
    respx.get(API_URL).mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            # seen from GDELT: "Server disconnected without sending a response."
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.Response(503, text="busy"),
            httpx.Response(200, text=answer(ARTICLE)),
        ]
    )
    items, full = await fetch(GdeltBackfill([]), clock)
    assert len(items) == 1 and not full


def test_the_request_timeout_allows_slow_answers() -> None:
    assert TIMEOUT.read is not None and TIMEOUT.read >= 60


@respx.mock
async def test_query_errors_raise_at_once(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, text="Your query was too short or too long.")
    )
    with pytest.raises(FeedError, match="too short or too long"):
        await fetch(GdeltBackfill([]), clock)
    respx.get(API_URL).mock(return_value=httpx.Response(404, text="nope"))
    with pytest.raises(FeedError, match="HTTP 404"):
        await fetch(GdeltBackfill([]), clock)
