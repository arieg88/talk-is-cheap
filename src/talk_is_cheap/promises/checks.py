"""The resolver's code checks, run on the judge model's verdict before anything is
stored. A check that fails changes the verdict to `cannot_determine` and is recorded by
name; a check that only corrects the timing (delivered on time or late) changes the
verdict and says so.

Checks, and when each fails:

- `evidence_exist`: a cited evidence id is not one of the gathered items;
- `evidence_after_statement`: a cited item is dated on or before the statement date, or
  has no date;
- `numbers_grounded`: a number in the reasoning is in no gathered item, no measured
  value and not in the rule or quote (the agent verifier's rules);
- `measured_from_tools`: a numeric target is judged without a measured value from
  `cite_value` or `compute`;
- `measured_meets_target`: `delivered` or `delivered_late` with a measured value short
  of the required one, or `not_delivered` with one that reaches it;
- `due_period_reported`: `not_delivered` before a filing covering the due period
  exists: silence is not failure;
- `partly_between`: `partly_delivered` on a numeric target with a measured value not
  between the baseline and the target; on another target, without the stated reason;
- `walked_back_cited`: `walked_back` citing no later statement, filing or news;
- `not_too_early`: `too_early` once the due date and its grace period have passed.

Timing (`on_time`): `delivered` needs its first proof dated on or before the end of the
grace period (by default, the first 10-Q or 10-K covering the due period); a later proof
makes it `delivered_late`, and a `delivered_late` whose proof is on time is
`delivered`. An open-ended claim is checked for delivery only: any other verdict leaves
it open, as `too_early`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from talk_is_cheap.agent.checks import evidence_values, grounded, mentions
from talk_is_cheap.agent.evidence import Evidence, evidence_date, is_derived
from talk_is_cheap.promises.framing import NUMERIC_KINDS
from talk_is_cheap.promises.schema import Judgment

# Tools whose items can show that a claim was walked back or restated.
LATER_WORDS_TOOLS = ("get_statements", "search_news", "search_filings", "cite_value")


@dataclass(frozen=True, slots=True)
class PromiseFacts:
    """What the checks need to know about the promise being judged."""

    statement_date: date
    due_date: date | None
    target_kind: str | None
    direction: str
    required_value: Decimal | None
    baseline_value: Decimal | None
    resolution_rule: str
    quote: str

    @property
    def numeric(self) -> bool:
        return self.target_kind in NUMERIC_KINDS

    @property
    def open_ended(self) -> bool:
        return self.due_date is None


@dataclass(frozen=True, slots=True)
class Measured:
    """The measured value, from `cite_value` or `compute`, or why there is none."""

    value: Decimal | None
    unit: str | None
    item: Evidence | None
    error: str | None = None


@dataclass(slots=True)
class Checked:
    verdict: str
    checks: list[dict[str, object]] = field(default_factory=list)
    proof_date: date | None = None
    days_late: int | None = None

    @property
    def failed(self) -> list[str]:
        return [str(c["check"]) for c in self.checks if c["passed"] is False]

    def record(self, check: str, passed: bool | None, detail: str = "") -> None:
        self.checks.append({"check": check, "passed": passed, "detail": detail})


def item_date(item: Evidence, items: Mapping[str, Evidence]) -> date | None:
    """An item's date; a computed item's is the latest of its inputs'."""
    if is_derived(item):
        dates = [item_date(items[i], items) for i in item["data"].get("inputs", []) if i in items]
        known = [d for d in dates if d is not None]
        return max(known) if known else None
    return evidence_date(item)


