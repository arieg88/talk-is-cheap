"""The HTTP API's parts that need no database: keys, rate limits, settings, pagination
cursors and the mapping of LangGraph's events to the chat stream."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from langchain_core.messages import ToolMessage
from pydantic import SecretStr

from talk_is_cheap.api.app import ApiConfigError, check_settings
from talk_is_cheap.api.chat import _events, _pieces, sse
from talk_is_cheap.api.routes import page
from talk_is_cheap.api.schemas import TokenEvent
from talk_is_cheap.api.security import ANONYMOUS, Keys, RateLimiter, key_id
from talk_is_cheap.config import Settings


def test_keys_are_checked_and_identified_by_a_hash_only() -> None:
    keys = Keys([SecretStr("alpha-key"), SecretStr("beta-key")])
    assert keys.required
    assert keys.identify("beta-key") == key_id("beta-key")
    assert "beta" not in key_id("beta-key") and key_id("beta-key").startswith("key_")
    assert keys.identify("gamma-key") is None
    assert keys.identify(None) is None and keys.identify("") is None
    assert Keys([]).identify(None) == ANONYMOUS


def test_the_rate_limit_is_a_sliding_minute_per_key() -> None:
    now = [1000.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.hit("a") is None
    now[0] += 20
    assert limiter.hit("a") is None
    assert limiter.hit("b") is None  # each key has its own
    now[0] += 10
    assert limiter.hit("a") == pytest.approx(30)  # the first hit leaves the window in 30s
    now[0] += 30
    assert limiter.hit("a") is None


def test_production_needs_keys_and_a_real_model() -> None:
    with pytest.raises(ApiConfigError, match="API_KEYS"):
        check_settings(Settings(environment="production"))
    with pytest.raises(ApiConfigError, match="fake"):
        check_settings(Settings(environment="production", api_keys="k", llm_provider="fake"))
    check_settings(Settings(environment="production", api_keys="k"))
    check_settings(Settings(environment="local"))


def test_keys_and_origins_are_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", " secret-one, secret-two ,,")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://ui.example.com")
    settings = Settings()
    assert [k.get_secret_value() for k in settings.api_keys] == ["secret-one", "secret-two"]
    assert settings.cors_origins == ["http://localhost:3000", "https://ui.example.com"]
    assert "secret-one" not in repr(settings)


def test_pages_and_cursors() -> None:
    items = list(range(5))
    first, cursor = page(items, 2, None)
    assert first == [0, 1] and cursor
    second, cursor = page(items, 2, cursor)
    assert second == [2, 3] and cursor
    last, cursor = page(items, 2, cursor)
    assert last == [4] and cursor is None
    for bad in ("not-a-cursor", "eyJvZmZzZXQiOiAtMX0="):  # garbage; a negative offset
        with pytest.raises(HTTPException) as caught:
            page(items, 2, bad)
        assert caught.value.status_code == 400


def test_langgraph_events_become_stream_events() -> None:
    node = {
        "event": "on_chain_start",
        "name": "verifier",
        "metadata": {"langgraph_node": "verifier"},
    }
    assert [(n, e.model_dump()) for n, e in _events(node)] == [
        ("status", {"node": "verifier", "step": "Checking the draft against the evidence…"})
    ]
    # A runnable inside a node is not a step of its own.
    inner = {"event": "on_chain_start", "name": "verifier", "metadata": {"langgraph_node": "x"}}
    assert _events(inner) == []
    start = {
        "event": "on_tool_start",
        "name": "search_filings",
        "run_id": "r1",
        "data": {"input": {"query": "risks", "ticker": "NVDA"}},
    }
    assert [n for n, _ in _events(start)] == ["status", "tool_start"]
    assert _events(start)[0][1].model_dump()["step"] == "Searching filings…"
    found = ToolMessage(
        json.dumps({"evidence": []}),
        tool_call_id="c1",
        artifact=[{"id": "E4"}, {"id": "E5"}],
    )
    end = {
        "event": "on_tool_end",
        "name": "search_filings",
        "run_id": "r1",
        "data": {"output": found},
    }
    ((name, event),) = _events(end)
    assert name == "tool_end"
    assert event.model_dump() == {
        "call_id": "r1",
        "name": "search_filings",
        "summary": "2 evidence items: E4, E5",
        "evidence": ["E4", "E5"],
        "error": None,
    }
    failed = ToolMessage(
        json.dumps({"error": "NOPE is not loaded"}), tool_call_id="c2", artifact=[]
    )
    failure = {
        "event": "on_tool_end",
        "name": "get_actuals",
        "run_id": "r2",
        "data": {"output": failed},
    }
    assert _events(failure)[0][1].model_dump()["summary"] == "error: NOPE is not loaded"


def test_the_answer_streams_in_pieces_that_join_back() -> None:
    text = "Revenue was $11.0 billion [1].\n\nIt grew 10% [2]."
    pieces = _pieces(text)
    assert "".join(pieces) == text and len(pieces) == 9
    assert sse("token", TokenEvent(text="Hi ")) == 'event: token\ndata: {"text":"Hi "}\n\n'


def test_cli_startup_does_not_import_the_api() -> None:
    """FastAPI, uvicorn and structlog are imported only by `serve`."""
    heavy = ("fastapi", "starlette", "uvicorn", "structlog", "talk_is_cheap.api")
    code = f"import sys, talk_is_cheap.cli; print([m for m in {heavy!r} if m in sys.modules])"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_serve_refuses_production_without_keys(tmp_path: Path) -> None:
    env = {**os.environ, "ENVIRONMENT": "production", "API_KEYS": ""}
    result = subprocess.run(
        [sys.executable, "-m", "talk_is_cheap.cli", "serve"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 2
    assert "API_KEYS must be set in production" in result.stderr
