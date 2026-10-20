"""Statement extraction benchmark: extracted claims against hand-labeled stories.

`evals/statements/labels.jsonl` holds one line per candidate story (found by article URL
and company): the management claims a reader finds in its stored text, and claims by
others (analysts, unnamed sources, reporters) that must never be attributed to
management. Labels quote the text; figures are in dollars, dollars per share or percent
("64B", "300M" are shorthand). A line with only `_note` is a comment.

An extracted claim matches a label when their quotes overlap in the text; each label
takes at most one claim, best overlap first, and before that a claim whose figure is
written in the label's quote (two claims often quote one sentence). Then:

- claim recall: labeled management claims found, with any attribution;
- claim precision: claims the system attributes to management that are labeled
  management claims (unlabeled extras count against it);
- attribution precision, the main number: of claims the system attributes to management
  and the labels cover (management or not), the share labeled management;
- attribution recall: of labeled management claims found, the share attributed to
  management; and leaks, claims labeled as someone else's that the system attributed to
  management;
- field accuracy, of matched management claims: claim type, metric, resolved target
  period and figures (low, high and point all equal, when the label gives a figure);
- grounding drops: claims whose quote was not in the text.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from talk_is_cheap.db.models import Article, Company
from talk_is_cheap.edgar.fiscal import FiscalCalendar
from talk_is_cheap.guidance.evaluate import amount
from talk_is_cheap.guidance.grounding import locate
from talk_is_cheap.guidance.llm import Budget, ExtractionModel, ResponseCache, Usage, complete
from talk_is_cheap.statements.candidates import Candidate, StoryText, stories
from talk_is_cheap.statements.extract import (
    PROMPT_VERSION,
    estimate_usage,
    load_prompt,
    render_prompt,
)
from talk_is_cheap.statements.items import Claim, SourceText, process
from talk_is_cheap.statements.schema import StatementExtraction
from talk_is_cheap.universe import UniverseCompany


@dataclass(frozen=True, slots=True)
class LabeledClaim:
    quote: str
    management: bool
    attribution: str
    speaker: str | None = None
    claim_type: str | None = None
    metric: str | None = None
    target: str | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    point: Decimal | None = None
    span: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class LabeledStory:
    id: str
    ticker: str
    url: str
    claims: list[LabeledClaim]
    note: str | None = None


def load_labels(path: Path) -> list[LabeledStory]:
    stories_: list[LabeledStory] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        if set(data) == {"_note"}:
            continue
        try:
            claims = [_claim(c, True) for c in data.get("management", [])] + [
                _claim(c, False) for c in data.get("not_management", [])
            ]
            stories_.append(
                LabeledStory(data["id"], data["ticker"], data["url"], claims, data.get("note"))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"line {number}: {exc}") from exc
    ids = [s.id for s in stories_]
    if len(set(ids)) != len(ids):
        raise ValueError("story ids must be unique")
    return stories_


def _claim(data: Mapping[str, Any], management: bool) -> LabeledClaim:
    return LabeledClaim(
        quote=data["quote"],
        management=management,
        attribution=data["attribution"],
        speaker=data.get("speaker"),
        claim_type=data.get("claim_type"),
        metric=data.get("metric"),
        target=data.get("target"),
        low=amount(data.get("low")),
        high=amount(data.get("high")),
        point=amount(data.get("point")),
    )


@dataclass(slots=True)
class StoryScore:
    id: str
    labeled_management: int = 0
    found_management: int = 0  # labeled management claims matched by any claim
    attributed_management: int = 0  # ... and attributed to management
    system_management: int = 0  # claims the system attributes to management
    system_management_true: int = 0  # ... matching a labeled management claim
    system_management_covered: int = 0  # ... matching any label
    leaks: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    field_errors: list[str] = field(default_factory=list)
    fields: dict[str, list[bool]] = field(default_factory=dict)
    dropped: int = 0
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    cost: Decimal = Decimal(0)
    cached: bool = True


@dataclass(slots=True)
class EvalScore:
    model: str
    stories: list[StoryScore]

    def _sum(self, name: str) -> int:
        return sum(int(getattr(s, name)) for s in self.stories)

    @staticmethod
    def _ratio(a: int, b: int) -> float | None:
        return a / b if b else None

    @property
    def claim_recall(self) -> float | None:
        return self._ratio(self._sum("found_management"), self._sum("labeled_management"))

    @property
    def claim_precision(self) -> float | None:
        return self._ratio(self._sum("system_management_true"), self._sum("system_management"))

    @property
    def attribution_precision(self) -> float | None:
        return self._ratio(
            self._sum("system_management_true"), self._sum("system_management_covered")
        )

    @property
    def attribution_recall(self) -> float | None:
        return self._ratio(self._sum("attributed_management"), self._sum("found_management"))

    @property
    def leaks(self) -> int:
        return sum(len(s.leaks) for s in self.stories)

    def field_accuracy(self, name: str) -> tuple[int, int]:
        values = [v for s in self.stories for v in s.fields.get(name, [])]
        return sum(values), len(values)

    @property
    def dropped(self) -> int:
        return self._sum("dropped")

    @property
    def usage(self) -> Usage:
        total = Usage(0, 0)
        for s in self.stories:
            total += s.usage
        return total

    @property
    def cost(self) -> Decimal:
        return sum((s.cost for s in self.stories), Decimal(0))


@dataclass(frozen=True, slots=True)
class LabeledInput:
    label: LabeledStory
    candidate: Candidate
    company: UniverseCompany
    calendar: FiscalCalendar | None
    prompt: str


async def labeled_inputs(
    session: AsyncSession,
    labels: Sequence[LabeledStory],
    companies: Mapping[str, UniverseCompany],
    prompt_version: str = PROMPT_VERSION,
) -> list[LabeledInput]:
    """Each label's story as extraction reads it, and its prompt. Raises ValueError when a
    story or a label's quote is not in the stored text."""
    template = load_prompt(prompt_version)
    texts = await stories(session, sorted({label.ticker for label in labels}))
    calendars = {
        t: FiscalCalendar.parse(end)
        for t, end in await session.execute(select(Company.ticker, Company.fiscal_year_end))
        if end
    }
    urls = {label.url for label in labels}
    clusters = {
        url: cluster
        for url, cluster in await session.execute(
            select(Article.url, Article.cluster_id).where(Article.url.in_(urls))
        )
    }
    ids = {
        url: article_id
        for url, article_id in await session.execute(
            select(Article.url, Article.id).where(Article.url.in_(urls))
        )
    }
    inputs = []
    for label in labels:
        if label.url not in ids:
            raise ValueError(f"{label.id}: no stored article {label.url}")
        cluster = clusters[label.url] or ids[label.url]
        story = next((s for s in texts.get(label.ticker, []) if s.cluster_id == cluster), None)
        if story is None:
            raise ValueError(f"{label.id}: the story is not linked to {label.ticker}")
        located = [_locate(label, claim, story) for claim in label.claims]
        candidate = Candidate(label.ticker, story)
        company = companies[label.ticker]
        inputs.append(
            LabeledInput(
                LabeledStory(label.id, label.ticker, label.url, located, label.note),
                candidate,
                company,
                calendars.get(label.ticker),
                render_prompt(template, candidate, company),
            )
        )
    return inputs


