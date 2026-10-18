"""The set of companies, forms and history window the system covers (universe.yaml)."""

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class UniverseCompany(BaseModel):
    ticker: str

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        return ticker


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
        return [company.ticker for company in self.companies]

    def select(self, tickers: Sequence[str] | None = None) -> list[str]:
        """Return the requested tickers (all when None), rejecting any not in the universe."""
        if not tickers:
            return self.tickers
        requested = [t.strip().upper() for t in tickers if t.strip()]
        unknown = [t for t in requested if t not in self.tickers]
        if unknown:
            raise ValueError(f"not in universe.yaml: {', '.join(unknown)}")
        return requested


def load_universe(path: Path) -> Universe:
    with path.open(encoding="utf-8") as handle:
        return Universe.model_validate(yaml.safe_load(handle))
