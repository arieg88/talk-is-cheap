"""Convert SEC filing HTML into plain text that keeps the structure later code relies on.

The output is markdown-flavoured:

- `<h1>`-`<h6>` become `#` lines. SEC documents rarely use them: headings are usually
  paragraphs whose text is entirely bold, underlined or set in large type, so such a
  paragraph that is short and does not end like a sentence also becomes a heading: `#`
  when set in title-sized type (16pt or more), `##` otherwise ("## Outlook",
  "## Item 7. ..."). Slide decks filed as HTML carry each slide's text in one block that
  opens with a letter-spaced title ("O U T L O O K ..."); that title becomes a heading.
- Other bold text stays bold as `**text**`. Risk-factor headings are bold sentences, so
  they come out as `**...**` paragraphs.
- Tables become markdown tables. Spacer columns are dropped and the `$`, `(`, `)` and `%`
  cells that SEC tables put beside numbers are joined to them. Tables used only for
  layout (one row, one column, or bullet + text) become ordinary paragraphs, one per row.
- Scripts, styles, comments, hidden elements and the inline XBRL header are removed.
- Whitespace, line breaks and zero-width spaces in and between text nodes become one
  space, never nothing; a single `<br>` is a space too, and `<br><br>` ends a paragraph.
  A paragraph that would read as markup (the "###" that ends a press release) is escaped
  with a backslash.
- Page furniture is removed: running headers and footers ("Apple Inc. | 2025 Form 10-K |
  5", "PART I / Item 2", "Table of Contents") and standalone page numbers. Pages come
  from the filing's page breaks; furniture is a block among the first or last three of a
  page that is a page number, or that recurs at page boundaries on at least three
  consecutive pages (digits ignored, so the page number may change). What was removed is
  kept per page (`Page.furniture`), since a footer can name the section a page belongs to.

The conversion is a pure function of its input, so the same HTML always gives the same
text and the same content hash.
"""

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from lxml import html as lxml_html
from lxml.etree import _Element

