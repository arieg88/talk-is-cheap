"""Archived articles: URL formats of archives.yaml, the slug filter, search planning,
index answers, WARC records and page metadata. HTTP is mocked; nothing leaves the
machine."""

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from talk_is_cheap.db.models import ArchiveCapture
from talk_is_cheap.news.archive_index import (
    CC_INDEX_URL,
    WAYBACK_CDX_URL,
    ArchiveError,
    Capture,
    Crawl,
    Page,
    cc_captures,
    cc_pages,
    crawls,
    decode_page,
    page_metadata,
    parse_warc_record,
    read_capture,
    wayback_captures,
)
from talk_is_cheap.news.archives import (
    ArchivesFile,
    SlugFilter,
    _publication,
    article_url,
    cc_units,
    load_archives_file,
    months,
    wayback_units,
)
from talk_is_cheap.news.http_cache import PoliteFetcher
from talk_is_cheap.news.records import TimePrecision
from talk_is_cheap.universe import Executive, UniverseCompany

ROOT = Path(__file__).resolve().parents[1]
CONFIG: ArchivesFile = load_archives_file(ROOT / "archives.yaml")
PUBLISHERS = {p.key: p for p in CONFIG.publishers}
COMPANIES = [
    UniverseCompany(
        ticker="NVDA",
        name="Nvidia",
        executives=[Executive(name="Jen-Hsun Huang", role="CEO", aliases=["Jensen Huang"])],
    ),
    UniverseCompany(
        ticker="AAPL",
        name="Apple",
        exclude_phrases=["Big Apple"],
        executives=[Executive(name="Tim Cook", role="CEO")],
    ),
    UniverseCompany(ticker="AMD", name="AMD", aliases=["Advanced Micro Devices"]),
]
SLUGS = SlugFilter(COMPANIES, CONFIG.claim_words)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def fetcher(client: httpx.AsyncClient, tmp_path: Path, service: str = "test") -> PoliteFetcher:
    clock = Clock()
    return PoliteFetcher(
        client, tmp_path, service, interval=1.0, clock=clock.time, sleep=clock.sleep
    )


@pytest.mark.parametrize(
    ("publisher", "url", "slug", "day"),
    [
        (
            "cnbc",
            "https://www.cnbc.com/2024/05/22/nvidia-ceo-says-demand-will-outpace-supply.html"
            "?__source=sharebar|email",
            "nvidia-ceo-says-demand-will-outpace-supply",
            date(2024, 5, 22),
        ),
        (
            "theregister",
            "https://www.theregister.com/2024/05/23/nvidia_q1_2025/?td=keepreading",
            "nvidia_q1_2025",
            date(2024, 5, 23),
        ),
        (
            "businessinsider",
            "https://www.businessinsider.com/nvidia-ceo-jensen-huang-says-ai-2024-5",
            "nvidia-ceo-jensen-huang-says-ai-2024-5",
            date(2024, 5, 1),
        ),
        (
            "theverge",
            "https://www.theverge.com/2025/1/1/24333454/elon-musk-spent-new-years-eve",
            "elon-musk-spent-new-years-eve",
            date(2025, 1, 1),
        ),
        (
            "theverge",
            "https://www.theverge.com/news/1000532/microsoft-copilot-super-app",
            "microsoft-copilot-super-app",
            None,
        ),
        (
            "yahoo",
            "https://finance.yahoo.com/news/nvidia-1-huge-competitive-advantage-125746289.html",
            "nvidia-1-huge-competitive-advantage-125746289",
            None,
        ),
        (
            "marketwatch",
            "http://www.marketwatch.com:80/story/nvidia-adds-four-ea-games-2021-09-30?siteid=rss",
            "nvidia-adds-four-ea-games-2021-09-30",
            date(2021, 9, 30),
        ),
        (
            "arstechnica",
            "https://arstechnica.com/ai/2025/01/meta-says-it-will-build/",
            "meta-says-it-will-build",
            date(2025, 1, 1),
        ),
    ],
)
def test_article_urls_of_each_format_carry_their_slug_and_date(
    publisher: str, url: str, slug: str, day: date | None
) -> None:
    found = article_url(PUBLISHERS[publisher], url)
    assert found is not None
    assert found.slug == slug and found.url_date == day
    # The publisher's own URL: https, its host, no query.
    assert found.url.startswith(f"https://{PUBLISHERS[publisher].domain}/")
    assert "?" not in found.url


