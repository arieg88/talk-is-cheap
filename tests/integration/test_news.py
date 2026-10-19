"""News polling, backfill and queries end to end against a real database, with feeds and
GDELT mocked at the HTTP layer."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
import respx
from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.cli import main
from talk_is_cheap.config import get_settings
from talk_is_cheap.db import create_session_factory
from talk_is_cheap.db.models import (
    Article,
    ArticleCompany,
    ArticleFeed,
    BackfillWindow,
    Company,
    Feed,
)
from talk_is_cheap.edgar.ingest import ingest_edgar
from talk_is_cheap.filings.ingest import ingest_filings
from talk_is_cheap.news import locks
from talk_is_cheap.news.adapters import FeedEntry, build_feeds, feed_links
from talk_is_cheap.news.gdelt import (
    API_URL,
    MAX_FAILURES,
    MAX_RECORDS,
    SECONDS_PER_ANSWER,
    BackfillProgress,
    GdeltBackfill,
    Pacer,
    backfill,
    earnings_windows,
    estimate,
    plan_backfill,
    weekly_windows,
)
from talk_is_cheap.news.linking import CompanyMatcher
from talk_is_cheap.news.poll import poll_feeds
from talk_is_cheap.news.queries import backfill_progress, company_news, feed_health
from talk_is_cheap.news.store import reprocess
from talk_is_cheap.universe import Universe, UniverseCompany
from tests.integration.test_ingest import CIK, FakeSource
from tests.integration.test_ingest import UNIVERSE as EDGAR_UNIVERSE

pytestmark = pytest.mark.integration

HOST = "https://feeds.example.test"
NEWS = "https://news.example.test"
# TICA is the fake EDGAR company of the other integration tests; TICN is news-only.
UNIVERSE = Universe.model_validate(
    {
        "history_start": "2025-01-01",
        "forms": ["10-K", "10-Q", "8-K"],
        "companies": [
            {"ticker": "TICA", "name": "Ticacorp", "aliases": ["Tica Systems"]},
            {"ticker": "TICN", "name": "Ticonium", "news_only": True},
        ],
    }
)
MATCHER = CompanyMatcher(UNIVERSE.companies)
ENTRIES = [
    FeedEntry(type="yahoo_finance", companies="all", url=HOST + "/yahoo?s={ticker}"),
    FeedEntry(type="rss", name="test-market", publisher="Test Wire", url=HOST + "/market"),
    FeedEntry(type="press_releases", ticker="TICA", url=HOST + "/tica-ir"),
]
# The day after Ticacorp's FY2025Q1 earnings release was filed (2025-04-24).
DAY = datetime(2025, 4, 24, 14, tzinfo=UTC)


def rss(*items: tuple[str, str, datetime, str]) -> bytes:
    """An RSS document of (title, path, published, description) items."""
    body = "".join(
        f"<item><title>{escape(title)}</title><link>{NEWS}{path}?utm_source=rss</link>"
        f"<pubDate>{format_datetime(published)}</pubDate>"
        f"<description>{escape(description)}</description></item>"
        for title, path, published, description in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


EARNINGS = ("Ticacorp beats estimates as cloud sales jump", "/a/earnings", DAY, "")
REWRITE = ("Ticacorp Beats Estimates As Cloud Sales Jump", "/b/earnings", DAY, "")
UNRELATED = ("Green Thumb shares rise on cannabis hopes", "/c/cannabis", DAY, "")
TICONIUM = ("Ticonium wins a large defense order", "/d/order", DAY - timedelta(hours=3), "")
MARKET = ("Stocks slide; Ticonium and Ticacorp lead losers", "/e/market", DAY, "")
RELEASE = ("Tica Systems reports first quarter results", "/f/release", DAY, "")


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(Article).where(Article.url.like(f"{NEWS}/%")))
        await session.execute(
            delete(Feed).where(
                or_(Feed.ticker.in_(["TICA", "TICN"]), Feed.key == "rss:test-market")
            )
        )


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = create_session_factory(engine)
    await _clean(factory)
    yield factory
    await _clean(factory)


@pytest.fixture
def feeds_mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=HOST, assert_all_called=False) as router:
        router.get("/yahoo", params={"s": "TICA"}, name="tica").mock(
            return_value=httpx.Response(200, content=rss(EARNINGS, UNRELATED))
        )
        router.get("/yahoo", params={"s": "TICN"}, name="ticn").mock(
            return_value=httpx.Response(200, content=rss(TICONIUM, EARNINGS))
        )
        router.get("/market", name="market").mock(
            return_value=httpx.Response(200, content=rss(MARKET, REWRITE), headers={"ETag": '"1"'})
        )
        router.get("/tica-ir", name="ir").mock(
            return_value=httpx.Response(200, content=rss(RELEASE))
        )
        yield router


async def poll(sessions: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    feeds = build_feeds(ENTRIES, UNIVERSE.select_news())
    async with httpx.AsyncClient() as client:
        results = await poll_feeds(sessions, feeds, MATCHER, client)
    return {r.key: r for r in results}


async def links(sessions: async_sessionmaker[AsyncSession], path: str) -> dict[str, tuple]:
    async with sessions() as session:
        rows = await session.execute(
            select(ArticleCompany.ticker, ArticleCompany.method, ArticleCompany.confidence)
            .join(Article, Article.id == ArticleCompany.article_id)
            .where(Article.url == f"{NEWS}{path}")
        )
        return {t: (m, float(c)) for t, m, c in rows}


async def test_articles_are_stored_once_across_polls_and_feeds(
    sessions: async_sessionmaker[AsyncSession], feeds_mock: respx.MockRouter
) -> None:
    first = await poll(sessions)
    assert all(r.ok for r in first.values())  # type: ignore[attr-defined]
    tica, ticn = first["yahoo_finance:TICA"], first["yahoo_finance:TICN"]
    assert (tica.items, tica.new, tica.duplicates) == (2, 2, 0)  # type: ignore[attr-defined]
    # EARNINGS came through the TICA feed first.
    assert (ticn.items, ticn.new, ticn.duplicates) == (2, 1, 1)  # type: ignore[attr-defined]

    async with sessions() as session:
        stored = await session.scalar(
            select(func.count()).select_from(Article).where(Article.url.like(f"{NEWS}/%"))
        )
        earnings = (
            await session.execute(select(Article).where(Article.url == f"{NEWS}/a/earnings"))
        ).scalar_one()
        supplying = await session.scalar(
            select(func.count())
            .select_from(ArticleFeed)
            .where(ArticleFeed.article_id == earnings.id)
        )
    assert stored == 6
    assert supplying == 2  # both Yahoo feeds
    assert earnings.published_at == DAY and earnings.time_precision == "published"

    # The second poll: the market feed answers 304 to its ETag, the others repeat.
    feeds_mock.routes["market"].mock(return_value=httpx.Response(304))
    second = await poll(sessions)
    assert second["rss:test-market"].not_modified  # type: ignore[attr-defined]
    assert feeds_mock.routes["market"].calls.last.request.headers["If-None-Match"] == '"1"'
    assert all(r.new == 0 for r in second.values())  # type: ignore[attr-defined]
    assert all(r.not_modified for r in second.values())  # type: ignore[attr-defined]


async def test_near_duplicates_share_a_cluster(
    sessions: async_sessionmaker[AsyncSession], feeds_mock: respx.MockRouter
) -> None:
    results = await poll(sessions)
    assert results["rss:test-market"].clustered == 1  # type: ignore[attr-defined]
    async with sessions() as session:
        rows = await session.execute(
            select(Article.url, Article.cluster_id).where(Article.url.like(f"{NEWS}/%"))
        )
        clusters = {url: cluster_id for url, cluster_id in rows}
    assert clusters[f"{NEWS}/a/earnings"] == clusters[f"{NEWS}/b/earnings"]
    assert len(set(clusters.values())) == 5

    async with sessions() as session:
        stories = await company_news(session, "TICA")
    earnings = next(s for s in stories if s.title == EARNINGS[0])
    assert earnings.others == ["Test Wire"]


async def test_links_record_method_and_confidence(
    sessions: async_sessionmaker[AsyncSession], feeds_mock: respx.MockRouter
) -> None:
    await poll(sessions)
    # Named in the headline and carried by its own Yahoo feed. TICN's feed carried it too,
    # which alone is weak evidence.
    assert await links(sessions, "/a/earnings") == {
        "TICA": ("headline", 1.0),
        "TICN": ("feed", 0.3),
    }
    # Yahoo's TICA feed carried a story about someone else: a weak feed-only link.
    assert await links(sessions, "/c/cannabis") == {"TICA": ("feed", 0.3)}
    # A market-wide feed links by name only.
    assert await links(sessions, "/e/market") == {
        "TICA": ("headline", 0.9),
        "TICN": ("headline", 0.9),
    }
    # The company's own feed, naming it by an alias.
    assert await links(sessions, "/f/release") == {"TICA": ("issuer", 1.0)}

    async with sessions() as session:
        shown = {s.title for s in await company_news(session, "TICA")}
    assert UNRELATED[0] not in shown  # below the default minimum confidence
    assert {EARNINGS[0], MARKET[0], RELEASE[0]} <= shown


async def article_ids(sessions: async_sessionmaker[AsyncSession]) -> list[int]:
    async with sessions() as session:
        rows = await session.execute(select(Article.id).where(Article.url.like(f"{NEWS}/%")))
        return list(rows.scalars().all())


async def test_reprocess_applies_the_current_rules_and_universe(
    sessions: async_sessionmaker[AsyncSession], feeds_mock: respx.MockRouter
) -> None:
    await poll(sessions)
    # Rows stored by older rules: spam, a publisher suffix, and two headlines about
    # different companies merged into one cluster.
    async with sessions() as session, session.begin():
        anchor = await session.scalar(select(Article.id).where(Article.url == f"{NEWS}/a/earnings"))
        for path, title, publisher in [
            ("/g/spam", "++[Ticonium-Watch]TV!] Game LIVE Stream Free", "Test Wire"),
            ("/h/suffix", "Ticonium opens a new plant - Test Wire", "Test Wire"),
            ("/i/cash-tica", "Should You Buy Ticacorp Stock For Its Cash?", "Test Wire"),
            ("/j/cash-ticn", "Should You Buy Ticonium Stock For Its Cash?", "Test Wire"),
        ]:
            await session.execute(
                insert(Article).values(
                    url_hash=path,
                    url=f"{NEWS}{path}",
                    title=title,
                    publisher=publisher,
                    published_at=DAY + timedelta(hours=1),
                    time_precision="published",
                    cluster_id=anchor,
                )
            )
    renamed = Universe.model_validate(
        {
            "history_start": "2025-01-01",
            "forms": ["10-K"],
            "companies": [
                # "Green Thumb" is now an alias; the release's alias is gone.
                {"ticker": "TICA", "name": "Ticacorp", "aliases": ["Green Thumb"]},
                {"ticker": "TICN", "name": "Ticonium", "news_only": True},
            ],
        }
    )
    async with sessions() as session, session.begin():
        result = await reprocess(
            session, CompanyMatcher(renamed.companies), feed_links(), await article_ids(sessions)
        )
    assert (result.articles, result.spam_removed, result.titles_changed) == (9, 1, 1)
    # Before: everything inserted above had joined the earnings cluster.
    assert (result.clusters_before, result.clusters_after) == (5, 8)

    async with sessions() as session:
        rows = await session.execute(
            select(Article.url, Article.title, Article.cluster_id).where(
                Article.url.like(f"{NEWS}/%")
            )
        )
        stored = {url.removeprefix(NEWS): (title, cluster) for url, title, cluster in rows}
    assert "/g/spam" not in stored
    assert stored["/h/suffix"][0] == "Ticonium opens a new plant"
    assert stored["/i/cash-tica"][1] != stored["/j/cash-ticn"][1]
    assert stored["/a/earnings"][1] == stored["/b/earnings"][1]  # still one story
    assert await links(sessions, "/c/cannabis") == {"TICA": ("headline", 1.0)}
    assert await links(sessions, "/f/release") == {"TICA": ("issuer", 1.0)}
    assert await links(sessions, "/a/earnings") == {
        "TICA": ("headline", 1.0),
        "TICN": ("feed", 0.3),
    }


async def test_poll_drops_spam_and_publisher_suffixes(
    sessions: async_sessionmaker[AsyncSession], feeds_mock: respx.MockRouter
) -> None:
    spam = (
        "Ticonium vs Ticacorp \U0001d40b\U0001d408\U0001d415\U0001d404 Stream",
        "/k/spam",
        DAY,
        "",
    )
    suffixed = ("Ticonium hires a new CFO | Test Wire", "/l/cfo", DAY, "")
    feeds_mock.routes["market"].mock(
        return_value=httpx.Response(200, content=rss(MARKET, spam, suffixed))
    )
    results = await poll(sessions)
    market = results["rss:test-market"]
    assert (market.items, market.new, market.spam) == (3, 2, 1)  # type: ignore[attr-defined]
    async with sessions() as session:
        titles = set(
            (
                await session.execute(select(Article.title).where(Article.url.like(f"{NEWS}/%")))
            ).scalars()
        )
    assert "Ticonium hires a new CFO" in titles
    assert not any("Stream" in t for t in titles)


async def test_failing_feed_does_not_stop_the_others_and_health_tracks_it(
    sessions: async_sessionmaker[AsyncSession], feeds_mock: respx.MockRouter
) -> None:
    feeds_mock.routes["ticn"].mock(return_value=httpx.Response(404))
    feeds_mock.routes["ir"].mock(return_value=httpx.Response(200, content=b"<rss><chan"))
    results = await poll(sessions)
    assert results["yahoo_finance:TICN"].error == "HTTP 404"  # type: ignore[attr-defined]
    assert "malformed" in str(results["press_releases:TICA"].error)  # type: ignore[attr-defined]
    assert results["yahoo_finance:TICA"].new == 2  # type: ignore[attr-defined]

    feeds_mock.routes["ticn"].mock(side_effect=httpx.ConnectTimeout("timed out"))
    await poll(sessions)

    now = datetime.now(UTC)
    async with sessions() as session:
        health = {h.key: h for h in await feed_health(session, now)}
    ticn = health["yahoo_finance:TICN"]
    assert (ticn.status, ticn.consecutive_failures) == ("failing", 2)
    assert ticn.last_error is not None and "ConnectTimeout" in ticn.last_error
    tica = health["yahoo_finance:TICA"]
    assert (tica.status, tica.consecutive_failures, tica.articles) == ("ok", 0, 2)
    assert tica.last_new_item_at is not None

    # Recovery resets the failure count.
    feeds_mock.routes["ticn"].mock(return_value=httpx.Response(200, content=rss(TICONIUM)))
    await poll(sessions)
    async with sessions() as session:
        health = {h.key: h for h in await feed_health(session, now)}
    assert health["yahoo_finance:TICN"].consecutive_failures == 0

    # Seventy-three hours later without new items, a feed is stale.
    async with sessions() as session:
        later = {h.key: h for h in await feed_health(session, now + timedelta(hours=73))}
    assert later["yahoo_finance:TICA"].status == "stale"


def gdelt_answer(count: int, start: datetime, prefix: str) -> str:
    articles = [
        {
            "url": f"{NEWS}/gdelt/{prefix}/{n}",
            "title": f"Ticonium story number {n} from {prefix} ( update ) .",
            "seendate": (start + timedelta(minutes=n)).strftime("%Y%m%dT%H%M%SZ"),
            "domain": "reuters.com",
            "language": "English",
        }
        for n in range(count)
    ]
    return json.dumps({"articles": articles})


async def run_weeks(
    sessions: async_sessionmaker[AsyncSession],
    source: GdeltBackfill,
    companies: list[UniverseCompany],
    client: httpx.AsyncClient,
    since: datetime,
    until: datetime,
) -> list[BackfillProgress]:
    windows = [(c, s, e) for c in companies for s, e in weekly_windows(since, until)]
    planned = await plan_backfill(sessions, source, windows)
    return await backfill(sessions, source, planned, MATCHER, client)


async def test_full_answers_escalate_to_publishers_then_halves_and_runs_resume(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    since = datetime(2025, 1, 6, tzinfo=UTC)
    until = since + timedelta(days=14)
    source = GdeltBackfill(["reuters.com"], pacer=Pacer(interval=0))
    ticonium = UNIVERSE.select_news(["TICN"])
    answers: list[tuple[str, str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        start = request.url.params["startdatetime"]
        end = request.url.params["enddatetime"]
        kind = "domains" if "domainis:" in request.url.params["query"] else "name"
        answers.append((kind, start, end))
        moment = datetime.strptime(start, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        if start.startswith("20250113"):
            return httpx.Response(404, text="not found")
        if end == "20250113000000" and start == "20250106000000":
            # Both the name search and the publisher search fill the record limit.
            return httpx.Response(200, text=gdelt_answer(MAX_RECORDS, moment, kind))
        return httpx.Response(200, text=gdelt_answer(3, moment, start))

    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).mock(side_effect=respond)
        async with httpx.AsyncClient() as client:
            (first,) = await run_weeks(sessions, source, ticonium, client, since, until)
    assert answers == [
        ("name", "20250106000000", "20250113000000"),  # full: kept, then by publisher
        ("domains", "20250106000000", "20250113000000"),  # still full: halved
        ("domains", "20250106000000", "20250109120000"),
        ("domains", "20250109120000", "20250113000000"),
        ("name", "20250113000000", "20250120000000"),
    ]
    assert first.error is not None and "HTTP 404" in first.error
    assert (first.windows_done, first.windows_total) == (1, 2)
    assert first.new_articles == 2 * MAX_RECORDS + 6  # the full answers are kept too

    async with sessions() as session:
        mine = (
            (
                await session.execute(
                    select(BackfillWindow)
                    .join(Feed, Feed.id == BackfillWindow.feed_id)
                    .where(Feed.key == "gdelt:TICN")
                )
            )
            .scalars()
            .all()
        )
        article = (
            (await session.execute(select(Article).where(Article.url.like(f"{NEWS}/gdelt/%"))))
            .scalars()
            .first()
        )
    assert [(w.window_start, w.requests) for w in mine] == [(since, 4)]
    assert article is not None and article.time_precision == "seen"
    assert article.title.endswith("(update).")
    assert await links(sessions, article.url.removeprefix(NEWS)) == {"TICN": ("headline", 1.0)}

    # The re-run fetches only the week that failed; a third run has nothing to fetch.
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).mock(
            side_effect=lambda r: httpx.Response(200, text=gdelt_answer(2, since, "again"))
        )
        async with httpx.AsyncClient() as client:
            (second,) = await run_weeks(sessions, source, ticonium, client, since, until)
            assert (second.error, second.windows_done, second.requests) == (None, 1, 1)
            assert await run_weeks(sessions, source, ticonium, client, since, until) == []
        assert router.calls.call_count == 1


async def test_flagged_companies_search_publishers_from_the_start(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    since = datetime(2025, 1, 6, tzinfo=UTC)
    flagged = [UniverseCompany(ticker="TICN", name="Ticonium", gdelt_filter_publishers=True)]
    source = GdeltBackfill(["reuters.com", "cnbc.com"], pacer=Pacer(interval=0))
    with respx.mock(assert_all_called=False) as router:
        route = router.get(API_URL).mock(return_value=httpx.Response(200, text=""))
        async with httpx.AsyncClient() as client:
            (result,) = await run_weeks(
                sessions, source, flagged, client, since, since + timedelta(days=7)
            )
        (call,) = route.calls
    assert result.requests == 1
    assert "(domainis:reuters.com OR domainis:cnbc.com)" in call.request.url.params["query"]


async def test_a_window_that_keeps_failing_is_skipped_and_the_run_goes_on(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("talk_is_cheap.news.gdelt.asyncio.sleep", no_sleep)
    since = datetime(2025, 1, 6, tzinfo=UTC)
    source = GdeltBackfill([], pacer=Pacer(interval=0))
    refusal = "Please limit requests to one every 5 seconds or contact us."

    def respond(request: httpx.Request) -> httpx.Response:
        # TICA's first week is always refused; everything else is answered.
        if "Ticacorp" in request.url.params["query"] and request.url.params[
            "startdatetime"
        ].startswith("20250106"):
            return httpx.Response(429, text=refusal)
        return httpx.Response(200, text="")

    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).mock(side_effect=respond)
        async with httpx.AsyncClient() as client:
            tica, ticn = await run_weeks(
                sessions, source, UNIVERSE.select_news(), client, since, since + timedelta(days=14)
            )
        assert router.calls.call_count == MAX_FAILURES + 1 + 2
    assert (tica.skipped, tica.windows_done, tica.error) == (1, 1, None)
    assert (ticn.skipped, ticn.windows_done) == (0, 2)

    async with sessions() as session:
        polled = {h.key for h in await feed_health(session, datetime.now(UTC))}
        sources = {b.key: b for b in await backfill_progress(session)}
    assert "gdelt:TICA" not in polled  # backfill sources are not polled feeds
    # The skipped week is not recorded, so the next run fetches it.
    assert sources["gdelt:TICA"].windows == 1


async def test_earnings_windows_come_from_stored_releases(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(Company).where(Company.cik == CIK))
    await ingest_edgar(FakeSource(), sessions, EDGAR_UNIVERSE)
    await ingest_filings(FakeSource(), sessions, EDGAR_UNIVERSE)
    source = GdeltBackfill(["reuters.com"], pacer=Pacer(interval=0))
    try:
        async with sessions() as session:
            # The fake company files one earnings release (2025-04-24) and a deliveries
            # report under item 2.02 (2025-07-02), which is not a release.
            windows = await earnings_windows(
                session, UNIVERSE.select_news(), days_before=1, days_after=3
            )
        assert [(c.ticker, s, e) for c, s, e in windows] == [
            ("TICA", datetime(2025, 4, 23, tzinfo=UTC), datetime(2025, 4, 28, tzinfo=UTC))
        ]
        planned = await plan_backfill(sessions, source, windows)
        assert [(w.start, w.end) for w in planned] == [(windows[0][1], windows[0][2])]

        # Cut where RSS polling began, dropped when it began before the window.
        cut = await plan_backfill(
            sessions, source, windows, not_after=datetime(2025, 4, 25, tzinfo=UTC)
        )
        assert cut[0].end == datetime(2025, 4, 25, tzinfo=UTC)
        assert await plan_backfill(sessions, source, windows, not_after=windows[0][1]) == []

        # Dropped when recorded windows (here a weekly one) cover it.
        async with sessions() as session, session.begin():
            feed_id = await session.scalar(select(Feed.id).where(Feed.key == "gdelt:TICA"))
            await session.execute(
                insert(BackfillWindow).values(
                    feed_id=feed_id,
                    window_start=datetime(2025, 4, 21, tzinfo=UTC),
                    window_end=datetime(2025, 4, 28, tzinfo=UTC),
                    requests=1,
                    items=0,
                    new_articles=0,
                    truncated=False,
                )
            )
        assert await plan_backfill(sessions, source, windows) == []
        requests, duration = estimate(source, planned)
        assert requests == 1 and duration.total_seconds() == SECONDS_PER_ANSWER
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(Company).where(Company.cik == CIK))


@pytest.fixture
def news_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    universe = tmp_path / "universe.yaml"
    universe.write_text(json.dumps(UNIVERSE.model_dump(mode="json")))
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(
        json.dumps(
            {
                "feeds": [e.model_dump(exclude_unset=True) for e in ENTRIES],
                "backfill": [{"type": "gdelt", "companies": "all", "domains": ["reuters.com"]}],
            }
        )
    )
    monkeypatch.setenv("UNIVERSE_FILE", str(universe))
    monkeypatch.setenv("FEEDS_FILE", str(feeds))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def private_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lock keys of the test's own, so a real poll or backfill running against the same
    database neither blocks the test nor is blocked by it."""
    monkeypatch.setattr(locks, "POLL_LOCK", locks.POLL_LOCK + 1000)
    monkeypatch.setattr(locks, "BACKFILL_LOCK", locks.BACKFILL_LOCK + 1000)


