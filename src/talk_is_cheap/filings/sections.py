"""Find and tag the sections of a converted filing document.

Every section is a character range of the document text (`html_text.html_to_text`), so
its text is always `text[char_start:char_end]` and can be cited by offset.

Earnings press releases and other earnings exhibits are split on their headings. Each
section is tagged by its heading: `outlook` for guidance, `results` for results,
highlights and financial statements, and `other` (or `commentary` in exhibits such as CFO
commentary) for the rest. A release section made up mostly of tables of figures is
`results` whatever its heading ("MICROSOFT CORPORATION", "Segment Information"). Many
companies give no guidance in their release; such a release simply has no `outlook`
section, and an outlook section that only says guidance is given on the earnings call
has no guidance (`has_guidance`). Deliveries reports and exhibits unrelated to earnings
are split the same way, with every section `other`.

10-Q and 10-K documents are split on their item headings ("Item 1A. Risk Factors",
"ITEM 7 -", "PART II - Item 1A", ...) into MD&A (`mdna`: 10-K Item 7, 10-Q Part I Item 2)
and risk factors (`risk_factors`: 10-K Item 1A, 10-Q Part II Item 1A). An item heading
can appear several times: in the table of contents, as running page headers, and in
cross references. Repeats of the same item with no other item between them are one
section; of the remaining candidates the longest is the item itself, since a table of
contents entry runs only to the next entry. Some companies (Intel) title their sections
instead of numbering them, so a heading that names the section ("Management's Discussion
and Analysis", "Risk Factors") is a candidate too, ending at the next heading that names
another standard section, or later when page footers show the section continues.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum

from talk_is_cheap.filings.html_text import Page


class SectionType(StrEnum):
    OUTLOOK = "outlook"
    RESULTS = "results"
    OTHER = "other"
    COMMENTARY = "commentary"
    MDNA = "mdna"
    RISK_FACTORS = "risk_factors"


@dataclass(frozen=True, slots=True)
class Section:
    section_type: SectionType
    title: str
    char_start: int
    char_end: int

    def text(self, document: str) -> str:
        return document[self.char_start : self.char_end]


@dataclass(frozen=True, slots=True)
class _Line:
    start: int
    end: int
    raw: str

    @property
    def plain(self) -> str:
        """The line without markdown heading, bold and list markers."""
        return _MARKUP.sub("", self.raw).strip()

    @property
    def is_heading(self) -> bool:
        return self.raw.startswith("#")

    @property
    def is_emphasised(self) -> bool:
        return self.raw.startswith(("#", "**"))


_MARKUP = re.compile(r"^#+\s*|\*\*|^-\s+")


def _lines(text: str) -> Iterator[_Line]:
    for match in re.finditer(r"[^\n]+", text):
        yield _Line(match.start(), match.end(), match.group())


# --- earnings releases and exhibits -------------------------------------------------------

_OUTLOOK = re.compile(
    r"\b(outlook|guidance|expect(s|ed|ations)?|forward|forecasts?|targets?|estimates)\b",
    re.IGNORECASE,
)
# A release headline ("Adobe Reports Record Revenue ... Raises Targets") reports results
# even when it mentions targets or guidance.
_HEADLINE = re.compile(r"\b(reports|announces|delivers|posts)\b", re.IGNORECASE)
_NOT_OUTLOOK = re.compile(r"forward[- ]looking\s+statements?|safe harbor|cautionary", re.IGNORECASE)
_RESULTS = re.compile(
    r"\b(results?|highlights?|summary|financial (information|data|statements?)"
    r"|statements? of (income|operations|earnings|cash flows?|comprehensive)"
    r"|balance sheets?|cash flows?|reconciliations?|revenue|earnings)\b",
    re.IGNORECASE,
)
# Headings that only name a period ("Fourth Quarter Fiscal 2026", "Q3 FY27", "Full Year
# 2026") subdivide the section above them, typically an outlook.
_PERIOD_ONLY = re.compile(
    r"^(?:(?:first|second|third|fourth|full|fiscal|calendar)\b|q[1-4]\b|fy\s?\d{2,4}\b)"
    r"[\w\s'\u2019-]*$",
    re.IGNORECASE,
)


def tag_heading(title: str, *, default: SectionType) -> SectionType:
    if default is SectionType.OTHER and _HEADLINE.search(title):
        return SectionType.RESULTS
    if _OUTLOOK.search(title) and not _NOT_OUTLOOK.search(title):
        return SectionType.OUTLOOK
    if default is SectionType.OTHER and _RESULTS.search(title):
        return SectionType.RESULTS
    return default


def press_release_sections(text: str) -> list[Section]:
    """Sections of an earnings press release: outlook, results or other."""
    return _heading_sections(text, default=SectionType.OTHER)


def exhibit_sections(text: str) -> list[Section]:
    """Sections of another earnings exhibit (CFO commentary, prepared remarks)."""
    return _heading_sections(text, default=SectionType.COMMENTARY)


def untagged_sections(text: str) -> list[Section]:
    """Sections of a document that reports no results or guidance, all `other`."""
    return _heading_sections(text, default=SectionType.OTHER, tagged=False)


# Outlooks that only point to the earnings call, or decline to give guidance.
_GUIDANCE_ELSEWHERE = re.compile(
    r"\b(?:will|to)\s+(?:provide|give|discuss|share|offer)\b[^.]{0,80}?\b(?:guidance|outlook)\b"
    r"[^.]{0,120}?\b(?:call|webcast)\b"
    r"|\b(?:does|do|will)\s+not\s+(?:provide|give|issue)\s+(?:\w+\s+){0,2}(?:guidance|outlook)\b",
    re.IGNORECASE,
)
_FIGURE = re.compile(
    r"[$\u20ac\u00a3]\s?\d|\d\s?%|\d\s*(?:percent|basis points|billion|million)\b", re.I
)


def has_guidance(section_text: str) -> bool:
    """Whether an outlook section gives guidance, rather than saying where it is given."""
    body = section_text.split("\n", 1)[1] if "\n" in section_text else ""
    if not body.strip():
        return False
    return bool(_FIGURE.search(body)) or not _GUIDANCE_ELSEWHERE.search(body)


_TABLE_FIGURE = re.compile(r"^[$\u20ac\u00a3(]*-?\d[\d,.]*\s*[)%]*$")


def _mostly_figure_tables(body: str) -> bool:
    """Whether a section is mostly tables of figures, such as financial statements."""
    lines = [line for line in body.split("\n") if line.strip()]
    rows = [line for line in lines if line.startswith("|") and not line.startswith("| ---")]
    if len(rows) < 3 or len(rows) * 2 < len(lines):
        return False
    cells = [c.strip() for row in rows for c in row.strip("|").split(" | ") if c.strip()]
    return sum(bool(_TABLE_FIGURE.match(cell)) for cell in cells) * 10 >= len(cells) * 3


def _heading_sections(text: str, *, default: SectionType, tagged: bool = True) -> list[Section]:
    headings = [line for line in _lines(text) if line.is_heading]
    starts = [0] if not headings or text[: headings[0].start].strip() else []
    bounds = [(start, "") for start in starts]
    bounds += [(line.start, line.plain) for line in headings]

    sections: list[Section] = []
    pending: list[tuple[int, str]] = []  # headings directly above another heading
    for index, (start, title) in enumerate(bounds):
        end = bounds[index + 1][0] if index + 1 < len(bounds) else len(text)
        body = text[start:end]
        if title and not body[len(body.split("\n", 1)[0]) :].strip():
            pending.append((start, title))
            continue
        if pending:
            start = pending[0][0]
            title = " / ".join([*(t for _, t in pending), title])
            pending = []
        section_type = tag_heading(title, default=default) if title and tagged else default
        if tagged and section_type is SectionType.OTHER and _mostly_figure_tables(text[start:end]):
            section_type = SectionType.RESULTS
        previous = sections[-1] if sections else None
        if (
            previous is not None
            and previous.section_type is SectionType.OUTLOOK
            and _PERIOD_ONLY.match(title)
        ):
            sections[-1] = Section(
                previous.section_type,
                previous.title,
                previous.char_start,
                _trim_end(text, previous.char_start, end),
            )
            continue
        sections.append(Section(section_type, title, start, _trim_end(text, start, end)))
    if pending:
        start, title = pending[0][0], " / ".join(t for _, t in pending)
        sections.append(Section(default, title, start, _trim_end(text, start, len(text))))
    return sections


def _trim_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


# --- 10-Q and 10-K ------------------------------------------------------------------------

_ITEM = re.compile(
    r"^(?:part\s+(?P<part>iv|i{1,3})\b[\s,.:\-\u2013\u2014]*)?"
    r"item[\s\xa0]*(?P<item>\d{1,2}[a-c]?)\b\s*[.:\-\u2013\u2014]*\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_PART = re.compile(
    r"^part\s+(?P<part>iv|i{1,3})\b[\s,.:\-\u2013\u2014]*(?P<title>.*)$", re.IGNORECASE
)
_HEADING_MAX_CHARS = 200
_RUNNING_HEADER_REPEATS = 3

_MDNA_TITLE = re.compile(r"^management['\u2019`]?s?\s+discussion\s+and\s+analysis", re.IGNORECASE)
_RISK_TITLE = re.compile(r"^risk\s+factors\b(?!\s+summary)", re.IGNORECASE)
_MDNA_FOOTER = re.compile(
    r"(?:MD&A|management['\u2019`]s\s+discussion\s+and\s+analysis)(?:\s+\d{1,3})?", re.IGNORECASE
)
_RISK_FOOTER = re.compile(r"risk\s+factors(?:\s+\d{1,3})?", re.IGNORECASE)
_NOT_MDNA_ITEM_2 = re.compile(r"unregistered|sales\s+of\s+equity|properties", re.IGNORECASE)
# Titles of the standard 10-K and 10-Q sections, which end a titled section.
_STANDARD_TITLE = re.compile(
    r"(?:item\s*\d{1,2}[a-c]?\W*)?(?:"
    r"(?:our\s+)?business|risk\s+factors(?:\s+and\s+other\s+key\s+information)?"
    r"|other\s+key\s+information|unresolved\s+staff\s+comments|cybersecurity|properties"
    r"|legal\s+proceedings|mine\s+safety\s+disclosures|market\s+for\s+(?:the\s+)?registrant.*"
    r"|\[reserved\]|selected\s+financial\s+data|management['\u2019`]?s\s+discussion\s+and\s+analysis.*"
    r"|quantitative\s+and\s+qualitative\s+disclosures?\s+about\s+market\s+risks?"
    r"|(?:consolidated\s+(?:condensed\s+)?)?financial\s+statements(?:\s+and\s+\w+\s+\w+)?"
    r"|report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm"
    r"|changes\s+in\s+and\s+disagreements.*|controls\s+and\s+procedures|other\s+information"
    r"|unregistered\s+sales\s+of\s+equity\s+securities.*|defaults\s+upon\s+senior\s+securities"
    r"|exhibits.*|signatures?"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Heading:
    start: int
    title: str
    part: str | None  # "I", "II", ... when known
    item: str  # "1A", "7", ...


def _item_headings(text: str) -> list[_Heading]:
    headings: list[_Heading] = []
    part: str | None = None
    lines = list(_lines(text))
    for index, line in enumerate(lines):
        if line.raw.startswith("|"):
            continue  # inside a table, such as the table of contents
        plain = line.plain
        if len(plain) > _HEADING_MAX_CHARS and not line.is_emphasised:
            continue
        part_match = _PART.match(plain)
        item_match = _ITEM.match(plain)
        if item_match:
            if item_match.group("part"):
                part = item_match.group("part").upper()
            title = item_match.group("title").strip()
            if not title and index + 1 < len(lines):
                title = lines[index + 1].plain
            headings.append(_Heading(line.start, plain, part, item_match.group("item").upper()))
        elif part_match and len(plain) <= _HEADING_MAX_CHARS:
            # A PART heading sets the part of the items after it. It is not a boundary
            # itself: filers repeat "PART I" with the item as a running page header.
            part = part_match.group("part").upper()
    return headings


def _item_title(heading: _Heading, text: str) -> str:
    match = _ITEM.match(heading.title)
    title = match.group("title").strip() if match else ""
    if title:
        return title
    rest = text[heading.start :].split("\n", 3)
    return _MARKUP.sub("", rest[2] if len(rest) > 2 else "").strip()


def periodic_report_sections(text: str, form: str, pages: Sequence[Page] = ()) -> list[Section]:
    """MD&A and risk factors of a 10-Q or 10-K. A section that cannot be found is absent.
    `pages` are the document's pages, whose removed footers can name their section."""
    annual = form.upper().startswith("10-K")
    headings = _collapse_repeats(_item_headings(text))

    def is_mdna(heading: _Heading) -> bool:
        if annual:
            return heading.item == "7"
        return (
            heading.item == "2"
            and heading.part != "II"
            and not _NOT_MDNA_ITEM_2.search(_item_title(heading, text))
        )

    def is_risk(heading: _Heading) -> bool:
        return heading.item == "1A"  # Part I of a 10-Q has no Item 1A

    sections = []
    for section_type, matches, title_pattern, footer in (
        (SectionType.MDNA, is_mdna, _MDNA_TITLE, _MDNA_FOOTER),
        (SectionType.RISK_FACTORS, is_risk, _RISK_TITLE, _RISK_FOOTER),
    ):
        found = _longest_item(text, headings, matches)
        if found is None:
            found = _longest_titled(text, headings, title_pattern, footer, pages)
        if found is not None:
            start, end, title = found
            sections.append(Section(section_type, title, start, _trim_end(text, start, end)))
    return sorted(sections, key=lambda s: s.char_start)


