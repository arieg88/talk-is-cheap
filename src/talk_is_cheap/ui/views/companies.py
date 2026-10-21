"""Companies: what is stored for each covered company, and where it is thin."""

import streamlit as st

from talk_is_cheap.ui import data, theme
from talk_is_cheap.ui.client import ApiError
from talk_is_cheap.ui.format import company_name, count, period

ROW_PIXELS = 35


def render() -> None:
    api = data.client()
    try:
        companies = data.companies(api)
    except ApiError as exc:
        theme.show_error(exc, "Could not load the companies")
        return
    st.subheader("Coverage")
    if not companies:
        st.info("No companies are loaded yet.")
        return
    claims = sum(c.management_claims for c in companies)
    due = sum(c.due_claims for c in companies)
    without_guidance = [c.ticker for c in companies if c.guidance_rows == 0]
    sentences = [
        f"{count(len(companies), 'company', 'companies')}, "
        f"{count(claims, 'management claim')} from the news, {due} of them due to be judged."
    ]
    if claims and due < claims / 4:
        sentences.append(
            "Most claims are not due yet: the stored news is mostly recent, so most "
            "deadlines are still ahead."
        )
    if without_guidance:
        sentences.append(f"No guidance in the earnings releases of {', '.join(without_guidance)}.")
    st.markdown(" ".join(sentences))
    rows = [
        {
            "Company": company_name(c.name),
            "Ticker": c.ticker,
            "Latest quarter": period(c.latest_quarter),
            "Filings": c.filings,
            "Guidance rows": c.guidance_rows,
            "Linked articles": c.linked_articles,
            "Management claims": c.management_claims,
            "Due claims": c.due_claims,
        }
        for c in sorted(companies, key=lambda c: (-c.management_claims, c.ticker))
    ]
    top = max((c.management_claims for c in companies), default=0) or 1
    st.dataframe(
        rows,
        hide_index=True,
        height=ROW_PIXELS * (len(rows) + 1) + 3,
        column_config={
            "Latest quarter": st.column_config.TextColumn(
                help="The latest fiscal quarter with reported values"
            ),
            "Linked articles": st.column_config.NumberColumn(
                help="News articles linked to the company with confidence 0.5 or more",
                format="localized",
            ),
            "Management claims": st.column_config.ProgressColumn(
                help="Forward-looking claims by executives, spokespeople or the company",
                format="%d",
                min_value=0,
                max_value=top,
            ),
            "Due claims": st.column_config.NumberColumn(
                help="Promises whose due date and grace period have passed",
            ),
        },
    )
    st.caption(
        "A company with few claims has a thin record: a track record needs 5 resolved "
        "claims before it gives any rate."
    )
