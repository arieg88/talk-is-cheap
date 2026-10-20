"""The response cache, pricing, the cost cap and model configuration. No real model is
called."""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from talk_is_cheap.config import Settings
from talk_is_cheap.guidance.llm import (
    Budget,
    BudgetExhausted,
    CacheOnlyModel,
    CostCapReached,
    LangChainModel,
    LlmError,
    Pricing,
    ResponseCache,
    Usage,
    build_model,
    complete,
    estimate_usage,
    model_label,
    pricing_for,
)
from talk_is_cheap.guidance.schema import Extraction
from tests.guidance_fakes import USAGE, FakeModel

PRICING = Pricing(Decimal("2.00"), Decimal("10.00"))
EMPTY = Extraction(items=[])


def test_cost_is_per_million_tokens() -> None:
    assert PRICING.cost(Usage(1_000_000, 100_000)) == Decimal("3.00")
    assert PRICING.cost(USAGE) == Decimal("0.007")


async def test_responses_are_cached_by_model_prompt_version_and_prompt(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    model = FakeModel(lambda prompt: EMPTY)
    budget = Budget(Decimal(1), PRICING)

    first = await complete(model, "prompt A", "v1", cache, budget)
    again = await complete(model, "prompt A", "v1", cache, budget)

    assert (first.cached, again.cached) == (False, True)
    assert model.prompts == ["prompt A"]  # the second answer came from the cache
    assert again.cost == first.cost == Decimal("0.007")  # what the call cost when made
    assert (budget.calls, budget.spent, budget.usage) == (1, Decimal("0.007"), USAGE)

    await complete(model, "prompt A", "v2", cache, budget)  # another prompt version
    await complete(FakeModel(lambda p: EMPTY, "other@low"), "prompt A", "v1", cache, budget)
    assert budget.calls == 3
    assert cache.path("fake-model@test", "v1", "prompt A").exists()


async def test_the_cost_cap_stops_a_call_that_could_exceed_it(tmp_path: Path) -> None:
    prompt = "x" * 2_400
    worst = PRICING.cost(estimate_usage(prompt))
    model = FakeModel(lambda p: EMPTY)
    budget = Budget(worst + Decimal("0.001"), PRICING)

    await complete(model, prompt, "v1", ResponseCache(tmp_path), budget)
    assert budget.spent == Decimal("0.007")
    with pytest.raises(CostCapReached, match="cost cap"):
        await complete(model, prompt + "!", "v1", ResponseCache(tmp_path), budget)
    assert len(model.prompts) == 1
    # A cached response needs no budget.
    cached = await complete(model, prompt, "v1", ResponseCache(tmp_path), budget)
    assert cached.cached


async def test_parallel_calls_reserve_their_cost(tmp_path: Path) -> None:
    prompt = "y" * 2_400
    worst = PRICING.cost(estimate_usage(prompt))
    started = asyncio.Event()

    class Slow(FakeModel):
        async def extract(self, prompt: str) -> tuple[Extraction, Usage]:
            started.set()
            await asyncio.sleep(0.05)
            return await super().extract(prompt)

    model = Slow(lambda p: EMPTY)
    budget = Budget(worst * Decimal("1.5"), PRICING)
    cache = ResponseCache(tmp_path)
    results = await asyncio.gather(
        complete(model, prompt + "1", "v1", cache, budget),
        complete(model, prompt + "2", "v1", cache, budget),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CostCapReached) for r in results) == 1
    assert budget.reserved == 0 and budget.calls == 1


async def test_failed_answers_still_count_what_they_cost(tmp_path: Path) -> None:
    class Broken(FakeModel):
        async def extract(self, prompt: str) -> tuple[Extraction, Usage]:
            raise LlmError("no structured answer", USAGE)

    budget = Budget(Decimal(1), PRICING)
    with pytest.raises(LlmError):
        await complete(Broken(lambda p: EMPTY), "prompt", "v1", ResponseCache(tmp_path), budget)
    assert (budget.spent, budget.reserved) == (Decimal("0.007"), 0)
    assert not ResponseCache(tmp_path).path("fake-model@test", "v1", "prompt").exists()


async def test_cache_only_model_never_calls(tmp_path: Path) -> None:
    with pytest.raises(LlmError, match="no cached response"):
        await complete(
            CacheOnlyModel("m", Extraction),
            "p",
            "v1",
            ResponseCache(tmp_path),
            Budget(Decimal(1), PRICING),
        )


def test_estimates_are_generous_and_bounded() -> None:
    estimate = estimate_usage("z" * 24_000)
    assert estimate.input_tokens == 11_500
    assert estimate.output_tokens == 1_500 + int(11_500 * 1.6)
    assert estimate_usage("z" * 240_000).output_tokens == 32_000