def _collapse_repeats(headings: Sequence[_Heading]) -> list[_Heading]:
    collapsed: list[_Heading] = []
    for heading in headings:
        previous = collapsed[-1] if collapsed else None
        if previous is not None and (previous.item, previous.part or heading.part) == (
            heading.item,
            heading.part or previous.part,
        ):
            # A running header ("Item 2") repeats the item; keep the first start but the
            # first title that names the item ("ITEM 2. MANAGEMENT'S DISCUSSION ...").
            if not _named(previous) and _named(heading):
                collapsed[-1] = _Heading(
                    previous.start, heading.title, previous.part, previous.item
                )
            continue
        collapsed.append(heading)
    return collapsed


def _named(heading: _Heading) -> bool:
    match = _ITEM.match(heading.title)
    return bool(match and match.group("title").strip())


def _longest_item(
    text: str, headings: Sequence[_Heading], matches: Callable[[_Heading], bool]
) -> tuple[int, int, str] | None:
    best: tuple[int, int, str] | None = None
    for index, heading in enumerate(headings):
        if not matches(heading):
            continue
        end = next((h.start for h in headings[index + 1 :] if h.item != heading.item), len(text))
        if best is None or end - heading.start > best[1] - best[0]:
            best = (heading.start, end, heading.title)
    return best


