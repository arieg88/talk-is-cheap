"""The chat model behind guidance extraction, its response cache, and what calls cost.

The model is any LangChain chat model that supports structured output, chosen by
configuration (`LLM_PROVIDER`, `LLM_MODEL`). Responses are cached on disk by model,
prompt version and a hash of the request, so a re-run makes no call it has made before.
Every call that is not cached is priced, and a run stops before a call that could take it
over its cost cap.

LangChain and the provider SDKs are imported only when a model is built.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from talk_is_cheap.guidance.schema import Extraction

if TYPE_CHECKING:
    from talk_is_cheap.config import Settings


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True, slots=True)
class Pricing:
    """US dollars per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal

    def cost(self, usage: Usage) -> Decimal:
        return (
            self.input_per_mtok * usage.input_tokens + self.output_per_mtok * usage.output_tokens
        ) / 1_000_000


# Published list prices (checked 2026-10-19). Other models need LLM_INPUT_PRICE and
# LLM_OUTPUT_PRICE, since a run must know what it spends.
PRICES: Mapping[str, Pricing] = {
    "claude-opus-5-5": Pricing(Decimal("4.00"), Decimal("20.00")),
    "claude-sonnet-5-5": Pricing(Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-5-5": Pricing(Decimal("0.10"), Decimal("0.50")),
}

# LangChain integration per provider: (module, chat model class).
PROVIDERS: Mapping[str, tuple[str, str]] = {
    "anthropic": ("langchain_anthropic", "ChatAnthropic"),
    "openai": ("langchain_openai", "ChatOpenAI"),
    "google_genai": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
}

# Estimates for --dry-run and the cost cap, before a call is made, measured on the 24
# labeled documents with Haiku, Sonnet and Opus 5.5 (docs/guidance.md): input was 1,258
# tokens (instructions and the output schema) plus one per 2.64 characters of prompt;
# output was at most 1.57 times the input. Both are rounded up.
INPUT_OVERHEAD_TOKENS = 1_500
CHARS_PER_TOKEN = 2.4
OUTPUT_TOKENS_PER_CALL = 1_500
OUTPUT_TOKENS_PER_INPUT_TOKEN = 1.6
MAX_OUTPUT_TOKENS = 32_000


def estimate_usage(prompt: str) -> Usage:
    """A generous estimate of one call's tokens, for planning and the cost cap."""
    input_tokens = INPUT_OVERHEAD_TOKENS + math.ceil(len(prompt) / CHARS_PER_TOKEN)
    output = OUTPUT_TOKENS_PER_CALL + int(input_tokens * OUTPUT_TOKENS_PER_INPUT_TOKEN)
    return Usage(input_tokens, min(output, MAX_OUTPUT_TOKENS))


class LlmError(RuntimeError):
    """The model could not be configured or did not return a usable answer. `usage` is
    what an unusable answer still cost."""

    def __init__(self, message: str, usage: "Usage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage


class CostCapReached(LlmError):
    """The next call could take the run over its cost cap."""


class ExtractionModel(Protocol):
    """Turns a prompt into guidance items. `name` identifies the model and its settings in
    the cache and in stored rows."""

    @property
    def name(self) -> str: ...

    async def extract(self, prompt: str) -> tuple[Extraction, Usage]: ...


class LangChainModel:
    """A LangChain chat model with structured output (`with_structured_output` and the
    provider's JSON-schema mode)."""

    def __init__(self, chat_model: Any, name: str) -> None:
        self._name = name
        self._runnable = chat_model.with_structured_output(
            Extraction, method="json_schema", include_raw=True
        )

    @property
    def name(self) -> str:
        return self._name

    async def extract(self, prompt: str) -> tuple[Extraction, Usage]:
        result = await self._runnable.ainvoke(prompt)
        raw = result["raw"]
        metadata = getattr(raw, "usage_metadata", None) or {}
        usage = Usage(int(metadata.get("input_tokens", 0)), int(metadata.get("output_tokens", 0)))
        if result.get("parsing_error") is not None or result.get("parsed") is None:
            stop = (getattr(raw, "response_metadata", None) or {}).get("stop_reason")
            raise LlmError(
                f"no structured answer (stop reason {stop}): {result['parsing_error']}", usage
            )
        parsed = result["parsed"]
        return Extraction.model_validate(parsed), usage


class CacheOnlyModel:
    """Answers only from the cache: for runs that need no new call, which then need no
    API key."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def extract(self, prompt: str) -> tuple[Extraction, Usage]:
        raise LlmError("no cached response and no API key to make a call")


def model_label(settings: "Settings", model: str | None = None) -> str:
    """The name responses and rows are stored under: the model, and for Anthropic models
    its effort ("claude-sonnet-5-5@low")."""
    name = model or settings.llm_model
    if settings.llm_provider == "anthropic" and settings.llm_effort:
        return f"{name}@{settings.llm_effort}"
    return name


def build_model(settings: "Settings", model: str | None = None) -> LangChainModel:
    """The configured chat model. Raises LlmError when the provider is unknown, its
    LangChain package is missing, or no API key is set."""
    provider = settings.llm_provider
    name = model or settings.llm_model
    if provider not in PROVIDERS:
        raise LlmError(f"LLM_PROVIDER {provider!r} is not one of {', '.join(PROVIDERS)}")
    if settings.llm_api_key is None:
        raise LlmError("no API key: set LLM_API_KEY (or ANTHROPIC_API_KEY) in .env")
    module_name, class_name = PROVIDERS[provider]
    try:
        chat_class = getattr(import_module(module_name), class_name)
    except ImportError as exc:
        raise LlmError(f"provider {provider!r} needs the {module_name} package") from exc
    options: dict[str, Any] = {
        "model": name,
        "api_key": settings.llm_api_key.get_secret_value(),
        "max_tokens": MAX_OUTPUT_TOKENS,
        "max_retries": 4,
        "timeout": 300,
    }
    if provider == "anthropic" and settings.llm_effort:
        options["effort"] = settings.llm_effort
    return LangChainModel(chat_class(**options), model_label(settings, name))


def pricing_for(model_name: str, settings: "Settings") -> Pricing | None:
    """List price of a model (its name without the "@effort" suffix), or the configured
    prices, which take precedence."""
    if settings.llm_input_price is not None and settings.llm_output_price is not None:
        return Pricing(settings.llm_input_price, settings.llm_output_price)
    return PRICES.get(model_name.split("@", 1)[0])


@dataclass(frozen=True, slots=True)
class Response:
    extraction: Extraction
    usage: Usage
    cost: Decimal  # what the call cost when it was made
    cached: bool


class ResponseCache:
    """One JSON file per response: `<dir>/<model>/<prompt version>/<request hash>.json`."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @staticmethod
    def request_hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode()).hexdigest()

    def path(self, model: str, prompt_version: str, prompt: str) -> Path:
        safe_model = model.replace("/", "_").replace(":", "_")
        return self._directory / safe_model / prompt_version / f"{self.request_hash(prompt)}.json"

    def get(self, model: str, prompt_version: str, prompt: str) -> Response | None:
        path = self.path(model, prompt_version, prompt)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Response(
            Extraction.model_validate(data["extraction"]),
            Usage(data["input_tokens"], data["output_tokens"]),
            Decimal(data["cost"]),
            cached=True,
        )

    def put(self, model: str, prompt_version: str, prompt: str, response: Response) -> None:
        path = self.path(model, prompt_version, prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "model": model,
            "prompt_version": prompt_version,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost": str(response.cost),
            "extraction": response.extraction.model_dump(mode="json"),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
        temporary.replace(path)


class BudgetExhausted(CostCapReached):
    """The next call could take total spending over LLM_BUDGET_USD."""


class Ledger(Protocol):
    """Where paid calls are recorded (`talk_is_cheap.spend.DatabaseLedger`)."""

    async def record(
        self, model: str, prompt_version: str | None, usage: Usage, cost: Decimal
    ) -> None: ...


class Budget:
    """Spending of one run against its cap (LLM_COST_CAP) and, when `limit` is set,
    against the project's total budget (LLM_BUDGET_USD), of which `spent_before` was
    spent by earlier runs. Calls in flight hold their estimated cost until they finish,
    so parallel calls cannot overrun either together. Every paid call is recorded in
    the ledger as soon as it returns."""

    def __init__(
        self,
        cap: Decimal,
        pricing: Pricing,
        *,
        limit: Decimal | None = None,
        spent_before: Decimal = Decimal(0),
        ledger: Ledger | None = None,
    ) -> None:
        self.cap = cap
        self.pricing = pricing
        self.limit = limit
        self.spent_before = spent_before
        self.ledger = ledger
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self.usage = Usage(0, 0)
        self.calls = 0

    @property
    def total_spent(self) -> Decimal:
        return self.spent_before + self.spent

    def reserve(self, prompt: str) -> Decimal:
        """Hold the estimated cost of a call, or raise CostCapReached (this run) or
        BudgetExhausted (the total budget) if it could exceed either."""
        worst = self.pricing.cost(estimate_usage(prompt))
        if self.limit is not None and self.total_spent + self.reserved + worst > self.limit:
            remaining = max(self.limit - self.total_spent, Decimal(0))
            raise BudgetExhausted(
                f"LLM budget: ${self.total_spent:.4f} of ${self.limit:.2f} spent, "
                f"${remaining:.4f} remaining; the next call could cost up to ${worst:.4f}, "
                "so it was not made (raise LLM_BUDGET_USD to continue)"
            )
        if self.spent + self.reserved + worst > self.cap:
            raise CostCapReached(
                f"cost cap ${self.cap} reached: ${self.spent:.4f} spent, the next call could "
                f"cost up to ${worst:.4f} (raise LLM_COST_CAP to continue)"
            )
        self.reserved += worst
        return worst

    def settle(self, reservation: Decimal, usage: Usage | None) -> Decimal:
        """Release a reservation and count what the call used (None if it failed
        without a response)."""
        self.reserved -= reservation
        if usage is None:
            return Decimal(0)
        cost = self.pricing.cost(usage)
        self.spent += cost
        self.usage += usage
        self.calls += 1
        return cost


async def complete(
    model: ExtractionModel,
    prompt: str,
    prompt_version: str,
    cache: ResponseCache,
    budget: Budget,
) -> Response:
    """The cached response for this request, or a new call within the budget."""
    found = cache.get(model.name, prompt_version, prompt)
    if found is not None:
        return found
    reservation = budget.reserve(prompt)
    try:
        extraction, usage = await model.extract(prompt)
    except LlmError as exc:
        await _paid(budget, model.name, prompt_version, reservation, exc.usage)
        raise
    except BaseException:
        budget.settle(reservation, None)
        raise
    cost = await _paid(budget, model.name, prompt_version, reservation, usage)
    response = Response(extraction, usage, cost, cached=False)
    cache.put(model.name, prompt_version, prompt, response)
    return response


async def _paid(
    budget: Budget, model: str, prompt_version: str, reservation: Decimal, usage: Usage | None
) -> Decimal:
    cost = budget.settle(reservation, usage)
    if usage is not None and budget.ledger is not None:
        await budget.ledger.record(model, prompt_version, usage, cost)
    return cost
