"""Fetching article text: robots.txt, pacing, redirects, refusals and pages without an
article. HTTP is mocked with respx; nothing leaves the machine."""

import httpx
import pytest
import respx

from talk_is_cheap.news.article_text import (
    MIN_INTERVAL,
    Candidate,
    HostRules,
    extract_text,
    fetch_article,
    retryable,
)

AGENT = "talk-is-cheap"
PARAGRAPH = (
    "Nvidia expects revenue of about $28 billion in the current quarter, chief financial "
    "officer Colette Kress said on Wednesday, as demand for its data center chips keeps "
    "running ahead of supply across every cloud provider it sells to."
)
ARTICLE = f"""<html><head><title>Nvidia outlook</title></head><body>
<nav>Home | Markets | Tech</nav>
<article><h1>Nvidia sees demand outrunning supply</h1>
<p>{PARAGRAPH}</p><p>{PARAGRAPH} Analysts had expected less.</p><p>{PARAGRAPH}</p>
</article><footer>Copyright</footer></body></html>"""


class Clock:
    """A clock the pacing reads and a sleep that advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def rules(client: httpx.AsyncClient, clock: Clock | None = None) -> HostRules:
    clock = clock or Clock()
    return HostRules(client, AGENT, clock=clock.time, sleep=clock.sleep)


def candidate(url: str) -> Candidate:
    return Candidate(article_id=1, cluster_id=1, url=url, publisher="Example")


def test_article_text_is_the_body_without_navigation() -> None:
    text = extract_text(ARTICLE, "https://news.example.com/a")
    assert text is not None and "Colette Kress said on Wednesday" in text
    assert "Home | Markets" not in text
    assert extract_text("<html><body><p>Subscribe to read.</p></body></html>", "u") is None


@respx.mock
async def test_robots_txt_is_obeyed_before_any_page() -> None:
    respx.get("https://news.example.com/robots.txt").respond(
        200, text=f"User-agent: {AGENT}\nDisallow: /private/\n"
    )
    page = respx.get("https://news.example.com/private/a").respond(200, html=ARTICLE)
    async with httpx.AsyncClient() as client:
        fetched = await fetch_article(
            rules(client), candidate("https://news.example.com/private/a")
        )
    assert fetched.status == "unavailable"
    assert fetched.reason == "robots.txt disallows: news.example.com"
    assert not page.called


@respx.mock
async def test_a_page_is_fetched_and_its_text_extracted() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get("https://news.example.com/a").respond(200, html=ARTICLE)
    async with httpx.AsyncClient() as client:
        fetched = await fetch_article(rules(client), candidate("https://news.example.com/a"))
    assert fetched.status == "ok" and fetched.text and "data center chips" in fetched.text


@respx.mock
@pytest.mark.parametrize(
    ("status", "reason", "again"),
    [(403, "HTTP 403", False), (401, "HTTP 401", False), (503, "server error: HTTP 503", True)],
)
async def test_refusals_are_recorded_and_only_server_errors_tried_again(
    status: int, reason: str, again: bool
) -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    route = respx.get("https://news.example.com/a").respond(status)
    async with httpx.AsyncClient() as client:
        fetched = await fetch_article(rules(client), candidate("https://news.example.com/a"))
    assert (fetched.status, fetched.reason) == ("unavailable", reason)
    assert route.call_count == 1  # never retried within a run
    assert retryable(fetched.reason) is again


@respx.mock
async def test_a_paywall_or_teaser_page_is_unavailable() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get("https://news.example.com/a").respond(
        200,
        html="<html><body><article><p>Subscribe to continue reading.</p></article></body></html>",
    )
    async with httpx.AsyncClient() as client:
        fetched = await fetch_article(rules(client), candidate("https://news.example.com/a"))
    assert (fetched.status, fetched.reason) == ("unavailable", "no article text")
    assert not retryable(fetched.reason)


@respx.mock
async def test_a_redirect_to_another_site_checks_that_sites_robots_txt() -> None:
    respx.get("https://short.example/robots.txt").respond(404)
    respx.get("https://short.example/x").respond(
        301, headers={"Location": "https://paper.example/a"}
    )
    respx.get("https://paper.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    page = respx.get("https://paper.example/a").respond(200, html=ARTICLE)
    async with httpx.AsyncClient() as client:
        fetched = await fetch_article(rules(client), candidate("https://short.example/x"))
    assert fetched.reason == "robots.txt disallows: paper.example"
    assert not page.called


@respx.mock
async def test_requests_to_one_host_are_a_second_apart() -> None:
    respx.get("https://news.example.com/robots.txt").respond(404)
    respx.get(url__regex=r"https://news\.example\.com/[ab]").respond(200, html=ARTICLE)
    clock = Clock()
    async with httpx.AsyncClient() as client:
        shared = rules(client, clock)
        await fetch_article(shared, candidate("https://news.example.com/a"))
        await fetch_article(shared, candidate("https://news.example.com/b"))
    # robots.txt, then each page: two waits of the full interval.
    assert clock.slept == [MIN_INTERVAL, MIN_INTERVAL]
    assert shared.requests == 3


@respx.mock
async def test_an_unreadable_robots_txt_means_no_fetch_until_a_later_run() -> None:
    respx.get("https://news.example.com/robots.txt").respond(500)
    page = respx.get("https://news.example.com/a").respond(200, html=ARTICLE)
    async with httpx.AsyncClient() as client:
        fetched = await fetch_article(rules(client), candidate("https://news.example.com/a"))
    assert fetched.reason is not None and fetched.reason.startswith("robots.txt unreadable")
    assert retryable(fetched.reason) and not page.called
