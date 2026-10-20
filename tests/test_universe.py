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


def test_news_fields_and_news_only_companies() -> None:
    universe = Universe.model_validate(
        {
            "history_start": "2024-01-01",
            "forms": ["10-K"],
            "companies": [
                {"ticker": "GOOGL", "name": "Alphabet", "aliases": [" Google ", ""]},
                {"ticker": "avgo", "name": "Broadcom", "news_only": True},
            ],
        }
    )
    googl = universe.companies[0]
    assert googl.names == ["Alphabet", "Google"]
    # News-only companies get news but no filings.
    assert universe.tickers == ["GOOGL"]
    assert [c.ticker for c in universe.select_news()] == ["GOOGL", "AVGO"]
    assert [c.ticker for c in universe.select_news(["avgo"])] == ["AVGO"]
    with pytest.raises(ValueError, match="news only"):
        universe.select(["AVGO"])


def test_news_needs_names() -> None:
    universe = make_universe("AAPL")
    with pytest.raises(ValueError, match="needs a name"):
        universe.select_news()
    with pytest.raises(ValueError, match="ZZZZ"):
        universe.select_news(["ZZZZ"])


def test_project_universe_names_every_company() -> None:
    universe = load_universe(PROJECT_ROOT / "universe.yaml")
    assert len(universe.select_news()) == 15


def test_every_company_has_a_ceo_and_cfo_on_its_roster_today() -> None:
    universe = load_universe(PROJECT_ROOT / "universe.yaml")
    today = date(2026, 10, 20)
    for company in universe.companies:
        roles = " ".join(e.role.lower() for e in company.executives_on(today))
        assert "chief executive" in roles or "co-chief executive" in roles, company.ticker
        assert "financial" in roles, company.ticker
        for executive in company.executives:
            if executive.start and executive.end:
                assert executive.start <= executive.end, executive.name


def test_executives_hold_roles_between_their_dates() -> None:
    company = load_universe(PROJECT_ROOT / "universe.yaml").companies[0]  # Apple
    on = {e.name for e in company.executives_on(date(2024, 6, 1))}
    later = {e.name for e in company.executives_on(date(2026, 10, 1))}
    assert "Luca Maestri" in on and "Kevan Parekh" not in on
    assert "Kevan Parekh" in later and "John Ternus" in later
