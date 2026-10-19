"""Split filing sections and news articles into chunks for retrieval.

A filing chunk lies inside one section and is cut on paragraph boundaries, aiming at
`TARGET_TOKENS` tokens of the embedding model's own tokenizer, with about
`OVERLAP_SHARE` of a chunk repeated from the end of the one before. A paragraph longer
than that is cut between sentences, and a sentence longer than the model's limit between
words. A markdown table stays whole when it fits; a longer one is cut between rows, and
every piece starts with the table's header rows.

Every chunk records the character range of the document it covers. Its text is exactly
that range, except for a piece of a split table, whose text is the repeated header rows,
a newline, and then exactly the range.

What gets embedded is a short context header ("NVDA · Nvidia · FY2027 Q2 · press
release · outlook"), a blank line and the chunk text. No chunk may make that longer than
the model's maximum input: `chunk_section` raises `ChunkTooLong` rather than letting the
model truncate it.
"""

import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

TARGET_TOKENS = 450  # chunks are filled up to this many tokens of text
OVERLAP_SHARE = 0.15
HEADER_SEPARATOR = "\n\n"


class TokenCounter(Protocol):
    """The embedding model's tokenizer, as far as chunking needs it."""

    @property
    def max_input_tokens(self) -> int:
        """The longest input the model reads, special tokens included."""
        ...

    @property
    def special_tokens(self) -> int:
        """Special tokens the model adds to every input ([CLS] and [SEP] for BERT)."""
        ...

    def count(self, texts: Sequence[str]) -> list[int]:
        """Tokens in each text, special tokens not included, without truncation."""
        ...


class ChunkTooLong(ValueError):
    """A chunk would exceed the model's maximum input and be silently truncated."""


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    char_start: int  # into the document
    char_end: int
    token_count: int  # of the embedded input: header, separator and text, specials included


def embedded_text(header: str, text: str) -> str:
    """The text the model embeds for a chunk: its context header, then its text."""
    return f"{header}{HEADER_SEPARATOR}{text}" if header else text


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Kind(StrEnum):
    PROSE = "prose"
    TABLE = "table"  # a whole table
    TABLE_PART = "table_part"  # rows of a split table, with `prefix` holding its header


@dataclass(frozen=True, slots=True)
class _Piece:
    start: int  # into the section text
    end: int
    tokens: int
    kind: _Kind
    prefix: str = ""


# A sentence ends after . ! ? ; or : (and any closing quotes), before a capital, digit,
# opening quote, currency sign or bullet.
_SENTENCE_END = re.compile(r"(?<=[.!?;:])[\"'”’)]*\s+(?=[\"'“‘(\[$€£0-9A-Z•\-*])")  # noqa: RUF001
_WORD = re.compile(r"\S+")
_TABLE_SEPARATOR = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")


def chunk_section(
    text: str,
    offset: int,
    header: str,
    tokenizer: TokenCounter,
    *,
    target: int = TARGET_TOKENS,
    overlap: float = OVERLAP_SHARE,
) -> list[Chunk]:
    """Chunks of one section's `text`, which starts at character `offset` of its document."""
    if not text.strip():
        return []
    header_tokens = tokenizer.count([header + HEADER_SEPARATOR])[0] if header else 0
    limit = tokenizer.max_input_tokens - tokenizer.special_tokens - header_tokens
    if limit < tokenizer.max_input_tokens // 2:
        raise ChunkTooLong(f"the context header leaves only {limit} tokens: {header!r}")
    target = min(target, limit)
    pieces = _pieces(text, tokenizer, target, limit)
    spans = _pack(text, pieces, tokenizer, target, int(target * overlap))

    chunks = []
    for prefix, start, end in spans:
        body = (prefix + "\n" if prefix else "") + text[start:end]
        chunks.append((body, start, end))
    counts = tokenizer.count([embedded_text(header, body) for body, _, _ in chunks])
    result = []
    for (body, start, end), count in zip(chunks, counts, strict=True):
        total = count + tokenizer.special_tokens
        if total > tokenizer.max_input_tokens:
            raise ChunkTooLong(
                f"chunk of {total} tokens exceeds the model's {tokenizer.max_input_tokens}: "
                f"{body[:80]!r}"
            )
        result.append(Chunk(body, offset + start, offset + end, total))
    return result


