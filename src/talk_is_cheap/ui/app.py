"""The Streamlit app: `streamlit run` runs this file (`talk-is-cheap ui`, `make ui`)."""

import streamlit as st

from talk_is_cheap.ui import theme
from talk_is_cheap.ui.views import ask, companies, promises, scorecard


def main() -> None:
    st.set_page_config(
        page_title=theme.TITLE,
        page_icon=theme.ICON,
        layout="wide",
        menu_items={"About": f"{theme.TITLE}: {theme.PITCH} Not investment advice."},
    )
    theme.styles()
    theme.header()
    page = st.navigation(
        [
            st.Page(
                promises.render,
                title="Promises",
                icon=":material/gavel:",
                default=True,
            ),
            st.Page(ask.render, title="Ask", icon=":material/forum:", url_path="ask"),
            st.Page(
                scorecard.render,
                title="Scorecard",
                icon=":material/scoreboard:",
                url_path="scorecard",
            ),
            st.Page(
                companies.render,
                title="Companies",
                icon=":material/domain:",
                url_path="companies",
            ),
        ],
        position="top",
    )
    page.run()
    theme.footer()


main()
