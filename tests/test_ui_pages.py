"""Each page under Streamlit's AppTest, with the API client replaced by a fake: with
data, with no data, and with the API failing."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from talk_is_cheap.ui.client import (
    ApiClient,
    BudgetExceeded,
    ServerError,
    Unauthorized,
    Unreachable,
)
from talk_is_cheap.ui.data import CLIENT_KEY
from tests.ui_fakes import (
    ARTICLE,
    FILING,
    RELEASE,
    SECRET_KEY,
    FakeApi,
    answer_events,
    company,
    detail,
    guidance,
    promise,
    record,
)

TIMEOUT = 30


def promises_page() -> None:
    from talk_is_cheap.ui.views import promises

    promises.render()


def ask_page() -> None:
    from talk_is_cheap.ui.views import ask

    ask.render()


def scorecard_page() -> None:
    from talk_is_cheap.ui.views import scorecard

    scorecard.render()


def companies_page() -> None:
    from talk_is_cheap.ui.views import companies

    companies.render()


def run(page: Callable[[], None], api: FakeApi, **query: str) -> AppTest:
    at = AppTest.from_function(page, default_timeout=TIMEOUT)
    at.session_state[CLIENT_KEY] = api
    for name, value in query.items():
        at.query_params[name] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def texts(at: AppTest) -> str:
    """Everything the page says, in one string."""
    parts: list[str] = []
    for kind in ("markdown", "caption", "info", "warning", "error", "subheader", "title"):
        parts.extend(str(e.value) for e in getattr(at, kind))
    parts.extend(f"{m.label}: {m.value}" for m in at.metric)
    return "\n".join(parts)


def two_companies() -> FakeApi:
    claims = [
        promise(11, date=date(2026, 3, 1), measured=None),
        promise(12, verdict="walked_back", speaker="company statement", role=None),
        promise(13, verdict=None, due_date=date(2027, 3, 31)),
        promise(14, verdict=None, scored=False, specificity="vague", due_date=None),
        promise(15, claim_type="scheduled_action", subject="the split"),
    ]
    return FakeApi(
        companies=[company("APIX"), company("THIN", name="Thinco, Inc.", management_claims=2)],
        promises={
            "APIX": claims,
            "THIN": [
                promise(21, ticker="THIN", verdict=None, due_date=date(2027, 3, 31)),
                promise(22, ticker="THIN", verdict=None, due_date=date(2027, 5, 1)),
            ],
        },
        details={11: detail(11)},
        records={
            ("all", None): [record("all", 6)],
            ("company", "APIX"): [record("APIX", 6)],
            ("company", "THIN"): [record("THIN", 0, claims=2)],
            ("executive", "APIX"): [record("Jane Roe (APIX)", 2), record("APIX (company)", 1)],
        },
        guidance={"APIX": [guidance(1), guidance(2, "met"), guidance(3, "not_comparable")]},
    )


# Promises.


def test_promises_with_data_show_the_track_record_table_and_legend() -> None:
    at = run(promises_page, two_companies())
    said = texts(at)
    assert "Track record: the covered companies" in said
    assert "Resolved claims: 6" in said and "Bold-claim hit rate: 50%" in said
    assert "Median days late: 12 days" in said
    assert "too few to judge" not in said.lower()
    assert "expired unproven" in said and "cannot determine" in said  # the legend
    table = at.dataframe[0].value
    assert len(table) == 6  # the scheduled action is left out
    assert set(table["Article"]) == {ARTICLE}
    assert ["walked back"] in list(table["Verdict"])


def test_a_group_under_the_minimum_is_too_few_to_judge_with_its_claim_count() -> None:
    at = AppTest.from_function(promises_page, default_timeout=TIMEOUT)
    at.session_state[CLIENT_KEY] = two_companies()
    at.run()
    at.selectbox(key="company").set_value("APIX").run()
    at.selectbox(key="executive").set_value("Jane Roe (APIX)").run()
    assert not at.exception
    said = texts(at)
    assert "Track record: Jane Roe · APIX" in said
    assert "Too few to judge: 2 resolved claims of 4 scorable claims; rates need 5." in said
    assert "Median days late: too few to judge" in said
    assert "Bold-claim hit rate: too few to judge" in said
    assert "%" not in "".join(m.value for m in at.metric)  # never a rate
    assert len(at.dataframe[0].value) == 3  # Jane Roe's claims only


def test_a_thin_company_says_plainly_that_nothing_is_due() -> None:
    at = run(promises_page, two_companies(), company="THIN")
    said = texts(at)
    assert "2 claims, none due yet. The earliest is due in March 2027." in said
    assert "Too few to judge: 0 resolved claims of 2 scorable claims; rates need 5." in said


def test_filters_narrow_the_table_and_scheduled_actions_can_be_shown() -> None:
    at = run(promises_page, two_companies(), company="APIX")
    at.multiselect(key="verdicts").set_value(["pending"]).run()
    assert list(at.dataframe[0].value["Verdict"]) == [["pending"]]
    assert "Showing 1 of 4 claims." in texts(at)
    at.multiselect(key="verdicts").set_value([]).run()
    at.toggle(key="scheduled").set_value(True).run()
    assert len(at.dataframe[0].value) == 5


def test_the_drill_down_shows_the_rule_evidence_links_and_checks() -> None:
    at = run(promises_page, two_companies(), company="APIX", claim="11")
    said = texts(at)
    assert f"(<{ARTICLE}>)" in said  # the original article
    assert "Rule version v3+5" in said and "True if Apixco ships product 11" in said
    assert f"[source ↗](<{FILING}>)" in said  # the evidence link
    assert "E1** · 2026-05-01 · SEC filing" in said
    assert "Products Product 1 shipped in April." in said  # the key sentence, cleaned
    assert "Judge: satisfies the rule" in said
    assert "✅ passed · `evidence_exist`" in said
    assert "❌ failed · `key_terms`: Widget" in said
    assert "💬 note · `on_time`" in said


def test_a_missing_claim_is_a_plain_warning() -> None:
    at = run(promises_page, two_companies(), claim="999")
    assert "Could not load the claim: no promise 999" in texts(at)


def test_promises_with_no_data() -> None:
    at = run(promises_page, FakeApi())
    assert "No companies are loaded yet" in texts(at)
    at = run(promises_page, FakeApi(companies=[company()]))
    said = texts(at)
    assert "No management claims are stored for the covered companies yet." in said
    assert "Nothing to list for this selection yet." in said
    assert not at.dataframe


def test_promises_with_the_api_failing() -> None:
    at = run(promises_page, FakeApi(error=Unreachable("Cannot reach the API at http://x.")))
    assert at.error and "Could not load the companies. Cannot reach the API" in at.error[0].value
    assert not at.dataframe


# Ask.


def test_ask_streams_an_answer_with_linked_citations_cost_and_feedback() -> None:
    api = FakeApi(chat=answer_events())
    at = run(ask_page, api)
    assert len([b for b in at.button if b.key and b.key.startswith("example-")]) == 4
    at.button(key="example-0").click().run()
    assert not at.exception
    assert api.asked == [("Did Nvidia deliver Blackwell when it said it would?", None)]
    said = texts(at)
    assert f"\\$96.2 billion [\\[1\\]](<{FILING}>)." in said  # the marker links the filing
    assert f"[APIX 10-Q filed 2026-08-26](<{FILING}>) · SEC filing" in said
    assert "$0.0016 · 4.6 s · 1 tool call" in said
    assert at.session_state["ask_thread"] == "thread-1"

    at.chat_input[0].set_value("And the quarter before?").run()
    assert api.asked[-1] == ("And the quarter before?", "thread-1")  # the same thread

    at.feedback[0].set_value(0).run()
    at.text_input(key="comment-0").set_value("wrong quarter")
    at.button[-1].click().run()  # Send feedback
    assert api.feedback_sent == [("a" * 32, "down", "wrong quarter")]
    assert "Thanks, your feedback was saved." in texts(at)

    at.button(key="new-conversation").click().run()
    assert at.session_state["ask_turns"] == [] and at.session_state["ask_thread"] is None


def test_ask_with_nothing_asked_yet_offers_examples() -> None:
    at = run(ask_page, FakeApi())
    assert "Try one of these:" in texts(at)
    assert at.chat_input and not at.chat_message


@pytest.mark.parametrize(
    "error",
    [
        BudgetExceeded("The question budget is used up for today; it resets at midnight UTC."),
        ServerError("The question took longer than a minute and was stopped."),
    ],
)
def test_ask_errors_are_messages_not_tracebacks(error: Exception) -> None:
    at = run(ask_page, FakeApi(chat_error=error))  # type: ignore[arg-type]
    at.button(key="example-1").click().run()
    assert not at.exception
    assert at.error[0].value == str(error)
    assert "Traceback" not in texts(at)


# Scorecard.


def test_scorecard_counts_only_comparable_rows_and_says_why_others_are_left_out() -> None:
    api = two_companies()
    api._guidance["APIX"] += [
        guidance(4, "beat", mark="REVIEW", counts_in_verdicts=False, review_status="needs review"),
        guidance(5, "pending", actual=None),
    ]
    at = run(scorecard_page, api)
    said = texts(at)
    assert "Comparable rows: 2" in said and "Beat: 1 · 50%" in said and "Met: 1 · 50%" in said
    assert "Counted: 2 of 5 rows." in said
    assert "1 not comparable (non-GAAP guidance; reported values are GAAP)" in said
    assert "1 pending (target period not reported yet)" in said
    assert "1 flagged for review" in said
    table = at.dataframe[0].value
    assert table["Guided"].iloc[0] == "$105.8B to $110.2B"
    assert table["Actual"].iloc[0] == "$112.0B"
    assert "⚑ needs review" in list(table["Flag"])
    assert set(table["Release"]) == {RELEASE}


def test_scorecard_without_guidance() -> None:
    api = FakeApi(companies=[company(guidance_rows=0)])
    at = run(scorecard_page, api)
    assert "gives no numeric guidance in its earnings releases" in texts(at)


def test_scorecard_with_the_api_failing() -> None:
    at = run(scorecard_page, FakeApi(error=Unauthorized("The API refused the UI's key.")))
    assert "Could not load the companies. The API refused the UI's key." in at.error[0].value


# Companies.


def test_companies_show_coverage_and_where_it_is_thin() -> None:
    at = run(companies_page, two_companies())
    said = texts(at)
    assert "2 companies, 6 management claims from the news, 2 of them due to be judged." in said
    table = at.dataframe[0].value
    assert list(table["Company"]) == ["Apixco", "Thinco"]
    assert list(table["Latest quarter"]) == ["FY2026 Q2", "FY2026 Q2"]


def test_companies_with_no_data_and_with_the_api_failing() -> None:
    assert "No companies are loaded yet." in texts(run(companies_page, FakeApi()))
    at = run(companies_page, FakeApi(error=ServerError("The API failed to answer (HTTP 500).")))
    assert "HTTP 500" in at.error[0].value


# The whole app.


def api_over_http(fake: FakeApi, keys: list[str]) -> ApiClient:
    """A real client, with the key, over a transport that answers from `fake`."""

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("X-API-Key", ""))
        path = request.url.path
        found: Any
        if path == "/v1/companies":
            found = {"items": fake.companies()}
        elif path.endswith("/promises"):
            found = {"items": fake.promises(path.split("/")[3])}
        elif path == "/v1/track-record":
            ticker = request.url.params.get("ticker")
            found = fake.track_record(request.url.params["by"], ticker).model_dump()
        else:
            return httpx.Response(500, json={"detail": f"boom {request.headers}"})
        body = {"next_cursor": None} | found
        return httpx.Response(200, content=json.dumps(body, default=_json))

    return ApiClient("http://api.test", SECRET_KEY, transport=httpx.MockTransport(handler))


def _json(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)


def test_the_app_has_its_header_pages_and_footer_and_never_shows_the_key() -> None:
    import talk_is_cheap.ui as ui

    keys: list[str] = []
    at = AppTest.from_file(str(Path(ui.__file__).with_name("app.py")), default_timeout=TIMEOUT)
    at.session_state[CLIENT_KEY] = api_over_http(two_companies(), keys)
    at.query_params["claim"] = "11"  # its detail fails with an error that holds the key
    at.run()
    assert not at.exception
    said = texts(at)
    assert "Talk Is Cheap" in said
    assert "What executives said in public, and what actually happened." in said
    assert "Not investment advice." in said and "SEC EDGAR" in said
    assert len(at.dataframe[0].value) == 6  # the Promises page is the front page
    assert "Could not load the claim. The API failed to answer (HTTP 500)." in said
    assert keys and set(keys) == {SECRET_KEY}  # sent on every request
    assert SECRET_KEY not in said
    assert SECRET_KEY not in str(at.dataframe[0].value.to_dict())