def chunk_article(title: str, summary: str | None, header: str, tokenizer: TokenCounter) -> Chunk:
    """One chunk per article: its headline and the feed's summary. Its character range is
    of that text, since articles have no stored document."""
    text = title if not summary else f"{title}\n\n{summary}"
    (count,) = tokenizer.count([embedded_text(header, text)])
    total = count + tokenizer.special_tokens
    if total > tokenizer.max_input_tokens:
        # Summaries are capped at 500 characters, so this cannot happen with a real model.
        raise ChunkTooLong(f"article of {total} tokens: {title[:80]!r}")
    return Chunk(text, 0, len(text), total)


def _blocks(text: str) -> Iterator[tuple[int, int, bool]]:
    """(start, end, is_table) of each paragraph and table. Paragraphs are separated by
    blank lines; a table is a run of lines starting with "|"."""
    position = 0
    block_start: int | None = None
    block_table = False
    previous_end = 0
    for line in text.splitlines(keepends=True):
        start, end = position, position + len(line.rstrip("\n"))
        position += len(line)
        stripped = line.strip()
        is_table = stripped.startswith("|")
        if not stripped or (block_start is not None and is_table != block_table):
            if block_start is not None:
                yield block_start, previous_end, block_table
            block_start = None
        if stripped:
            if block_start is None:
                block_start, block_table = start, is_table
            previous_end = end
    if block_start is not None:
        yield block_start, previous_end, block_table


def _pieces(text: str, tokenizer: TokenCounter, target: int, limit: int) -> list[_Piece]:
    blocks = list(_blocks(text))
    counts = tokenizer.count([text[s:e] for s, e, _ in blocks])
    pieces: list[_Piece] = []
    for (start, end, is_table), tokens in zip(blocks, counts, strict=True):
        if is_table and tokens <= limit:
            pieces.append(_Piece(start, end, tokens, _Kind.TABLE))
        elif is_table:
            pieces.extend(_table_parts(text, start, end, tokenizer, target, limit))
        elif tokens <= target:
            pieces.append(_Piece(start, end, tokens, _Kind.PROSE))
        else:
            pieces.extend(_split_prose(text, start, end, tokenizer, limit))
    return pieces


