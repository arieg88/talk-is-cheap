"""Feed adapters: one class per kind of source, configured in feeds.yaml.

An adapter fetches its feed and returns raw items; normalization, deduplication, company
linking and storage are the same for every source. Each class registers itself under its
`type`, so a new kind of source is a new class here plus entries in feeds.yaml.

Fetching news is not SEC access: adapters use httpx directly, with a descriptive
User-Agent, timeouts and conditional requests (ETag / Last-Modified). Most feeds send no
validators; a feed whose items are the same as last time is skipped before any database
work.
"""

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import quote_plus

import httpx
import yaml
from pydantic import BaseModel, Field, model_validator

from talk_is_cheap.news.normalize import domain
from talk_is_cheap.news.records import FetchResult, RawItem, Validators
from talk_is_cheap.universe import UniverseCompany

# Seconds; a slow feed fails on its own instead of holding up the poll.
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class FeedError(Exception):
    """A feed could not be fetched or read."""


class FeedEntry(BaseModel):
    """One entry of feeds.yaml."""

    type: str
    url: str = ""
    companies: Literal["all"] | None = None
    ticker: str | None = None
    name: str | None = None
    publisher: str | None = None
    domains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_scope(self) -> "FeedEntry":
        scopes = [s for s in (self.companies, self.ticker, self.name) if s]
        if len(scopes) != 1:
            raise ValueError(f"{self.type}: give exactly one of companies, ticker or name")
        if self.ticker:
            self.ticker = self.ticker.strip().upper()
        return self


class FeedsFile(BaseModel):
    feeds: list[FeedEntry] = Field(default_factory=list)
    backfill: list[FeedEntry] = Field(default_factory=list)


def load_feeds_file(path: Path) -> FeedsFile:
    with path.open(encoding="utf-8") as handle:
        return FeedsFile.model_validate(yaml.safe_load(handle) or {})


def company_query(company: UniverseCompany) -> str:
    """The company's name and aliases as a search query: "Alphabet" OR "Google"."""
    return " OR ".join(f'"{name}"' for name in company.names)


class FeedAdapter(ABC):
    """A source of news items, polled as one feed."""

    type: ClassVar[str]
    # For a feed about one company, the link its items get from the feed alone, as
    # (method, confidence). None for market-wide feeds.
    feed_link: ClassVar[tuple[str, float] | None] = None

    def __init__(
        self, key: str, url: str, ticker: str | None = None, publisher: str | None = None
    ) -> None:
        self.key = key
        self.url = url
        self.ticker = ticker
        self.publisher = publisher

    @classmethod
    def from_entry(cls, entry: FeedEntry, companies: Sequence[UniverseCompany]) -> list[Any]:
        """The feeds an entry describes, for the selected companies."""
        if entry.name:
            return [cls(f"{cls.type}:{entry.name}", entry.url, publisher=entry.publisher)]
        selected = {c.ticker: c for c in companies}
        if entry.ticker:
            if entry.ticker not in selected:
                return []
            targets = [selected[entry.ticker]]
        else:
            targets = list(companies)
        return [
            cls(
                f"{cls.type}:{c.ticker}",
                entry.url.format(ticker=quote_plus(c.ticker), query=quote_plus(company_query(c))),
                ticker=c.ticker,
                publisher=entry.publisher,
            )
            for c in targets
        ]

    async def fetch(self, client: httpx.AsyncClient, validators: Validators) -> FetchResult:
        headers = {}
        if validators.etag:
            headers["If-None-Match"] = validators.etag
        if validators.last_modified:
            headers["If-Modified-Since"] = validators.last_modified
        try:
            response = await client.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            raise FeedError(f"{type(exc).__name__}: {exc}".rstrip(": ")) from exc
        if response.status_code == 304:
            return FetchResult(not_modified=True, validators=validators)
        if response.status_code != 200:
            raise FeedError(f"HTTP {response.status_code}")

        items = self.parse(response.content)
        fresh = Validators(
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            items_hash=items_hash(items),
        )
        if fresh.items_hash == validators.items_hash:
            return FetchResult(not_modified=True, validators=fresh)
        return FetchResult(items=items, validators=fresh)

    @abstractmethod
    def parse(self, body: bytes) -> list[RawItem]:
        """Items of a feed document."""


