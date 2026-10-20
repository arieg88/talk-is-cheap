"""Which news stories go to the model: a deterministic filter, so the model only reads
text likely to hold an attributed forward-looking claim.

A story (a dedup cluster) is a candidate for a company when one of its articles is linked
to the company with confidence 0.5 or more, and one passage of its text (a paragraph and
the next) does all three of:

1. names an executive on the company's roster, or the company speaking
   ("the company said", "a Nvidia spokesperson", "Nvidia said");
2. has a quotation mark, or an attribution ("said", "says", "told", "according to");
3. has a forward-looking marker ("expects", "will", "plans", "target", "outlook",
   "next year", "by 2027", "guidance", "forecast", ...).

The text of a story is the longest text among its articles: a fetched article body
(`article_texts`), else headline and feed summary. One extraction per story and company.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Article, ArticleCompany, ArticleText
from talk_is_cheap.statements.roster import roster_names
from talk_is_cheap.universe import UniverseCompany

MIN_CONFIDENCE = 0.5

_QUOTE = re.compile(r"[\"“”]")
_ATTRIBUTION = re.compile(r"\b(?:said|says|told|according to|stated|wrote|posted)\b", re.I)
_FORWARD = re.compile(
    r"\b(?:expects?|expected|expecting|anticipates?|will|plans?|planning|planned|"
    r"targets?|targeting|outlook|guidance|guided|forecasts?|forecasting|projects?|"
    r"projected|intends?|aims?|next (?:year|quarter|fiscal year)|this year|"
    r"by (?:the end of )?20\d\d|in 20\d\d|second half|going forward|on track to)\b",
    re.I,
)
_COMPANY_VOICE = (
    r"\b(?:the company|a company spokes(?:person|man|woman)|a spokes(?:person|man|woman) "
    r"for {name}|{name} spokes(?:person|man|woman)|{name}(?:'s|\u2019s)? spokes(?:person|"
    r"man|woman)|{name} (?:said|says|told|expects|plans|forecast))\b"
)


@dataclass(frozen=True, slots=True)
class StoryText:
    """The text a story is read from: an article body, or a headline and summary."""

    article_id: int
    cluster_id: int
    title: str
    text: str
    source: str  # "article" (fetched body) or "feed" (headline and summary)
    publisher: str | None
    url: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker: str
    story: StoryText


def feed_text(title: str, summary: str | None) -> str:
    """The text of an article without a fetched body: its headline, then its summary."""
    return f"{title}\n\n{summary}" if summary else title


def passes_filter(text: str, company: UniverseCompany) -> bool:
    """Rules 1 to 3 of the module docstring, within one passage: a paragraph together
    with the next one, since a quote often follows the paragraph that names its
    speaker."""
    names = roster_names(company)
    speaker = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in names) or r"(?!)")
    voice_names = "|".join(re.escape(n) for n in company.names) or re.escape(company.ticker)
    voice = re.compile(_COMPANY_VOICE.format(name=f"(?:{voice_names})"), re.I)
    paragraphs = [p for p in text.split("\n") if p.strip()]
    for index in range(len(paragraphs)):
        passage = "\n".join(paragraphs[index : index + 2])
        if not (speaker.search(passage) or voice.search(passage)):
            continue
        if (_QUOTE.search(passage) or _ATTRIBUTION.search(passage)) and _FORWARD.search(passage):
            return True
    return False


async def stories(session: AsyncSession, tickers: Sequence[str]) -> dict[str, list[StoryText]]:
    """Per company, the text of every story linked to it: the longest text among the
    story's articles."""
    cluster = func.coalesce(Article.cluster_id, Article.id)
    linked = (
        await session.execute(
            select(ArticleCompany.ticker, cluster)
            .join(Article, Article.id == ArticleCompany.article_id)
            .where(
                ArticleCompany.confidence >= MIN_CONFIDENCE,
                ArticleCompany.ticker.in_(list(tickers)),
            )
            .distinct()
        )
    ).all()
    clusters = {int(c) for _, c in linked}
    rows = (
        await session.execute(
            select(
                Article.id,
                cluster,
                Article.title,
                Article.summary,
                Article.publisher,
                Article.url,
                Article.published_at,
                ArticleText.text,
            )
            .outerjoin(
                ArticleText,
                (ArticleText.article_id == Article.id) & (ArticleText.status == "ok"),
            )
            .where(cluster.in_(clusters))
        )
    ).all()
    best: dict[int, StoryText] = {}
    for article_id, cluster_id, title, summary, publisher, url, published_at, body in rows:
        story = StoryText(
            article_id=int(article_id),
            cluster_id=int(cluster_id),
            title=title,
            text=body if body else feed_text(title, summary),
            source="article" if body else "feed",
            publisher=publisher,
            url=url,
            published_at=published_at,
        )
        current = best.get(story.cluster_id)
        if current is None or (len(story.text), -story.article_id) > (
            len(current.text),
            -current.article_id,
        ):
            best[story.cluster_id] = story
    by_ticker: dict[str, list[StoryText]] = defaultdict(list)
    for ticker, cluster_id in linked:
        if int(cluster_id) in best:
            by_ticker[ticker].append(best[int(cluster_id)])
    for found in by_ticker.values():
        found.sort(key=lambda s: (s.published_at, s.cluster_id))
    return dict(by_ticker)


async def candidates(
    session: AsyncSession, companies: Sequence[UniverseCompany]
) -> tuple[dict[str, list[Candidate]], dict[str, int]]:
    """The candidate stories per company, oldest first, and the stories each company
    has in all."""
    texts = await stories(session, [c.ticker for c in companies])
    kept: dict[str, list[Candidate]] = {}
    totals: dict[str, int] = {}
    for company in companies:
        found = texts.get(company.ticker, [])
        totals[company.ticker] = len(found)
        kept[company.ticker] = [
            Candidate(company.ticker, s) for s in found if passes_filter(s.text, company)
        ]
    return kept, totals