def _longest_titled(
    text: str,
    headings: Sequence[_Heading],
    pattern: re.Pattern[str],
    footer: re.Pattern[str],
    pages: Sequence[Page],
) -> tuple[int, int, str] | None:
    """A section that is titled but not numbered: from a heading that names it to the next
    heading that is the title of a different standard section, or the next item heading.
    Headings repeated as running page headers ("Table of Contents") are not boundaries.
    When pages carry a running footer naming the section ("MD&A 35"), the section runs at
    least through the last page with that footer, since a standard section can sit on a
    page inside it (Intel places Properties within MD&A)."""
    all_lines = list(_lines(text))
    lines = [line for line in all_lines if line.is_heading]
    counts = Counter(line.plain.lower() for line in lines)
    footers = [line.end for line in all_lines if footer.fullmatch(line.plain)]
    footers += [
        page.end for page in pages if any(footer.fullmatch(item) for item in page.furniture)
    ]
    best: tuple[int, int, str] | None = None
    for index, line in enumerate(lines):
        if not pattern.match(line.plain):
            continue
        end = next(
            (
                following.start
                for following in lines[index + 1 :]
                if counts[following.plain.lower()] < _RUNNING_HEADER_REPEATS
                and _STANDARD_TITLE.fullmatch(following.plain.rstrip(".:"))
                and not pattern.match(following.plain)
            ),
            len(text),
        )
        end = min([end, *(h.start for h in headings if h.start > line.start)])
        end = max([end, *(f for f in footers if f > line.start)])
        end = _trim_end(text, line.start, end)
        if best is None or end - line.start > best[1] - best[0]:
            best = (line.start, end, line.plain)
    return best