def test_models_are_named_with_their_effort() -> None:
    settings = Settings(llm_model="claude-sonnet-5-5", llm_effort="low")
    assert model_label(settings) == "claude-sonnet-5-5@low"
    assert model_label(settings, "claude-haiku-5-5") == "claude-haiku-5-5@low"
    assert model_label(Settings(llm_effort="")) == "claude-haiku-5-5"
    assert model_label(Settings(llm_provider="openai", llm_model="gpt-x")) == "gpt-x"


def test_prices_are_known_or_configured() -> None:
    assert pricing_for("claude-haiku-5-5@low", Settings()) == Pricing(
        Decimal("0.10"), Decimal("0.50")
    )
    assert pricing_for("unknown-model", Settings()) is None
    configured = Settings(llm_input_price=Decimal(1), llm_output_price=Decimal(3))
    assert pricing_for("unknown-model", configured) == Pricing(Decimal(1), Decimal(3))


def test_building_a_model_needs_a_key_and_a_known_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(LlmError, match="no API key"):
        build_model(Settings())
    with pytest.raises(LlmError, match="LLM_PROVIDER"):
        build_model(Settings(llm_provider="nope", llm_api_key="k"))
    # The key is read from LLM_API_KEY or ANTHROPIC_API_KEY.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert Settings().llm_api_key is not None


async def test_langchain_models_use_structured_output_and_report_usage() -> None:
    calls: dict[str, Any] = {}

    class Message:
        def __init__(self) -> None:
            self.usage_metadata = {"input_tokens": 120, "output_tokens": 30}
            self.response_metadata = {"stop_reason": "end_turn"}

    class Runnable:
        async def ainvoke(self, prompt: str) -> dict[str, Any]:
            calls["prompt"] = prompt
            return {"raw": Message(), "parsed": EMPTY, "parsing_error": None}

    class ChatModel:
        def with_structured_output(self, schema: type, **options: Any) -> Runnable:
            calls["schema"], calls["options"] = schema, options
            return Runnable()

    model = LangChainModel(ChatModel(), "chat@low", Extraction)
    extraction, usage = await model.extract("hello")
    assert (extraction, usage) == (EMPTY, Usage(120, 30))
    assert calls["schema"] is Extraction
    assert calls["options"] == {"method": "json_schema", "include_raw": True}

    class Unparsed(Runnable):
        async def ainvoke(self, prompt: str) -> dict[str, Any]:
            return {"raw": Message(), "parsed": None, "parsing_error": ValueError("bad")}

    class BrokenChat(ChatModel):
        def with_structured_output(self, schema: type, **options: Any) -> Runnable:
            return Unparsed()

    with pytest.raises(LlmError) as raised:
        await LangChainModel(BrokenChat(), "chat", Extraction).extract("hello")
    assert raised.value.usage == Usage(120, 30)


class MemoryLedger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str | None, Usage, Decimal]] = []

    async def record(
        self, model: str, prompt_version: str | None, usage: Usage, cost: Decimal
    ) -> None:
        self.records.append((model, prompt_version, usage, cost))


async def test_paid_calls_are_recorded_and_cached_ones_are_not(tmp_path: Path) -> None:
    ledger = MemoryLedger()
    budget = Budget(Decimal(1), PRICING, ledger=ledger)
    model = FakeModel(lambda p: EMPTY)
    await complete(model, "prompt", "v1", ResponseCache(tmp_path), budget)
    await complete(model, "prompt", "v1", ResponseCache(tmp_path), budget)
    assert ledger.records == [("fake-model@test", "v1", USAGE, Decimal("0.007"))]

    class Broken(FakeModel):
        async def extract(self, prompt: str) -> tuple[Extraction, Usage]:
            raise LlmError("no structured answer", USAGE)

    with pytest.raises(LlmError):
        await complete(Broken(lambda p: EMPTY), "other", "v1", ResponseCache(tmp_path), budget)
    assert len(ledger.records) == 2  # an unusable answer is still paid for


async def test_the_total_budget_refuses_a_call_that_could_exceed_it(tmp_path: Path) -> None:
    prompt = "x" * 2_400
    worst = PRICING.cost(estimate_usage(prompt))
    budget = Budget(
        Decimal(100), PRICING, limit=Decimal("2.50"), spent_before=Decimal("2.50") - worst / 2
    )
    model = FakeModel(lambda p: EMPTY)
    with pytest.raises(BudgetExhausted) as refused:
        await complete(model, prompt, "v1", ResponseCache(tmp_path), budget)
    message = str(refused.value)
    assert "of $2.50 spent" in message and "remaining" in message and "LLM_BUDGET_USD" in message
    assert model.prompts == []  # refused before calling
    # Within the budget, the call is made and counted.
    roomy = Budget(Decimal(100), PRICING, limit=Decimal("2.50"), spent_before=Decimal(1))
    await complete(model, prompt, "v1", ResponseCache(tmp_path), roomy)
    assert roomy.total_spent == Decimal("1.007")