async def test_cli_ingest_feeds_and_news(
    private_locks: None,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    feeds_mock: respx.MockRouter,
    news_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(delete(Company).where(Company.cik == CIK))
    await ingest_edgar(FakeSource(), sessions, EDGAR_UNIVERSE)
    await ingest_filings(FakeSource(), sessions, EDGAR_UNIVERSE)

    def run(*argv: str) -> int:
        return main(list(argv))

    try:
        assert await asyncio.to_thread(run, "ingest", "news") == 0
        out = capsys.readouterr().out
        assert "yahoo_finance:TICA                    2 items,   2 new" in out
        assert "done: 4/4 feeds ok, 0 not modified, 6 new articles" in out

        assert await asyncio.to_thread(run, "ingest", "news", "--tickers", "TICN") == 0
        out = capsys.readouterr().out
        assert "yahoo_finance:TICN" in out and "rss:test-market" not in out
        assert "done: 1/1 feeds ok, 1 not modified" in out

        assert await asyncio.to_thread(run, "feeds") == 0
        out = capsys.readouterr().out
        assert "yahoo_finance:TICA                ok" in out
        assert "rss:test-market                   ok" in out
        assert "gdelt:" not in out
        assert await asyncio.to_thread(run, "feeds", "--backfill") == 0
        assert capsys.readouterr().out.startswith("SOURCE")

        # A poll that starts while another holds the lock exits quietly.
        async with locks.advisory_locks(engine, locks.POLL_LOCK) as locked:
            assert locked
            assert await asyncio.to_thread(run, "ingest", "news") == 0
        assert capsys.readouterr().out == "another news poll is still running; skipped\n"

        assert await asyncio.to_thread(run, "news", "TICN") == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith("2025-04-24 14:00Z  Test Wire")
        assert out[0].endswith("Stocks slide; Ticonium and Ticacorp lead losers")
        assert out[1].startswith("2025-04-24 11:00Z  news.example.test")
        assert out[1].endswith("Ticonium wins a large defense order")

        assert await asyncio.to_thread(run, "news", "TICA", "--since", "2025-04-25") == 0
        assert "no news stored" in capsys.readouterr().out

        assert await asyncio.to_thread(run, "news", "TICA", "--around-earnings", "FY2025Q1") == 0
        out = capsys.readouterr().out
        assert "TICA FY2025Q1: earnings release filed 2025-04-24 00:00Z" in out
        assert "Ticacorp beats estimates as cloud sales jump  (+1: Test Wire)" in out

        with respx.mock(assert_all_called=False) as gdelt:
            gdelt.get(API_URL).mock(return_value=httpx.Response(200, text=""))
            args = ("ingest", "news", "--backfill", "--around-earnings", "--tickers", "TICA")
            assert await asyncio.to_thread(run, *args) == 0
            out = capsys.readouterr().out
            assert "1 earnings windows to fetch (0 already fetched" in out
            assert "attempt 1: 0 articles in" in out
            assert await asyncio.to_thread(run, *args) == 0
            assert "0 earnings windows to fetch (1 already fetched" in capsys.readouterr().out

        assert await asyncio.to_thread(run, "news", "TICA", "--around-earnings", "FY2024Q1") == 1
        assert "no earnings release loaded" in capsys.readouterr().err
        assert await asyncio.to_thread(run, "news", "ZZZZ") == 1
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(Company).where(Company.cik == CIK))