def _sentences(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans, position = [], start
    for match in _SENTENCE_END.finditer(text, start, end):
        spans.append((position, match.start()))
        position = match.end()
    spans.append((position, end))
    return [(s, e) for s, e in spans if e > s]


def _split_prose(
    text: str, start: int, end: int, tokenizer: TokenCounter, limit: int
) -> list[_Piece]:
    """A long paragraph as sentences; a sentence over the limit as runs of words."""
    sentences = _sentences(text, start, end)
    counts = tokenizer.count([text[s:e] for s, e in sentences])
    pieces = []
    for (s, e), tokens in zip(sentences, counts, strict=True):
        if tokens <= limit:
            pieces.append(_Piece(s, e, tokens, _Kind.PROSE))
        else:
            pieces.extend(_split_words(text, s, e, tokenizer, limit, _Kind.PROSE))
    return pieces


def _split_words(
    text: str,
    start: int,
    end: int,
    tokenizer: TokenCounter,
    limit: int,
    kind: _Kind,
    prefix: str = "",
) -> list[_Piece]:
    words = [(m.start(), m.end()) for m in _WORD.finditer(text, start, end)]
    counts = tokenizer.count([text[s:e] for s, e in words])
    prefix_tokens = tokenizer.count([prefix])[0] if prefix else 0
    pieces: list[_Piece] = []
    run_start, run_tokens, run_end = words[0][0], prefix_tokens, words[0][0]
    for (s, e), tokens in zip(words, counts, strict=True):
        if run_tokens + tokens > limit and run_end > run_start:
            pieces.append(_Piece(run_start, run_end, run_tokens, kind, prefix))
            run_start, run_tokens = s, prefix_tokens
        run_tokens += tokens
        run_end = e
    pieces.append(_Piece(run_start, run_end, run_tokens, kind, prefix))
    return pieces


def _table_parts(
    text: str, start: int, end: int, tokenizer: TokenCounter, target: int, limit: int
) -> list[_Piece]:
    """A table too long for one chunk, as runs of rows that each start with the header
    rows: every row up to the `| --- |` line (only the first row when there is none)."""
    rows = []
    position = start
    for line in text[start:end].split("\n"):
        rows.append((position, position + len(line)))
        position += len(line) + 1
    header_rows = 1
    for index, (s, e) in enumerate(rows[:4]):
        if _TABLE_SEPARATOR.match(text[s:e]):
            header_rows = index + 1
            break
    header = text[rows[0][0] : rows[header_rows - 1][1]]
    body = rows[header_rows:]
    if not body:
        return _split_words(text, start, end, tokenizer, limit, _Kind.PROSE)
    header_tokens = tokenizer.count([header])[0]
    if header_tokens > limit // 2:  # a header that long cannot usefully repeat
        return _split_words(text, start, end, tokenizer, limit, _Kind.PROSE)
    counts = tokenizer.count([text[s:e] for s, e in body])
    pieces: list[_Piece] = []
    run: list[tuple[int, int]] = []
    run_tokens = header_tokens
    budget = max(target, header_tokens + 1)

    def flush() -> None:
        if run:
            pieces.append(_Piece(run[0][0], run[-1][1], run_tokens, _Kind.TABLE_PART, header))

    for (s, e), tokens in zip(body, counts, strict=True):
        if header_tokens + tokens > limit:  # one row longer than a chunk
            flush()
            run, run_tokens = [], header_tokens
            pieces.extend(_split_words(text, s, e, tokenizer, limit, _Kind.TABLE_PART, header))
            continue
        if run and run_tokens + tokens > budget:
            flush()
            run, run_tokens = [], header_tokens
        run.append((s, e))
        run_tokens += tokens
    flush()
    return pieces


def _pack(
    text: str,
    pieces: Sequence[_Piece],
    tokenizer: TokenCounter,
    target: int,
    overlap_tokens: int,
) -> list[tuple[str, int, int]]:
    """Group consecutive pieces into chunks of at most `target` tokens: (prefix, start,
    end). A chunk that follows prose starts with the last sentences of the chunk before,
    up to `overlap_tokens`. Pieces of split tables are chunks of their own."""
    spans: list[tuple[str, int, int]] = []
    current: list[_Piece] = []
    tokens = 0
    carry: tuple[int, int] | None = None  # (start, tokens) of the overlap to begin with

    def emit() -> None:
        nonlocal current, tokens, carry
        if not current:
            return
        start = carry[0] if carry else current[0].start
        spans.append(("", start, current[-1].end))
        carry = _overlap(text, current[-1], tokenizer, overlap_tokens)
        current, tokens = [], 0

    for piece in pieces:
        if piece.kind is _Kind.TABLE_PART:
            emit()
            spans.append((piece.prefix, piece.start, piece.end))
            carry = None
            continue
        if current and tokens + piece.tokens > target:
            emit()
        if not current:
            tokens = carry[1] if carry else 0
            if tokens + piece.tokens > target:
                carry, tokens = None, 0  # no room for overlap before this piece
        current.append(piece)
        tokens += piece.tokens
    emit()
    return spans


def _overlap(
    text: str, last: _Piece, tokenizer: TokenCounter, budget: int
) -> tuple[int, int] | None:
    """The trailing sentences of a prose piece that fit in `budget` tokens."""
    if last.kind is not _Kind.PROSE or budget <= 0:
        return None
    sentences = _sentences(text, last.start, last.end)
    counts = tokenizer.count([text[s:e] for s, e in sentences])
    start, total = None, 0
    for (s, _), tokens in zip(reversed(sentences), reversed(counts), strict=True):
        if total + tokens > budget:
            break
        start, total = s, total + tokens
    return None if start is None else (start, total)