def items_hash(items: Sequence[RawItem]) -> str:
    """A fingerprint of a feed's items, independent of their order and of the rest of the
    document (Yahoo stamps every response with a new lastBuildDate)."""
    lines = sorted("\x1f".join((i.link, i.title, i.published or "")) for i in items)
    return hashlib.sha256("\x1e".join(lines).encode("utf-8")).hexdigest()


_REGISTRY: dict[str, type[FeedAdapter]] = {}


def register(cls: type[FeedAdapter]) -> type[FeedAdapter]:
    _REGISTRY[cls.type] = cls
    return cls


def feed_links() -> dict[str, tuple[str, float]]:
    """The feed-only link of each feed type that has one."""
    return {t: cls.feed_link for t, cls in _REGISTRY.items() if cls.feed_link is not None}


def build_feeds(
    entries: Sequence[FeedEntry],
    companies: Sequence[UniverseCompany],
    *,
    company_feeds_only: bool = False,
) -> list[FeedAdapter]:
    """Adapters for the feeds in feeds.yaml. With `company_feeds_only`, market-wide feeds
    are left out (a poll restricted to some tickers)."""
    feeds: list[FeedAdapter] = []
    for entry in entries:
        cls = _REGISTRY.get(entry.type)
        if cls is None:
            raise ValueError(f"unknown feed type {entry.type!r} in feeds file")
        if company_feeds_only and entry.name:
            continue
        feeds.extend(cls.from_entry(entry, companies))
    keys = [f.key for f in feeds]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"duplicate feeds: {', '.join(duplicates)}")
    return feeds


def parse_feed(body: bytes) -> Any:
    """A parsed RSS or Atom document; FeedError when it is not one."""
    import feedparser  # slow to import; only polling needs it

    parsed = feedparser.parse(body)
    if not parsed.get("version") and not parsed.entries:
        raise FeedError("not an RSS or Atom feed")
    return parsed


def _malformed(parsed: Any) -> FeedError:
    reason = parsed.get("bozo_exception")
    return FeedError(f"malformed feed: {reason}" if reason else "malformed feed")


def _entry_time(entry: Any) -> str | None:
    value = entry.get("published") or entry.get("updated")
    return str(value) if value else None


def _entry_summary(entry: Any) -> str | None:
    summary = entry.get("summary")
    if not summary and entry.get("content"):
        summary = entry["content"][0].get("value")
    return str(summary) if summary else None


def _items(body: bytes, publisher: Any) -> list[RawItem]:
    parsed = parse_feed(body)
    items = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        items.append(
            RawItem(
                title=str(title),
                link=str(link),
                summary=_entry_summary(entry),
                published=_entry_time(entry),
                publisher=publisher(entry, parsed),
            )
        )
    # feedparser salvages what it can from broken XML; nothing usable is an error, while
    # a well-formed feed may simply be empty.
    if parsed.bozo and not items:
        raise _malformed(parsed)
    return items


@register
class RssFeed(FeedAdapter):
    """A market-wide RSS or Atom feed (MarketWatch, CNBC)."""

    type = "rss"

    def parse(self, body: bytes) -> list[RawItem]:
        return _items(body, lambda _entry, feed: self.publisher or feed.feed.get("title"))


@register
class PressReleaseFeed(FeedAdapter):
    """A company's own newsroom or investor-relations feed: the company is the issuer."""

    type = "press_releases"
    feed_link = ("issuer", 1.0)

    def parse(self, body: bytes) -> list[RawItem]:
        return _items(body, lambda _entry, feed: self.publisher or feed.feed.get("title"))


@register
class YahooFinanceFeed(FeedAdapter):
    """Yahoo Finance headlines per ticker. Items link to the publisher's site and many are
    about other companies, so the feed alone is weak evidence."""

    type = "yahoo_finance"
    feed_link = ("feed", 0.3)

    def parse(self, body: bytes) -> list[RawItem]:
        return _items(body, lambda entry, _feed: domain(str(entry.get("link", ""))) or None)


@register
class GoogleNewsFeed(FeedAdapter):
    """Google News search for a company's names. Links are Google redirect URLs; the
    publisher is the item's <source>, and titles end in " - Publisher"."""

    type = "google_news"
    # The search ignores case: "Apple" also finds the apple festival.
    feed_link = ("feed", 0.3)

    def parse(self, body: bytes) -> list[RawItem]:
        def publisher(entry: Any, _feed: Any) -> str | None:
            source = entry.get("source") or {}
            title = source.get("title")
            return str(title) if title else None

        return _items(body, publisher)
