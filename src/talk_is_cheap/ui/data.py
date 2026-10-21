"""The pages' reads of the API, cached for a minute so filtering and paging feel fast.

Chat and feedback are never cached. Cached reads are keyed by the client's `scope` (its
base URL) and their arguments; an error is not cached, so the next rerun asks again.
"""

from typing import Any

import streamlit as st

from talk_is_cheap.api.schemas import (
    CompanyCoverageOut,
    GuidanceOut,
    PromiseDetail,
    PromiseOut,
    TrackRecordPage,
)
from talk_is_cheap.ui.client import ApiClient, TrackBy
from talk_is_cheap.ui.config import get_ui_settings

CACHE_SECONDS = 60
# Tests put a fake client here (`st.session_state`, on the Streamlit server only).
CLIENT_KEY = "api_client"


@st.cache_resource(show_spinner=False)
def _shared_client() -> ApiClient:
    return ApiClient.from_settings(get_ui_settings())


def client() -> ApiClient:
    found: Any = st.session_state.get(CLIENT_KEY)
    return found if found is not None else _shared_client()


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _companies(_client: ApiClient, scope: str) -> list[CompanyCoverageOut]:
    return _client.companies()


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _promises(
    _client: ApiClient, scope: str, ticker: str, include_scheduled: bool
) -> list[PromiseOut]:
    return _client.promises(ticker, include_scheduled=include_scheduled)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _promise(_client: ApiClient, scope: str, promise_id: int) -> PromiseDetail:
    return _client.promise(promise_id)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _track_record(
    _client: ApiClient, scope: str, by: TrackBy, ticker: str | None
) -> TrackRecordPage:
    return _client.track_record(by, ticker)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _guidance(_client: ApiClient, scope: str, ticker: str) -> list[GuidanceOut]:
    return _client.guidance(ticker)


def companies(api: ApiClient) -> list[CompanyCoverageOut]:
    return _companies(api, api.scope)


def promises(api: ApiClient, ticker: str, *, include_scheduled: bool) -> list[PromiseOut]:
    return _promises(api, api.scope, ticker, include_scheduled)


def promise(api: ApiClient, promise_id: int) -> PromiseDetail:
    return _promise(api, api.scope, promise_id)


def track_record(api: ApiClient, by: TrackBy, ticker: str | None = None) -> TrackRecordPage:
    return _track_record(api, api.scope, by, ticker)


def guidance(api: ApiClient, ticker: str) -> list[GuidanceOut]:
    return _guidance(api, api.scope, ticker)
