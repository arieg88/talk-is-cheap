"""Group near-duplicate headlines (the same story from several outlets or feeds).

Exact duplicates are caught earlier by the canonical URL hash. Here, two headlines are
the same story when their normalized texts score at least NEAR_DUPLICATE_THRESHOLD
(rapidfuzz, the better of plain and token-sorted ratio, 0-100), they state the same
numbers, and they were published within CLUSTER_WINDOW of each other. The threshold was
set on real pairs from Yahoo Finance and Google News (tests/test_news_dedup.py): rewrites
of one wire story score 86-99, while different stories in one template ("NVIDIA
Announces New Investments in France's Startups" / "... in the UK Startup Ecosystem")
reach 83. Numbers keep apart templated stories that differ only in an amount ("Commits
$250 Million" / "Commits $1 Billion"). Very short headlines must match exactly.

Headlines that name different companies are never merged by similarity: "Should You Buy
Adobe Stock For Its Cash?" and "Should You Buy Qualcomm Stock For Its Cash?" score 90 but
are two stories. The same title from several publishers is always one story.
"""

import re
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass, field
from datetime import datetime, timedelta

NEAR_DUPLICATE_THRESHOLD = 85.0
CLUSTER_WINDOW = timedelta(hours=48)
# Headlines with fewer words only cluster when their normalized texts are equal.
MIN_FUZZY_WORDS = 4

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NOT_WORD = re.compile(r"[^\w$%]+")
# Wire credit appended by aggregators ("... Telegraph reports By Reuters").
_CREDIT = re.compile(r"\s+by\s+(?:reuters|bloomberg|the associated press|ap)$")


def headline_key(title: str) -> str:
    """The headline lower-cased, without punctuation or a trailing wire credit."""
    text = title.lower().replace("\u2019", "'").replace("'s ", " ")
    text = _NOT_WORD.sub(" ", text).strip()
    return _CREDIT.sub("", text)


def similarity(a: str, b: str) -> float:
    """Similarity of two headline keys, 0-100."""
    from rapidfuzz import fuzz  # only clustering needs it

    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))


def is_near_duplicate(a: str, b: str) -> bool:
    """Whether two headlines tell the same story (publish times aside)."""
    return _keys_match(headline_key(a), headline_key(b))


def _keys_match(key_a: str, key_b: str) -> bool:
    if key_a == key_b:
        return True
    if min(len(key_a.split()), len(key_b.split())) < MIN_FUZZY_WORDS:
        return False
    if sorted(_NUMBER.findall(key_a)) != sorted(_NUMBER.findall(key_b)):
        return False
    return similarity(key_a, key_b) >= NEAR_DUPLICATE_THRESHOLD


@dataclass(order=True, slots=True)
class _Member:
    published_at: datetime
    cluster_id: int = field(compare=False)
    key: str = field(compare=False)
    companies: frozenset[str] = field(compare=False)


class Clusterer:
    """Assigns articles to clusters against the articles already known.

    Load it with the stored articles around the new ones' publish times; then, for each
    new article in turn, `find` its cluster and `add` it so later articles can join it.
    `companies` are the tickers a headline names (`CompanyMatcher.match` on the title).
    """

    def __init__(self) -> None:
        self._members: list[_Member] = []  # by publish time

    def add(
        self,
        cluster_id: int,
        title: str,
        published_at: datetime,
        companies: frozenset[str] = frozenset(),
    ) -> None:
        insort(self._members, _Member(published_at, cluster_id, headline_key(title), companies))

    def find(
        self, title: str, published_at: datetime, companies: frozenset[str] = frozenset()
    ) -> int | None:
        """The cluster of the most similar known headline within the window, if any."""
        best: tuple[float, int] | None = None
        key = headline_key(title)
        start = bisect_left(self._members, _Member(published_at - CLUSTER_WINDOW, 0, "", companies))
        end = bisect_right(self._members, _Member(published_at + CLUSTER_WINDOW, 0, "", companies))
        for member in self._members[start:end]:
            if member.key == key:
                return member.cluster_id
            if member.companies != companies or not _keys_match(member.key, key):
                continue
            score = similarity(member.key, key)
            if best is None or score > best[0]:
                best = (score, member.cluster_id)
        return None if best is None else best[1]
