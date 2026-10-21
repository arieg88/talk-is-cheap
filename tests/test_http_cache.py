"""The polite, cached fetcher of archive and API sources: pacing, backoff, the disk
cache and answers that must not be cached. HTTP is mocked with respx."""

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from talk_is_cheap.news.http_cache import (
    BACKOFF_START,
    Attempt,
    GatewayTimeout,
    PoliteFetcher,
    RetryPolicy,
    ServiceUnavailable,
)

URL = "https://index.example.test/cdx"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def polite(
    client: httpx.AsyncClient, tmp_path: Path, clock: Clock, attempts: list[Attempt] | None = None
) -> PoliteFetcher:
    return PoliteFetcher(
        client,
        tmp_path,
        "test",
        interval=3.0,
        max_attempts=4,
        on_attempt=None if attempts is None else attempts.append,
        clock=clock.time,
        sleep=clock.sleep,
    )


async def test_answers_are_cached_on_disk_and_never_fetched_twice(tmp_path: Path) -> None:
    clock = Clock()
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(200, text="[]"))
        async with httpx.AsyncClient() as client:
            first = await polite(client, tmp_path, clock).get(URL, params={"url": "a/"})
            again = polite(client, tmp_path, clock)
            second = await again.get(URL, params={"url": "a/"})
            other = await again.get(URL, params={"url": "b/"})
    assert route.call_count == 2  # a/ once, b/ once
    assert not first.cached and second.cached and second.text == "[]" and not other.cached
    assert again.cache_hits == 1


async def test_overload_answers_are_retried_with_a_doubling_pause(tmp_path: Path) -> None:
    clock = Clock()
    attempts: list[Attempt] = []
    with respx.mock() as router:
        router.get(URL).mock(
            side_effect=[
                httpx.Response(504),
                httpx.ConnectTimeout("slow"),
                httpx.Response(503),
                httpx.Response(200, text="[]"),
            ]
        )
        async with httpx.AsyncClient() as client:
            answer = await polite(client, tmp_path, clock, attempts).get(URL)
    assert answer.status == 200
    assert [a.outcome for a in attempts][:3] == ["HTTP 504", "ConnectTimeout: slow", "HTTP 503"]
    assert clock.slept == [BACKOFF_START, BACKOFF_START * 2, BACKOFF_START * 4]


async def test_a_service_that_keeps_failing_gives_up(tmp_path: Path) -> None:
    with respx.mock() as router:
        router.get(URL).mock(return_value=httpx.Response(429))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceUnavailable, match="4 attempts"):
                await polite(client, tmp_path, Clock()).get(URL)


async def test_requests_are_spaced_by_the_interval(tmp_path: Path) -> None:
    clock = Clock()
    with respx.mock() as router:
        router.get(url__startswith=URL).mock(return_value=httpx.Response(200, text="[]"))
        async with httpx.AsyncClient() as client:
            fetcher = polite(client, tmp_path, clock)
            await fetcher.get(URL, params={"n": "1"})
            clock.now += 1.0
            await fetcher.get(URL, params={"n": "2"})
    assert clock.slept == [2.0]


async def test_a_refused_answer_is_retried_and_not_cached(tmp_path: Path) -> None:
    clock = Clock()
    maintenance = "<html><title>Internet Archive: Temporarily Offline</title></html>"
    with respx.mock() as router:
        router.get(URL).mock(
            side_effect=[httpx.Response(200, text=maintenance), httpx.Response(200, text="[]")]
        )
        async with httpx.AsyncClient() as client:
            fetcher = polite(client, tmp_path, clock)
            answer = await fetcher.get(URL, accept=lambda r: r.body.startswith(b"["))
    assert answer.text == "[]" and fetcher.requests == 2


async def test_unsettled_answers_and_errors_are_not_cached(tmp_path: Path) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(200, text="[]"))
        gone = router.get(URL + "/gone").mock(return_value=httpx.Response(410))
        async with httpx.AsyncClient() as client:
            fetcher = polite(client, tmp_path, Clock())
            await fetcher.get(URL, cache=False)
            await fetcher.get(URL, cache=False)
            await fetcher.get(URL + "/gone")
            await fetcher.get(URL + "/gone")
    assert route.call_count == 2 and gone.call_count == 2


async def test_a_redirect_can_be_the_answer(tmp_path: Path) -> None:
    with respx.mock() as router:
        router.get(URL).mock(
            return_value=httpx.Response(302, headers={"Location": "https://news.example/a"})
        )
        async with httpx.AsyncClient() as client:
            answer = await polite(client, tmp_path, Clock()).get(URL, follow_redirects=False)
            cached = await polite(client, tmp_path, Clock()).get(URL, follow_redirects=False)
    assert answer.status == 302 and cached.cached
    assert cached.headers["location"] == "https://news.example/a"


# The Wayback Machine's retry policy, on a fake transport.

PROBE_URL = "https://index.example.test/probe"


class YieldingClock(Clock):
    """A fake clock whose sleeps let other tasks run, as real ones do."""

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        await asyncio.sleep(0)


def transport(answers: list[httpx.Response | Exception], seen: list[str]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handle)


def wayback(
    client: httpx.AsyncClient,
    tmp_path: Path,
    clock: Clock,
    log: list[str],
    *,
    probe: bool = True,
    interval: float = 2.5,
) -> PoliteFetcher:
    return PoliteFetcher(
        client,
        tmp_path,
        "test",
        interval=interval,
        policy=RetryPolicy(),
        on_wait=lambda reason, url: log.append(reason),
        probe=(PROBE_URL, {"limit": "1"}) if probe else None,
        clock=clock.time,
        sleep=clock.sleep,
    )


