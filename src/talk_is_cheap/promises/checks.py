"""The verdict on a promise, decided in code from the judge's findings, and the checks
it must pass before it is stored.

The judge says only what each gathered item shows about the rule: it `satisfies` it,
shows it `partly` met, `contradicts` it (the due period came and the rule was not met),
`walks_back` the claim (a later statement withdrew, delayed or lowered it), or is
`irrelevant`. Code then decides:

- an item satisfies the rule: `delivered`, or `delivered_late` when the earliest
  satisfying item is dated after the end of the grace period (by default, the first
  10-Q or 10-K covering the due period); `days_late` counts from the due date;
- else an item walks the claim back: `walked_back`;
- else, once the grace period has ended, an item shows it partly met:
  `partly_delivered`; one contradicts it: `not_delivered`;
- else: `too_early` while the grace period runs (and always for open-ended claims, which
  are checked for delivery only), `cannot_determine` after it. Silence is not failure.

A failed check makes the verdict `cannot_determine` and is recorded by name:

- `evidence_exist`: a cited evidence id is not one of the gathered items;
- `evidence_after_statement`: a cited item is dated on or before the statement date, or
  has no date;
- `key_terms`: a key term of the rule (a product, model, partner or place: a capitalized
  or alphanumeric word other than the company's own names, months and period labels) is
  in none of the cited items' text;
- `numbers_grounded`: a number in the reasoning is in no gathered item, no measured
  value and not in the rule or quote (the agent verifier's rules);
- `measured_from_tools`: a numeric target is judged without a measured value from
  `cite_value` or `compute`;
- `measured_meets_target`: `delivered` or `delivered_late` with a measured value short
  of the required one, or `not_delivered` with one that reaches it (it cannot fail once
  code has judged the measured items, below; it guards the rule);
- `partly_between`: `partly_delivered` with a measured value not between the baseline
  and the target; for targets without a number, without the judge's stated reason.

Cited items are those with a finding other than `irrelevant`, and the measured sources.
For a numeric target, code judges the measured items itself: they satisfy the rule when
the measured value (read by `cite_value` and `compute`) reaches the required one, show it
partly met between the baseline and the target, and contradict it otherwise, whatever
the judge said of them.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from talk_is_cheap.agent.checks import evidence_values, grounded, mentions
from talk_is_cheap.agent.evidence import Evidence, evidence_date, is_derived
from talk_is_cheap.promises.framing import NUMERIC_KINDS
from talk_is_cheap.promises.schema import Assessment, Finding, Judgment

# Capitalized words of a rule that are not key terms: its own wording, months, period
# and accounting labels, titles.
_NOT_KEY_TERMS = frozenset(
    {
        "true", "not", "resolvable", "fy", "q1", "q2", "q3", "q4", "gaap", "non-gaap",
        "inc", "corp", "corporation", "company", "the", "a", "an", "its", "this", "that",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december", "ceo", "cfo", "ai",
        # financial measures, written out in filings ("earnings per share")
        "eps", "arr", "rpo", "yoy", "capex", "opex", "ebitda",
    }
)  # fmt: skip
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+&-]*")
# Fields of an item whose text a key term may be found in.
_TEXT_FIELDS = (
    "text", "headline", "summary", "quote", "claim", "span", "section_title", "metric",
    "display", "kind",
)  # fmt: skip


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
    company_names: tuple[str, ...] = ()  # the company's own names, never key terms
    # When the statement's article was published, for evidence of the same day.
    statement_at: datetime | None = None
    # Stock splits since the statement, multiplied: per-share targets are divided by it.
    split_ratio: Decimal = Decimal(1)
    per_share: bool = False
    # An open-ended claim not shown delivered by this day is `expired_unproven`.
    expires: date | None = None

    @property
    def required(self) -> Decimal | None:
        """The required value in today's shares: a per-share target set before a split
        is divided by the split's ratio ($0.10 before a 10-for-1 split is $0.01)."""
        if self.required_value is None or not self.per_share:
            return self.required_value
        return self.required_value / self.split_ratio

    @property
    def baseline(self) -> Decimal | None:
        if self.baseline_value is None or not self.per_share:
            return self.baseline_value
        return self.baseline_value / self.split_ratio

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
    derived: str = ""  # the verdict the findings give, before the checks
    checks: list[dict[str, object]] = field(default_factory=list)
    proof_date: date | None = None
    proof_date_source: str | None = None  # event (stated in the evidence) or published
    days_late: int | None = None
    revised_before_due: bool = False
    cited: list[str] = field(default_factory=list)

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


