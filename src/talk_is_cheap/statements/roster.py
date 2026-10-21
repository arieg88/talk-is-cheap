"""Speakers in the news matched to the executive roster in `universe.yaml`, in code.

A speaker is management only if the name the article gives matches an executive who held
a role on the statement date: "Jensen Huang" matches Nvidia's "Jen-Hsun Huang" through
its alias, "Huang" alone matches when exactly one executive in the role on that date has
that surname, and "Colette M. Kress" matches "Colette Kress". Middle initials, titles
("Dr.", "Mr.") and punctuation are ignored; the rest must match closely
(`NAME_THRESHOLD`), so "Tim Cook" never matches "Tim Cooke's analyst". A speaker named
only by a bare title ("the CFO said") is the one executive with that title on the date.
"""

import re
import unicodedata
from collections.abc import Sequence
from datetime import date

from rapidfuzz import fuzz

from talk_is_cheap.universe import Executive, UniverseCompany

# rapidfuzz token_sort_ratio a full name must reach.
NAME_THRESHOLD = 90
_TITLES = re.compile(r"\b(?:dr|mr|mrs|ms|mx|sir|prof)\.?\s+", re.IGNORECASE)
_INITIAL = re.compile(r"\b[a-z]\b\.?")
_SUFFIX = re.compile(r"\b(?:jr|sr|ii|iii|iv)\b\.?")


def normalize_name(name: str) -> str:
    """Lower case, without accents, titles, single-letter initials, suffixes and
    punctuation: "Dr. Lisa T. Su" -> "lisa su"."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _TITLES.sub(" ", text).casefold()
    text = text.replace("\u2019", "'").replace("'s ", " ")
    text = re.sub(r"[^\w\s-]", " ", text).replace("-", " ")
    text = _SUFFIX.sub(" ", _INITIAL.sub(" ", text))
    return " ".join(text.split())


def match_executive(
    name: str | None, role: str | None, company: UniverseCompany, day: date
) -> Executive | None:
    """The roster entry of the executive `name` refers to, among those who held a role on
    `day`, or None. When one person held two roles that day (a CFO who was also interim
    co-CEO), the entry whose role is closest to `role` is returned."""
    serving = company.executives_on(day)
    if not name or not name.strip():
        return _by_title(role, company, serving)
    wanted = normalize_name(name)
    if not wanted:
        return None
    matches = [e for e in serving if any(_same_person(wanted, n) for n in e.names)]
    if not matches and " " not in wanted:
        by_surname = [e for e in serving if any(_surname(n) == wanted for n in e.names)]
        if len({e.name for e in by_surname}) == 1:
            matches = by_surname
    if not matches:
        return None
    return _closest_role(matches, role)


# A speaker named only by title ("the CFO said"): the title alone, maybe with the company.
_TITLES_ONLY = {
    "ceo": ("chief executive",),
    "chief executive": ("chief executive",),
    "chief executive officer": ("chief executive",),
    "cfo": ("chief financial", "financial officer"),
    "chief financial officer": ("chief financial", "financial officer"),
}
_FILLER = {"the", "its", "company", "company's", "s"}


def _by_title(
    role: str | None, company: UniverseCompany, serving: Sequence[Executive]
) -> Executive | None:
    """The executive a bare title refers to ("CFO", "the company's chief executive"), when
    exactly one held such a role on the day; a title naming anything else ("Arm CEO") is
    not matched."""
    if not role:
        return None
    words = [w for w in normalize_name(role).split() if w not in _FILLER]
    company_words = {w for n in company.names for w in normalize_name(n).split()}
    title = " ".join(w for w in words if w not in company_words)
    roles = _TITLES_ONLY.get(title)
    if roles is None:
        return None
    found = [e for e in serving if any(r in e.role.casefold() for r in roles)]
    return found[0] if len({e.name for e in found}) == 1 else None


def _same_person(wanted: str, roster_name: str) -> bool:
    known = normalize_name(roster_name)
    return wanted == known or fuzz.token_sort_ratio(wanted, known) >= NAME_THRESHOLD


def _surname(name: str) -> str:
    parts = normalize_name(name).split()
    return parts[-1] if parts else ""


def _closest_role(entries: Sequence[Executive], role: str | None) -> Executive:
    if role is None or len(entries) == 1:
        return entries[0]
    wanted = role.casefold()
    return max(entries, key=lambda e: fuzz.token_set_ratio(wanted, e.role.casefold()))


def roster_names(company: UniverseCompany) -> list[str]:
    """Every name and alias on the company's roster, for finding candidate articles."""
    return sorted({n for e in company.executives for n in e.names})


def roster_name(company: UniverseCompany, name: str) -> str:
    """The roster's name for an executive the press calls `name` ("Jensen Huang" is
    "Jen-Hsun Huang"), or `name` itself when no entry has it as a name or alias."""
    wanted = normalize_name(name)
    for executive in company.executives:
        if any(normalize_name(n) == wanted for n in executive.names):
            return executive.name
    return name
