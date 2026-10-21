"""The look shared by every page: styles, verdict badges and their legend, the header,
the footer, and how errors and markdown text are shown."""

import html
import re
from collections.abc import Iterable

import streamlit as st

from talk_is_cheap.ui.client import ApiError, NotFound, Unreachable
from talk_is_cheap.ui.format import TONE_COLOURS, Verdict

TITLE = "Talk Is Cheap"
ICON = "⚖️"
PITCH = "What executives said in public, and what actually happened."

STYLES = """
<style>
.tic-pitch { font-size: 1.15rem; opacity: 0.8; margin: -0.6rem 0 0.4rem 0; }
.tic-badge {
  display: inline-block; padding: 0.05rem 0.55rem; border-radius: 999px;
  font-size: 0.8rem; font-weight: 600; line-height: 1.5; white-space: nowrap;
  border: 1.5px solid transparent; color: #ffffff; vertical-align: middle;
}
.tic-badge.outline { color: inherit; border-color: currentColor; background: transparent; }
.tic-legend { display: flex; flex-wrap: wrap; gap: 0.4rem 0.9rem; align-items: center;
  font-size: 0.85rem; margin: 0.2rem 0 0.6rem 0; }
.tic-legend span.tic-item { display: inline-flex; gap: 0.35rem; align-items: center; }
.tic-quote { border-left: 3px solid rgba(128, 128, 128, 0.5); padding: 0.2rem 0 0.2rem 0.9rem;
  font-size: 1.05rem; margin: 0.4rem 0 0.6rem 0; }
.tic-footer { font-size: 0.8rem; opacity: 0.7; margin-top: 2.5rem;
  border-top: 1px solid rgba(128, 128, 128, 0.3); padding-top: 0.6rem; }
</style>
"""

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|$~])")


def plain(text: str) -> str:
    """Text shown as itself in markdown: no emphasis, links or LaTeX ("$96.2 billion")."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def styles() -> None:
    st.html(STYLES)


def badge_html(verdict: Verdict) -> str:
    colour = TONE_COLOURS[verdict.tone]
    kind = " outline" if verdict.tone == "outline" else ""
    return (
        f'<span class="tic-badge{kind}" style="background:{colour}" '
        f'title="{html.escape(verdict.meaning)}">{html.escape(verdict.label)}</span>'
    )


def badge(verdict: Verdict) -> None:
    st.markdown(badge_html(verdict), unsafe_allow_html=True)


def legend(verdicts: Iterable[Verdict]) -> None:
    items = "".join(f'<span class="tic-item">{badge_html(v)}</span>' for v in verdicts)
    st.markdown(f'<div class="tic-legend">{items}</div>', unsafe_allow_html=True)


def header() -> None:
    st.title(f"{ICON} {TITLE}")
    st.markdown(f'<p class="tic-pitch">{html.escape(PITCH)}</p>', unsafe_allow_html=True)


def footer() -> None:
    st.markdown(
        '<div class="tic-footer">Data: SEC EDGAR filings (XBRL financial facts, 10-Q, '
        "10-K and earnings 8-K text), company press releases, and news articles quoted "
        "with a link to each publisher. Verdicts are drafted by a model and checked in "
        "code; reviewed verdicts win. Not investment advice.</div>",
        unsafe_allow_html=True,
    )


def show_error(exc: ApiError, what: str) -> None:
    """An API failure as one plain sentence, never a traceback."""
    if isinstance(exc, NotFound):
        st.warning(f"{what}: {exc}")
    elif isinstance(exc, Unreachable):
        st.error(f"{what}. {exc}", icon=":material/cloud_off:")
    else:
        st.error(f"{what}. {exc}", icon=":material/error:")
