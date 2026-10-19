"""Plain records passed between news adapters, normalization and storage."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class TimePrecision(StrEnum):
    """What an article's `published_at` means."""

    PUBLISHED = "published"  # the publish time the feed states
    SEEN = "seen"  # when a crawler first saw the article (GDELT); later than publishing
    FETCHED = "fetched"  # the feed gave no time; when this system first fetched it


@dataclass(frozen=True, slots=True)
class RawItem:
    """One item as an adapter read it from a feed, before normalization."""

    title: str
    link: str
    summary: str | None = None
    published: str | None = None  # the feed's own timestamp text
    publisher: str | None = None
    time_precision: TimePrecision = TimePrecision.PUBLISHED


@dataclass(frozen=True, slots=True)
class Validators:
    """What a feed's previous response left to compare against: its HTTP cache validators
    and a fingerprint of its items."""

    etag: str | None = None
    last_modified: str | None = None
    items_hash: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A feed poll. `not_modified` means the server answered 304, or sent the same items
    as last time; either way there are no items to process."""

    items: list[RawItem] = field(default_factory=list)
    not_modified: bool = False
    validators: Validators = field(default_factory=Validators)


@dataclass(frozen=True, slots=True)
class Article:
    """A normalized article, ready to store."""

    url: str
    url_hash: str
    title: str
    summary: str | None
    publisher: str | None
    published_at: datetime
    time_precision: TimePrecision
