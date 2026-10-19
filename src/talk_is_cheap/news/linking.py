"""Link articles to companies by the names, aliases and tickers in their text.

Names and aliases match as whole words in their configured spelling, in Title Case or in
UPPER CASE ("Nvidia", "NVIDIA"), never in lower case ("apple", "meta"). A hyphen is a word
boundary ("Nvidia-backed", "AMD-powered", "Arm-Qualcomm", "All-AMD"), and a possessive
("Nvidia's") matches. A name only seen after "ex-" ("Ex-Microsoft exec") is a former
connection and links with CONFIDENCE_FORMER. A number joined to a name is a measure
("7-Tesla MRI"), and compounds where a name means something else ("Meta-analysis") are
excluded phrases. Upper case of a name
shorter than SHORT_NAME letters reads as an acronym ("META tags"), so it only counts in
an all-caps headline. Phrases where a name means something else ("Big Apple", "Amazon
rainforest") are configured per company and removed before matching.

Tickers match in market notation anywhere ("$NVDA", "(NASDAQ: NVDA)", "NYSE:CRM"). A
bare ticker matches only in upper case, outside an all-caps headline, and only when it
is not also an everyday word or acronym (COMMON_WORD_TICKERS).

A link's confidence depends on where the evidence is: a headline mention is strong, a
summary-only mention weaker, and a per-company feed alone weakest, since such feeds also
carry other stories (Yahoo's carry articles about other companies; Google News searches
ignore case and return "apple festival" and "intel officials"). A publisher whose name
starts with the company's name ("Netflix", "Cisco Talos Blog", "Google DeepMind") is the
company itself. When the text uses one of a company's excluded phrases ("Cisco Aguilar",
"Amazon rainforest"), a feed alone does not link it.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from talk_is_cheap.universe import UniverseCompany

# Links at or above this confidence count as high confidence.
HIGH_CONFIDENCE = 0.8

CONFIDENCE_TICKER_NOTATION = 0.95
CONFIDENCE_HEADLINE = 0.9
CONFIDENCE_BARE_TICKER = 0.8
CONFIDENCE_SUMMARY = 0.6
CONFIDENCE_PUBLISHER = 0.9
CONFIDENCE_FORMER = 0.6
# Added when a per-company feed agrees with a mention in the text.
FEED_AGREEMENT_BONUS = 0.1

# Tickers that are also ordinary words or acronyms ("CRM software", "META tags"); they
# only match in market notation.
COMMON_WORD_TICKERS = frozenset({"CRM", "META", "AI", "IT", "ON", "ALL", "NOW", "ARE"})

SHORT_NAME = 5

_EXCHANGES = r"(?:NASDAQ|NYSE|Nasdaq|NasdaqGS|NasdaqGM|AMEX)"


@dataclass(frozen=True, slots=True)
class Link:
    ticker: str
    method: str  # issuer | ticker | headline | summary | feed
    confidence: float


@dataclass(frozen=True, slots=True)
class _Pattern:
    ticker: str
    names: re.Pattern[str]
    names_in_caps: re.Pattern[str]  # for all-caps text
    former: re.Pattern[str]  # "Ex-Microsoft"
    publisher: re.Pattern[str]  # a publisher name that starts with the company's name
    excluded: re.Pattern[str] | None
    notation: re.Pattern[str]
    bare: re.Pattern[str] | None


class CompanyMatcher:
    """Finds the companies of a universe mentioned in a headline and summary."""

    def __init__(self, companies: Sequence[UniverseCompany]) -> None:
        self._patterns = [_compile(c) for c in companies if c.names]

    def link(
        self,
        title: str,
        summary: str | None,
        publisher: str | None,
        feed_links: Iterable[Link] = (),
    ) -> list[Link]:
        """All links of an article: from its text, its publisher and the per-company feeds
        that supplied it, one per company, best first."""
        found = {link.ticker: link for link in self.match(title, summary)}
        excluded = set()
        for pattern in self._patterns:
            if publisher and _publishes(pattern, publisher):
                current = found.get(pattern.ticker)
                if current is None or current.confidence < CONFIDENCE_PUBLISHER:
                    found[pattern.ticker] = Link(pattern.ticker, "issuer", CONFIDENCE_PUBLISHER)
            if pattern.excluded is not None and pattern.ticker not in found:
                text = f"{title}\n{summary or ''}"
                if pattern.excluded.search(text):
                    excluded.add(pattern.ticker)
        feeds = [f for f in feed_links if f.method == "issuer" or f.ticker not in excluded]
        return combine(found.values(), feeds)

    def match(self, title: str, summary: str | None = None) -> list[Link]:
        """Links found in the text alone, one per company, best evidence first."""
        links = []
        headline_caps = _is_all_caps(title)
        for pattern in self._patterns:
            in_title = _mentions(pattern, title, caps=headline_caps)
            in_summary = bool(summary) and _mentions(pattern, summary or "", caps=False)
            if in_title is not None:
                links.append(Link(pattern.ticker, *in_title))
            elif in_summary:
                links.append(Link(pattern.ticker, "summary", CONFIDENCE_SUMMARY))
        return links


def combine(text_links: Iterable[Link], feed_links: Iterable[Link]) -> list[Link]:
    """One link per company from text matches and per-company feeds.

    A feed link raises a text match by FEED_AGREEMENT_BONUS; an issuer feed always wins.
    """
    text = {link.ticker: link for link in text_links}
    best = dict(text)
    for feed_link in feed_links:
        current = best.get(feed_link.ticker)
        mention = text.get(feed_link.ticker)
        if feed_link.method == "issuer" or mention is None:
            if current is None or feed_link.confidence > current.confidence:
                best[feed_link.ticker] = feed_link
        elif current is mention:  # the first feed that agrees; later ones add nothing
            boosted = min(1.0, round(mention.confidence + FEED_AGREEMENT_BONUS, 2))
            best[feed_link.ticker] = Link(mention.ticker, mention.method, boosted)
    return sorted(best.values(), key=lambda link: (-link.confidence, link.ticker))


def _publishes(pattern: _Pattern, publisher: str) -> bool:
    cleaned = pattern.excluded.sub(" ", publisher) if pattern.excluded else publisher
    return bool(pattern.publisher.match(cleaned.strip()))


def _mentions(pattern: _Pattern, text: str, *, caps: bool) -> tuple[str, float] | None:
    if not text:
        return None
    if pattern.notation.search(text):
        return "ticker", CONFIDENCE_TICKER_NOTATION
    cleaned = pattern.excluded.sub(" ", text) if pattern.excluded else text
    current = pattern.former.sub(" ", cleaned)
    if (pattern.names_in_caps if caps else pattern.names).search(current):
        return "headline", CONFIDENCE_HEADLINE
    if pattern.former.search(cleaned):
        return "headline", CONFIDENCE_FORMER
    if not caps and pattern.bare is not None and pattern.bare.search(text):
        return "ticker", CONFIDENCE_BARE_TICKER
    return None


def _compile(company: UniverseCompany) -> _Pattern:
    spellings: set[str] = set()
    caps: set[str] = set()
    for name in company.names:
        spellings |= {name, name[0].upper() + name[1:]}
        (spellings if len(name) >= SHORT_NAME else caps).add(name.upper())
    ticker = re.escape(company.ticker)
    excluded = None
    if company.exclude_phrases:
        phrases = "|".join(re.escape(p) for p in company.exclude_phrases)
        excluded = re.compile(rf"(?<!\w)(?:{phrases})(?!\w)", re.IGNORECASE)
    bare = None
    if company.ticker not in COMMON_WORD_TICKERS and len(company.ticker) >= 3:
        bare = re.compile(rf"(?<![\w$.-]){ticker}(?![\w-])")
    return _Pattern(
        ticker=company.ticker,
        names=_whole_words(spellings),
        publisher=re.compile(rf"(?:{_alternatives(spellings)})(?![\w-])"),
        names_in_caps=_whole_words(spellings | caps),
        former=re.compile(
            rf"(?<![\w@.])(?:ex|Ex|EX)-(?:{_alternatives(spellings | caps)})(?!\w|\.\w)"
        ),
        excluded=excluded,
        notation=re.compile(rf"(?:\${ticker}|\({ticker}\)|{_EXCHANGES}\s*:\s*{ticker})(?![\w-])"),
        bare=bare,
    )


def _alternatives(spellings: set[str]) -> str:
    return "|".join(re.escape(s) for s in sorted(spellings, key=len, reverse=True))


def _whole_words(spellings: set[str]) -> re.Pattern[str]:
    # Not inside a word, an e-mail address or a domain; a hyphen is a boundary, except
    # after a number, which makes a measure ("7-Tesla MRI").
    return re.compile(rf"(?<![\w@.])(?<!\d-)(?:{_alternatives(spellings)})(?!\w|\.\w)")


def _is_all_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.8
