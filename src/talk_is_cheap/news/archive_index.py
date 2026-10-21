"""Web archives: the Common Crawl URL index and the Wayback Machine CDX API, the
captures they hold, and the pages read from them.

- Common Crawl (`index.commoncrawl.org`): one index per crawl, listed in collinfo.json.
  A prefix query is answered in pages of one index block (`showNumPages`, `pageSize`);
  a page is filtered on the server
  by status and MIME type (the index ignores URL filters). A capture is read with one
  HTTP range request for its WARC record at `data.commoncrawl.org` (no S3 access).
- Wayback Machine (`web.archive.org/cdx/search/cdx`): one query per URL prefix and
  month, filtered on the server by status, MIME type and a regular expression over the
  URL, collapsed to one row per URL, with only the timestamp and URL returned; a prefix
  without a date in its path is limited to the month's captures by `from`/`to`
  (YYYYMM). A month too large to answer (504) is read in the index's pages
  (`showNumPages`, then `page`). A capture is read raw (the `id_` URL), as the
  publisher served it.

Requests go through `http_cache.PoliteFetcher` with a disk cache: Common Crawl at most
one a second, with backoff on 429 and 5xx; the Wayback Machine at the pace the Internet
Archive asks clients to keep (the `wayback` library's limits: a CDX search every 2.5
seconds, 80% of 30 a minute; up to 8 archived pages a second), with waits matched to
each failure (`WAYBACK_RETRY`); when the archive seems to block us, searches pause
until a one-row probe query is answered.
"""

import gzip
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

from lxml import etree
from lxml import html as lxml_html

from talk_is_cheap.news.http_cache import CachedResponse, PoliteFetcher, RetryPolicy
from talk_is_cheap.news.normalize import parse_timestamp

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
CC_INDEX_URL = "https://index.commoncrawl.org/{crawl}-index"
CC_DATA_URL = "https://data.commoncrawl.org/{filename}"
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_RAW_URL = "https://web.archive.org/web/{timestamp}id_/{url}"
COMMON_CRAWL = "commoncrawl"
WAYBACK = "wayback"
CC_INTERVAL = 1.0  # seconds between requests to Common Crawl
# Index blocks per page (about 3,000 captures each). The index cuts answers off at about
# 960 KB: a page of the default five blocks never arrives whole for a large site.
CC_PAGE_SIZE = 1
WAYBACK_INTERVAL = 2.5  # seconds between CDX searches (80% of 30 a minute)
WAYBACK_PAGE_INTERVAL = 0.125  # seconds between archived page reads (8 a second)
# 503: at once, then after 2, 4 and 8 seconds. 504: not retried (the search reads the
# month in pages instead). 429: once, after Retry-After or 60 seconds. 3 connection
# timeouts in a row: searches pause, and `WAYBACK_PROBE` is sent once a minute until it
# is answered.
WAYBACK_RETRY = RetryPolicy()
# The smallest CDX query: one row of one URL.
WAYBACK_PROBE = (WAYBACK_CDX_URL, {"url": "archive.org", "output": "json", "limit": "1"})


class ArchiveError(Exception):
    """An index or capture answered something unusable."""


@dataclass(frozen=True, slots=True)
class Crawl:
    id: str  # CC-MAIN-2024-22
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class Capture:
    """One archived response for a URL."""

    source: str  # commoncrawl or wayback
    url: str  # as captured
    timestamp: datetime
    status: int
    mime: str
    # Common Crawl: where the WARC record is.
    filename: str | None = None
    offset: int | None = None
    length: int | None = None


@dataclass(frozen=True, slots=True)
class Page:
    status: int
    body: bytes
    content_type: str | None


async def crawls(fetcher: PoliteFetcher, since: date, today: date) -> list[Crawl]:
    """Crawls that ran on or after `since`, oldest first. The list changes when a crawl
    is published, so it is cached per day."""
    response = await fetcher.get(COLLINFO_URL, params={"day": today.isoformat()})
    if response.status != 200:
        raise ArchiveError(f"collinfo.json: HTTP {response.status}")
    found = []
    for entry in response.json():
        start = _iso_date(entry.get("from"))
        end = _iso_date(entry.get("to"))
        if start is None or end is None or end < since:
            continue
        found.append(Crawl(str(entry["id"]), start, end))
    return sorted(found, key=lambda c: c.start)


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


