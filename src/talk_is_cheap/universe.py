"""The set of companies, forms and history window the system covers (universe.yaml)."""

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class UniverseCompany(BaseModel):
    ticker: str
    # The name the press uses ("Nvidia", not "NVIDIA CORP"). News needs it.
    name: str | None = None
    # Other names that refer to the company in news ("Google" for Alphabet).
    aliases: list[str] = Field(default_factory=list)
    # Phrases in which the name means something else ("Big Apple", "Amazon rainforest").
    exclude_phrases: list[str] = Field(default_factory=list)
    # Collect news only; filings are not loaded until this is removed.
    news_only: bool = False
    # Search GDELT only within the configured publishers. Pays off for names that are
    # common words ("Apple") or heavily covered, whose unfiltered searches fill GDELT's
    # record limit with articles that are dropped anyway.
    gdelt_filter_publishers: bool = False

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        return ticker

    @field_validator("aliases", "exclude_phrases")
    @classmethod
    def _strip_phrases(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v.strip()]

    @property
    def names(self) -> list[str]:
        """The name followed by its aliases."""
        return ([self.name] if self.name else []) + self.aliases


class Universe(BaseModel):
    history_start: date
    forms: list[str] = Field(min_length=1)
    companies: list[UniverseCompany] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_tickers(self) -> "Universe":
        tickers = [company.ticker for company in self.companies]
        duplicates = sorted({t for t in tickers if tickers.count(t) > 1})
        if duplicates:
            raise ValueError(f"duplicate tickers in universe: {', '.join(duplicates)}")
        return self

    @property
    def tickers(self) -> list[str]:
        """Companies whose filings are loaded (news-only companies are left out)."""
        return [company.ticker for company in self.companies if not company.news_only]

    def select(self, tickers: Sequence[str] | None = None) -> list[str]:
        """Return the requested tickers (all when None), rejecting any not in the universe."""
        if not tickers:
            return self.tickers
        requested = _normalize(tickers)
        news_only = [t for t in requested if t in self._by_ticker and t not in self.tickers]
        if news_only:
            raise ValueError(f"listed for news only in universe.yaml: {', '.join(news_only)}")
        unknown = [t for t in requested if t not in self.tickers]
        if unknown:
            raise ValueError(f"not in universe.yaml: {', '.join(unknown)}")
        return requested

    def select_news(self, tickers: Sequence[str] | None = None) -> list[UniverseCompany]:
        """Companies to collect news for (all, including news-only ones, when None).

        Every selected company must have a name, since news is matched by name.
        """
        if tickers:
            requested = _normalize(tickers)
            unknown = [t for t in requested if t not in self._by_ticker]
            if unknown:
                raise ValueError(f"not in universe.yaml: {', '.join(unknown)}")
            selected = [self._by_ticker[t] for t in requested]
        else:
            selected = list(self.companies)
        unnamed = [c.ticker for c in selected if not c.name]
        if unnamed:
            raise ValueError(f"news needs a name in universe.yaml for: {', '.join(unnamed)}")
        return selected

    @property
    def _by_ticker(self) -> dict[str, UniverseCompany]:
        return {company.ticker: company for company in self.companies}


def _normalize(tickers: Sequence[str]) -> list[str]:
    return [t.strip().upper() for t in tickers if t.strip()]


def load_universe(path: Path) -> Universe:
    with path.open(encoding="utf-8") as handle:
        return Universe.model_validate(yaml.safe_load(handle))