def key_terms(rule: str, company_names: Sequence[str]) -> list[str]:
    """The rule's key terms: products, models, partners, places. Capitalized words and
    model numbers ("MI300X", "H100", "4070") of the rule's claim (after its "True if" or
    its colon), without the company's own names, months, years, figures and period
    labels."""
    claim = rule.split(": ", 1)[1] if ": " in rule else rule.removeprefix("True if ")
    claim = re.sub(
        r"\$[\d,.]+\s*(?:billion|million|trillion)?|\d[\d,.]*%|\d{1,3}(?:,\d{3})+|\d+\.\d+",
        " ",
        claim,
    )
    claim = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\((?:FY|last|as stated)[^)]*\)", " ", claim)
    own = {w.casefold() for n in company_names for w in re.split(r"[^\w]+", n) if w}
    terms: list[str] = []
    # A hyphenated word counts by its parts: "Blackwell-based" is Blackwell, "10-for-1"
    # no term at all.
    words = [part for word in _WORD.findall(claim) for part in word.split("-")]
    for word in words:
        bare = re.sub(r"(?:'s|\u2019s)$", "", word)
        folded = bare.casefold()
        if not bare or folded in _NOT_KEY_TERMS or folded in own:
            continue
        if re.fullmatch(r"FY\d{2,4}(?:Q[1-4])?|(?:19|20)\d{2}s?|\d{1,2}", bare, re.I):
            continue  # years, period labels and small numbers are not terms
        named = bare[0].isupper() or any(c.isdigit() for c in bare)
        if named and folded not in {t.casefold() for t in terms}:
            terms.append(bare)
    return terms


def _mentioned(term: str, text: str) -> bool:
    """Whether `term` is a word of `text`, in any case, singular or plural."""
    stem = term.casefold()
    stem = stem[:-1] if stem.endswith("s") and len(stem) > 3 else stem
    return re.search(rf"(?<!\w){re.escape(stem)}(?:e?s)?(?!\w)", text.casefold()) is not None


def _text_of(item: Evidence) -> str:
    data = item["data"]
    return " ".join(str(data[f]) for f in _TEXT_FIELDS if isinstance(data.get(f), str))


@dataclass(frozen=True, slots=True)
class Derived:
    verdict: str
    proof_date: date | None = None
    proof_date_source: str | None = None
    days_late: int | None = None
    revised_before_due: bool = False


def event_date(item: Evidence, span: str) -> date | None:
    """The date `span` states, when the span is in the item's text: "June 7, 2024",
    "2024-06-07", "June 2024" (its last day). None otherwise."""
    if not span.strip():
        return None
    from talk_is_cheap.guidance.grounding import locate

    text = _text_of(item)
    place = locate(span, text)
    if place is None:
        return None
    return parse_date(text[place[0] : place[1]])


_MONTH_NAMES = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}  # fmt: skip
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DAY_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MONTH_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{4})\b")