def test_section_pages_videos_and_other_hosts_are_not_articles() -> None:
    assert article_url(PUBLISHERS["techcrunch"], "https://techcrunch.com/2026/05/") is None
    assert article_url(PUBLISHERS["cnbc"], "https://www.cnbc.com/video/2024/05/22/x.html") is None
    assert (
        article_url(PUBLISHERS["fortune"], "https://fortune.com/article/100-top-hospitals/") is None
    )
    assert article_url(PUBLISHERS["cnbc"], "https://example.com/2024/05/22/a-b.html") is None


@pytest.mark.parametrize(
    ("slug", "tickers"),
    [
        ("nvidia-ceo-says-demand-will-outpace-supply", ["NVDA"]),
        ("jensen-huang-says-blackwell-is-in-full-production", ["NVDA"]),  # executive
        ("three-things-we-learned-about-apples-ai-plans", ["AAPL"]),  # possessive
        ("tim-cook-interview-vision-pro", ["AAPL"]),
        ("advanced_micro_devices_expects_mi300_sales", ["AMD"]),  # underscores
        ("nvda-stock-forecast-and-amd-targets", ["NVDA", "AMD"]),
        ("nvidia-earnings-recap", []),  # no claim word
        ("big-apple-mayor-says-subway-will-reopen", []),  # an excluded phrase
        ("cook-says-dinner-will-be-late", []),  # a surname alone is not a name
        ("williams-will-sing", []),
    ],
)
def test_the_slug_filter_needs_a_name_and_a_claim_word(slug: str, tickers: list[str]) -> None:
    assert SLUGS.match(slug)[0] == tickers


def test_the_server_side_regex_is_looser_than_the_slug_filter() -> None:
    import re

    pattern = re.compile(SLUGS.url_regex())
    assert pattern.fullmatch("https://www.cnbc.com/2024/05/22/NVIDIA-CEO-Says.html")
    assert pattern.fullmatch("https://www.theregister.com/2024/05/23/jensen_huang_says/")
    assert not pattern.fullmatch("https://www.cnbc.com/2024/05/22/oil-prices-fall.html")
    assert SLUGS.company_terms == [
        "aapl",
        "advanced-micro-devices",
        "amd",
        "apple",
        "nvda",
        "nvidia",
    ]


def test_common_crawl_searches_each_crawl_by_static_prefix_and_year() -> None:
    crawl = Crawl("CC-MAIN-2025-22", date(2025, 5, 17), date(2025, 5, 30))
    since = date(2024, 1, 1)
    units = cc_units(PUBLISHERS["arstechnica"], [crawl], since)
    assert [u.prefix for u in units][:3] == [
        "arstechnica.com/gadgets/2024/",
        "arstechnica.com/gadgets/2025/",
        "arstechnica.com/ai/2024/",
    ]
    assert len(units) == 10 and {u.period for u in units} == {"CC-MAIN-2025-22"}
    # A pattern without {year} is searched whole; one starting with {term} by its domain.
    assert cc_units(PUBLISHERS["tomshardware"], [crawl], since)[0].prefix == (
        "www.tomshardware.com/pc-components/"
    )
    assert [u.prefix for u in cc_units(PUBLISHERS["yahoo"], [crawl], since)] == [
        "finance.yahoo.com/news/"
    ]


