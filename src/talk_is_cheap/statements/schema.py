"""What the model returns for one news story: forward-looking claims as written.

The model reads and classifies; it does not compute or match. Figures stay as written
(`value_text`), periods as named (`period_text`), speakers as the article names them.
Numbers, fiscal periods, grounding and matching speakers to the executive roster are done
in code (`statements/items.py`).
"""

from typing import Literal

from pydantic import BaseModel, Field

from talk_is_cheap.guidance.schema import Basis, Measure, Metric

Attribution = Literal[
    "named_executive",
    "company_spokesperson",
    "company_statement",
    "analyst",
    "anonymous_source",
    "reporter",
]
Venue = Literal[
    "interview", "conference", "earnings_call", "press_release", "social_media", "other", "unknown"
]
ClaimType = Literal["numeric_forecast", "directional", "plan", "qualitative"]
Direction = Literal["increase", "decrease", "flat"]

ATTRIBUTIONS: tuple[str, ...] = Attribution.__args__  # type: ignore[attr-defined]
VENUES: tuple[str, ...] = Venue.__args__  # type: ignore[attr-defined]
CLAIM_TYPES: tuple[str, ...] = ClaimType.__args__  # type: ignore[attr-defined]


class ExtractedClaim(BaseModel):
    """One forward-looking claim about the company and who made it."""

    quote: str = Field(
        description="The exact sentence or span the claim comes from, copied character for "
        "character from the article text. No ellipses."
    )
    speaker_name: str | None = Field(
        default=None,
        description="The person who made the claim, as the article names them; null for a "
        "company statement or an unnamed source.",
    )
    speaker_role: str | None = Field(
        default=None,
        description="The speaker's title or affiliation as written ('CEO', 'analyst at "
        "Morgan Stanley'); null if not given.",
    )
    attribution: Attribution
    is_direct_quote: bool = Field(
        description="True when the claim is inside quotation marks; false when paraphrased."
    )
    venue: Venue = "unknown"
    statement_date: str | None = Field(
        default=None,
        description="YYYY-MM-DD when the article says when the statement was made ('on "
        "Tuesday' counted back from the article date); null when it does not say.",
    )
    claim_type: ClaimType
    claim: str = Field(description="The claim in brief, at most 20 words.")
    metric: Metric | None = Field(
        default=None,
        description="Canonical financial metric the claim is about; 'other' for another "
        "measure; null for none.",
    )
    metric_raw: str | None = Field(default=None, description="The measure in the article's words.")
    segment: str | None = Field(
        default=None,
        description="Business segment or product line the figure covers; null for the "
        "whole company.",
    )
    basis: Basis = "unspecified"
    measure: Measure = "level"
    period_text: str | None = Field(
        default=None,
        description="The target period exactly as written ('next quarter', 'fiscal 2026', "
        "'2025', 'this year'); null when none is given.",
    )
    value_text: str | None = Field(
        default=None,
        description="The figure as written, with its range or approximation ('$64 billion "
        "to $72 billion', 'about 20%'); null when there is no figure.",
    )
    direction: Direction | None = Field(
        default=None, description="For directional claims, the change the claim expects; else null."
    )


class StatementExtraction(BaseModel):
    claims: list[ExtractedClaim]