def _locate(label: LabeledStory, claim: LabeledClaim, story: StoryText) -> LabeledClaim:
    span = locate(claim.quote, story.text)
    if span is None:
        raise ValueError(f"{label.id}: label quote not in the text: {claim.quote[:80]!r}")
    return LabeledClaim(**{**_fields(claim), "span": span})


def _fields(claim: LabeledClaim) -> dict[str, Any]:
    return {name: getattr(claim, name) for name in LabeledClaim.__dataclass_fields__}


def planned_calls(
    inputs: Sequence[LabeledInput], model_name: str, cache: ResponseCache
) -> tuple[int, Usage]:
    """Calls an evaluation would make (responses not cached) and their largest usage."""
    calls, usage = 0, Usage(0, 0)
    for item in inputs:
        if cache.get(model_name, PROMPT_VERSION, item.prompt, StatementExtraction) is None:
            calls += 1
            usage += estimate_usage(item.prompt)
    return calls, usage


async def evaluate(
    inputs: Sequence[LabeledInput],
    model: ExtractionModel[StatementExtraction],
    cache: ResponseCache,
    budget: Budget,
    on_story: Callable[[StoryScore], None] | None = None,
) -> EvalScore:
    scores = []
    for item in inputs:
        response = await complete(
            model, item.prompt, PROMPT_VERSION, cache, budget, estimate_usage(item.prompt)
        )
        story = item.candidate.story
        claims, dropped = process(
            SourceText(story.article_id, story.text, story.published_at.date()),
            response.extraction,
            item.company,
            item.calendar,
        )
        score = score_story(item.label, claims)
        score.dropped = len(dropped)
        score.usage, score.cost, score.cached = response.usage, response.cost, response.cached
        scores.append(score)
        if on_story is not None:
            on_story(score)
    return EvalScore(model.name, scores)