def test_wayback_searches_the_missing_months_by_their_own_prefix_or_by_name() -> None:
    missing = [date(2024, 5, 1), date(2024, 7, 1)]
    cnbc = wayback_units(PUBLISHERS["cnbc"], missing, ["nvidia"])
    assert [(u.period, u.prefix, u.since) for u in cnbc] == [
        ("2024-05", "www.cnbc.com/2024/05/", None),
        ("2024-07", "www.cnbc.com/2024/07/", None),
    ]
    verge = wayback_units(PUBLISHERS["theverge"], missing, ["nvidia"])
    assert [u.prefix for u in verge] == [
        "www.theverge.com/2024/5/",
        "www.theverge.com/2024/7/",
        "www.theverge.com/news/",
    ]
    # An undated format is searched once over the span of missing months.
    assert (verge[-1].period, verge[-1].since, verge[-1].until) == (
        "2024-05..2024-07",
        date(2024, 5, 1),
        date(2024, 7, 31),
    )
    yahoo = wayback_units(PUBLISHERS["yahoo"], missing, ["amd", "nvidia"])
    assert [u.prefix for u in yahoo] == [
        "finance.yahoo.com/news/amd",
        "finance.yahoo.com/news/nvidia",
    ]
    assert wayback_units(PUBLISHERS["cnbc"], [], ["nvidia"]) == []
    assert months(date(2024, 11, 15), date(2025, 2, 1)) == [
        date(2024, 11, 1),
        date(2024, 12, 1),
        date(2025, 1, 1),
        date(2025, 2, 1),
    ]


def warc_record(body: bytes, headers: str = "Content-Type: text/html; charset=utf-8") -> bytes:
    http = f"HTTP/1.1 200 OK\r\n{headers}\r\n\r\n".encode() + body
    warc = (
        "WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: https://www.cnbc.com/a.html\r\n"
        f"Content-Length: {len(http)}\r\n\r\n"
    ).encode()
    return gzip.compress(warc + http + b"\r\n\r\n")


def test_a_warc_record_holds_the_http_response() -> None:
    page = parse_warc_record(warc_record(b"<html><p>caf\xc3\xa9</p></html>"))
    assert page.status == 200 and page.content_type == "text/html; charset=utf-8"
    assert decode_page(page) == "<html><p>café</p></html>"
    with pytest.raises(ArchiveError):
        parse_warc_record(b"not gzip")


PAGE = """<html><head><title>Nvidia sees demand outrunning supply - CNBC</title>
<meta property="og:title" content="Nvidia sees demand outrunning supply">
<meta name="description" content="CEO Jensen Huang said demand will exceed supply.">
<meta property="article:published_time" content="2024-05-22T20:30:00+0000">
</head><body><p>text</p></body></html>"""


def test_page_metadata_prefers_open_graph_and_article_tags() -> None:
    meta = page_metadata(PAGE)
    assert meta.title == "Nvidia sees demand outrunning supply"
    assert meta.description == "CEO Jensen Huang said demand will exceed supply."
    assert meta.published == datetime(2024, 5, 22, 20, 30, tzinfo=UTC)
    assert not meta.date_only
    ld = '<html><head><script type="application/ld+json">{"datePublished": "2024-05-23"}'
    ld += "</script><title>T</title></head></html>"
    meta = page_metadata(ld)
    assert (meta.title, meta.published, meta.date_only) == (
        "T",
        datetime(2024, 5, 23, tzinfo=UTC),
        True,
    )


def capture(**values: object) -> ArchiveCapture:
    defaults: dict[str, object] = {
        "captured_at": datetime(2024, 5, 23, 3, tzinfo=UTC),
        "url_date": date(2024, 5, 22),
    }
    return ArchiveCapture(**(defaults | values))


def test_the_publication_time_is_the_pages_unless_it_is_later_than_the_capture() -> None:
    page_time = datetime(2024, 5, 22, 20, 30, tzinfo=UTC)
    assert _publication(page_time, False, capture()) == (page_time, TimePrecision.PUBLISHED)
    # A modification time after the capture: the URL's date instead.
    later = datetime(2025, 1, 1, tzinfo=UTC)
    assert _publication(later, False, capture()) == (
        datetime(2024, 5, 22, tzinfo=UTC),
        TimePrecision.DATE,
    )
    undated = capture(url_date=None)
    assert _publication(None, False, undated) == (undated.captured_at, TimePrecision.SEEN)


# Shaped like real answers (October 2026).
CC_ROWS = [
    {
        "url": "https://www.theregister.com/2025/09/01/nvidia_ceo_says_x/?td=keepreading",
        "timestamp": "20250906120000",
        "status": "200",
        "mime": "text/html",
        "filename": "crawl-data/CC-MAIN-2025-38/segments/1/warc/CC-MAIN-1-00001.warc.gz",
        "offset": "1234",
        "length": "5678",
    },
]
CDX = [
    ["timestamp", "original", "statuscode", "mimetype"],
    [
        "20260911150417",
        "https://www.theregister.com/2026/07/14/musk_promises_x/",
        "200",
        "text/html",
    ],
]


