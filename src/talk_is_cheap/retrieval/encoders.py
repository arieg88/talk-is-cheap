"""Embedding and reranking models behind small interfaces.

The defaults run locally through fastembed (ONNX Runtime, no torch): BAAI/bge-small-en-v1.5
for embeddings (384 dimensions, 512 input tokens) and Xenova/ms-marco-MiniLM-L-6-v2 as the
cross-encoder reranker (settings EMBEDDING_MODEL and RERANK_MODEL). fastembed is imported
only when a model is created, so commands that do not index or search start without it.
Models are downloaded once into MODEL_CACHE_DIR.

Tests use the deterministic fakes in `tests/retrieval_fakes.py`.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from talk_is_cheap.config import Settings
from talk_is_cheap.retrieval.chunking import TokenCounter

if TYPE_CHECKING:
    from tokenizers import Tokenizer


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def tokenizer(self) -> TokenCounter: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Unit-length vectors, one per text. No text may exceed the model's input."""
        ...

    def embed_query(self, query: str) -> list[float]: ...


class Reranker(Protocol):
    @property
    def model_name(self) -> str: ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Relevance of each passage to the query; higher is more relevant."""
        ...


class HuggingFaceTokenCounter:
    """Counts tokens with a model's own tokenizer, with truncation turned off, so a text
    over the limit shows its real length instead of being cut to fit."""

    def __init__(self, tokenizer: "Tokenizer") -> None:
        from tokenizers import Tokenizer

        truncation = tokenizer.truncation
        if not truncation or not truncation.get("max_length"):
            raise ValueError("the tokenizer does not state the model's maximum input length")
        self._max = int(truncation["max_length"])
        self._tokenizer = Tokenizer.from_str(tokenizer.to_str())
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()
        self._special = len(self._tokenizer.encode("").ids)

    @property
    def max_input_tokens(self) -> int:
        return self._max

    @property
    def special_tokens(self) -> int:
        return self._special

    def count(self, texts: Sequence[str]) -> list[int]:
        if not texts:
            return []
        encodings = self._tokenizer.encode_batch(list(texts), add_special_tokens=False)
        return [len(e.ids) for e in encodings]


class FastEmbedEmbedder:
    # One text per model call: chunks differ in length, and a batch pads every text to its
    # longest. Measured on 192 real chunks (16 threads): 13.0 chunks/s at 1, 8.0 at 4, 5.9
    # at 16, with the same vectors.
    def __init__(self, model_name: str, cache_dir: Path, batch_size: int = 1) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir))
        self._name = model_name
        self._batch_size = batch_size
        description = next(
            m for m in TextEmbedding.list_supported_models() if m["model"] == model_name
        )
        self._dimensions = int(description["dim"])
        self._tokenizer = HuggingFaceTokenCounter(_tokenizer_of(self._model))

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def tokenizer(self) -> TokenCounter:
        return self._tokenizer

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self._check_lengths(texts)
        return [v.tolist() for v in self._model.passage_embed(texts, batch_size=self._batch_size)]

    def embed_query(self, query: str) -> list[float]:
        (vector,) = self._model.query_embed(query)
        return [float(x) for x in vector]

    def _check_lengths(self, texts: Sequence[str]) -> None:
        limit = self._tokenizer.max_input_tokens - self._tokenizer.special_tokens
        for text, count in zip(texts, self._tokenizer.count(texts), strict=True):
            if count > limit:
                raise ValueError(
                    f"{count + self._tokenizer.special_tokens} tokens exceed the model's "
                    f"{self._tokenizer.max_input_tokens} and would be truncated: {text[:80]!r}"
                )


class FastEmbedReranker:
    """A cross-encoder. A query and passage longer than its input together lose the end
    of the passage, which only affects the score, not what is returned."""

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model_name, cache_dir=str(cache_dir))
        self._name = model_name

    @property
    def model_name(self) -> str:
        return self._name

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(passages), batch_size=8)]


def load_embedder(settings: Settings) -> FastEmbedEmbedder:
    return FastEmbedEmbedder(settings.embedding_model, settings.model_cache_dir)


def load_reranker(settings: Settings) -> FastEmbedReranker:
    return FastEmbedReranker(settings.rerank_model, settings.model_cache_dir)


def _tokenizer_of(model: Any) -> "Tokenizer":
    # fastembed keeps the loaded tokenizer on the ONNX model it wraps.
    tokenizer: Tokenizer = model.model.tokenizer
    return tokenizer
