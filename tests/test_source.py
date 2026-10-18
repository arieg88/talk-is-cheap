"""The edgartools adapter, tested offline against edgartools' own parsing and table schema."""

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from edgar.entity.filings import COMPANY_FILINGS_SCHEMA
from edgar.entity.parser import EntityFactsParser

from talk_is_cheap.edgar.records import FilingRecord
from talk_is_cheap.edgar.source import (
    EdgarSourceError,
    EdgartoolsSource,
    configure_edgartools,
    filing_documents,
    filing_records,
    raw_facts,
)

ACCN_Q3 = "0000000001-25-000030"
ACCN_NEXT = "0000000001-26-000030"

# Shaped like SEC's companyfacts JSON. Values are illustrative.
COMPANY_FACTS: dict[str, Any] = {
    "cik": 1,
    "entityName": "EXAMPLE CORP",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "description": "",
                "units": {
                    "USD": [
                        {
                            "start": "2025-07-01",
                            "end": "2025-09-30",
                            "val": 500,
                            "accn": ACCN_Q3,
                            "fy": 2025,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2025-10-30",
                        },
                        # The same quarter, restated a year later.
                        {
                            "start": "2025-07-01",
                            "end": "2025-09-30",
                            "val": 510,
                            "accn": ACCN_NEXT,
                            "fy": 2026,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2026-10-29",
                        },
                        # Too old for the requested window.
                        {
                            "start": "2022-07-01",
                            "end": "2022-09-30",
                            "val": 300,
                            "accn": "0000000001-22-000030",
                            "fy": 2022,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2022-10-30",
                        },
                    ]
                },
            },
            "EarningsPerShareDiluted": {
                "label": "EPS",
                "description": "",
                "units": {
                    "USD/shares": [
                        {
                            "start": "2025-07-01",
                            "end": "2025-09-30",
                            "val": 1.25,
                            "accn": ACCN_Q3,
                            "fy": 2025,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2025-10-30",
                        }
                    ]
                },
            },
            "Assets": {
                "label": "Assets",
                "description": "",
                "units": {
                    "USD": [
                        {
                            "end": "2025-09-30",
                            "val": 9000,
                            "accn": ACCN_Q3,
                            "fy": 2025,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2025-10-30",
                        }
                    ]
                },
            },
        }
    },
}

WANTED = {"us-gaap:Revenues", "us-gaap:EarningsPerShareDiluted"}


def parsed_facts() -> Any:
    entity_facts = EntityFactsParser.parse_company_facts(COMPANY_FACTS)
    assert entity_facts is not None
    return entity_facts


def test_raw_facts_keep_every_filing_version_with_sec_units() -> None:
    facts = raw_facts(parsed_facts(), concepts=WANTED, period_end_from=date(2024, 1, 1))

    revenue = sorted((f for f in facts if f.concept == "Revenues"), key=lambda f: f.filed)
    assert [(f.value, f.accession_no, f.filed) for f in revenue] == [
        (Decimal("500"), ACCN_Q3, date(2025, 10, 30)),
        (Decimal("510"), ACCN_NEXT, date(2026, 10, 29)),
    ]
    assert revenue[0].taxonomy == "us-gaap"
    assert revenue[0].qualified_concept == "us-gaap:Revenues"
    assert (revenue[0].period_start, revenue[0].period_end) == (
        date(2025, 7, 1),
        date(2025, 9, 30),
    )
    assert revenue[0].form == "10-Q"

    eps = [f for f in facts if f.concept == "EarningsPerShareDiluted"]
    assert [(f.unit, f.value) for f in eps] == [("USD/shares", Decimal("1.25"))]


def test_raw_facts_drop_unrequested_concepts_and_old_periods() -> None:
    facts = raw_facts(parsed_facts(), concepts=WANTED, period_end_from=date(2024, 1, 1))
    assert {f.concept for f in facts} == {"Revenues", "EarningsPerShareDiluted"}
    assert all(f.period_end >= date(2024, 1, 1) for f in facts)


def filings_table(rows: list[dict[str, Any]]) -> pa.Table:
    columns = {name: [row.get(name) for row in rows] for name in COMPANY_FILINGS_SCHEMA.names}
    return pa.Table.from_pydict(columns, schema=COMPANY_FILINGS_SCHEMA)