async def test_index_answers_become_captures(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=CC_INDEX_URL.format(crawl="CC-MAIN-2025-38")).mock(
            side_effect=[
                httpx.Response(200, json={"pages": 1, "pageSize": 5, "blocks": 1}),
                httpx.Response(200, text="\n".join(json.dumps(r) for r in CC_ROWS) + "\n"),
            ]
        )
        router.get(url__startswith=WAYBACK_CDX_URL).mock(return_value=httpx.Response(200, json=CDX))
        router.get("https://index.commoncrawl.org/collinfo.json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "CC-MAIN-2025-38",
                        "from": "2025-09-05T00:00:00",
                        "to": "2025-09-18T00:00:00",
                    },
                    {
                        "id": "CC-MAIN-2023-50",
                        "from": "2023-11-28T00:00:00",
                        "to": "2023-12-11T00:00:00",
                    },
                ],
            )
        )
        async with httpx.AsyncClient() as client:
            cc = fetcher(client, tmp_path, "commoncrawl")
            found = await crawls(cc, date(2024, 1, 1), date(2026, 10, 21))
            assert [c.id for c in found] == ["CC-MAIN-2025-38"]
            assert await cc_pages(cc, "CC-MAIN-2025-38", "www.theregister.com/") == 1
            page = await cc_captures(cc, "CC-MAIN-2025-38", "www.theregister.com/", 0)
            rows = page.captures
            assert not page.truncated and rows[0].offset == 1234 and rows[0].length == 5678
            assert rows[0].timestamp == datetime(2025, 9, 6, 12, tzinfo=UTC)
            wayback = await wayback_captures(
                fetcher(client, tmp_path, "wayback"), "www.theregister.com/2026/07/", ".*"
            )
            assert wayback[0].url.endswith("musk_promises_x/") and wayback[0].filename is None


async def test_a_capture_is_read_with_one_range_request_or_raw(tmp_path: Path) -> None:
    record = warc_record(PAGE.encode())
    cc_row = (await _rows(tmp_path))[0]
    with respx.mock(assert_all_called=True) as router:
        data = router.get(
            "https://data.commoncrawl.org/crawl-data/CC-MAIN-2025-38/segments/1/warc/"
            "CC-MAIN-1-00001.warc.gz"
        ).mock(return_value=httpx.Response(206, content=record))
        raw = router.get(
            "https://web.archive.org/web/20250906120000id_/https://www.theregister.com/a/"
        ).mock(return_value=httpx.Response(200, text=PAGE))
        async with httpx.AsyncClient() as client:
            page = await read_capture(fetcher(client, tmp_path), cc_row)
            assert data.calls.last.request.headers["Range"] == "bytes=1234-6911"
            assert isinstance(page, Page) and b"og:title" in page.body
            from dataclasses import replace

            wayback = replace(cc_row, source="wayback", url="https://www.theregister.com/a/")
            assert (await read_capture(fetcher(client, tmp_path), wayback)).status == 200
            assert raw.called


async def _rows(tmp_path: Path) -> list[Capture]:
    with respx.mock() as router:
        router.get(url__startswith=CC_INDEX_URL.format(crawl="CC-MAIN-2025-38")).mock(
            return_value=httpx.Response(200, text=json.dumps(CC_ROWS[0]) + "\n")
        )
        async with httpx.AsyncClient() as client:
            page = await cc_captures(fetcher(client, tmp_path / "rows"), "CC-MAIN-2025-38", "x/", 0)
            return page.captures


async def test_a_page_cut_off_by_the_index_keeps_its_whole_lines(tmp_path: Path) -> None:
    whole = json.dumps(CC_ROWS[0])
    with respx.mock() as router:
        route = router.get(url__startswith=CC_INDEX_URL.format(crawl="CC-MAIN-2025-38")).mock(
            return_value=httpx.Response(200, text=f"{whole}\n{whole}\n{whole[:90]}")
        )
        async with httpx.AsyncClient() as client:
            page = await cc_captures(fetcher(client, tmp_path), "CC-MAIN-2025-38", "x/", 0)
    assert page.truncated and len(page.captures) == 2 and route.call_count == 1
