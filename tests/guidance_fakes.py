"""A deterministic stand-in for the extraction model, so tests never call a real one."""

from collections.abc import Callable

from talk_is_cheap.guidance.llm import Usage
from talk_is_cheap.guidance.schema import Extraction

USAGE = Usage(input_tokens=1_000, output_tokens=500)


class FakeModel:
    """Answers each prompt with `respond(prompt)` and records the prompts it was given."""

    def __init__(self, respond: Callable[[str], Extraction], name: str = "fake-model@test") -> None:
        self._respond = respond
        self._name = name
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def extract(self, prompt: str) -> tuple[Extraction, Usage]:
        self.prompts.append(prompt)
        return self._respond(prompt), USAGE