_SKIPPED_TAGS = frozenset(
    {"head", "script", "style", "title", "noscript", "template", "ix:header", "ix:hidden"}
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "body",
        "br",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "form",
        "header",
        "hr",
        "html",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "ul",
    }
)
_HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
_BOLD_TAGS = frozenset({"b", "strong"})

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_BREAK_BEFORE = re.compile(r"(?:page-)?break-before\s*:\s*(?:always|page)", re.IGNORECASE)
_BREAK_AFTER = re.compile(r"(?:page-)?break-after\s*:\s*(?:always|page)", re.IGNORECASE)
_FONT_WEIGHT = re.compile(r"font-weight\s*:\s*([a-z0-9]+)", re.IGNORECASE)
_UNDERLINE = re.compile(r"text-decoration[^;]*underline", re.IGNORECASE)
_FONT_SIZE = re.compile(r"font-size\s*:\s*([0-9.]+)\s*(pt|px)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
# Joiners and soft hyphens sit inside words and are dropped; a zero-width space separates
# words and becomes a space.
_INVISIBLE = re.compile("[\u200c\u200d\u2060\ufeff\u00ad]")
_ZERO_WIDTH_SPACE = "\u200b"
_LINE_BREAK = "\u2028"  # marks a <br> until the paragraph is rendered
_BULLET = re.compile(r"^[\u2022\u25cf\u25e6\u25aa\u25a0\u00b7\uf0b7\uf0a7\u2023\u2043]\s*")
_SENTENCE_END = (".", ",", ";", "?", "!")
# "O U T L O O K Volume ..." -> the title "OUTLOOK", then the slide's text; a slide whose
# text is only a letter-spaced title, with or without a page number, is a heading too.
_SPACED_TITLE = re.compile(r"^((?:\S ){3,}\S)(?: \d{1,3})?(?: (?=[A-Z][a-z])|$)")

HEADING_MAX_CHARS = 120
# Body text in filings is 8-12pt; text set larger is a heading even when not bold.
LARGE_TYPE_PT = 13.5
TITLE_TYPE_PT = 16.0
_PAGE_EDGE = 3  # blocks at each end of a page that can be headers or footers
_RUNNING_PAGES = 3  # consecutive pages a running header or footer appears on
_FURNITURE_MAX_CHARS = 120
_PAGE_NUMBER = re.compile(
    r"(?:[Pp]age\s+)?(?:[A-Z]-)?\d{1,3}(?:\s+of\s+\d{1,3})?|[-\u2013\u2014]\s*\d{1,3}\s*[-\u2013\u2014]"
    r"|[ivxl]{1,6}"
)
_NUMBER = re.compile("[$\u20ac\u00a3(]*\\s*(?:\\d[\\d,.]*|[-\u2013\u2014])\\s*[)%]*")
_PREFIX_FRAGMENTS = frozenset({"$", "(", "$(", "€", "£"})
_SUFFIX_FRAGMENTS = frozenset({"%", ")", ")%", "%)", "pts", "bps"})


def content_hash(text: str) -> str:
    """SHA-256 of the text, as hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Page:
    """One printed page: a character range of the converted text, and the running headers,
    footers and page numbers removed from it."""

    start: int
    end: int
    furniture: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConvertedText:
    text: str
    pages: tuple[Page, ...]


def html_to_text(document: str | bytes) -> str:
    """Convert one HTML document to structured plain text (see the module docstring)."""
    return convert_html(document).text


def convert_html(document: str | bytes) -> ConvertedText:
    """Convert one HTML document, keeping where its pages start and end."""
    raw = document.encode("utf-8") if isinstance(document, str) else document
    if not raw.strip():
        return ConvertedText("", ())
    parser = lxml_html.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True)
    root = lxml_html.document_fromstring(raw, parser=parser)
    converter = _Converter()
    converter.walk(root, _Style(bold=False, underline=False, size=None))
    converter.page_break()
    return _assemble(_remove_furniture(converter.pages))


def _assemble(pages: Sequence[tuple[list[str], list[str]]]) -> ConvertedText:
    parts: list[str] = []
    ranges: list[Page] = []
    offset = 0
    for kept, furniture in pages:
        start: int | None = None
        for block in kept:
            if parts:
                parts.append("\n\n")
                offset += 2
            if start is None:
                start = offset
            parts.append(block)
            offset += len(block)
        removed = tuple(_plain_block(block) for block in furniture)
        ranges.append(Page(offset if start is None else start, offset, removed))
    return ConvertedText("".join(parts), tuple(ranges))


def _remove_furniture(pages: Sequence[list[str]]) -> list[tuple[list[str], list[str]]]:
    """Split each page's blocks into kept text and page furniture. Headers and footers are
    told apart: Intel's "Risk Factors" footer must not remove its "Risk Factors" title,
    which opens a page."""
    if len(pages) < 2:
        return [(list(blocks), []) for blocks in pages]
    seen: defaultdict[tuple[str, str], set[int]] = defaultdict(set)
    for number, blocks in enumerate(pages):
        for edge, position in _edges(blocks):
            if _can_be_furniture(blocks[position]):
                seen[edge, _furniture_key(blocks[position])].add(number)
    running = {key for key, numbers in seen.items() if _has_run(numbers, _RUNNING_PAGES)}

    result = []
    for blocks in pages:
        furniture = {
            position
            for edge, position in _edges(blocks)
            if _can_be_furniture(blocks[position])
            and (
                _PAGE_NUMBER.fullmatch(_plain_block(blocks[position]))
                or (edge, _furniture_key(blocks[position])) in running
            )
        }
        result.append(
            (
                [b for i, b in enumerate(blocks) if i not in furniture],
                [b for i, b in enumerate(blocks) if i in furniture],
            )
        )
    return result


def _edges(blocks: Sequence[str]) -> list[tuple[str, int]]:
    """The positions where a page header ("top") or footer ("bottom") can be. On a short
    page the top half can hold headers and the bottom half footers."""
    count = len(blocks)
    middle = (count + 1) // 2
    top = [("top", i) for i in range(min(_PAGE_EDGE, middle))]
    return top + [("bottom", i) for i in range(max(count - _PAGE_EDGE, middle), count)]


def _can_be_furniture(block: str) -> bool:
    return not block.startswith("|") and len(block) <= _FURNITURE_MAX_CHARS


def _plain_block(block: str) -> str:
    return re.sub(r"^#+\s*|\*\*|^\\", "", block).strip()


def _furniture_key(block: str) -> str:
    return _WHITESPACE.sub(" ", re.sub(r"\d+", "#", _plain_block(block).lower()))


def _has_run(numbers: set[int], length: int) -> bool:
    return any(all(n + k in numbers for k in range(length)) for n in numbers)


@dataclass(frozen=True, slots=True)
class _Style:
    bold: bool
    underline: bool
    size: float | None  # font size in points, when set

    def of(self, element: _Element) -> "_Style":
        return _Style(
            _is_bold(element, inherited=self.bold),
            self.underline or _tag(element) == "u" or bool(_UNDERLINE.search(_css(element))),
            _font_size(element, self.size),
        )


@dataclass(slots=True)
class _Run:
    text: str
    bold: bool
    underline: bool = False
    size: float | None = None

    @property
    def emphasis(self) -> tuple[bool, bool, float | None]:
        return self.bold, self.underline, self.size

    @property
    def large(self) -> bool:
        return self.size is not None and self.size >= LARGE_TYPE_PT


@dataclass(slots=True)
class _Converter:
    pages: list[list[str]] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    runs: list[_Run] = field(default_factory=list)

    def walk(self, element: _Element, inherited: _Style) -> None:
        css = _css(element)
        if _BREAK_BEFORE.search(css):
            self.page_break()
        self.element(element, inherited)
        if _BREAK_AFTER.search(css):
            self.page_break()

    def element(self, element: _Element, inherited: _Style) -> None:
        tag = _tag(element)
        if tag in _HEADING_TAGS:
            self.flush()
            text = _plain(_inline_text(element))
            if text:
                self.blocks.append(f"{'#' * _HEADING_TAGS[tag]} {text}")
            return
        style = inherited.of(element)
        if tag == "table":
            self.table(element, style)
            return
        if tag == "br":
            self.line_break()
            return

        is_block = tag in _BLOCK_TAGS
        if is_block:
            self.flush()
        self.inline(element, style)
        if is_block:
            self.flush()

    def line_break(self) -> None:
        """One <br> is a space within the paragraph; a second one in a row ends it."""
        for run in reversed(self.runs):
            if run.text == _LINE_BREAK:
                self.flush()
                return
            if run.text.strip():
                break
        self.runs.append(_Run(_LINE_BREAK, bold=False))

    def page_break(self) -> None:
        self.flush()
        if self.blocks:
            self.pages.append(self.blocks)
            self.blocks = []

    def inline(self, element: _Element, style: _Style) -> None:
        if element.text:
            self.runs.append(_Run(element.text, style.bold, style.underline, style.size))
        for child in element:
            if not _skipped(child):
                self.walk(child, style)
            if child.tail:
                self.runs.append(_Run(child.tail, style.bold, style.underline, style.size))

    def table(self, table: _Element, style: _Style) -> None:
        self.flush()
        rows = _table_rows(table)
        if not rows:
            return
        if not _is_layout(rows):
            self.blocks.append(_render_table(rows))
            return
        for row in _visible_rows(table):
            row_style = style.of(row)
            for cell in row:
                if _tag(cell) in {"td", "th"} and not _skipped(cell):
                    self.inline(cell, row_style.of(cell))
                    self.runs.append(_Run(" ", bold=False))
            self.flush()

    def flush(self) -> None:
        if self.runs:
            paragraph = _render_paragraph(self.runs)
            self.runs = []
            if paragraph:
                self.blocks.append(paragraph)


def _tag(element: _Element) -> str:
    tag = element.tag
    return tag.lower() if isinstance(tag, str) else ""


def _skipped(element: _Element) -> bool:
    if not isinstance(element.tag, str):  # comments and processing instructions
        return True
    if _tag(element) in _SKIPPED_TAGS or element.get("hidden") is not None:
        return True
    return bool(_HIDDEN_STYLE.search(_css(element)))


def _css(element: _Element) -> str:
    return element.get("style") or ""


def _font_size(element: _Element, inherited: float | None) -> float | None:
    match = _FONT_SIZE.search(_css(element))
    if match is None:
        return inherited
    size = float(match.group(1))
    return size * 0.75 if match.group(2).lower() == "px" else size


def _is_bold(element: _Element, *, inherited: bool) -> bool:
    if _tag(element) in _BOLD_TAGS:
        return True
    match = _FONT_WEIGHT.search(_css(element))
    if match is None:
        return inherited
    weight = match.group(1).lower()
    if weight in {"bold", "bolder"}:
        return True
    return weight.isdigit() and int(weight) >= 600


def _clean(text: str) -> str:
    text = _INVISIBLE.sub("", text.replace(_ZERO_WIDTH_SPACE, " "))
    return _WHITESPACE.sub(" ", text)


def _plain(text: str) -> str:
    return _clean(text).strip()


def _render_paragraph(runs: list[_Run]) -> str:
    merged: list[_Run] = []
    for run in runs:
        text = _clean(run.text)
        if not text:
            continue
        if not text.strip():
            run = _Run(text, bold=False)
        else:
            run = _Run(text, run.bold, run.underline, run.size)
        if merged and (not text.strip() or merged[-1].emphasis == run.emphasis):
            merged[-1].text += text
        else:
            merged.append(run)
    if not any(run.text.strip() for run in merged):
        return ""

    prefix = ""
    first = merged[0]
    bullet = _BULLET.match(first.text.lstrip())
    if bullet:
        prefix = "- "
        first.text = first.text.lstrip()[bullet.end() :]
        merged = [run for run in merged if run.text.strip()] if not first.text.strip() else merged
        if not merged:
            return ""

    plain = _plain("".join(run.text for run in merged))
    visible = [run for run in merged if run.text.strip()]
    if not prefix and _looks_like_heading(plain):
        if all(run.large for run in visible):
            title = all((run.size or 0) >= TITLE_TYPE_PT for run in visible)
            return f"{'#' if title else '##'} {plain}"
        if all(run.bold for run in visible) or all(run.underline for run in visible):
            return f"## {plain}"
    spaced = _SPACED_TITLE.match(plain)
    if spaced:
        heading, rest = spaced.group(1).replace(" ", ""), plain[spaced.end() :]
        return f"## {heading}\n\n{rest}" if rest else f"## {heading}"
    if not prefix and all(run.bold for run in visible):
        return f"**{plain}**"

    parts = []
    for run in merged:
        if run.bold:
            leading = " " if run.text[:1].isspace() else ""
            trailing = " " if run.text[-1:].isspace() else ""
            parts.append(f"{leading}**{run.text.strip()}**{trailing}")
        else:
            parts.append(run.text)
    paragraph = _WHITESPACE.sub(" ", "".join(parts)).strip()
    if not prefix and paragraph.startswith(("#", "|")):
        return "\\" + paragraph  # text, not a heading or a table row
    return prefix + paragraph


def _looks_like_heading(text: str) -> bool:
    if len(text) > HEADING_MAX_CHARS or text.endswith(_SENTENCE_END):
        return False
    return sum(character.isalpha() for character in text) >= 2


def _inline_text(element: _Element) -> str:
    """All visible text under `element`, with block boundaries turned into spaces."""
    return "".join(_visible_text(element))


def _visible_text(element: _Element) -> Iterator[str]:
    block = _tag(element) in _BLOCK_TAGS or _tag(element) in {"td", "th", "tr"}
    if block:
        yield " "
    if element.text:
        yield element.text
    for child in element:
        if not _skipped(child):
            yield from _visible_text(child)
        if child.tail:
            yield child.tail
    if block:
        yield " "


def _is_layout(rows: list[list[str]]) -> bool:
    """A table that only positions text: one row, one column, or bullets beside text."""
    if len(rows) == 1 or len(rows[0]) == 1:
        return True
    return len(rows[0]) == 2 and all(_BULLET.match(row[0]) for row in rows)


def _render_table(rows: list[list[str]]) -> str:
    width = len(rows[0])
    lines = [_table_line(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(_table_line(row) for row in rows[1:])
    return "\n".join(lines)


def _table_line(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _visible_rows(table: _Element) -> Iterator[_Element]:
    for row in table.iter("tr"):
        if _nearest_table(row) is table and not _hidden_within(row, table):
            yield row


def _table_rows(table: _Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in _visible_rows(table):
        cells: list[str] = []
        for cell in row:
            if _tag(cell) not in {"td", "th"} or _skipped(cell):
                continue
            text = _plain(_inline_text(cell))
            padding = [""] * (_colspan(cell) - 1)
            # Numbers are right-aligned: a number spanning the "$" and value columns of
            # the rows that show a "$" belongs in the value column.
            cells.extend([*padding, text] if _NUMBER.fullmatch(text) else [text, *padding])
        rows.append(_join_fragments(cells))

    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    rows = [[cells[c] for c in range(width) if any(r[c] for r in rows)] for cells in rows]
    # A header spanning "$ | 1,234" sits in a different column than its values: merge
    # neighbouring value columns that never both hold a value. The first column holds the
    # row labels and is never merged.
    column = 1
    while column < len(rows[0]) - 1:
        if any(cells[column] and cells[column + 1] for cells in rows):
            column += 1
            continue
        for cells in rows:
            cells[column] = cells[column] or cells[column + 1]
            del cells[column + 1]
    return rows


def _join_fragments(cells: list[str]) -> list[str]:
    """Join the `$`, `(`, `)` and `%` cells SEC tables place beside numbers onto them."""
    cells = list(cells)
    for index, cell in enumerate(cells):
        if cell in _PREFIX_FRAGMENTS:
            target = next((i for i in range(index + 1, len(cells)) if cells[i]), None)
            if target is not None and cells[target] not in _PREFIX_FRAGMENTS:
                cells[target] = cell + cells[target]
                cells[index] = ""
        elif cell in _SUFFIX_FRAGMENTS:
            target = next((i for i in range(index - 1, -1, -1) if cells[i]), None)
            if target is not None:
                cells[target] = cells[target] + cell
                cells[index] = ""
    return cells


def _nearest_table(element: _Element) -> _Element | None:
    parent = element.getparent()
    while parent is not None and _tag(parent) != "table":
        parent = parent.getparent()
    return parent


def _hidden_within(element: _Element, container: _Element) -> bool:
    current: _Element | None = element
    while current is not None and current is not container:
        if _skipped(current):
            return True
        current = current.getparent()
    return False


def _colspan(cell: _Element) -> int:
    try:
        return max(1, min(50, int(cell.get("colspan") or 1)))
    except ValueError:
        return 1