FILING_ROWS = [
    {
        "accession_number": "0000000001-25-000040",
        "filing_date": date(2025, 10, 30),
        "reportDate": "2025-09-30",
        "acceptanceDateTime": datetime(2025, 10, 30, 16, 5),
        "form": "8-K",
        "items": "2.02,9.01",
        "isXBRL": "1",
        "primaryDocument": "ex991.htm",
    },
    {
        "accession_number": ACCN_Q3,
        "filing_date": date(2025, 10, 30),
        "reportDate": "2025-09-30",
        "form": "10-Q",
        "items": "",
        "isXBRL": "1",
        "primaryDocument": "q3.htm",
    },
    {
        "accession_number": "0000000001-25-000041",
        "filing_date": date(2025, 11, 2),
        "form": "4",
        "isXBRL": "0",
    },
    {
        "accession_number": "0000000001-23-000001",
        "filing_date": date(2023, 2, 1),
        "form": "10-K",
        "isXBRL": "1",
    },
]


def test_filing_records_from_an_edgartools_table() -> None:
    rows = filings_table(FILING_ROWS).to_pylist()
    records = filing_records(rows, 1, forms=["10-K", "10-Q", "8-K"], since=date(2024, 1, 1))

    assert [r.accession_no for r in records] == ["0000000001-25-000040", ACCN_Q3]
    earnings = records[0]
    assert earnings.form == "8-K"
    assert earnings.items == "2.02,9.01"
    assert earnings.report_date == date(2025, 9, 30)
    assert earnings.acceptance_datetime == datetime(2025, 10, 30, 16, 5, tzinfo=UTC)
    assert earnings.primary_document == "ex991.htm"
    assert earnings.is_xbrl
    assert records[1].items is None
    assert records[1].acceptance_datetime is None


def fake_company(**overrides: Any) -> SimpleNamespace:
    company = SimpleNamespace(
        cik=1,
        name="EXAMPLE CORP",
        fiscal_year_end="1231",
        sic="3571",
        industry="Electronic Computers",
        not_found=False,
        get_filings=lambda **_: SimpleNamespace(data=filings_table(FILING_ROWS)),
        get_facts=parsed_facts,
    )
    for key, value in overrides.items():
        setattr(company, key, value)
    return company


def test_source_converts_company_filings_and_facts() -> None:
    calls: list[str | int] = []

    def factory(identifier: str | int) -> SimpleNamespace:
        calls.append(identifier)
        return fake_company()

    source = EdgartoolsSource(company_factory=factory)
    info = source.company("exmp")
    assert (info.cik, info.ticker, info.name, info.fiscal_year_end) == (
        1,
        "EXMP",
        "EXAMPLE CORP",
        "1231",
    )
    assert (info.sic, info.sic_description) == ("3571", "Electronic Computers")

    filings = source.filings(1, forms=["10-Q"], since=date(2024, 1, 1))
    assert [f.accession_no for f in filings] == [ACCN_Q3]
    facts = source.facts(1, concepts=WANTED, period_end_from=date(2024, 1, 1))
    assert len(facts) == 3
    assert calls == ["exmp"]  # the company object is reused, not looked up again


def test_source_reports_unknown_companies() -> None:
    source = EdgartoolsSource(company_factory=lambda _: fake_company(not_found=True))
    with pytest.raises(EdgarSourceError, match="not found"):
        source.company("NOPE")


def test_source_wraps_edgartools_failures() -> None:
    def broken(_: str | int) -> SimpleNamespace:
        raise ConnectionError("SEC unreachable")

    with pytest.raises(EdgarSourceError, match="SEC unreachable"):
        EdgartoolsSource(company_factory=broken).company("AAPL")

    def failing_filings(**_: Any) -> None:
        raise TimeoutError("timed out")

    source = EdgartoolsSource(company_factory=lambda _: fake_company(get_filings=failing_filings))
    source.company("EXMP")
    with pytest.raises(EdgarSourceError, match="timed out"):
        source.filings(1, forms=["10-Q"], since=date(2024, 1, 1))


def test_company_without_xbrl_facts_has_no_facts() -> None:
    source = EdgartoolsSource(company_factory=lambda _: fake_company(get_facts=lambda: None))
    assert source.facts(1, concepts=WANTED, period_end_from=date(2024, 1, 1)) == []