def check(
    promise: PromiseFacts,
    judgment: Judgment,
    items: Mapping[str, Evidence],
    measured: Measured,
    grace_end: date | None,
    today: date,
) -> Checked:
    """The verdict that may be stored, and every check with its result."""
    result = Checked(judgment.verdict)
    verdict: str = judgment.verdict
    cited = list(dict.fromkeys(judgment.evidence_ids + [m.evidence_id for m in judgment.measured]))
    if judgment.first_proof_id:
        cited.append(judgment.first_proof_id)
    missing = [i for i in cited if i not in items]
    result.record("evidence_exist", not missing, ", ".join(missing))
    present = [items[i] for i in dict.fromkeys(cited) if i in items]
    early = [
        f"{i['id']} ({item_date(i, items) or 'no date'})"
        for i in present
        if (d := item_date(i, items)) is None or d <= promise.statement_date
    ]
    result.record("evidence_after_statement", not early, ", ".join(early))
    _numbers(result, judgment, list(items.values()), measured, promise)
    if promise.open_ended and verdict != "delivered":
        result.record("open_ended", None, f"{verdict}: an open-ended claim stays open")
        verdict = "too_early"
    if verdict in ("delivered", "delivered_late", "not_delivered", "partly_delivered") and (
        promise.numeric
    ):
        ok = measured.value is not None
        result.record("measured_from_tools", ok, measured.error or "")
        if measured.value is not None and verdict in (
            "delivered",
            "delivered_late",
            "not_delivered",
        ):
            reached = _reaches(promise, measured.value)
            if reached is not None:
                expected = verdict != "not_delivered"
                result.record(
                    "measured_meets_target",
                    reached == expected,
                    f"measured {measured.value:f} against required {promise.required_value:f}",
                )
    if verdict == "not_delivered":
        result.record(
            "due_period_reported",
            grace_end is not None and grace_end <= today,
            "" if grace_end else "no filing covering the due period yet",
        )
    if verdict == "partly_delivered":
        _partly(result, promise, judgment, measured)
    if verdict == "walked_back":
        later = [i for i in present if i["tool"] in LATER_WORDS_TOOLS]
        result.record("walked_back_cited", bool(later), "")
    if verdict == "too_early" and not promise.open_ended:
        passed = grace_end is None or grace_end > today
        result.record("not_too_early", passed, f"grace period ended {grace_end}")
    if verdict in ("delivered", "delivered_late"):
        verdict = _timing(result, verdict, judgment, present, items, promise, grace_end)
    result.verdict = "cannot_determine" if result.failed else verdict
    return result


def _numbers(
    result: Checked,
    judgment: Judgment,
    present: Sequence[Evidence],
    measured: Measured,
    promise: PromiseFacts,
) -> None:
    extra = f"{promise.resolution_rule}\n{promise.quote}"
    values = evidence_values(present, extra)
    if measured.value is not None:
        values.append(measured.value)
    if measured.item is not None:
        values.extend(evidence_values([measured.item]))
    unknown = [
        m.text for m in mentions(judgment.reasoning) if not m.small and not grounded(m, values)
    ]
    result.record("numbers_grounded", not unknown, ", ".join(unknown))


def _reaches(promise: PromiseFacts, value: Decimal) -> bool | None:
    if promise.required_value is None:
        return None
    if promise.direction == "at_most":
        return value <= promise.required_value
    if promise.target_kind == "threshold" and promise.required_value == 0:
        return value > 0
    return value >= promise.required_value


def _partly(result: Checked, promise: PromiseFacts, judgment: Judgment, measured: Measured) -> None:
    if promise.numeric and promise.required_value is not None:
        if measured.value is None or promise.baseline_value is None:
            result.record("partly_between", False, "no measured value or no baseline")
            return
        low, high = sorted((promise.baseline_value, promise.required_value))
        result.record(
            "partly_between",
            low < measured.value < high,
            f"measured {measured.value:f} between {low:f} and {high:f}",
        )
        return
    reason = (judgment.partial_reason or "").strip()
    result.record("partly_between", bool(reason), reason)


def _timing(
    result: Checked,
    verdict: str,
    judgment: Judgment,
    present: Sequence[Evidence],
    items: Mapping[str, Evidence],
    promise: PromiseFacts,
    grace_end: date | None,
) -> str:
    proof = items.get(judgment.first_proof_id or "")
    candidates = [proof] if proof is not None else list(present)
    dates = [d for i in candidates if (d := item_date(i, items)) is not None]
    if not dates:
        result.record("on_time", False, "no dated proof of delivery")
        return verdict
    first = min(dates)
    result.proof_date = first
    if promise.due_date is None:
        result.record("on_time", None, "open-ended: delivered at any time")
        return "delivered"
    deadline = grace_end or promise.due_date
    if grace_end is None:
        result.record("on_time", None, "no filing covering the due period yet")
        return "delivered" if first <= promise.due_date else verdict
    late = first > deadline
    if late:
        result.days_late = (first - promise.due_date).days
    timed = "delivered_late" if late else "delivered"
    note = f"first proof {first}, grace period ends {deadline}"
    if timed != verdict:
        note += f"; {verdict} changed to {timed}"
    result.record("on_time", None, note)
    return timed