def score_story(label: LabeledStory, claims: Sequence[Claim]) -> StoryScore:
    score = StoryScore(label.id)
    pairs = match_claims(label.claims, claims)
    matched_claims = {id(c) for _, c in pairs}
    for labeled, claim in pairs:
        if labeled.management:
            score.found_management += 1
            if claim.management:
                score.attributed_management += 1
                _compare_fields(score, labeled, claim)
            else:
                score.misses.append(f"attributed to {claim.attribution}: {labeled.quote[:70]}")
        elif claim.management:
            score.leaks.append(
                f"{labeled.attribution} claim attributed to management "
                f"({claim.attribution} {claim.executive_name or ''}): {labeled.quote[:70]}"
            )
    found = {id(labeled) for labeled, _ in pairs}
    for labeled in label.claims:
        score.labeled_management += labeled.management
        if labeled.management and id(labeled) not in found:
            score.misses.append(f"not found: {labeled.quote[:70]}")
    by_claim = {id(c): labeled for labeled, c in pairs}
    for claim in claims:
        if not claim.management:
            continue
        score.system_management += 1
        covering = by_claim.get(id(claim))
        if covering is not None:
            score.system_management_covered += 1
            score.system_management_true += covering.management
        elif id(claim) not in matched_claims:
            score.extras.append(f"{claim.attribution}: {claim.quote[:70]}")
    return score


def match_claims(
    labels: Sequence[LabeledClaim], claims: Sequence[Claim]
) -> list[tuple[LabeledClaim, Claim]]:
    """Pairs of a label and a claim whose quotes overlap: a claim whose figure is in the
    label's quote first, then the largest overlap; each label and each claim in at most
    one pair."""
    options = []
    for li, labeled in enumerate(labels):
        assert labeled.span is not None
        for ci, claim in enumerate(claims):
            overlap = min(labeled.span[1], claim.char_end) - max(labeled.span[0], claim.char_start)
            if overlap > 0:
                options.append((_figure_in(claim, labeled), overlap, li, ci))
    pairs = []
    used_labels: set[int] = set()
    used_claims: set[int] = set()
    for _, _, li, ci in sorted(options, key=lambda o: (-o[0], -o[1], o[2], o[3])):
        if li in used_labels or ci in used_claims:
            continue
        used_labels.add(li)
        used_claims.add(ci)
        pairs.append((labels[li], claims[ci]))
    return pairs


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _figure_in(claim: Claim, labeled: LabeledClaim) -> bool:
    """Whether the claim's figure is written in the label's quote: two claims quoting
    one sentence ("could increase by 68%, ... in line with the company's guidance of
    70%") go to the labels of their own figures."""
    numbers = _NUMBER.findall(claim.value_text or "")
    return bool(numbers) and all(n in labeled.quote for n in numbers)


def _compare_fields(score: StoryScore, labeled: LabeledClaim, claim: Claim) -> None:
    checks: dict[str, bool] = {}
    if labeled.claim_type:
        checks["claim_type"] = labeled.claim_type == claim.claim_type
    if labeled.metric:
        checks["metric"] = labeled.metric == claim.metric
    if labeled.target:
        checks["period"] = labeled.target == (claim.target.label if claim.target else None)
    if any(v is not None for v in (labeled.low, labeled.high, labeled.point)):
        checks["figures"] = (labeled.low, labeled.high, labeled.point) == (
            claim.low,
            claim.high,
            claim.point,
        )
    for name, ok in checks.items():
        score.fields.setdefault(name, []).append(ok)
        if not ok:
            score.field_errors.append(f"{name}: {labeled.quote[:60]}")
