"""Evidence: what every tool returns, one citable item per fact.

Each item has a citation id (`E1`, `E2`, ... unique within a conversation), a provenance
tag, the URL a citation resolves to, the tool that produced it, and its data. Items are
plain JSON so they live in the graph's state and its checkpoints.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Literal

Provenance = Literal["sec_filing", "company_press_release", "news_report"]

PROVENANCES: tuple[Provenance, ...] = ("sec_filing", "company_press_release", "news_report")

Evidence = dict[str, Any]


def provenance_of(doc_type: str) -> Provenance:
    """The provenance of a document type: earnings releases and their exhibits are the
    company's press releases, 10-Qs and 10-Ks are filings, articles are news."""
    if doc_type == "news":
        return "news_report"
    if doc_type in ("press_release", "earnings_exhibit", "deliveries_report", "other_exhibit"):
        return "company_press_release"
    return "sec_filing"


def evidence_number(evidence_id: str) -> int:
    return int(evidence_id.removeprefix("E"))


class EvidenceLog:
    """Assigns citation ids and keeps every item of a conversation. Ids continue from
    the items already in the conversation, so they never repeat."""

    def __init__(self, items: Mapping[str, Evidence] | None = None) -> None:
        self.items: dict[str, Evidence] = dict(items or {})
        self.added: list[Evidence] = []

    def reset(self, items: Mapping[str, Evidence]) -> None:
        """Start a question with its conversation's items, and only those: ids follow on
        from the thread's own evidence, whatever other threads used."""
        self.items = dict(items)

    def sync(self, items: Mapping[str, Evidence]) -> None:
        """Take in items recorded in the conversation's state."""
        self.items.update(items)

    @property
    def next_number(self) -> int:
        return max((evidence_number(i) for i in self.items), default=0) + 1

    def add(self, provenance: Provenance, url: str, tool: str, data: Mapping[str, Any]) -> Evidence:
        item: Evidence = {
            "id": f"E{self.next_number}",
            "provenance": provenance,
            "url": url,
            "tool": tool,
            "data": dict(data),
        }
        self.items[item["id"]] = item
        self.added.append(item)
        return item

    def get(self, evidence_id: str) -> Evidence | None:
        return self.items.get(evidence_id)


def sources_of(item: Evidence, items: Mapping[str, Evidence]) -> list[Evidence]:
    """The items a citation of `item` stands for: a computed result stands for the
    items it was computed from."""
    if item["tool"] != "compute":
        return [item]
    found: list[Evidence] = []
    for source_id in item["data"].get("inputs", []):
        source = items.get(source_id)
        if source is not None:
            found.extend(s for s in sources_of(source, items) if s not in found)
    return found


def compact(items: Iterable[Evidence]) -> list[dict[str, Any]]:
    """Items as the model sees them: id, provenance, URL and data."""
    return [
        {"id": i["id"], "provenance": i["provenance"], "url": i["url"], **i["data"]} for i in items
    ]
