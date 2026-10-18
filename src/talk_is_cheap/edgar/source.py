"""SEC EDGAR access through edgartools.

edgartools sends the identity header the SEC requires, keeps within the SEC's request-rate
limit, caches responses on disk and pages through a company's full filing history. This
module only converts its objects into plain records (`talk_is_cheap.edgar.records`), so the
rest of the code and its tests never depend on edgartools or the network.

edgartools is synchronous; async callers run these methods in a worker thread.

edgartools is imported only inside the functions that use it: importing it loads pandas,
pyarrow and more, which takes seconds, and only ingestion needs it.
"""

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from talk_is_cheap.edgar.records import CompanyInfo, FilingRecord, RawFact

# edgartools renames some SEC units; map them back so config uses the SEC's own names.
_SEC_UNITS = {"USD per share": "USD/shares", "number": "pure"}


class EdgarSourceError(Exception):
    """EDGAR data could not be retrieved for a company."""


class EdgarSource(Protocol):
    """Where companies, filings and XBRL facts come from."""

    def company(self, ticker: str) -> CompanyInfo: ...

    def filings(self, cik: int, *, forms: Sequence[str], since: date) -> list[FilingRecord]: ...

    def facts(
        self, cik: int, *, concepts: Collection[str], period_end_from: date
    ) -> list[RawFact]: ...


def configure_edgartools(identity: str, data_dir: Path) -> None:
    """Set the SEC identity header and where edgartools keeps its response cache."""
    import edgar

    data_dir.mkdir(parents=True, exist_ok=True)
    edgar.set_identity(identity)
    edgar.set_local_storage_path(data_dir)


def _edgartools_company(identifier: str | int) -> Any:
    from edgar import Company

    return Company(identifier)


class EdgartoolsSource:
    """`EdgarSource` backed by edgartools. Call `configure_edgartools` first."""

    def __init__(self, company_factory: Callable[[str | int], Any] = _edgartools_company) -> None:
        self._company_factory = company_factory
        self._companies: dict[int, Any] = {}

    def company(self, ticker: str) -> CompanyInfo:
        # edgartools loads company data lazily, on first attribute access, so network
        # errors can surface while reading attributes, not only in the constructor.
        try:
            company = self._company_factory(ticker)
            not_found = company is None or bool(getattr(company, "not_found", False))
            info = None if not_found else company_info(company, ticker)
        except Exception as exc:
            raise EdgarSourceError(f"lookup failed for {ticker}: {exc}") from exc
        if info is None:
            raise EdgarSourceError(f"{ticker} was not found on EDGAR")
        self._companies[info.cik] = company
        return info

    def filings(self, cik: int, *, forms: Sequence[str], since: date) -> list[FilingRecord]:
        company = self._company(cik)
        try:
            found = company.get_filings(form=list(forms), filing_date=f"{since.isoformat()}:")
            rows = found.data.to_pylist() if found is not None else []
        except Exception as exc:
            raise EdgarSourceError(f"filings failed for CIK {cik}: {exc}") from exc
        return filing_records(rows, cik, forms=forms, since=since)

    def facts(self, cik: int, *, concepts: Collection[str], period_end_from: date) -> list[RawFact]:
        company = self._company(cik)
        try:
            entity_facts = company.get_facts()
        except Exception as exc:
            raise EdgarSourceError(f"XBRL facts failed for CIK {cik}: {exc}") from exc
        if entity_facts is None:
            return []  # the company has filed no XBRL financial data
        return raw_facts(entity_facts, concepts=concepts, period_end_from=period_end_from)

    def _company(self, cik: int) -> Any:
        if cik not in self._companies:
            try:
                self._companies[cik] = self._company_factory(cik)
            except Exception as exc:
                raise EdgarSourceError(f"lookup failed for CIK {cik}: {exc}") from exc
        return self._companies[cik]


def company_info(company: Any, ticker: str) -> CompanyInfo:
    """Convert an edgartools `Company`."""
    fye = str(getattr(company, "fiscal_year_end", None) or "").strip()
    sic = getattr(company, "sic", None)
    industry = getattr(company, "industry", None)
    return CompanyInfo(
        cik=int(company.cik),
        ticker=ticker.upper(),
        name=str(getattr(company, "name", None) or ticker.upper()),
        fiscal_year_end=fye if len(fye) == 4 and fye.isdigit() else None,
        sic=str(sic) if sic else None,
        sic_description=str(industry) if industry else None,
    )


def filing_records(
    rows: Iterable[Mapping[str, Any]],
    cik: int,
    *,
    forms: Collection[str],
    since: date,
) -> list[FilingRecord]:
    """Convert rows of an edgartools filings table, keeping the given forms filed since `since`."""
    wanted = set(forms)
    records = []
    for row in rows:
        form = str(row.get("form") or "")
        filed = _as_date(row.get("filing_date"))
        accession = row.get("accession_number")
        if form not in wanted or filed is None or filed < since or not accession:
            continue
        records.append(
            FilingRecord(
                accession_no=str(accession),
                cik=cik,
                form=form,
                filing_date=filed,
                report_date=_as_date(row.get("reportDate")),
                acceptance_datetime=_as_utc(row.get("acceptanceDateTime")),
                primary_document=str(row["primaryDocument"])
                if row.get("primaryDocument")
                else None,
                items=str(row["items"]) if row.get("items") else None,
                is_xbrl=str(row.get("isXBRL")).strip().lower() in {"1", "true"},
            )
        )
    return records


def raw_facts(
    entity_facts: Iterable[Any],
    *,
    concepts: Collection[str],
    period_end_from: date,
) -> list[RawFact]:
    """Convert edgartools `FinancialFact`s, keeping the listed `taxonomy:Concept` names whose
    period ends on or after `period_end_from`. Every filing's version of a fact is kept."""
    wanted = set(concepts)
    facts = []
    for fact in entity_facts:
        qualified = str(fact.concept)
        if ":" not in qualified:
            qualified = f"{fact.taxonomy}:{qualified}"
        if qualified not in wanted:
            continue
        period_end = _as_date(fact.period_end)
        filed = _as_date(fact.filing_date)
        value = _as_decimal(fact.value)
        if period_end is None or filed is None or value is None or not fact.accession:
            continue
        if period_end < period_end_from:
            continue
        taxonomy, concept = qualified.split(":", 1)
        facts.append(
            RawFact(
                taxonomy=taxonomy,
                concept=concept,
                unit=_SEC_UNITS.get(str(fact.unit), str(fact.unit)),
                value=value,
                period_start=_as_date(fact.period_start),
                period_end=period_end,
                accession_no=str(fact.accession),
                form=str(fact.form_type or ""),
                filed=filed,
                fiscal_year=int(fact.fiscal_year) if fact.fiscal_year else None,
                fiscal_period=str(fact.fiscal_period) if fact.fiscal_period else None,
            )
        )
    return facts


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None
