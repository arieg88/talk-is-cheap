"""Deterministic routing rules, tried before the router model: most questions name a
covered company and a metric, period or guidance word, and need no model to classify.

`route_by_rules` returns None when the rules cannot decide; the router model then
classifies the question. `out_of_scope_answer` is the polite reply to questions the agent
does not answer, made without any model or tool call.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from talk_is_cheap.universe import Universe, UniverseCompany

QuestionType = Literal["lookup", "guidance_check", "trend", "comparison", "out_of_scope"]
QUESTION_TYPES: tuple[QuestionType, ...] = (
    "lookup",
    "guidance_check",
    "trend",
    "comparison",
    "out_of_scope",
)

# Tickers that are also words, matched only in upper case.
_WORD_TICKERS = frozenset({"META", "CRM", "AMD"})

_ADVICE = re.compile(
    r"\b(should (?:i|we|you)|worth (?:buying|investing)|(?:buy|sell|short|hold) (?:the )?"
    r"(?:stock|shares?)|\bbuy\b(?![- ]?backs?)|\bsell\b|invest(?:ing|ment)? in|"
    r"good investment|price target|undervalued|overvalued|portfolio|stock pick)",
    re.IGNORECASE,
)
_PREDICTION = re.compile(
    r"\b(predict\w*|will (?:the )?(?:stock|shares?|price)\b|stock price (?:be|go|reach)|"
    r"will \w+(?:'s)? (?:stock|shares?|beat|miss|exceed|grow|rise|fall)|"
    r"going to (?:rise|fall|go up|go down|beat|miss))",
    re.IGNORECASE,
)
_GUIDANCE = re.compile(
    r"\b(guid\w*|outlook|forecast\w*|expect\w*|beat|miss\w*|met (?:its|their)|target\w*)\b",
    re.IGNORECASE,
)
_TREND = re.compile(
    r"\b(trend\w*|over the (?:last|past)|(?:last|past) (?:\d+|two|three|four|five|six|eight|few|"
    r"several) (?:quarters|years)|each quarter|every quarter|quarter over quarter|"
    r"since (?:fy)?\d{4}|history|evolv\w*|changed? over|"
    r"from (?:fiscal )?(?:fy|q)\w* .{0,20}\bto (?:fiscal )?(?:fy|q)\w*)\b",
    re.IGNORECASE,
)
_COMPARE = re.compile(r"\b(compare\w*|versus|vs\.?|than)\b", re.IGNORECASE)
_LOOKUP = re.compile(
    r"\b(revenue|sales|income|profit|margin|eps|earnings per share|cash flow|capex|"
    r"capital expenditure\w*|r&d|research and development|expenses?|cost of revenue|"
    r"first reported|restat\w*|md&a|management'?s discussion|risk factors?|risks?|explain\w*|"
    r"why|quarter|fiscal|fy\d{2,4}|q[1-4])\b",
    re.IGNORECASE,
)

ADVICE_NOTE = "This is not investment advice."


@dataclass(frozen=True, slots=True)
class Route:
    question_type: QuestionType
    reason: str
    trading: bool = False  # the question is about trading: the answer says it is not advice


def companies_in(question: str, universe: Universe) -> list[UniverseCompany]:
    """Covered companies named in a question: by ticker (upper case for tickers that are
    words), name or alias in any case, possessives included."""
    found = []
    for company in universe.companies:
        if company.news_only:
            continue
        ticker = re.escape(company.ticker)
        flags = 0 if company.ticker in _WORD_TICKERS else re.IGNORECASE
        names = "|".join(re.escape(n) for n in company.names)
        by_ticker = re.search(rf"(?<![\w$-]){ticker}(?![\w-])", question, flags)
        by_name = names and re.search(rf"\b(?:{names})(?:'s)?\b", question, re.IGNORECASE)
        if by_ticker or by_name:
            found.append(company)
    return found


def route_by_rules(
    question: str, universe: Universe, previous_type: QuestionType | None = None
) -> Route | None:
    """The question's type when the rules decide it, else None."""
    if _ADVICE.search(question):
        return Route("out_of_scope", "asks for investment advice", trading=True)
    if _PREDICTION.search(question):
        return Route("out_of_scope", "asks for a prediction", trading=True)
    companies = companies_in(question, universe)
    if len(companies) >= 2:
        return Route("comparison", f"names {len(companies)} companies")
    if not companies:
        if previous_type is not None and previous_type != "out_of_scope":
            return Route(previous_type, "a follow-up in the same conversation")
        return None
    if _GUIDANCE.search(question):
        return Route("guidance_check", "a company and a guidance word")
    if _TREND.search(question):
        return Route("trend", "a company and several periods")
    if _COMPARE.search(question):
        return None
    if _LOOKUP.search(question):
        return Route("lookup", "a company and a metric or period")
    return None


def out_of_scope_answer(question: str, universe: Universe, *, trading: bool) -> str:
    tickers = ", ".join(universe.tickers)
    lines = [
        "I can't help with that one. I answer questions about what these companies "
        f"reported and said, from their SEC filings, earnings releases and news: {tickers}.",
        "For example: reported figures as first reported or as restated, guidance and "
        "whether results beat it, trends across quarters, comparisons between two "
        "companies, and explanations or risk factors from their 10-Qs and 10-Ks.",
    ]
    if trading or _ADVICE.search(question) or _PREDICTION.search(question):
        lines.append(f"I don't make predictions or recommendations. {ADVICE_NOTE}")
    return "\n\n".join(lines)


def describe(companies: Sequence[UniverseCompany]) -> str:
    return ", ".join(f"{c.ticker} ({c.name})" if c.name else c.ticker for c in companies)