def parse_date(text: str) -> date | None:
    """The first date written in `text`; a month and year is the month's last day."""
    import calendar

    def make(year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    if found := _ISO.search(text):
        return make(int(found[1]), int(found[2]), int(found[3]))
    if (found := _MONTH_DAY_YEAR.search(text)) and found[1].lower() in _MONTH_NAMES:
        return make(int(found[3]), _MONTH_NAMES[found[1].lower()], int(found[2]))
    if (found := _DAY_MONTH_YEAR.search(text)) and found[2].lower() in _MONTH_NAMES:
        return make(int(found[3]), _MONTH_NAMES[found[2].lower()], int(found[1]))
    if (found := _MONTH_YEAR.search(text)) and found[1].lower() in _MONTH_NAMES:
        year, month = int(found[2]), _MONTH_NAMES[found[1].lower()]
        return make(year, month, calendar.monthrange(year, month)[1])
    return None


def derive(
    promise: PromiseFacts,
    findings: Sequence[Assessment],
    items: Mapping[str, Evidence],
    grace_end: date | None,
    today: date,
) -> Derived:
    """The verdict the findings give: when delivery happened (the earliest event date the
    satisfying items state, else their publication date), whether the claim was revised
    before its due date, and how late it was."""
    known = [a for a in findings if a.evidence_id in items]

    def dated(finding: str) -> list[tuple[date, str]]:
        out = []
        for a in known:
            if a.finding != finding:
                continue
            item = items[a.evidence_id]
            stated = event_date(item, a.event_date_text) if finding == "satisfies" else None
            if stated is not None and stated <= promise.statement_date:
                continue  # it happened before the claim: an earlier announcement repeated
            if stated is not None:
                out.append((stated, "event"))
            elif (published := item_date(item, items)) is not None:
                out.append((published, "published"))
        return sorted(out)

    # A revision counts as made before the due date when it came no later than the report
    # of the due period (the end of the grace period).
    revisions = [d for d, _ in dated("walks_back")]
    cutoff = grace_end or promise.due_date
    revised = any(cutoff is None or d <= cutoff for d in revisions)
    satisfying = dated("satisfies")
    if satisfying:
        proof, source = satisfying[0]
        if promise.due_date is None:
            return Derived("delivered", proof, source, None, revised)
        # A date the evidence states is compared with the due date itself; a publication
        # date with the end of the grace period, since reports come after the fact.
        deadline = promise.due_date if source == "event" else (grace_end or promise.due_date)
        if proof > deadline or (revised and proof > promise.due_date):
            return Derived(
                "delivered_late", proof, source, (proof - promise.due_date).days, revised
            )
        return Derived("delivered", proof, source, None, revised)
    if promise.open_ended:
        if promise.expires is not None and promise.expires <= today:
            return Derived("expired_unproven", revised_before_due=revised)
        return Derived("too_early", revised_before_due=revised)
    if revisions:
        return Derived("walked_back", revised_before_due=revised)
    grace_passed = grace_end is not None and grace_end <= today
    if grace_passed and dated("partly"):
        return Derived("partly_delivered")
    if grace_passed and dated("contradicts"):
        return Derived("not_delivered")
    return Derived("cannot_determine" if grace_passed else "too_early")


def check(
    promise: PromiseFacts,
    judgment: Judgment,
    items: Mapping[str, Evidence],
    measured: Measured,
    grace_end: date | None,
    today: date,
) -> Checked:
    """The verdict that may be stored, and every check with its result."""
    findings = _numeric_findings(promise, judgment, measured)
    decided = derive(promise, findings, items, grace_end, today)
    verdict, proof = decided.verdict, decided.proof_date
    result = Checked(
        verdict,
        derived=verdict,
        proof_date=proof,
        proof_date_source=decided.proof_date_source,
        days_late=decided.days_late,
        revised_before_due=decided.revised_before_due,
    )
    relevant = [a.evidence_id for a in judgment.assessments if a.finding != "irrelevant"]
    cited = list(dict.fromkeys(relevant + [m.evidence_id for m in judgment.measured]))
    result.cited = cited
    missing = [i for i in cited if i not in items]
    result.record("evidence_exist", not missing, ", ".join(missing))
    present = [items[i] for i in cited if i in items]
    early = [
        f"{i['id']} ({item_date(i, items) or 'no date'})"
        for i in present
        if not _after_statement(i, items, promise)
    ]
    result.record("evidence_after_statement", not early, ", ".join(early))
    if verdict not in ("too_early", "cannot_determine", "expired_unproven"):
        _key_terms(result, promise, present, items)
    _numbers(result, judgment, list(items.values()), measured, promise)
    if verdict in ("delivered", "delivered_late", "not_delivered", "partly_delivered") and (
        promise.numeric
    ):
        result.record("measured_from_tools", measured.value is not None, measured.error or "")
        if measured.value is not None and verdict != "partly_delivered":
            reached = _reaches(promise, measured.value)
            if reached is not None and promise.required is not None:
                result.record(
                    "measured_meets_target",
                    reached == (verdict != "not_delivered"),
                    f"measured {measured.value:f} against required {promise.required:f}",
                )
    if verdict == "partly_delivered":
        _partly(result, promise, judgment, measured)
    if proof is not None:
        source = "stated in the evidence" if decided.proof_date_source == "event" else "published"
        deadline = promise.due_date if decided.proof_date_source == "event" else grace_end
        note = f"delivered {proof} ({source}), on time until {deadline or 'unknown'}"
        result.record("on_time", None, note)
    if decided.revised_before_due:
        result.record("revised_before_due", None, "a later statement revised the claim")
    if promise.per_share and promise.split_ratio != 1:
        result.record(
            "split_adjusted",
            None,
            f"per-share target divided by {promise.split_ratio.normalize():f} for splits since "
            "the statement",
        )
    if result.failed:
        result.verdict = "cannot_determine"
    return result


def _numeric_findings(
    promise: PromiseFacts, judgment: Judgment, measured: Measured
) -> list[Assessment]:
    """The findings, with a numeric target's measured items judged by code: they satisfy
    the rule when the measured value reaches the required one, show it partly met when
    it lies between the baseline and the target, and contradict it otherwise, whatever
    the judge said of them."""
    if not promise.numeric or measured.value is None or promise.required is None:
        return list(judgment.assessments)
    reached = _reaches(promise, measured.value)
    if reached is None:
        return list(judgment.assessments)
    sources = {m.evidence_id for m in judgment.measured}
    finding: Finding = "satisfies" if reached else "contradicts"
    if not reached and promise.baseline is not None and promise.required is not None:
        low, high = sorted((promise.baseline, promise.required))
        if low < measured.value < high:
            finding = "partly"  # moved from the baseline toward the target
    kept = [
        a
        if a.evidence_id not in sources
        else Assessment(evidence_id=a.evidence_id, finding=finding, note=a.note)
        for a in judgment.assessments
    ]
    listed = {a.evidence_id for a in kept}
    kept += [Assessment(evidence_id=i, finding=finding) for i in sources - listed]
    return kept


def _after_statement(item: Evidence, items: Mapping[str, Evidence], promise: PromiseFacts) -> bool:
    """Dated after the statement; on the statement's own day, only when its timestamp is
    later than the statement's article, or it reports a period that ended after the
    statement."""
    day = item_date(item, items)
    if day is None or day < promise.statement_date:
        return False
    if day > promise.statement_date:
        return True
    data = item["data"]
    period_end = data.get("period_end")
    if isinstance(period_end, str) and period_end[:10] > promise.statement_date.isoformat():
        return True
    accepted = data.get("accepted")
    if isinstance(accepted, str) and promise.statement_at is not None:
        try:
            return datetime.fromisoformat(accepted) > promise.statement_at
        except ValueError:
            return False
    return False


def _key_terms(
    result: Checked,
    promise: PromiseFacts,
    present: Sequence[Evidence],
    items: Mapping[str, Evidence],
) -> None:
    texts = [_text_of(i) for i in present]
    for item in present:  # a computed item's terms are in the items it was computed from
        for source in item["data"].get("inputs", []) if is_derived(item) else []:
            if source in items:
                texts.append(_text_of(items[source]))
    terms = key_terms(promise.resolution_rule, promise.company_names)
    absent = [t for t in terms if not any(_mentioned(t, text) for text in texts)]
    detail = f"not in any cited item: {', '.join(absent)}" if absent else ", ".join(terms)
    result.record("key_terms", not absent, detail)


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
    required = promise.required
    if required is None:
        return None
    if promise.direction == "at_most":
        return value <= required
    if promise.target_kind == "threshold" and required == 0:
        return value > 0
    return value >= required


def _partly(result: Checked, promise: PromiseFacts, judgment: Judgment, measured: Measured) -> None:
    if promise.numeric and promise.required is not None:
        if measured.value is None or promise.baseline is None:
            result.record("partly_between", False, "no measured value or no baseline")
            return
        low, high = sorted((promise.baseline, promise.required))
        result.record(
            "partly_between",
            low < measured.value < high,
            f"measured {measured.value:f} between {low:f} and {high:f}",
        )
        return
    reason = judgment.partial_reason.strip()
    result.record("partly_between", bool(reason), reason)
