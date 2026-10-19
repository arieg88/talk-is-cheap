"""Retrieval benchmark: how often search puts a passage that answers a labeled question
near the top.

Each question in `evals/retrieval/questions.jsonl` names what a correct passage looks
like rather than which chunk it is, so labels survive re-chunking: its company, its
fiscal period (filings) or publish dates (news), and phrases or figures it must contain
(any of them, ignoring case, markup, spacing and the kind of dash). The section type is
recorded but not required, since the same fact appears in several documents (Nvidia's
outlook is in both its release and its CFO commentary). Search runs with the question's
own filters; with `filtered`, also with the question's period or dates, the way the
agent calls search when it knows them.

A question is `lexical` when it uses the passage's own key terms, and `paraphrase` when
it avoids them ("How much money does Nvidia think it will bring in next quarter?").

Reported per configuration (mode, with or without the context header) and per group:
recall@5 and recall@10, the share of questions with a correct passage in the top 5 or
10, and MRR, the mean of 1 / rank of the first correct passage in the top 10 (0 when
there is none).
"""

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talk_is_cheap.db.models import Chunk
from talk_is_cheap.edgar.fiscal import FiscalQuarter
from talk_is_cheap.retrieval.encoders import Embedder, Reranker
from talk_is_cheap.retrieval.index import embed_missing, ensure_vector_index, text_only_model
from talk_is_cheap.retrieval.search import Mode, Searcher, SearchResult

DEPTH = 10  # results judged per question
KINDS = ("lexical", "paraphrase")


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    question: str
    ticker: str
    answers: list[str]  # a correct passage contains at least one
    period: FiscalQuarter | None = None  # filings
    section_types: list[str] | None = None  # filings
    published_from: date | None = None  # news, inclusive
    published_to: date | None = None  # news, inclusive
    filters: dict[str, Any] = field(default_factory=dict)  # passed to search as is
    kind: str = "lexical"

    def period_filters(self) -> dict[str, Any]:
        """The question's own period, or its news dates, as search filters."""
        if self.period is not None:
            return {"periods": self.period.label}
        return {"since": self.published_from, "until": self.published_to}

    @property
    def source(self) -> str:
        return "filing" if self.period is not None else "news"


def load_questions(path: Path) -> list[Question]:
    questions = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            questions.append(_question(json.loads(line)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: question ids repeat")
    return questions


def _question(data: dict[str, Any]) -> Question:
    expected = data["expected"]
    answers = data["answer"] if isinstance(data["answer"], list) else [data["answer"]]
    period = expected.get("period")
    news = expected.get("published")
    if (period is None) == (news is None):
        raise ValueError("expected needs either a fiscal period or a published date range")
    section = expected.get("section_type")
    kind = data.get("kind", "lexical")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    return Question(
        id=data["id"],
        question=data["question"],
        ticker=expected["ticker"].upper(),
        answers=[str(a) for a in answers],
        period=FiscalQuarter.parse(period) if period else None,
        section_types=[section] if isinstance(section, str) else section,
        published_from=date.fromisoformat(news[0]) if news else None,
        published_to=date.fromisoformat(news[1]) if news else None,
        filters=data.get("filters", {}),
        kind=kind,
    )


_MARKUP = re.compile(r"\*\*|\\(?=[#|*])|[|]")
_SPACE = re.compile(r"\s+")
_HYPHEN = re.compile(r" ?- ?")
# Curly quotes, and every dash and hyphen, compare equal to plain ones.
_PLAIN = str.maketrans(
    {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'}
    | dict.fromkeys(["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"], "-")
)


def normalized(text: str) -> str:
    """Lower case, markdown bold and table bars removed, one space between words, and no
    spaces around hyphens: "10 - for - 1" and "10-for-1" compare equal."""
    plain = _SPACE.sub(" ", _MARKUP.sub(" ", text.translate(_PLAIN))).strip().lower()
    return _HYPHEN.sub("-", plain)


def answers(question: Question, result: SearchResult) -> bool:
    """Whether a search result is a correct passage for the question."""
    if question.ticker not in result.tickers:
        return False
    if question.period is not None:
        if result.source != "filing" or result.period != question.period:
            return False
    else:
        assert question.published_from is not None and question.published_to is not None
        start = datetime.combine(question.published_from, day_time(), tzinfo=UTC)
        end = datetime.combine(question.published_to, day_time(), tzinfo=UTC) + timedelta(days=1)
        if result.source != "news" or not start <= result.published_at < end:
            return False
    text = normalized(result.text)
    return any(normalized(a) in text for a in question.answers)


@dataclass(frozen=True, slots=True)
class Config:
    mode: Mode
    context_header: bool = True
    filtered: bool = False  # also filter by the question's period or news dates

    @property
    def label(self) -> str:
        label = self.mode.value + ("" if self.context_header else " (no header)")
        return label + (" + period" if self.filtered else "")


# Keyword search reads chunk text only, so the header makes no difference to it.
CONFIGS = [Config(Mode.KEYWORD)] + [
    Config(mode, header)
    for mode in (Mode.VECTOR, Mode.HYBRID, Mode.RERANK)
    for header in (True, False)
]


@dataclass(slots=True)
class Score:
    config: Config
    ranks: dict[str, int | None] = field(default_factory=dict)  # first correct rank per id
    seconds: list[float] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)  # question id -> kind

    def _ranks(self, kind: str | None) -> list[int | None]:
        return [r for i, r in self.ranks.items() if kind is None or self.kinds.get(i) == kind]

    def recall(self, k: int, kind: str | None = None) -> float:
        ranks = self._ranks(kind)
        hits = sum(1 for r in ranks if r is not None and r <= k)
        return hits / len(ranks) if ranks else 0.0

    def mrr(self, kind: str | None = None) -> float:
        ranks = self._ranks(kind)
        if not ranks:
            return 0.0
        return sum(1 / r for r in ranks if r is not None) / len(ranks)

    @property
    def mean_ms(self) -> float:
        return 1000 * sum(self.seconds) / len(self.seconds) if self.seconds else 0.0


async def prepare_text_only_vectors(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
) -> int:
    """Embed every chunk's text without its header, for the configurations without one.
    Kept between runs and pruned by `index` like any other vector."""
    stored_as = text_only_model(embedder.model_name)
    await ensure_vector_index(engine, stored_as, embedder.dimensions)
    async with sessions() as session:
        texts = dict((await session.execute(select(Chunk.text_hash, Chunk.text))).tuples().all())
    return await embed_missing(sessions, embedder, texts, stored_as=stored_as)


async def evaluate(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    reranker: Reranker,
    questions: Sequence[Question],
    configs: Sequence[Config] = CONFIGS,
    on_config: Callable[[Score], None] | None = None,
) -> list[Score]:
    scores = []
    for config in configs:
        score = Score(config)
        async with sessions() as session:
            searcher = Searcher(session, lambda: embedder, lambda: reranker)
            for question in questions:
                started = time.monotonic()
                filters = question.filters
                if config.filtered:
                    filters = filters | question.period_filters()
                results = await searcher.search(
                    question.question,
                    mode=config.mode,
                    k=DEPTH,
                    context_header=config.context_header,
                    **filters,
                )
                score.seconds.append(time.monotonic() - started)
                score.kinds[question.id] = question.kind
                score.ranks[question.id] = next(
                    (rank for rank, r in enumerate(results, 1) if answers(question, r)), None
                )
                await session.rollback()  # end the transaction holding SET LOCAL settings
        scores.append(score)
        if on_config is not None:
            on_config(score)
    return scores
