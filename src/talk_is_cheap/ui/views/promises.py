"""Promises, the front page: management's public claims, each with its verdict, the
track record of the selection, and every claim's rule, evidence and checks on a click.

The pickers choose a company and an executive (the track record's group) and narrow the
table by verdict, due date and boldness; scheduled actions (splits, dividends) are left
out unless asked for, as track records leave them out. The company and a clicked claim's id
go in the URL (`?company=NVDA&claim=1812`), so both can be linked to.
"""

import hashlib
import html
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

import streamlit as st

from talk_is_cheap.api.schemas import PromiseDetail, PromiseOut, TrackRecordOut
from talk_is_cheap.ui import data, theme
from talk_is_cheap.ui.client import ApiClient, ApiError, TrackBy
from talk_is_cheap.ui.format import (
    COMPANY_SPEAKERS,
    PROMISE_VERDICTS,
    TONE_COLOURS,
    company_name,
    count,
    days,
    excerpt,
    percent,
    promise_state,
    record_summary,
    speaker_key,
    speaker_label,
    speaker_text,
)
from talk_is_cheap.ui.format import claims_summary as summary_of

ALL = "all"
CLAIM_PARAM = "claim"
COMPANY_PARAM = "company"
TABLE_ROW_PIXELS = 35
TABLE_MAX_PIXELS = 640
PROVENANCE_LABELS = {
    "sec_filing": "SEC filing",
    "company_press_release": "company press release",
    "news_report": "news report",
}
TOOL_LABELS = {
    "get_actuals": "reported financials (XBRL)",
    "search_filings": "filing text",
    "search_news": "news",
    "get_statements": "a later statement",
    "cite_value": "a cited figure",
    "compute": "a computed figure",
}
FINDING_LABELS = {
    "satisfies": "satisfies the rule",
    "partly": "partly meets the rule",
    "contradicts": "contradicts the rule",
    "walks_back": "walks the claim back",
}


def render() -> None:
    api = data.client()
    try:
        companies = data.companies(api)
    except ApiError as exc:
        theme.show_error(exc, "Could not load the companies")
        return
    if not companies:
        st.info("No companies are loaded yet, so there are no claims to show.")
        return
    names = {c.ticker: company_name(c.name) for c in companies}

    with st.container(border=True):
        first = st.columns([1.3, 1.7, 2.4])
        second = st.columns([1.6, 1.0, 1.1, 1.0])
        options = [ALL, *names]
        linked = (st.query_params.get(COMPANY_PARAM) or "").upper()
        ticker: str = first[0].selectbox(
            "Company",
            options,
            index=options.index(linked) if linked in names else 0,
            format_func=lambda t: "All companies" if t == ALL else f"{names[t]} ({t})",
            key="company",
        )
        if ticker == ALL:
            st.query_params.pop(COMPANY_PARAM, None)
        else:
            st.query_params[COMPANY_PARAM] = ticker
        bold_only = second[1].toggle("Bold claims only", key="bold")
        include_scheduled = second[2].toggle(
            "Show scheduled actions",
            key="scheduled",
            help="Stock splits and dividends on a fixed date: resolved like any claim, "
            "but never counted in track records.",
        )
        try:
            claims = _claims(api, ticker, list(names), include_scheduled)
        except ApiError as exc:
            theme.show_error(exc, "Could not load the claims")
            return
        speakers = Counter(speaker_key(p.ticker, p.speaker) for p in claims)
        executive: str = first[1].selectbox(
            "Executive",
            [ALL, *(key for key, _ in speakers.most_common())],
            format_func=lambda k: (
                "All executives and company statements"
                if k == ALL
                else f"{speaker_label(k)} ({speakers[k]})"
            ),
            key="executive",
        )
        verdicts: list[str] = first[2].multiselect(
            "Verdict",
            list(PROMISE_VERDICTS),
            format_func=lambda v: PROMISE_VERDICTS[v].label,
            placeholder="Every verdict",
            key="verdicts",
        )
        due: Sequence[date] = second[0].date_input(
            "Due between", value=[], format="YYYY-MM-DD", key="due"
        )

    selected = [
        p for p in claims if executive == ALL or speaker_key(p.ticker, p.speaker) == executive
    ]
    whose = (
        speaker_label(executive)
        if executive != ALL
        else names[ticker]
        if ticker != ALL
        else "the covered companies"
    )
    _track_strip(api, ticker, executive, selected, whose)

    shown = [
        p
        for p in selected
        if (not verdicts or promise_state(p) in verdicts)
        and (not bold_only or p.bold)
        and _due_within(p, due)
    ]
    st.subheader("Claims")
    theme.legend(PROMISE_VERDICTS.values())
    if not shown:
        st.info(
            "No claims match these filters."
            if selected
            else "Nothing to list for this selection yet."
        )
    else:
        if len(shown) < len(selected):
            st.caption(f"Showing {len(shown)} of {count(len(selected), 'claim')}.")
        picked = _claims_table(shown, key=f"{ticker}|{executive}|{verdicts}|{bold_only}|{due}")
        if picked is not None:
            st.query_params[CLAIM_PARAM] = str(picked)
    _drill_down(api)


