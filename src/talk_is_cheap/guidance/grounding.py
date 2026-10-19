"""Find a model's supporting quote in the source text, and where it is.

Quotes are compared after normalizing what a copy can change without changing the
words: curly and straight quotes, dashes, no-break spaces, runs of whitespace, letter case,
and the markup of the converted text (`**bold**`, table pipes, `#` headings, escaping
backslashes). A quote that cuts text out with an ellipsis matches when its pieces appear in
order, and covers the text from the first piece to the last.
"""

import re

_EQUIVALENT = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u202f": " ",
        "\u2009": " ",
        "\u200b": " ",
        "*": " ",
        "|": " ",
        "#": " ",
        "\\": " ",
        "\u2022": " ",
    }
)
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|\u2026)\s*")
# A piece shorter than this could match by accident.
_MIN_PIECE = 8


def normalize(text: str) -> tuple[str, list[int]]:
    """The normalized text, and for each of its characters the index in `text` it came
    from."""
    out: list[str] = []
    origin: list[int] = []
    for index, original in enumerate(text):
        # Per character, since case folding can lengthen one ("ß" → "ss").
        for char in original.translate(_EQUIVALENT).casefold():
            if char.isspace():
                if not out or out[-1] == " ":
                    continue
                char = " "
            out.append(char)
            origin.append(index)
    if out and out[-1] == " ":
        out.pop()
        origin.pop()
    return "".join(out), origin


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """The character range of `text` that `quote` reproduces, or None."""
    pieces = [p for p in (normalize(p)[0].strip() for p in _ELLIPSIS.split(quote)) if p]
    if not pieces or (len(pieces) > 1 and any(len(p) < _MIN_PIECE for p in pieces)):
        return None
    haystack, origin = normalize(text)
    start = end = -1
    position = 0
    for piece in pieces:
        found = haystack.find(piece, position)
        if found < 0:
            return None
        if start < 0:
            start = found
        position = end = found + len(piece)
    return origin[start], origin[end - 1] + 1