async def cc_pages(fetcher: PoliteFetcher, crawl: str, prefix: str) -> int:
    """How many index pages a prefix query has in one crawl (0 when nothing is
    captured under it)."""
    response = await fetcher.get(
        CC_INDEX_URL.format(crawl=crawl),
        params={
            "url": prefix,
            "matchType": "prefix",
            "output": "json",
            "pageSize": str(CC_PAGE_SIZE),
            "showNumPages": "true",
        },
        accept=_json_answer,
    )
    if response.status == 404:
        return 0
    if response.status != 200:
        raise ArchiveError(f"{crawl} {prefix}: HTTP {response.status}")
    try:
        return int(response.json().get("pages", 0))
    except (ValueError, AttributeError) as exc:
        raise ArchiveError(f"{crawl} {prefix}: {response.text[:200]}") from exc


@dataclass(frozen=True, slots=True)
class CcPage:
    captures: list[Capture]
    truncated: bool  # cut off by the index; the captures after the cut are missing


async def cc_captures(fetcher: PoliteFetcher, crawl: str, prefix: str, page: int) -> CcPage:
    """Successful HTML captures on one index page. The index filters by status and MIME
    type but not by URL (a URL filter answers "No Captures found"), so URLs are
    filtered by the caller."""
    params = [
        ("url", prefix),
        ("matchType", "prefix"),
        ("output", "json"),
        ("page", str(page)),
        ("pageSize", str(CC_PAGE_SIZE)),
        ("fl", "url,timestamp,filename,offset,length"),
        ("filter", "status:200"),
        ("filter", "mime:text/html"),
    ]
    response = await fetcher.get(
        CC_INDEX_URL.format(crawl=crawl) + "?" + _encode(params), accept=_json_lines_answer
    )
    if response.status == 404:  # nothing on the page passes the filters
        return CcPage([], truncated=False)
    if response.status != 200:
        raise ArchiveError(f"{crawl} {prefix} page {page}: HTTP {response.status}")
    captures = []
    truncated = False
    for line in response.text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            truncated = True  # the last line, cut off (`_json_lines_answer`)
            continue
        capture = _cc_capture(row)
        if capture is not None:
            captures.append(capture)
    return CcPage(captures, truncated)


async def cc_lookup(fetcher: PoliteFetcher, crawl: str, url: str) -> Capture | None:
    """A successful HTML capture of exactly this URL in one crawl, or None."""
    params = [
        ("url", url),
        ("output", "json"),
        ("limit", "1"),
        ("fl", "url,timestamp,filename,offset,length"),
        ("filter", "status:200"),
        ("filter", "mime:text/html"),
    ]
    response = await fetcher.get(
        CC_INDEX_URL.format(crawl=crawl) + "?" + _encode(params), accept=_json_lines_answer
    )
    if response.status == 404:
        return None
    if response.status != 200:
        raise ArchiveError(f"{crawl} {url}: HTTP {response.status}")
    for line in response.text.splitlines():
        try:
            capture = _cc_capture(json.loads(line))
        except json.JSONDecodeError:
            continue
        if capture is not None:
            return capture
    return None


def _cc_capture(row: dict[str, Any]) -> Capture | None:
    try:
        return Capture(
            source=COMMON_CRAWL,
            url=str(row["url"]),
            timestamp=_timestamp(str(row["timestamp"])),
            # The query asks only for successful HTML captures.
            status=int(row.get("status", 200)),
            mime=str(row.get("mime", "text/html")),
            filename=str(row["filename"]),
            offset=int(row["offset"]),
            length=int(row["length"]),
        )
    except (KeyError, ValueError):
        return None