def test_missing_fiscal_year_end_is_none() -> None:
    source = EdgartoolsSource(company_factory=lambda _: fake_company(fiscal_year_end=""))
    assert source.company("EXMP").fiscal_year_end is None


def test_configure_edgartools_sets_identity_and_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("EDGAR_LOCAL_DATA_DIR", raising=False)
    data_dir = tmp_path / "edgar-cache"

    configure_edgartools("Jane Doe jane@example.com", data_dir)

    assert data_dir.is_dir()
    assert os.environ["EDGAR_IDENTITY"] == "Jane Doe jane@example.com"
    assert os.environ["EDGAR_LOCAL_DATA_DIR"] == str(data_dir.resolve())


class LazyCompany:
    """Mimics edgartools: data is fetched on first attribute access, not at construction."""

    cik = 1

    @property
    def not_found(self) -> bool:
        raise ConnectionError("403 Forbidden while loading company data")


def test_source_wraps_errors_raised_by_lazy_company_loading() -> None:
    source = EdgartoolsSource(company_factory=lambda _: LazyCompany())
    with pytest.raises(EdgarSourceError, match="403 Forbidden"):
        source.company("AAPL")


BASE_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000125000040"


def attachment(document: str, document_type: str, content: str | bytes = "<p>x</p>") -> Any:
    return SimpleNamespace(
        document=document,
        document_type=document_type,
        description=document_type,
        url=f"{BASE_URL}/{document}",
        content=content,
    )


ATTACHMENTS = [
    attachment("q3.htm", "8-K"),
    attachment("ex991.htm", "EX-99.1", b"<p>Press release</p>"),
    attachment("ex992.htm", "EX-99.2"),
    attachment("ex101.xsd", "EX-101.SCH"),
    attachment("logo.jpg", "GRAPHIC"),
    attachment("R1.htm", "XML"),
    attachment("ex10.htm", "EX-10.1"),
]


def earnings_filing(primary: str | None = "q3.htm", form: str = "8-K") -> FilingRecord:
    return FilingRecord(
        accession_no="0000000001-25-000040",
        cik=1,
        form=form,
        filing_date=date(2025, 10, 30),
        report_date=None,
        acceptance_datetime=None,
        primary_document=primary,
        items="2.02,9.01",
        is_xbrl=True,
    )


def test_filing_documents_keep_primary_and_ex99_html() -> None:
    documents = filing_documents(ATTACHMENTS, earnings_filing())
    assert [(d.document_name, d.document_type) for d in documents] == [
        ("q3.htm", "8-K"),
        ("ex991.htm", "EX-99.1"),
        ("ex992.htm", "EX-99.2"),
    ]
    release = documents[1]
    assert release.html == "<p>Press release</p>"  # bytes are decoded
    assert release.url == f"{BASE_URL}/ex991.htm"
    assert release.accession_no == "0000000001-25-000040"


def test_filing_documents_find_the_primary_by_form_when_unrecorded() -> None:
    documents = filing_documents(
        [attachment("q.htm", "10-Q"), attachment("other.htm", "EX-31.1")],
        earnings_filing(primary=None, form="10-Q"),
    )
    assert [d.document_name for d in documents] == ["q.htm"]


def test_source_fetches_documents_through_edgartools_filings() -> None:
    requested: list[str] = []

    def filing_factory(record: FilingRecord) -> SimpleNamespace:
        requested.append(record.accession_no)
        return SimpleNamespace(attachments=ATTACHMENTS)

    source = EdgartoolsSource(
        company_factory=lambda _: fake_company(), filing_factory=filing_factory
    )
    documents = source.documents(earnings_filing())
    assert len(documents) == 3
    assert requested == ["0000000001-25-000040"]


def test_source_wraps_document_failures() -> None:
    class Unreachable:
        @property
        def attachments(self) -> list[Any]:
            raise ConnectionError("SEC unreachable")

    source = EdgartoolsSource(filing_factory=lambda _: Unreachable())
    with pytest.raises(EdgarSourceError, match=r"0000000001-25-000040.*SEC unreachable"):
        source.documents(earnings_filing())
