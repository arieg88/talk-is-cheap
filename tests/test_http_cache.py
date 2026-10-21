"""The polite, cached fetcher of archive and API sources: pacing, backoff, the disk
cache and answers that must not be cached. HTTP is mocked with respx."""

from pathlib import Path

import httpx
import pytest
import respx

from talk_is_cheap.news.http_cache import (
    BACKOFF_START,
    Attempt,
    PoliteFetcher,
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