def wayback_query(
    prefix: str, url_regex: str, month: date | None = None, page: int | None = None
) -> list[tuple[str, str]]:
    """The CDX query of a prefix: successful HTML captures whose URL matches
    `url_regex`, one per URL (`collapse=urlkey`), only the fields read (timestamp and
    URL); captured in `month` when given (`from` and `to`, YYYYMM: the whole month);
    one page of the index when `page` is given."""
    params = [
        ("url", prefix),
        ("matchType", "prefix"),
        ("output", "json"),
        ("fl", "timestamp,original"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
        ("filter", f"original:{url_regex}"),
        ("collapse", "urlkey"),
    ]
    if month is not None:
        params += [("from", f"{month:%Y%m}"), ("to", f"{month:%Y%m}")]
    if page is not None:
        params.append(("page", str(page)))
    return params


def wayback_pages_query(prefix: str) -> list[tuple[str, str]]:
    """The query for how many index pages a prefix has. The count is of the prefix's
    index blocks, whatever the filters and capture times."""
    return [("url", prefix), ("matchType", "prefix"), ("showNumPages", "true")]


async def wayback_pages(fetcher: PoliteFetcher, prefix: str) -> int:
    """How many index pages a prefix query has (0 when nothing is captured under it).
    Not cached: the count grows as the archive does."""
    response = await fetcher.get(
        WAYBACK_CDX_URL + "?" + _encode(wayback_pages_query(prefix)),
        cache=False,
        accept=lambda r: r.status != 200 or r.text.strip().isdigit(),
    )
    if response.status == 404:
        return 0
    if response.status != 200:
        raise ArchiveError(f"Wayback {prefix} pages: HTTP {response.status}")
    return int(response.text.strip())


async def wayback_captures(
    fetcher: PoliteFetcher,
    prefix: str,
    url_regex: str,
    month: date | None = None,
    page: int | None = None,
) -> list[Capture]:
    """The captures `wayback_query` asks for. The server filters status and MIME type,
    so every row is a successful HTML capture."""
    params = wayback_query(prefix, url_regex, month, page)
    response = await fetcher.get(WAYBACK_CDX_URL + "?" + _encode(params), accept=_json_answer)
    where = prefix + (f" {month:%Y-%m}" if month else "")
    where += f" page {page}" if page is not None else ""
    if response.status != 200:
        raise ArchiveError(f"Wayback {where}: HTTP {response.status}: {response.text[:200]}")
    body = response.text.strip()
    if not body:
        return []
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"Wayback {where}: {body[:200]}") from exc
    captures = []
    for row in rows:
        if row[:1] == ["timestamp"]:  # the row that names the fields
            continue
        try:
            captures.append(
                Capture(
                    source=WAYBACK,
                    url=str(row[1]),
                    timestamp=_timestamp(str(row[0])),
                    status=200,
                    mime="text/html",
                )
            )
        except (IndexError, ValueError):
            continue
    return captures


def _json_answer(response: CachedResponse) -> bool:
    """An index answer is JSON in full (the archives' maintenance pages are HTML)."""
    body = response.body.strip()
    if not body:
        return True
    try:
        json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def _json_lines_answer(response: CachedResponse) -> bool:
    """A Common Crawl page is JSON lines. The index cuts long answers off at about
    960 KB, mid-line: such a page is kept up to its last whole line (`cc_captures`
    reports it truncated), since asking again gets the same cut."""
    lines = [line for line in response.body.splitlines() if line.strip()]
    try:
        for line in lines[:-1]:
            json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return not lines or lines[-1].lstrip().startswith(b"{")


def _encode(params: Sequence[tuple[str, str]]) -> str:
    return urlencode(list(params))


