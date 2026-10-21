"""Archived articles and Finnhub end to end against a real database, with the archives
and Finnhub mocked at the HTTP layer: a search keeps the URLs the slug filter passes,
the download stores the article with its publisher URL, page date, source and text,
and a Google News story matched on Finnhub is fetched from its publisher."""

import gzip
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    ArchiveCapture,
    ArchiveSearch,
    Article,
    ArticleCompany,
    ArticleFeed,
    ArticleText,
    Feed,
)
from talk_is_cheap.news import article_text
from talk_is_cheap.news.archive_index import CC_INDEX_URL, WAYBACK_CDX_URL, WAYBACK_RETRY
from talk_is_cheap.news.archives import (
    ArchivePattern,
    ArchivePublisher,
    SearchUnit,
    SlugFilter,
    capture_counts,
    covered_months,
    download,
    month_progress,
    pending,
    run_search,
    search_states,
    searched,
    wayback_units,
)
from talk_is_cheap.news.article_text import HostRules
from talk_is_cheap.news.finnhub import (
    FinnhubItem,
    Match,
    google_stories,
    store_publisher_url,
)
from talk_is_cheap.news.http_cache import PoliteFetcher
from talk_is_cheap.news.linking import CompanyMatcher
from talk_is_cheap.news.normalize import url_hash
from talk_is_cheap.universe import Executive, UniverseCompany

pytestmark = pytest.mark.integration

DOMAIN = "news.archive.test"
COMPANY = UniverseCompany(
    ticker="ARCV",
    name="Archivo",
    exclude_phrases=["Archivo Records"],
    executives=[Executive(name="Mia Lopez", role="Chief Executive Officer")],
)
MATCHER = CompanyMatcher([COMPANY])
SLUGS = SlugFilter([COMPANY], ["says", "will", "ceo", "expects"])
PUBLISHER = ArchivePublisher(
    key="archivetest",
    publisher="Archive Test News",
    domain=DOMAIN,
    patterns=[
        ArchivePattern(
            path="/{year}/{month}/",
            article=r"^/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/?$",
            date=r"^/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/",
        )
    ],
)
ARTICLE_PATH = "/2024/05/22/archivo-ceo-says-sales-will-double/"
PARAGRAPH = (
    "Archivo expects sales to double by 2026, chief executive Mia Lopez said in an "
    "interview on Wednesday, as orders for its storage systems keep climbing across "
    "every region the company sells to."
)
PAGE = f"""<html><head><title>Archivo CEO says sales will double - Archive Test News</title>
<meta property="og:title" content="Archivo CEO says sales will double">
<meta property="og:description" content="Mia Lopez expects sales to double by 2026.">
<meta property="article:published_time" content="2024-05-22T14:00:00Z">
</head><body><article><h1>Archivo CEO says sales will double</h1>
<p>{PARAGRAPH}</p><p>The company opened two plants this year and plans a third in Texas,
according to its annual report, which also listed a larger research budget.</p>
<p>Analysts at several banks said the target was ambitious but within reach if demand from
cloud providers holds up through the next two years of the cycle.</p></article></body></html>"""
WARC_FILE = "crawl-data/CC-MAIN-2024-22/segments/1/warc/CC-MAIN-1-00001.warc.gz"


def warc_record(body: bytes) -> bytes:
    http = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + body
    head = f"WARC/1.0\r\nWARC-Type: response\r\nContent-Length: {len(http)}\r\n\r\n".encode()
    return gzip.compress(head + http + b"\r\n\r\n")


