"""Normalize feed items: canonical URLs, UTC timestamps, plain-text titles and summaries."""

import hashlib
import html
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Summaries are the feed's teaser, kept short; full articles are never stored.
SUMMARY_MAX_CHARS = 500

# Query parameters that identify a campaign, feed or referrer, not the article.
_TRACKING_PARAMS = frozenset(
    {
        "cmpid",
        "cmp",
        "fbclid",
        "feedtype",
        "gclid",
        "guccounter",
        "mc_cid",
        "mc_eid",
        "mod",
        "ncid",
        "oc",
        "ocid",
        "partner",
        "ref",
        "rss",
        "smid",
        "soc_src",
        "soc_trk",
        "source",
        "sr_share",
        "taid",
        "tsrc",
        ".tsrc",
        "yptr",
    }
)
_TRACKING_PREFIXES = ("utm_", "guce_", "__")
_DEFAULT_PORTS = {"http": 80, "https": 443}

_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(?:p|br|div|li|ul|ol|h[1-6]|tr|table|blockquote)\b[^>]*>", re.I)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_SPACE = re.compile(r"\s+")
_GDELT_SEEN = re.compile(r"^(\d{8})T(\d{6})Z$")

# Outlets whose names trail headlines (compared lower-case, punctuation as spaces).
KNOWN_PUBLISHERS = frozenset(
    {
        "ap",
        "ap news",
        "appleinsider",
        "ars technica",
        "associated press",
        "axios",
        "barron s",
        "bbc",
        "bbc news",
        "benzinga",
        "bloomberg",
        "business insider",
        "cnbc",
        "cnn",
        "cnn business",
        "engadget",
        "financial times",
        "forbes",
        "fortune",
        "investing com",
        "investopedia",
        "macrumors",
        "marketbeat",
        "marketwatch",
        "microsoft devices blog",
        "nvidia blog",
        "nvidia technical blog",
        "pcworld",
        "reuters",
        "seeking alpha",
        "techcrunch",
        "the guardian",
        "the information",
        "the motley fool",
        "the new york times",
        "the register",
        "the telegraph",
        "the verge",
        "the wall street journal",
        "the washington post",
        "thestreet",
        "tipranks",
        "tom s hardware",
        "venturebeat",
        "wired",
        "wsj",
        "yahoo finance",
        "zacks",
        "zdnet",
    }
)
# Words a publisher adds to its name in a suffix ("CNN Business", "GSMArena.com news").
_PUBLISHER_WORDS = frozenset({"news", "business", "blog", "online", "com", "newsroom", "tech"})
_DOMAIN_ENDINGS = frozenset({"com", "net", "org", "co", "io", "uk", "news"})
_TITLE_SUFFIX = re.compile(r"\s+[-|\u2013\u2014]\s+([^-|\u2013\u2014]{2,60})$")
# Mathematical alphanumerics and full-width Latin letters and digits.
_STYLED_CHARACTERS = re.compile(r"[\U0001d400-\U0001d7ff\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")
_STREAM_SPAM = re.compile(
    r"\+\+\s*\[|\[[^\]]*watch[^\]]*\]|!\]"
    r"|\b(?:live\s*-?\s*stream(?:ing)?|livestream)\b.*\b(?:free|reddit|online|hd)\b"
    r"|\b(?:free|reddit)\b.*\b(?:live\s*-?\s*stream|livestream)",
    re.IGNORECASE,
)


def canonical_url(url: str) -> str:
    """The URL without tracking parameters, fragment or default port, with a lower-case
    scheme and host and the remaining parameters sorted."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit((scheme, host, parts.path or "/", urlencode(sorted(query)), ""))


def url_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def domain(url: str) -> str:
    """The host of a URL without a leading "www."."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def parse_timestamp(text: str | None) -> datetime | None:
    """A feed or API timestamp as an aware UTC datetime.

    Accepts RFC 822 dates from RSS (including two-digit years, "Mon, 12 Oct 26 09:00:00
    -0400"), ISO 8601 from Atom and GDELT's "20261001T120000Z". A time without a zone is
    taken as UTC.
    """
    if not text or not text.strip():
        return None
    value = text.strip()
    gdelt = _GDELT_SEEN.match(value)
    if gdelt:
        return datetime.strptime("".join(gdelt.groups()), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    parsed: datetime | None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def plain_text(markup: str | None) -> str:
    """Text of an HTML fragment: tags removed, entities decoded, whitespace collapsed."""
    if not markup:
        return ""
    text = _SCRIPT.sub(" ", markup)
    text = _BLOCK_TAG.sub(" ", text)
    text = _TAG.sub("", text)
    # Feeds sometimes escape twice ("&amp;nbsp;").
    text = html.unescape(html.unescape(text))
    return _SPACE.sub(" ", text.replace("\xa0", " ")).strip()


def clean_summary(markup: str | None, title: str = "") -> str | None:
    """The summary as plain text, capped at SUMMARY_MAX_CHARS on a word boundary.

    None when nothing is left, or when the summary only repeats the title (Google News
    summaries are the title and the publisher).
    """
    text = plain_text(markup)
    if not text or (title and _comparable(text).startswith(_comparable(title))):
        return None
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    cut = text[: SUMMARY_MAX_CHARS - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def clean_title(markup: str | None, publisher: str | None = None) -> str:
    """The headline as plain text, without a trailing " - Publisher" or " | Publisher".

    The suffix is removed when it names the item's publisher, alone or with a word such
    as "news" or "business" ("| CNN Business" from CNN, "- GSMArena.com news"), or a
    publisher in KNOWN_PUBLISHERS ("| NVIDIA Technical Blog" from NVIDIA Developer).
    Other trailing text ("- Here's Why", "- Oracle (NYSE:ORCL)") is part of the title.
    """
    title = plain_text(markup)
    if publisher:  # a publisher whose name has a dash in it ("LBB - Little Black Book")
        for separator in (" - ", " | ", " \u2013 ", " \u2014 "):
            suffix = f"{separator}{publisher}"
            if title.endswith(suffix) and len(title) - len(suffix) >= 10:
                return title[: -len(suffix)].rstrip()
    match = _TITLE_SUFFIX.search(title)
    if match and match.start() >= 10 and _names_publisher(match.group(1), publisher):
        return title[: match.start()].rstrip()
    return title


def is_spam(title: str) -> bool:
    """Whether a headline is spam: styled characters (mathematical bold "LIVE", full-width
    "Free"), bracketed watch tags ("++[Netflix-Watch]TV!]") or live-stream bait."""
    return bool(_STYLED_CHARACTERS.search(title) or _STREAM_SPAM.search(title))


def _names_publisher(suffix: str, publisher: str | None) -> bool:
    words = _name_words(suffix)
    if " ".join(words) in KNOWN_PUBLISHERS:
        return True
    if not publisher:
        return False
    name = _name_words(publisher)
    head, rest = words[: len(name)], words[len(name) :]
    return bool(name) and head == name and all(w in _PUBLISHER_WORDS for w in rest)


def _name_words(name: str) -> list[str]:
    """A publisher's name as comparable words; a domain loses "www." and its ending
    ("www.chosunbiz.com" is "chosunbiz"), a name keeps all its words ("Computing UK")."""
    text = name.strip().lower().removeprefix("www.")
    words = _comparable(text).split()
    if "." in text and " " not in text:
        while len(words) > 1 and words[-1] in _DOMAIN_ENDINGS:
            words.pop()
    return words


def _comparable(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()