def _timestamp(value: str) -> datetime:
    """A 14-digit archive timestamp (20240501120919) in UTC."""
    return datetime.strptime(value[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


async def read_capture(fetcher: PoliteFetcher, capture: Capture) -> Page:
    """The page of a capture: the WARC record's HTTP response (Common Crawl), or the raw
    capture (Wayback)."""
    if capture.source == COMMON_CRAWL:
        if capture.filename is None or capture.offset is None or capture.length is None:
            raise ArchiveError(f"no WARC location for {capture.url}")
        end = capture.offset + capture.length - 1
        response = await fetcher.get(
            CC_DATA_URL.format(filename=capture.filename),
            headers={"Range": f"bytes={capture.offset}-{end}"},
            accept=lambda r: r.status != 206 or r.body[:2] == b"\x1f\x8b",
        )
        if response.status not in (200, 206):
            raise ArchiveError(f"WARC record: HTTP {response.status}")
        return parse_warc_record(response.body)
    stamp = capture.timestamp.strftime("%Y%m%d%H%M%S")
    response = await fetcher.get(WAYBACK_RAW_URL.format(timestamp=stamp, url=capture.url))
    return Page(response.status, response.body, response.headers.get("content-type"))


def parse_warc_record(data: bytes) -> Page:
    """The HTTP response inside one gzipped WARC response record. Common Crawl stores
    payloads decoded (the original Transfer-Encoding and Content-Encoding headers are
    renamed), so the body follows the HTTP headers as is."""
    try:
        raw = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise ArchiveError(f"WARC record is not gzip: {exc}") from exc
    warc_end = raw.find(b"\r\n\r\n")
    if not raw.startswith(b"WARC/") or warc_end < 0:
        raise ArchiveError("not a WARC record")
    http = raw[warc_end + 4 :]
    head_end = http.find(b"\r\n\r\n")
    if head_end < 0:
        raise ArchiveError("WARC record holds no HTTP response")
    head = http[:head_end].decode("latin-1").split("\r\n")
    status_line = head[0].split()
    status = int(status_line[1]) if len(status_line) > 1 and status_line[1].isdigit() else 0
    content_type = None
    for line in head[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-type":
            content_type = value.strip()
    body = http[head_end + 4 :]
    # A record ends with two CRLFs after its block.
    return Page(status, body.removesuffix(b"\r\n\r\n"), content_type)


def decode_page(page: Page) -> str:
    """The page as text, in the charset it declares (UTF-8 otherwise)."""
    declared = re.search(r"charset=([\w-]+)", page.content_type or "", re.IGNORECASE)
    charset = declared.group(1) if declared else "utf-8"
    if declared is None:
        in_page = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", page.body[:4000], re.IGNORECASE)
        if in_page is not None:
            charset = in_page.group(1).decode("ascii", errors="ignore")
    try:
        return page.body.decode(charset, errors="replace")
    except LookupError:
        return page.body.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class PageMeta:
    title: str | None
    description: str | None
    published: datetime | None
    date_only: bool  # only the day of `published` is known


_PUBLISHED_META = (
    ("property", "article:published_time"),
    ("property", "og:article:published_time"),
    ("name", "article:published_time"),
    ("itemprop", "datePublished"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
    ("name", "pubdate"),
    ("name", "publish-date"),
    ("name", "date"),
    ("name", "dc.date"),
)
_JSON_LD_PUBLISHED = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def page_metadata(text: str) -> PageMeta:
    """Title, description and publication time from a page's metadata: Open Graph and
    article tags, JSON-LD `datePublished`, then the `<title>`."""
    try:
        tree = lxml_html.fromstring(text)
    except (ValueError, etree.ParserError):
        return PageMeta(None, None, None, False)
    tags: dict[tuple[str, str], str] = {}
    for node in tree.iter("meta"):
        content = (node.get("content") or node.get("datetime") or "").strip()
        if not content:
            continue
        for attribute in ("property", "name", "itemprop"):
            value = node.get(attribute)
            if value:
                tags.setdefault((attribute, value.strip()), content)

    title = tags.get(("property", "og:title")) or tags.get(("name", "twitter:title"))
    if not title:
        found = tree.findtext(".//title")
        title = found.strip() if found and found.strip() else None
    description = tags.get(("property", "og:description")) or tags.get(("name", "description"))
    published: datetime | None = None
    date_only = False
    for key in _PUBLISHED_META:
        if key in tags:
            published, date_only = _published(tags[key])
            if published is not None:
                break
    if published is None:
        for script in tree.iter("script"):
            if script.get("type") != "application/ld+json" or not script.text:
                continue
            match = _JSON_LD_PUBLISHED.search(script.text)
            if match:
                published, date_only = _published(match.group(1))
                if published is not None:
                    break
    return PageMeta(title, description, published, date_only)


def _published(value: str) -> tuple[datetime | None, bool]:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        return datetime(day.year, day.month, day.day, tzinfo=UTC), True
    return parse_timestamp(value), False
