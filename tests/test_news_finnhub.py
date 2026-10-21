"""Finnhub company news: full answers split, history depth, matching Google News stories
and resolving publisher links. HTTP is mocked with respx."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import respx

from talk_is_cheap.news.finnhub import (
    API_URL,
    MATCH_THRESHOLD,
    MAX_ITEMS,
    FinnhubItem,
    GoogleStory,
    fetch_range,
    history_depth,
    match_stories,
    matched_ids,
    resolve,
    windows,
)
from talk_is_cheap.news.http_cache import PoliteFetcher

TODAY = date(2026, 10, 21)


async def _sleep(_: float) -> None:
    return None


def fetcher(client: httpx.AsyncClient, tmp_path: Path) -> PoliteFetcher:
    return PoliteFetcher(client, tmp_path, "finnhub", interval=0.0, sleep=_sleep)


def row(n: int, day: date, headline: str = "Story") -> dict[str, object]:
    # Shaped like a real company-news item (October 2026).
    moment = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
    return {
        "category": "company",
        "datetime": int(moment.timestamp()),
        "headline": f"{headline} {n}",
        "id": n,
        "image": "",
        "related": "NVDA",
        "source": "Yahoo",
        "summary": "",
        "url": f"https://finnhub.io/api/news?id={n:064x}",
    }


async def test_a_full_answer_is_split_down_to_a_day(tmp_path: Path) -> None:
    start, end = date(2026, 9, 1), date(2026, 9, 4)

    def answer(request: httpx.Request) -> httpx.Response:
        first = date.fromisoformat(request.url.params["from"])
        last = date.fromisoformat(request.url.params["to"])
        if first == last == date(2026, 9, 2):  # a day with more than the limit
            return httpx.Response(200, json=[row(i, first) for i in range(MAX_ITEMS)])
        if first != last:
            return httpx.Response(200, json=[row(i, first) for i in range(MAX_ITEMS)])
        return httpx.Response(200, json=[row(1000 + first.day, first)])

    with respx.mock() as router:
        router.get(url__startswith=API_URL).mock(side_effect=answer)
        async with httpx.AsyncClient() as client:
            result = await fetch_range(fetcher(client, tmp_path), "key", "NVDA", start, end, TODAY)
        sent = router.calls[0].request
    assert sent.headers["X-Finnhub-Token"] == "key" and "token" not in str(sent.url)
    # 4 days -> 2 + 2 -> 1+1+1+1: 7 requests, and Sept 2 still full.
    assert result.requests == 7 and result.truncated
    assert len({i.id for i in result.items}) == len(result.items) == MAX_ITEMS + 3


async def test_history_depth_is_found_by_halving(tmp_path: Path) -> None:
    depth = TODAY - timedelta(days=362)

    def answer(request: httpx.Request) -> httpx.Response:
        first = date.fromisoformat(request.url.params["from"])
        return httpx.Response(200, json=[row(1, first)] if first >= depth else [])

    with respx.mock() as router:
        route = router.get(url__startswith=API_URL).mock(side_effect=answer)
        async with httpx.AsyncClient() as client:
            found = await history_depth(fetcher(client, tmp_path), "key", "AAPL", TODAY)
    assert found == depth and route.call_count <= 12


def item(headline: str, when: datetime, ticker: str = "NVDA", n: int = 1) -> FinnhubItem:
    return FinnhubItem(
        n, ticker, headline, "", "Yahoo", f"https://finnhub.io/api/news?id={n}", when
    )


def story(title: str, when: datetime, ticker: str = "NVDA") -> GoogleStory:
    return GoogleStory(7, 7, ticker, title, "Reuters", when)


def test_stories_match_the_closest_headline_of_the_same_company_within_two_days() -> None:
    when = datetime(2026, 10, 15, 14, tzinfo=UTC)
    title = "Nvidia CEO says Blackwell demand is insane as chip sales soar"
    stories = [story(title, when), story("Apple delays its smart glasses", when, "AAPL")]
    items = [
        item("Nvidia CEO Says Blackwell Demand Is 'Insane' as Chip Sales Soar", when, n=1),
        item(
            "Nvidia CEO says Blackwell demand is insane as chip sales soar",
            when + timedelta(days=3),
            n=2,
        ),
        item("Apple delays its smart glasses", when, "MSFT", n=3),  # another company's list
        item("Nvidia shares slip after record run", when, n=4),
    ]
    matches = match_stories(stories, items)
    assert len(matches) == 1
    assert matches[0].item.id == 1 and matches[0].score >= MATCH_THRESHOLD
    assert matched_ids(matches) == {1}
    weak = match_stories([story("Nvidia shares slip", when)], items[3:])
    assert weak[0].score < MATCH_THRESHOLD and matched_ids(weak) == set()


async def test_a_finnhub_link_resolves_to_the_publisher_without_following_it(
    tmp_path: Path,
) -> None:
    found = item("x", datetime(2026, 10, 1, tzinfo=UTC), n=5)
    other = item("y", datetime(2026, 10, 1, tzinfo=UTC), n=6)
    with respx.mock(assert_all_called=True) as router:
        router.get(found.url).mock(
            return_value=httpx.Response(302, headers={"Location": "https://www.fool.com/a/"})
        )
        router.get(other.url).mock(
            return_value=httpx.Response(302, headers={"Location": "https://news.google.com/x"})
        )
        async with httpx.AsyncClient() as client:
            assert await resolve(fetcher(client, tmp_path), found) == "https://www.fool.com/a/"
            assert await resolve(fetcher(client, tmp_path), other) is None


def test_windows_cover_every_day_once() -> None:
    found = windows(date(2026, 1, 1), date(2026, 1, 16))
    assert found == [
        (date(2026, 1, 1), date(2026, 1, 7)),
        (date(2026, 1, 8), date(2026, 1, 14)),
        (date(2026, 1, 15), date(2026, 1, 16)),
    ]