async def test_a_503_is_retried_at_once_then_after_2_4_and_8_seconds(tmp_path: Path) -> None:
    clock, log, seen = Clock(), [], []
    answers: list[httpx.Response | Exception] = [httpx.Response(503) for _ in range(5)]
    async with httpx.AsyncClient(transport=transport(answers, seen)) as client:
        with pytest.raises(ServiceUnavailable, match="5 attempts"):
            await wayback(client, tmp_path, clock, log, interval=0.0).get(URL)
    assert len(seen) == 5 and clock.slept == [2.0, 4.0, 8.0]  # the first retry at once
    assert log == [
        "HTTP 503: retry 1 of 4 at once",
        "HTTP 503: retry 2 of 4 in 2s",
        "HTTP 503: retry 3 of 4 in 4s",
        "HTTP 503: retry 4 of 4 in 8s",
    ]


async def test_the_pace_of_one_search_every_2_5_seconds_holds_between_retries(
    tmp_path: Path,
) -> None:
    clock, log, seen = Clock(), [], []
    answers: list[httpx.Response | Exception] = [
        httpx.Response(503), httpx.Response(503), httpx.Response(200, text="[]"),
    ]  # fmt: skip
    async with httpx.AsyncClient(transport=transport(answers, seen)) as client:
        await wayback(client, tmp_path, clock, log).get(URL)
    assert clock.slept == [2.5, 2.5]  # "at once" and "in 2s" wait for the pace


async def test_a_504_is_never_retried(tmp_path: Path) -> None:
    clock, log, seen = Clock(), [], []
    answers: list[httpx.Response | Exception] = [httpx.Response(504)]
    async with httpx.AsyncClient(transport=transport(answers, seen)) as client:
        with pytest.raises(GatewayTimeout):
            await wayback(client, tmp_path, clock, log).get(URL)
    assert len(seen) == 1 and clock.slept == [] and log == []


@pytest.mark.parametrize(
    ("headers", "wait", "reason"),
    [
        ({"Retry-After": "17"}, 17.0, "HTTP 429: waiting 17s as Retry-After asks, then one retry"),
        ({}, 60.0, "HTTP 429 without Retry-After: waiting 60s, then one retry"),
    ],
)
async def test_a_429_waits_for_retry_after_or_a_minute_once(
    tmp_path: Path, headers: dict[str, str], wait: float, reason: str
) -> None:
    clock, log, seen = Clock(), [], []
    answers: list[httpx.Response | Exception] = [
        httpx.Response(429, headers=headers), httpx.Response(429, headers=headers),
    ]  # fmt: skip
    async with httpx.AsyncClient(transport=transport(answers, seen)) as client:
        with pytest.raises(ServiceUnavailable, match="2 attempts"):
            await wayback(client, tmp_path, clock, log, interval=0.0).get(URL)
    assert clock.slept == [wait] and log == [reason]


async def test_three_connection_timeouts_pause_until_a_probe_is_answered(
    tmp_path: Path,
) -> None:
    clock, log, seen = Clock(), [], []
    timeout = httpx.ConnectTimeout("no answer")
    answers: list[httpx.Response | Exception] = [
        timeout, timeout, timeout,  # the request: a block
        timeout, httpx.Response(503),  # two probes, not answered
        httpx.Response(200, text="probe"),  # the third probe is
        httpx.Response(200, text="[]"),  # the request, at the normal pace again
    ]  # fmt: skip
    async with httpx.AsyncClient(transport=transport(answers, seen)) as client:
        answer = await wayback(client, tmp_path, clock, log, interval=0.0).get(URL)
    assert answer.text == "[]"
    assert seen == ["/cdx"] * 3 + ["/probe"] * 3 + ["/cdx"]
    assert clock.slept == [60.0, 60.0, 60.0]  # a probe a minute, nothing else
    assert log[2] == (
        "3 connection timeouts in a row: taken for a block; paused, one probe every 60s "
        "until one is answered"
    )
    assert log[3] == "pause over after 3 min 0 s (3 probes): resuming at the normal pace"


async def test_other_requests_go_on_while_searches_are_paused(tmp_path: Path) -> None:
    clock, log = YieldingClock(), []
    order: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        order.append(request.url.path)
        if request.url.path == "/page":
            return httpx.Response(200, text="page")
        if request.url.path == "/probe":
            # Answered once the page read went through while the searches waited.
            return httpx.Response(200 if "/page" in order else 503)
        if order.count("/cdx") <= 3:
            raise httpx.ConnectTimeout("no answer")
        return httpx.Response(200, text="[]")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        searches = wayback(client, tmp_path / "s", clock, log, interval=0.0)
        pages = wayback(client, tmp_path / "p", clock, log, probe=False, interval=0.0)

        async def read_page() -> str:
            await asyncio.sleep(0)
            return (await pages.get("https://index.example.test/page")).text

        search, page = await asyncio.gather(searches.get(URL), read_page())
    assert (search.text, page) == ("[]", "page")
    # The page is read during the pause, between two probes, not held up by it.
    assert order == ["/cdx"] * 3 + ["/probe", "/page", "/probe", "/cdx"]


async def test_without_a_probe_timeouts_are_retried_at_the_normal_pace(tmp_path: Path) -> None:
    clock, log, seen = Clock(), [], []
    answers: list[httpx.Response | Exception] = [httpx.ConnectTimeout("x") for _ in range(5)]
    async with httpx.AsyncClient(transport=transport(answers, seen)) as client:
        with pytest.raises(ServiceUnavailable, match="5 attempts"):
            await wayback(client, tmp_path, clock, log, probe=False, interval=0.0).get(URL)
    assert clock.slept == [] and len(seen) == 5
    assert log[0] == "ConnectTimeout: x: retry 1 of 4 at the normal pace"