def _claims(
    api: ApiClient, ticker: str, tickers: Sequence[str], include_scheduled: bool
) -> list[PromiseOut]:
    if ticker != ALL:
        return data.promises(api, ticker, include_scheduled=include_scheduled)
    found = [p for t in tickers for p in data.promises(api, t, include_scheduled=include_scheduled)]
    return sorted(found, key=lambda p: (p.date, p.id), reverse=True)


def _due_within(p: PromiseOut, due: Sequence[date]) -> bool:
    if not due:
        return True
    if p.due_date is None:
        return False
    start, end = due[0], due[-1]
    return start <= p.due_date <= end if len(due) == 2 else start <= p.due_date


# The track record.


def _track_strip(
    api: ApiClient, ticker: str, executive: str, selected: Sequence[PromiseOut], whose: str
) -> None:
    by: TrackBy
    of: str | None
    if executive != ALL:
        # The executive's company: options come from the claims, so one is selected.
        by, group, of = "executive", executive, selected[0].ticker if selected else None
    elif ticker != ALL:
        by, group, of = "company", ticker, ticker
    else:
        by, group, of = "all", "all", None
    st.subheader(f"Track record: {whose}")
    try:
        page = data.track_record(api, by, of)
    except ApiError as exc:
        theme.show_error(exc, "Could not load the track record")
        return
    record = next((r for r in page.items if r.key == group), None)
    said = summary_of(selected, date.today(), whose)
    if record is None or record.claims == 0:
        st.info(
            f"{said} {record_summary(record, page.min_resolved)}", icon=":material/hourglass_empty:"
        )
        return
    st.caption(said)
    _strip(record, page.min_resolved)


def _strip(record: TrackRecordOut, min_resolved: int) -> None:
    thin = record_summary(record, min_resolved)
    by = record.by_verdict
    rates = record.rates
    tiles = st.columns(7)
    tiles[0].metric(
        "Resolved claims",
        f"{record.resolved}",
        help=f"Of {count(record.claims, 'scorable claim')}; vague claims ({record.vague}) "
        "are never scored, and expired ones are not resolved.",
    )

    def counted(name: str, rate: float | None = None) -> str:
        n = by.get(name, 0)
        return f"{n} · {percent(rate)}" if rate is not None else f"{n}"

    tiles[1].metric("On time", counted("delivered", rates.on_time_rate if rates else None))
    tiles[2].metric("Late", counted("delivered_late"))
    tiles[3].metric(
        "Walked back", counted("walked_back", rates.walked_back_share if rates else None)
    )
    tiles[4].metric(
        "Expired unproven",
        f"{record.expired}",
        help="Open-ended claims not shown delivered 18 months after the statement: never "
        "hidden, never counted as resolved.",
    )
    if rates is None:
        tiles[5].metric("Median days late", "too few to judge")
        tiles[6].metric("Bold-claim hit rate", "too few to judge")
        st.info(thin, icon=":material/balance:")
        return
    tiles[5].metric("Median days late", days(rates.median_days_late))
    tiles[6].metric(
        "Bold-claim hit rate",
        percent(rates.bold_hit_rate),
        help=f"Bold claims delivered, on time or late; {percent(rates.bold_share)} of "
        "resolved claims are bold.",
    )


# The table.


