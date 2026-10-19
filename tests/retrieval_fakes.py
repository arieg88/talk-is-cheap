"""Deterministic stand-ins for the tokenizer, embedding model and reranker, so retrieval
tests run without downloading a model."""

import hashlib
import math
import re
from collections.abc import Sequence

_WORD = re.compile(r"[a-z0-9$%.]+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class FakeTokenizer:
    """One token per whitespace-separated word, and two special tokens per input."""

    def __init__(self, max_input_tokens: int = 512) -> None:
        self._max = max_input_tokens

    @property
    def max_input_tokens(self) -> int:
        return self._max

    @property
    def special_tokens(self) -> int:
        return 2

    def count(self, texts: Sequence[str]) -> list[int]:
        return [len(text.split()) for text in texts]


class FakeEmbedder:
    """Hashed bag of words: texts sharing words are close, so search behaves sensibly."""

    def __init__(self, dimensions: int = 64, model_name: str = "fake/bag-of-words-64") -> None:
        self._dimensions = dimensions
        self._name = model_name
        self._tokenizer = FakeTokenizer()
        self.embedded: list[str] = []

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def tokenizer(self) -> FakeTokenizer:
        return self._tokenizer

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for word in words(text):
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self._dimensions] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        if not any(vector):
            vector[0] = 1.0  # a zero vector has no cosine distance
            return vector
        return [x / norm for x in vector]


class FakeReranker:
    """Scores a passage by how many of the query's words it contains."""

    model_name = "fake/word-overlap"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        wanted = set(words(query))
        return [float(len(wanted & set(words(p)))) for p in passages]
