"""Scorecard: a company's formal guidance from its earnings releases against what it
then reported, as first reported.

Only comparable rows count in beat, met and missed: rows flagged by the plausibility
check and not reviewed, rows reviewed as extraction errors, pending rows and rows with
nothing reported to compare with are left out, and the summary says how many and why.
The company is in the URL (`?company=NVDA`).
"""

from typing import Any

import streamlit as st

from talk_is_cheap.api.schemas import CompanyCoverageOut, GuidanceOut
from talk_is_cheap.ui import data, theme
from talk_is_cheap.ui.client import ApiError
from talk_is_cheap.ui.format import (
    GUIDANCE_VERDICTS,
    TONE_COLOURS,
    actual,
    company_name,
    count,
    guided,
    metric_name,
    period,
    scorecard,
)

DEFAULT_TICKER = "NVDA"
ROW_PIXELS = 35
MAX_PIXELS = 720
BASIS = {"gaap": "GAAP", "non_gaap": "non-GAAP", "unspecified": "basis not stated"}


def render() -> None:
    api = data.client()
    try:
        companies = data.companies(api)
    except ApiError as exc:
        theme.show_error(exc, "Could not load the companies")
        return
    if not companies:
        st.info("No companies are loaded yet, so there is no guidance to score.")
        return
    ordered = sorted(companies, key=lambda c: (c.guidance_rows == 0, c.ticker))
    by_ticker = {c.ticker: c for c in ordered}
    linked = (st.query_params.get("company") or DEFAULT_TICKER).upper()
    default = list(by_ticker).index(linked) if linked in by_ticker else 0
    top = st.columns([1.4, 2.2, 1.2])
    ticker: str = top[0].selectbox(
        "Company",
        list(by_ticker),
        index=default,
        format_func=lambda t: _company_option(by_ticker[t]),
        key="score-company",
    )
    st.query_params["company"] = ticker
    company = by_ticker[ticker]
    try:
        rows = data.guidance(api, ticker)
    except ApiError as exc:
        theme.show_error(exc, f"Could not load {ticker}'s guidance")
        return
    name = company_name(company.name)
    if not rows:
        st.info(
            f"{name} gives no numeric guidance in its earnings releases, so there is nothing "
            "to score. Some companies (Apple, Alphabet) give none; others give it only on "
            "the earnings call, which is not stored."
        )
        return
    metrics = sorted({r.metric for r in rows})
    chosen: list[str] = top[1].multiselect(
        "Metric",
        metrics,
        format_func=metric_name,
        placeholder="Every metric",
        key="score-metrics",
    )
    comparable_only = top[2].toggle("Comparable rows only", key="score-comparable")
    selected = [r for r in rows if not chosen or r.metric in chosen]

    _summary(selected, name)
    st.subheader("Guidance against results")
    theme.legend(GUIDANCE_VERDICTS.values())
    shown = [r for r in selected if not comparable_only or _counts(r)]
    if not shown:
        st.info("No comparable rows for this selection.")
        return
    _table(shown)


def _company_option(c: CompanyCoverageOut) -> str:
    rows = count(c.guidance_rows, "row")
    return f"{company_name(c.name)} ({c.ticker}) · {rows}"


def _counts(r: GuidanceOut) -> bool:
    return r.counts_in_verdicts and r.verdict in ("beat", "met", "missed")


def _summary(rows: list[GuidanceOut], name: str) -> None:
    card = scorecard(rows)
    st.subheader(f"Scorecard: {name}")
    tiles = st.columns(4)
    tiles[0].metric("Comparable rows", f"{card.comparable}", help=f"Of {card.rows} rows")
    for tile, verdict in zip(tiles[1:], ("beat", "met", "missed"), strict=True):
        n = card.counted[verdict]
        share = f" · {n / card.comparable:.0%}" if card.comparable else ""
        tile.metric(GUIDANCE_VERDICTS[verdict].label.capitalize(), f"{n}{share}")
    reasons = card.exclusions()
    if card.comparable == 0:
        st.info(
            f"None of {count(card.rows, 'row')} can be compared with a reported result yet"
            + (f": {'; '.join(reasons)}." if reasons else ".")
        )
    elif reasons:
        st.caption(
            f"Counted: {card.comparable} of {count(card.rows, 'row')}. Left out: "
            + "; ".join(reasons)
            + "."
        )


def _table(rows: list[GuidanceOut]) -> None:
    labels = [v.label for v in GUIDANCE_VERDICTS.values()]
    colours = [TONE_COLOURS[v.tone] for v in GUIDANCE_VERDICTS.values()]
    table: list[dict[str, Any]] = []
    for r in rows:
        metric = metric_name(r.metric)
        if r.segment:
            metric += f" ({r.segment})"
        source = r.sources[0] if r.sources else None
        table.append(
            {
                "Issued": f"{period(r.issued_period)} · {r.issued_at.isoformat()}",
                "For": period(r.target_period),
                "Metric": metric,
                "Basis": BASIS.get(r.basis, r.basis)
                + (" (assumed GAAP)" if r.basis_assumed else ""),
                "Guided": guided(r),
                "Actual": actual(r),
                "Verdict": [GUIDANCE_VERDICTS[r.verdict].label],
                "Flag": _flag(r),
                "Why not compared": r.verdict_reason or "",
                "Release": source.url if source else None,
            }
        )
    st.dataframe(
        table,
        hide_index=True,
        height=min(ROW_PIXELS * (len(table) + 1) + 3, MAX_PIXELS),
        column_config={
            "Issued": st.column_config.TextColumn(width="medium"),
            "For": st.column_config.TextColumn(width="small", help="The target period"),
            "Guided": st.column_config.TextColumn(width="medium"),
            "Actual": st.column_config.TextColumn(
                width="small", help="As first reported, in the guidance's unit"
            ),
            "Verdict": st.column_config.MultiselectColumn(
                "Verdict", options=labels, color=colours, width="small"
            ),
            "Flag": st.column_config.TextColumn(
                width="small",
                help="Flagged as implausible against the prior period (left out until "
                "reviewed), or reviewed",
            ),
            "Release": st.column_config.LinkColumn(display_text="Release ↗", width="small"),
        },
    )


def _flag(r: GuidanceOut) -> str:
    if r.mark == "REVIEW":
        return "⚑ needs review"
    if r.mark == "WRONG":
        return "✕ reviewed: wrong"
    if r.review_status:
        return "✓ " + r.review_status
    return ""