def _claims_table(shown: Sequence[PromiseOut], key: str) -> int | None:
    """The claims, one per row; the id of a clicked row, if any."""
    labels = [v.label for v in PROMISE_VERDICTS.values()]
    colours = [TONE_COLOURS[v.tone] for v in PROMISE_VERDICTS.values()]
    rows: list[dict[str, Any]] = [
        {
            "id": p.id,
            "Said": p.date,
            "Speaker": f"{p.ticker} {p.speaker}"
            if p.speaker in COMPANY_SPEAKERS
            else f"{p.speaker} ({p.ticker})",
            "Quote": p.quote,
            "Due": p.due_date.isoformat() if p.due_date else "no deadline",
            "Verdict": [PROMISE_VERDICTS[promise_state(p)].label],
            "Measured vs target": _against(p),
            "Revised": "revised before due" if p.revised_before_due else "",
            "Publisher": p.publisher or "",
            "Article": p.url,
        }
        for p in shown
    ]
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    event: Any = st.dataframe(
        rows,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"claims-{digest}",
        height=min(TABLE_ROW_PIXELS * (len(rows) + 1) + 3, TABLE_MAX_PIXELS),
        column_order=[c for c in rows[0] if c != "id"],
        column_config={
            "Said": st.column_config.DateColumn("Said", format="YYYY-MM-DD", width="small"),
            "Speaker": st.column_config.TextColumn(width="medium"),
            "Quote": st.column_config.TextColumn(width="large", help="At most 40 words"),
            "Due": st.column_config.TextColumn(width="small"),
            "Verdict": st.column_config.MultiselectColumn(
                "Verdict", options=labels, color=colours, width="small"
            ),
            "Measured vs target": st.column_config.TextColumn(width="small"),
            "Revised": st.column_config.TextColumn(
                width="small",
                help="Delivered after its due date, but revised by the company before its "
                "due period was reported",
            ),
            "Publisher": st.column_config.TextColumn(width="small"),
            "Article": st.column_config.LinkColumn(display_text="Read ↗", width="small"),
        },
    )
    st.caption("Click a row: its rule, evidence and checks open below the table.")
    picked = event.selection.rows if event is not None and hasattr(event, "selection") else []
    return int(rows[picked[0]]["id"]) if picked else None


def _against(p: PromiseOut) -> str:
    if p.measured and p.target:
        return f"{p.measured.display} vs {p.target.display}"
    if p.target:
        return f"target {p.target.display}"
    return "event" if p.scored and p.specificity != "vague" else "—"


# The drill-down.


def _drill_down(api: ApiClient) -> None:
    raw = st.query_params.get(CLAIM_PARAM)
    if not raw:
        return
    if not raw.isdigit():
        st.warning(f"“{raw}” is not a claim id.")
        return
    try:
        detail = data.promise(api, int(raw))
    except ApiError as exc:
        theme.show_error(exc, "Could not load the claim")
        return
    with st.container(border=True):
        show_claim(detail)
        if st.button("Close", key="close-claim", icon=":material/close:"):
            del st.query_params[CLAIM_PARAM]
            st.rerun()


def show_claim(d: PromiseDetail) -> None:
    state = PROMISE_VERDICTS[promise_state(d)]
    st.markdown(
        f"{theme.badge_html(state)}&nbsp; <strong>{html.escape(d.subject)}</strong>",
        unsafe_allow_html=True,
    )
    said = f"{speaker_text(d)} ({d.ticker}), {d.date.isoformat()}"
    st.caption(f"{said} · reported by {d.publisher or 'the article'} · claim {d.id}")
    st.markdown(f'<div class="tic-quote">“{html.escape(d.claim)}”</div>', unsafe_allow_html=True)
    st.markdown(
        f"[Read the original article at {theme.plain(d.publisher or 'the source')} ↗](<{d.url}>)"
    )

    left, right = st.columns(2)
    with left:
        st.markdown("**Resolution rule**")
        st.markdown(theme.plain(d.rule))
        st.caption(
            f"Rule version {d.version}: written when the claim was framed, before any "
            "outcome was known, and never rewritten."
        )
        facts = [f"**Due:** {_due_text(d)}"]
        if d.target:
            facts.append(f"**Target:** {theme.plain(d.target.display)}")
        if d.baseline and d.baseline.value is not None:
            facts.append(
                f"**Baseline:** {theme.plain(str(d.baseline.value))} "
                f"({theme.plain(d.baseline.period or '')}, from {d.baseline.source})"
            )
        facts.append(f"**Specificity:** {d.specificity}; **bold:** {'yes' if d.bold else 'no'}")
        if d.bold and d.bold_basis:
            facts[-1] += f" ({theme.plain(d.bold_basis)})"
        st.markdown("  \n".join(facts))
    with right:
        st.markdown("**Verdict**")
        _verdict_text(d, state.meaning)

    if d.resolution is not None:
        _evidence(d)
        _checks(d)