CC_ROWS = [
    # The article, twice (two captures of one URL), and URLs the filter drops.
    {"url": f"https://{DOMAIN}{ARTICLE_PATH}?utm_source=x", "timestamp": "20240523030000"},
    {"url": f"https://{DOMAIN}{ARTICLE_PATH}", "timestamp": "20240522180000"},
    {
        "url": f"https://{DOMAIN}/2024/05/22/archivo-records-says-album-will-ship/",
        "timestamp": "20240522180000",
    },  # an excluded phrase
    {
        "url": f"https://{DOMAIN}/2024/05/23/archivo-quarterly-results/",
        "timestamp": "20240523180000",
    },  # no claim word
    {"url": f"https://{DOMAIN}/2024/05/", "timestamp": "20240523180000"},  # a section page
    {
        "url": f"https://{DOMAIN}/2023/12/01/archivo-ceo-says-x/",
        "timestamp": "20240523180000",
    },  # before --since
]
for number, row in enumerate(CC_ROWS):
    row |= {
        "status": "200",
        "mime": "text/html",
        "filename": WARC_FILE,
        "offset": str(1000 * number),
        "length": "500",
    }


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(Article).where(Article.url.like(f"https://{DOMAIN}/%")))
        await session.execute(delete(Article).where(Article.url.like("https://news.google.com/%")))
        await session.execute(delete(Article).where(Article.url.like("https://finnhub.io/%")))
        await session.execute(
            delete(Article).where(Article.url.like("https://paper.archive.test/%"))
        )
        await session.execute(
            delete(ArchiveCapture).where(ArchiveCapture.publisher == "archivetest")
        )
        await session.execute(delete(ArchiveSearch).where(ArchiveSearch.publisher == "archivetest"))
        await session.execute(delete(Feed).where(Feed.ticker == "ARCV"))


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    yield factory
    await _clean(factory)


async def _no_sleep(_: float) -> None:
    return None


def polite(client: httpx.AsyncClient, tmp_path: Path, service: str) -> PoliteFetcher:
    return PoliteFetcher(client, tmp_path, service, interval=0.0, sleep=_no_sleep)


async def test_archived_articles_are_searched_filtered_downloaded_and_linked(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    since = date(2024, 1, 1)
    unit = SearchUnit(
        PUBLISHER, "commoncrawl", "CC-MAIN-2024-22", f"{DOMAIN}/", crawl="CC-MAIN-2024-22"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=CC_INDEX_URL.format(crawl="CC-MAIN-2024-22")).mock(
            side_effect=[
                httpx.Response(200, json={"pages": 1}),
                httpx.Response(200, text="\n".join(json.dumps(r) for r in CC_ROWS)),
            ]
        )
        data = router.get(f"https://data.commoncrawl.org/{WARC_FILE}").mock(
            return_value=httpx.Response(206, content=warc_record(PAGE.encode()))
        )
        router.get(f"https://{DOMAIN}/robots.txt").respond(404)
        live = router.get(f"https://{DOMAIN}{ARTICLE_PATH}").respond(200, html=PAGE)
        async with httpx.AsyncClient() as client:
            cc = polite(client, tmp_path, "commoncrawl")
            result = await run_search(sessions, cc, unit, SLUGS, since)
            assert (result.captures, result.articles, result.kept) == (6, 5, 1)
            async with sessions() as session:
                assert await searched(session, "archivetest", "commoncrawl") == {
                    ("CC-MAIN-2024-22", f"{DOMAIN}/")
                }
                # Coverage counts every article URL found, filtered or not.
                assert await covered_months(session, "archivetest") == {
                    date(2023, 12, 1),
                    date(2024, 5, 1),
                }
                counts = await capture_counts(session, ["archivetest"], None, since)
                assert counts.by_company == {"ARCV": {2024: 1}}
                queue = await pending(session, ["archivetest"], None, since, None)
            assert len(queue) == 1
            # The earliest capture of the URL is the one kept.
            assert queue[0].captured_at == datetime(2024, 5, 22, 18, tzinfo=UTC)
            rules = HostRules(client, "talk-is-cheap", interval=0.0)
            summary = await download(
                sessions, rules, {"commoncrawl": cc}, queue, {"archivetest": PUBLISHER}, MATCHER,
                since,
            )  # fmt: skip
        # The article is read from the live page; the archive only found its URL.
        assert live.called and not data.called
    assert summary.by_publisher["archivetest"]["stored"] == 1
    assert summary.by_publisher["archivetest"]["text: the live page"] == 1

    url = f"https://{DOMAIN}{ARTICLE_PATH}"
    async with sessions() as session:
        article = await session.scalar(select(Article).where(Article.url_hash == url_hash(url)))
        assert article is not None
        assert article.title == "Archivo CEO says sales will double"
        assert article.publisher == "Archive Test News"
        assert article.published_at == datetime(2024, 5, 22, 14, tzinfo=UTC)
        assert article.time_precision == "published"
        feeds = await session.scalars(
            select(Feed.key).join(ArticleFeed).where(ArticleFeed.article_id == article.id)
        )
        assert list(feeds) == ["commoncrawl:ARCV"]
        link = await session.scalar(
            select(ArticleCompany.confidence).where(ArticleCompany.article_id == article.id)
        )
        assert link is not None and float(link) == 1.0  # headline 0.9, the source agrees
        text = await session.scalar(
            select(ArticleText.text).where(ArticleText.article_id == article.id)
        )
        assert text is not None and "Mia Lopez said in an interview" in text

    # A second search of the same URL finds it stored and keeps nothing new.
    with respx.mock() as router:
        router.get(url__startswith=WAYBACK_CDX_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    ["timestamp", "original", "statuscode", "mimetype"],
                    ["20240601000000", url, "200", "text/html"],
                ],
            )
        )
        async with httpx.AsyncClient() as client:
            again = await run_search(
                sessions,
                polite(client, tmp_path, "wayback"),
                SearchUnit(PUBLISHER, "wayback", "2024-05", f"{DOMAIN}/2024/05/"),
                SLUGS,
                since,
            )
    assert (again.kept, again.already_stored) == (0, 1)


