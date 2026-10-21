"""Where the resolver gets a claim's measured outcome: one source per kind of subject.

A promise names what it is about (`subject_kind`, `subject_key`): a company (its
ticker), or an index, a sector or a macro series (its identifier). The resolver asks
`outcome_request(subject, periods)` for the tool call that reports the subject's metric
over those periods, and runs it like every other evidence call, so the outcome is cited
and dated like any evidence. A company's outcome is its reported results, XBRL values
as first reported (`get_actuals`). Another kind of subject plugs in by registering a
source for it (`register`) backed by a tool of its own; until one is registered, its
claims are judged from filings and news alone.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from talk_is_cheap.db.models import Promise

# Reported metrics behind each promise metric, for `get_actuals`.
ACTUAL_METRICS: Mapping[str, list[str]] = {
    "revenue": ["revenue"],
    "operating_income": ["operating_income"],
    "operating_expenses": ["operating_expenses"],
    "capex": ["capex"],
    "eps": ["eps_diluted"],
    "gross_margin": ["gross_profit", "revenue"],
    "operating_margin": ["operating_income", "revenue"],
}


@dataclass(frozen=True, slots=True)
class Subject:
    kind: str  # company, index, sector, macro
    key: str  # a ticker, or the series' identifier
    metric: str | None


ToolCall = tuple[str, dict[str, Any]]


class OutcomeSource(Protocol):
    def reports(self, subject: Subject) -> bool:
        """Whether the source has a series for the subject's metric."""
        ...

    def request(self, subject: Subject, periods: Sequence[str]) -> ToolCall | None:
        """The tool call that reports the subject's metric for these periods (fiscal
        periods such as FY2025Q2 for a company), or None when there is none."""
        ...


class CompanyActuals:
    """A company's reported results, as first reported."""

    def reports(self, subject: Subject) -> bool:
        return subject.metric in ACTUAL_METRICS

    def request(self, subject: Subject, periods: Sequence[str]) -> ToolCall | None:
        metrics = ACTUAL_METRICS.get(subject.metric or "")
        if metrics is None or not periods:
            return None
        return (
            "get_actuals",
            {
                "ticker": subject.key,
                "metrics": metrics,
                "periods": list(periods),
                "vintage": "first",
            },
        )


_SOURCES: dict[str, OutcomeSource] = {"company": CompanyActuals()}


def register(kind: str, source: OutcomeSource) -> None:
    _SOURCES[kind] = source


def subject_of(promise: Promise) -> Subject:
    """What a promise is about; promises framed before subjects were recorded are
    about their company."""
    kind = promise.subject_kind or "company"
    key = promise.subject_key or promise.ticker
    return Subject(kind, key, promise.metric)


def has_series(subject: Subject) -> bool:
    """Whether a registered source reports this subject's metric (the resolver then also
    asks for the period the due date closes)."""
    source = _SOURCES.get(subject.kind)
    return source is not None and source.reports(subject)


def outcome_request(subject: Subject, periods: Sequence[str]) -> ToolCall | None:
    source = _SOURCES.get(subject.kind)
    return None if source is None else source.request(subject, periods)