def _due_text(d: PromiseDetail) -> str:
    if d.due_date is None:
        return "no deadline (open-ended: checked for 18 months after the statement)"
    basis = d.due_basis.replace("_", " ")
    said = f", “{theme.plain(d.deadline_text)}”" if d.deadline_text else ""
    return f"{d.due_date.isoformat()} ({basis}{said})"


def _verdict_text(d: PromiseDetail, meaning: str) -> None:
    r = d.resolution
    lines = [theme.plain(meaning[:1].upper() + meaning[1:]) + "."]
    if not d.scored:
        if d.guidance_id is not None:
            lines = ["Restates formal guidance, so it is scored with the guidance (Scorecard)."]
        else:
            lines = ["Vague: no checkable target or deadline, so it is never scored."]
    elif r is None:
        lines.append("Not judged yet: claims are judged once due, from later evidence.")
    if d.claim_type == "scheduled_action":
        lines.append("A scheduled action: resolved like any claim, left out of track records.")
    if r is not None:
        when = r.resolved_at.date().isoformat()
        lines.append(f"Judged {when} against rule version {r.rule_version}.")
        if d.measured and d.target:
            lines.append(
                f"**Measured:** {theme.plain(d.measured.display)} against "
                f"{theme.plain(d.target.display)}."
            )
        if r.proof_date:
            source = f" ({r.proof_date_source})" if r.proof_date_source else ""
            lines.append(f"**First proof:** {r.proof_date.isoformat()}{source}.")
        if r.days_late:
            lines.append(f"**Late by:** {days(float(r.days_late))}.")
        if d.revised_before_due:
            lines.append("**Revised before due:** the company revised it before its due "
                         "period was reported.")  # fmt: skip
        if r.model_verdict != r.verdict and d.review is None:
            judge = PROMISE_VERDICTS[r.model_verdict].label
            lines.append(f"The judge said {judge}; a failed code check changed it.")
        st.markdown("  \n".join(lines))
        if r.reasoning:
            st.caption(theme.plain(r.reasoning))
    else:
        st.markdown("  \n".join(lines))
    if d.review is not None:
        note = f": {theme.plain(d.review.note)}" if d.review.note else ""
        reviewed = PROMISE_VERDICTS[d.review.verdict].label
        st.markdown(
            f"**Reviewed** {d.review.decided_at.date().isoformat()} as {reviewed}{note}. "
            "A review always wins."
        )


def _evidence(d: PromiseDetail) -> None:
    r = d.resolution
    assert r is not None
    findings = {f.evidence_id: f for f in r.findings}
    st.markdown(f"**Evidence** ({len(r.evidence)} cited of {r.gathered} gathered)")
    if not r.evidence:
        st.caption("No evidence was cited: nothing gathered after the statement bears on it.")
        return
    for e in r.evidence:
        kind = PROVENANCE_LABELS.get(e.provenance or "", "") or TOOL_LABELS.get(e.tool, e.tool)
        found = findings.get(e.id)
        when = e.date.isoformat() if e.date else "undated"
        link = f" · [source ↗](<{e.url}>)" if e.url else ""
        line = f"**{e.id}** · {when} · {kind}{link}"
        if e.description:
            line += f"  \n{theme.plain(excerpt(e.description))}"
        if found is not None:
            verdict = FINDING_LABELS.get(found.finding, found.finding)
            note = f": {theme.plain(found.note)}" if found.note else ""
            line += f"  \n*Judge: {verdict}*{note}"
        st.markdown(line)


def _checks(d: PromiseDetail) -> None:
    r = d.resolution
    assert r is not None
    st.markdown("**Code checks**")
    lines = []
    for c in r.checks:
        mark = "✅ passed" if c.passed else "❌ failed" if c.passed is False else "💬 note"
        detail = f": {theme.plain(c.detail)}" if c.detail else ""
        lines.append(f"{mark} · `{c.check}`{detail}")
    st.markdown("  \n".join(lines) or "No checks recorded.")