SECTION = ArchivePublisher(
    key="archivetest",
    publisher="Archive Test News",
    domain=DOMAIN,
    patterns=[ArchivePattern(path="/news/", article=r"^/news/\d+/[a-z0-9-]+/?$")],
)


def wayback_fetcher(client: httpx.AsyncClient, tmp_path: Path) -> PoliteFetcher:
    return PoliteFetcher(client, tmp_path, "wayback", interval=0.0, policy=WAYBACK_RETRY,
                         sleep=_no_sleep)  # fmt: skip


async def test_a_month_answered_with_504_is_read_by_pages_and_fails_only_on_a_504_page(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    month = date(2024, 5, 1)
    [unit] = wayback_units(SECTION, [month], SLUGS.company_terms)
    url = f"https://{DOMAIN}/news/1001/archivo-ceo-says-sales-will-double"
    header = ["timestamp", "original"]
    counts: dict[str, int] = {}
    split: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        pages = router.get(url__startswith=WAYBACK_CDX_URL, params={"showNumPages": "true"})
        pages.respond(200, text="2")
        first = router.get(url__startswith=WAYBACK_CDX_URL, params={"page": "0"}).respond(
            200, json=[header, ["20240510000000", url]]
        )
        second = router.get(url__startswith=WAYBACK_CDX_URL, params={"page": "1"}).mock(
            side_effect=[httpx.Response(504), httpx.Response(200, json=[header])]
        )
        whole = router.get(url__startswith=WAYBACK_CDX_URL).respond(504)  # the whole month
        async with httpx.AsyncClient() as client:
            wayback = wayback_fetcher(client, tmp_path)
            failed = await run_search(
                sessions, wayback, unit, SLUGS, date(2024, 1, 1), page_counts=counts,
                on_split=lambda u, why: split.append(why),
            )  # fmt: skip
            # One 504 for the month, then its pages at once; page 1's 504 fails it.
            assert whole.call_count == 1 and first.call_count == 1 and second.call_count == 1
            assert split == ["HTTP 504 for the month: reading its 2 index pages"]
            assert failed.error is not None and "504" in failed.error and failed.kept == 1
            query = first.calls.last.request.url.params
            assert (query["from"], query["to"]) == ("202405", "202405")
            async with sessions() as session:
                states = await search_states(session, "archivetest", "wayback")
                row = await session.scalar(
                    select(ArchiveSearch).where(ArchiveSearch.publisher == "archivetest")
                )
            assert states == {("2024-05", f"{DOMAIN}/news/"): False}
            assert row is not None and (row.pages, row.pages_done, row.complete) == (2, 1, False)
            assert month_progress([unit], states).failed == 1

            # The next run asks only for the page that failed.
            again = await run_search(
                sessions, wayback, unit, SLUGS, date(2024, 1, 1), page_counts=counts
            )
    assert again.error is None and whole.call_count == 1 and first.call_count == 1
    assert second.call_count == 2 and pages.call_count == 1
    async with sessions() as session:
        states = await search_states(session, "archivetest", "wayback")
        kept = await session.scalar(
            select(func.count())
            .select_from(ArchiveCapture)
            .where(ArchiveCapture.publisher == "archivetest")
        )
    assert month_progress([unit], states).done == 1 and kept == 1


async def test_a_month_of_one_index_page_answered_with_504_fails(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [unit] = wayback_units(SECTION, [date(2024, 6, 1)], SLUGS.company_terms)
    with respx.mock() as router:
        router.get(url__startswith=WAYBACK_CDX_URL, params={"showNumPages": "true"}).respond(
            200, text="1"
        )
        whole = router.get(url__startswith=WAYBACK_CDX_URL).respond(504)
        async with httpx.AsyncClient() as client:
            result = await run_search(
                sessions, wayback_fetcher(client, tmp_path), unit, SLUGS, date(2024, 1, 1)
            )
    assert whole.call_count == 1  # never asked again
    assert result.error is not None and "nothing smaller to ask" in result.error
    async with sessions() as session:
        assert await search_states(session, "archivetest", "wayback") == {
            ("2024-06", f"{DOMAIN}/news/"): False
        }


async def test_a_search_over_a_span_of_months_counts_for_each_of_them(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    def search(period: str, prefix: str, complete: bool) -> ArchiveSearch:
        return ArchiveSearch(
            publisher="archivetest", source="wayback", period=period, prefix=prefix,
            requests=1, captures=0, kept=0, months=[], complete=complete,
        )  # fmt: skip

    async with sessions() as session, session.begin():
        session.add_all([
            search("2024-03..2024-05", f"{DOMAIN}/archivo", True),  # done before, as a span
            search("2024-03..2024-05", f"{DOMAIN}/news/", False),  # a span that failed
            search("2024-04", f"{DOMAIN}/news/", True),  # its month, done since
        ])  # fmt: skip
    async with sessions() as session:
        states = await search_states(session, "archivetest", "wayback")
        done = await searched(session, "archivetest", "wayback")
    assert states[("2024-03", f"{DOMAIN}/news/")] is False
    assert states[("2024-04", f"{DOMAIN}/news/")] is True  # a complete search wins
    assert {("2024-03", f"{DOMAIN}/archivo"), ("2024-05", f"{DOMAIN}/archivo")} <= done


async def test_a_google_news_story_matched_on_finnhub_is_fetched_from_its_publisher(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    google = "https://news.google.com/rss/articles/CBMiArchivo"
    async with sessions() as session, session.begin():
        article = Article(
            url_hash=url_hash(google),
            url=google,
            title="Archivo CEO says sales will double",
            publisher="Reuters",
            published_at=datetime(2026, 10, 15, tzinfo=UTC),
            time_precision="published",
        )
        session.add(article)
        await session.flush()
        article.cluster_id = article.id
        session.add(
            ArticleCompany(article_id=article.id, ticker="ARCV", method="headline", confidence=0.9)
        )
        session.add(
            ArticleText(
                article_id=article.id,
                status="unavailable",
                reason="robots.txt disallows: news.google.com",
            )
        )
        article_id = article.id
    async with sessions() as session:
        stories = await google_stories(session, ["ARCV"])
        assert [s.article_id for s in stories] == [article_id]
        assert (await article_text.plan(session, ["ARCV"]))[0] == []  # nothing to try
    item = FinnhubItem(
        1,
        "ARCV",
        "Archivo CEO Says Sales Will Double",
        "",
        "Yahoo",
        "https://finnhub.io/api/news?id=1",
        datetime(2026, 10, 15, tzinfo=UTC),
    )
    await store_publisher_url(sessions, Match(stories[0], item, 97.5), "https://www.reuters.com/a")
    async with sessions() as session:
        planned, _, _ = await article_text.plan(session, ["ARCV"])
        recovered, _, _ = await article_text.plan(session, ["ARCV"], recovered_only=True)
        assert recovered == planned
        stored = await session.get(Article, article_id)
        assert stored is not None and float(stored.publisher_url_score or 0) == 97.5
        assert await google_stories(session, ["ARCV"]) == []  # matched now
    assert [(c.article_id, c.url) for c in planned] == [(article_id, "https://www.reuters.com/a")]


async def _linked_article(
    sessions: async_sessionmaker[AsyncSession], url: str, published: datetime | None = None
) -> int:
    async with sessions() as session, session.begin():
        article = Article(
            url_hash=url_hash(url), url=url, title="Archivo CEO says sales will double",
            publisher="Example", published_at=published or datetime(2025, 6, 1, tzinfo=UTC),
            time_precision="published",
        )  # fmt: skip
        session.add(article)
        await session.flush()
        article.cluster_id = article.id
        session.add(ArticleCompany(article_id=article.id, ticker="ARCV", method="headline",
                                   confidence=0.9))  # fmt: skip
        return article.id


async def test_a_host_showing_interstitials_is_paused_and_finnhub_links_resolved(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from talk_is_cheap.news.article_text import Candidate, fetch_texts

    slow = [f"https://{DOMAIN}/slow/{n}" for n in range(4)]
    link = "https://finnhub.io/api/news?id=9"
    ids = [await _linked_article(sessions, u) for u in [*slow, link]]
    candidates = [Candidate(i, i, u, "Example") for i, u in zip(ids, [*slow, link], strict=True)]
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)
        router.get(url__startswith=f"https://{DOMAIN}/slow/").respond(202)
        router.get(link).respond(302, headers={"Location": "https://paper.archive.test/b"})
        router.get("https://paper.archive.test/b").respond(200, html=PAGE)
        async with httpx.AsyncClient() as client:
            summary = await fetch_texts(
                sessions, client, "talk-is-cheap", selected=candidates, interval=0.0,
                robots_dir=tmp_path,
            )  # fmt: skip
    assert summary.paused_hosts == {DOMAIN}
    assert summary.total("paused") == 1 and summary.total("unavailable") == 3
    assert summary.success_rate == 0.25  # 1 of the 4 articles requested
    async with sessions() as session:
        resolved = await session.get(Article, ids[-1])
        assert resolved is not None and resolved.publisher_url == "https://paper.archive.test/b"
        texts = dict(
            (await session.execute(select(ArticleText.article_id, ArticleText.reason))).all()
        )
    assert texts[ids[-1]] is None  # ok
    assert ids[3] not in texts  # paused: left for a later run
    assert texts[ids[0]] == "interstitial: HTTP 202, empty page"
    await _delete(sessions, "https://paper.archive.test/")


async def _delete(sessions: async_sessionmaker[AsyncSession], prefix: str) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(Article).where(Article.url.like(f"{prefix}%")))


async def test_pages_that_failed_live_are_read_from_common_crawl_but_never_refused_ones(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from talk_is_cheap.news.archive_index import Crawl
    from talk_is_cheap.news.cc_fallback import NO_COPY, failed_articles, recover

    rows = {
        "gone": (f"https://{DOMAIN}/2025/06/01/gone/", "HTTP 404"),
        "missing": (f"https://{DOMAIN}/2025/06/01/missing/", "no article text"),
        "robots": (f"https://{DOMAIN}/2025/06/01/robots/", f"robots.txt disallows: {DOMAIN}"),
        "paywall": (f"https://{DOMAIN}/2025/06/01/paywall/", "paywall: HTTP 403 (x)"),
    }
    ids = {}
    for name, (url, reason) in rows.items():
        ids[name] = await _linked_article(sessions, url)
        async with sessions() as session, session.begin():
            session.add(ArticleText(article_id=ids[name], status="unavailable", reason=reason))
    async with sessions() as session:
        failed = await failed_articles(session, ["ARCV"])
    assert [f.article_id for f in failed] == [ids["gone"], ids["missing"]]

    crawl = Crawl("CC-MAIN-2025-30", date(2025, 7, 1), date(2025, 7, 15))
    hit = {"url": rows["gone"][0], "timestamp": "20250705000000", "filename": WARC_FILE,
           "offset": "0", "length": "500"}  # fmt: skip
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=CC_INDEX_URL.format(crawl=crawl.id)).mock(
            side_effect=lambda request: (
                httpx.Response(200, text=json.dumps(hit))
                if "gone" in str(request.url)
                else httpx.Response(404, json={"message": "No Captures found"})
            )
        )
        router.get(f"https://data.commoncrawl.org/{WARC_FILE}").respond(
            206, content=warc_record(PAGE.encode())
        )
        async with httpx.AsyncClient() as client:
            summary = await recover(
                sessions, polite(client, tmp_path, "commoncrawl"), [crawl], failed
            )
    assert summary.recovered == {DOMAIN: 1} and summary.tried == {DOMAIN: 2}
    async with sessions() as session:
        texts = {
            i: (s, r)
            for i, s, r in await session.execute(
                select(ArticleText.article_id, ArticleText.status, ArticleText.reason)
            )
        }
        assert await failed_articles(session, ["ARCV"]) == []  # never looked up twice
    assert texts[ids["gone"]] == ("ok", "text from Common Crawl CC-MAIN-2025-30")
    assert texts[ids["missing"]] == ("unavailable", f"no article text; {NO_COPY}")


async def test_a_search_records_each_page_and_resumes_at_the_first_page_not_done(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    unit = SearchUnit(PUBLISHER, "commoncrawl", "CC-MAIN-2024-22", f"{DOMAIN}/",
                      crawl="CC-MAIN-2024-22")  # fmt: skip
    page0 = json.dumps(CC_ROWS[1])
    page1 = json.dumps({**CC_ROWS[1], "url": f"https://{DOMAIN}/2024/05/23/archivo-ceo-says-x/"})
    answers = {"0": httpx.Response(200, text=page0), "1": httpx.Response(503)}

    def index(request: httpx.Request) -> httpx.Response:
        if "showNumPages" in str(request.url):
            return httpx.Response(200, json={"pages": 2})
        return answers[request.url.params["page"]]

    with respx.mock() as router:
        route = router.get(url__startswith=CC_INDEX_URL.format(crawl="CC-MAIN-2024-22"))
        route.mock(side_effect=index)
        async with httpx.AsyncClient() as client:
            first = await run_search(
                sessions, PoliteFetcher(client, tmp_path, "cc", interval=0.0, max_attempts=1,
                                        sleep=_no_sleep), unit, SLUGS, date(2024, 1, 1),
            )  # fmt: skip
            assert first.error is not None and first.kept == 1
            async with sessions() as session:
                row = (
                    await session.execute(
                        select(ArchiveSearch.pages, ArchiveSearch.pages_done,
                               ArchiveSearch.complete, ArchiveSearch.kept)
                        .where(ArchiveSearch.publisher == "archivetest")
                    )
                ).one()  # fmt: skip
                assert tuple(row) == (2, 1, False, 1)
                assert await searched(session, "archivetest", "commoncrawl") == set()
            answers["1"] = httpx.Response(200, text=page1)
            pages_asked = []
            route.side_effect = lambda request: (
                pages_asked.append(request.url.params.get("page")) or index(request)
            )
            second = await run_search(
                sessions, polite(client, tmp_path / "again", "cc"), unit, SLUGS, date(2024, 1, 1)
            )
    assert second.error is None and second.kept == 1
    assert pages_asked == [None, "1"]  # the page count, then only the page not done
    async with sessions() as session:
        assert await searched(session, "archivetest", "commoncrawl") == {
            ("CC-MAIN-2024-22", f"{DOMAIN}/")
        }
        kept = await session.scalar(
            select(func.count())
            .select_from(ArchiveCapture)
            .where(ArchiveCapture.publisher == "archivetest")
        )
    assert kept == 2


@pytest.mark.parametrize(
    ("live", "robots", "status", "text_from"),
    [
        (httpx.Response(404), "", "stored", "text from the Wayback Machine capture"),
        (httpx.Response(410), "", "stored", "text from the Wayback Machine capture"),
        (httpx.Response(403), "", "unavailable", None),  # a refusal: no copy
        (httpx.Response(200, html=PAGE), "Disallow: /2024/", "unavailable", None),  # robots.txt
    ],
)
async def test_the_archive_copy_is_read_only_when_the_live_page_is_gone_and_allowed(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    live: httpx.Response,
    robots: str,
    status: str,
    text_from: str | None,
) -> None:
    url = f"https://{DOMAIN}{ARTICLE_PATH}"
    # trafilatura drops paragraphs it saw in earlier pages of the process: a page of its own.
    copy_page = PAGE.replace(
        "Archivo expects", f"Archivo, in case {live.status_code}{robots}, expects"
    )
    async with sessions() as session, session.begin():
        session.add(
            ArchiveCapture(
                url_hash=url_hash(url), url=url, publisher="archivetest", source="wayback",
                captured_url=url, captured_at=datetime(2024, 5, 23, tzinfo=UTC),
                url_date=date(2024, 5, 22), tickers=["ARCV"],
            )
        )  # fmt: skip
    with respx.mock(assert_all_called=False) as router:
        router.get(f"https://{DOMAIN}/robots.txt").respond(200, text=f"User-agent: *\n{robots}\n")
        page = router.get(url).mock(return_value=live)
        copy = router.get(url__startswith="https://web.archive.org/web/").respond(
            200, html=copy_page
        )
        async with httpx.AsyncClient() as client:
            async with sessions() as session:
                queue = await pending(session, ["archivetest"], None, date(2024, 1, 1), None)
            await download(
                sessions, HostRules(client, "talk-is-cheap", interval=0.0),
                {"wayback": polite(client, tmp_path, "wayback")}, queue,
                {"archivetest": PUBLISHER}, MATCHER, date(2024, 1, 1),
            )  # fmt: skip
    assert page.called is (robots == "")
    assert copy.called is (text_from is not None)
    async with sessions() as session:
        stored = await session.scalar(
            select(ArchiveCapture.status).where(ArchiveCapture.url_hash == url_hash(url))
        )
        reason = await session.scalar(
            select(ArticleText.reason)
            .join(Article, Article.id == ArticleText.article_id)
            .where(Article.url == url)
        )
    assert stored == status, await _capture_reason(sessions, url)
    assert (reason or "").startswith(text_from or "") and (text_from is None) == (reason is None)


async def _capture_reason(sessions: async_sessionmaker[AsyncSession], url: str) -> str | None:
    async with sessions() as session:
        return await session.scalar(
            select(ArchiveCapture.reason).where(ArchiveCapture.url_hash == url_hash(url))
        )


async def test_ingest_archives_searches_then_downloads_from_the_cli(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    import yaml

    from talk_is_cheap.cli import main
    from talk_is_cheap.config import get_settings

    universe = tmp_path / "universe.yaml"
    universe.write_text(yaml.safe_dump({
        "history_start": "2024-05-01", "forms": ["10-Q"],
        "companies": [COMPANY.model_dump(mode="json")],
    }))  # fmt: skip
    archives = tmp_path / "archives.yaml"
    archives.write_text(yaml.safe_dump({
        "claim_words": ["says", "will"],
        "publishers": [PUBLISHER.model_dump() | {"common_crawl": False}],
    }))  # fmt: skip
    for name, value in {
        "UNIVERSE_FILE": str(universe), "ARCHIVES_FILE": str(archives),
        "NEWS_CACHE_DIR": str(tmp_path / "cache"), "EDGAR_IDENTITY": "Test Person t@example.com",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    from talk_is_cheap.news import archive_index

    monkeypatch.setattr(archive_index, "WAYBACK_INTERVAL", 0.0)
    monkeypatch.setattr(archive_index, "CC_INTERVAL", 0.0)
    url = f"https://{DOMAIN}{ARTICLE_PATH}"
    cdx = [["timestamp", "original", "statuscode", "mimetype"],
           ["20240523000000", url, "200", "text/html"]]  # fmt: skip
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get("https://index.commoncrawl.org/collinfo.json").respond(200, json=[])
            router.get(url__startswith=WAYBACK_CDX_URL).mock(
                side_effect=lambda request: httpx.Response(
                    200, json=cdx if "2024%2F05" in str(request.url) else cdx[:1]
                )
            )
            router.get(f"https://{DOMAIN}/robots.txt").respond(404)
            router.get(url).respond(
                200, html=PAGE.replace("Archivo expects", "Archivo, via the CLI, expects")
            )
            assert await asyncio.to_thread(main, ["ingest", "archives", "--dry-run"]) == 0
            out = capsys.readouterr().out
            assert "Wayback only" in out and "captures by status: 1 pending" in out
            assert await asyncio.to_thread(main, ["ingest", "archives"]) == 0
            out = capsys.readouterr().out
        assert "downloading 1 articles from their live publisher pages" in out
        table = out.split("WAYBACK MONTHS")[1].splitlines()[1].split()
        assert table[0] == "archivetest" and table[2:4] == ["0", "0"]  # none failed or left
        assert "archivetest" in out.split("PUBLISHER")[-1]
    finally:
        get_settings.cache_clear()


async def test_a_rounds_downloads_start_while_its_wayback_searches_are_paused(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    import yaml

    from talk_is_cheap.cli import main
    from talk_is_cheap.config import get_settings
    from talk_is_cheap.news import archive_index
    from talk_is_cheap.news.http_cache import RetryPolicy

    universe = tmp_path / "universe.yaml"
    universe.write_text(yaml.safe_dump({
        "history_start": "2024-05-01", "forms": ["10-Q"],
        "companies": [COMPANY.model_dump(mode="json")],
    }))  # fmt: skip
    archives = tmp_path / "archives.yaml"
    archives.write_text(yaml.safe_dump({
        "claim_words": ["says", "will"],
        "publishers": [PUBLISHER.model_dump() | {"common_crawl": False}],
    }))  # fmt: skip
    for name, value in {
        "UNIVERSE_FILE": str(universe), "ARCHIVES_FILE": str(archives),
        "NEWS_CACHE_DIR": str(tmp_path / "cache"), "EDGAR_IDENTITY": "Test Person t@example.com",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    monkeypatch.setattr(archive_index, "WAYBACK_INTERVAL", 0.0)
    monkeypatch.setattr(archive_index, "WAYBACK_RETRY", RetryPolicy(probe_every=0.01))
    url = f"https://{DOMAIN}{ARTICLE_PATH}"
    header = ["timestamp", "original"]
    order: list[str] = []

    def cdx(request: httpx.Request) -> httpx.Response:
        query = str(request.url)
        if request.url.params.get("limit") == "1":  # the probe
            order.append("probe")
            return httpx.Response(200 if "live page" in order else 503, json=[header])
        if "2024%2F05" in query:
            return httpx.Response(200, json=[header, ["20240523000000", url]])
        if "2024%2F06" in query and "live page" not in order:
            order.append("June: timeout")
            raise httpx.ConnectTimeout("no answer")
        return httpx.Response(200, json=[header])

    def live(request: httpx.Request) -> httpx.Response:
        order.append("live page")
        # Text of its own: the other tests store this page's text for the same URL.
        return httpx.Response(200, html=PAGE.replace("Archivo expects", "Archivo, paused, expects"))

    try:
        with respx.mock(assert_all_called=False) as router:
            router.get("https://index.commoncrawl.org/collinfo.json").respond(200, json=[])
            router.get(url__startswith=WAYBACK_CDX_URL).mock(side_effect=cdx)
            router.get(f"https://{DOMAIN}/robots.txt").respond(404)
            router.get(url).mock(side_effect=live)
            assert await asyncio.to_thread(main, ["ingest", "archives"]) == 0
            out = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    # Three timeouts pause the searches; the article May found is read meanwhile; the
    # next probe is answered and June is searched again.
    assert order[:3] == ["June: timeout"] * 3
    paused = order[3:]
    read = paused.index("live page")
    assert set(paused[:read]) <= {"probe"}  # unanswered probes while the page is read
    assert paused[read + 1 :] == ["probe"]  # the first probe after it is answered
    assert "downloading 1 articles while Wayback searches are paused" in out
    assert "taken for a block" in out and "pause over after" in out
    assert "downloading 0 articles from their live publisher pages" in out
    table = out.split("WAYBACK MONTHS")[1].splitlines()[1].split()
    assert table[2:4] == ["0", "0"]  # June done after the pause: none failed or left
