from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from talk_is_cheap.universe import Universe, load_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_universe(*tickers: str) -> Universe:
    return Universe.model_validate(
        {
            "history_start": "2024-01-01",
            "forms": ["10-K", "10-Q"],
            "companies": [{"ticker": t} for t in tickers],
        }
    )


def test_project_universe_file_is_valid() -> None:
    universe = load_universe(PROJECT_ROOT / "universe.yaml")
    assert len(universe.tickers) == 15
    assert universe.history_start == date(2024, 1, 1)
    assert {"10-K", "10-Q", "8-K"} <= set(universe.forms)


def test_tickers_are_normalized() -> None:
    assert make_universe(" aapl ", "Msft").tickers == ["AAPL", "MSFT"]


def test_select_defaults_to_all_and_accepts_any_case() -> None:
    universe = make_universe("AAPL", "MSFT", "NVDA")
    assert universe.select(None) == ["AAPL", "MSFT", "NVDA"]
    assert universe.select(["nvda", " aapl"]) == ["NVDA", "AAPL"]


def test_select_rejects_tickers_outside_the_universe() -> None:
    with pytest.raises(ValueError, match="TSLA"):
        make_universe("AAPL").select(["AAPL", "TSLA"])


def test_duplicate_tickers_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        make_universe("AAPL", "aapl")


def test_empty_universe_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_universe()
