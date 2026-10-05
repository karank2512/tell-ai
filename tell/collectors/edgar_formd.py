"""Form D: the pydantic contract, the ``primary_doc.xml`` parser, events and the TSV backfill.

Split out of :mod:`tell.collectors.edgar` (which owns the daily index, HTTP and the
``COLLECTOR``) so the Form D field list lives in one place. This module has no
``COLLECTOR``; discovery skips it.

What is kept is exactly the SPEC section 3.6 field list: ``primaryIssuer``
(``entityName``, ``cik``, ``issuerAddress`` city/state/zip, ``jurisdictionOfInc``,
``yearOfInc``, ``entityType``), ``issuerPreviousNameList`` (names only, for Phase 5
aliases), ``offeringData.typeOfFiling.newOrAmendment.isAmendment``, ``dateOfFirstSale``,
``industryGroup.industryGroupType``, ``offeringSalesAmounts`` (``totalOfferingAmount``,
``totalAmountSold``, ``totalRemaining``), ``investors.totalNumberAlreadyInvested``,
``federalExemptionsExclusions`` and the filing's ``fileNumber``; plus
``previousAccessionNumber`` from ``newOrAmendment`` as a fallback amendment link.

The parser walks only ``primaryIssuer`` and ``offeringData``. The officers-and-directors
element of the document is personal data: it is never read, never parsed and never
stored, and every model here forbids unknown keys so it cannot be smuggled in through a
JSON rendering either. The quarterly loader likewise never opens the related-persons
TSV of the DERA data set.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import pydantic
from lxml import etree
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import Event, WriteStats, update_event_payload, write_events
from tell.models import Company, EventRow

__all__ = [
    "FORM_D_AMENDMENT",
    "FORM_D_FILED",
    "QUARTERLY_FILES",
    "SOURCE",
    "FormDFiling",
    "FormDFilings",
    "FormDIngest",
    "amendment_entry",
    "amendment_patch",
    "cik10",
    "company_ids_by_cik",
    "filing_folder_url",
    "formd_event",
    "formd_payload",
    "ingest_formd_filings",
    "load_formd_quarterly",
    "parse_formd_xml",
    "primary_doc_url",
]

SOURCE = "edgar"
FORM_D_FILED = "form_d_filed"
FORM_D_AMENDMENT = "form_d_amendment"

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
INDEFINITE = "indefinite"
_NONE_NAMES = frozenset({"", "none", "n/a", "na"})
_ACCESSION = r"^\d{10}-\d{2}-\d{6}$"

QUARTERLY_FILES: tuple[str, ...] = ("FORMDSUBMISSION.tsv", "ISSUERS.tsv", "OFFERING.tsv")
"""The only DERA Form D data-set files the backfill reads (never the related-persons one)."""

_AMOUNT_KEYS: tuple[str, ...] = (
    "total_offering_amount",
    "total_offering_amount_indefinite",
    "total_amount_sold",
    "total_remaining",
    "total_remaining_indefinite",
    "total_number_already_invested",
)


# --- small helpers ---------------------------------------------------------------------------


def cik10(cik: str | int) -> str:
    """Zero-padded 10-digit CIK, the form ``companies.cik`` stores (SPEC section 5)."""
    return f"{int(cik):010d}"


def accession_no_dashes(accession: str) -> str:
    return accession.replace("-", "")


def filing_folder_url(cik: str | int, accession: str) -> str:
    """``https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/``."""
    return f"{ARCHIVES}/{int(cik)}/{accession_no_dashes(accession)}/"


def primary_doc_url(cik: str | int, accession: str) -> str:
    """The Form D ``primary_doc.xml`` inside the filing folder."""
    return f"{filing_folder_url(cik, accession)}primary_doc.xml"


def parse_amount(value: Any) -> int | None:
    """USD amount as an int; ``None`` for blank. ``Indefinite`` is handled by the caller."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return int(Decimal(text).to_integral_value())
    except InvalidOperation as exc:
        msg = f"not a USD amount: {value!r}"
        raise ValueError(msg) from exc


def _is_indefinite(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == INDEFINITE


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def _bool(value: str | None) -> bool | None:
    text = (value or "").strip().lower()
    if text in {"true", "y", "yes", "1"}:
        return True
    if text in {"false", "n", "no", "0"}:
        return False
    return None


# --- the contract: pydantic models (aliases are the provider's element names) ----------------


class _Strict(pydantic.BaseModel):
    """Frozen, unknown keys forbidden: nothing outside the field list gets through."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class IssuerAddress(_Strict):
    city: str | None = None
    state_or_country: str | None = Field(default=None, alias="stateOrCountry")
    zip_code: str | None = Field(default=None, alias="zipCode")


class PrimaryIssuer(_Strict):
    cik: str = Field(pattern=r"^\d{1,10}$")
    entity_name: str = Field(alias="entityName", min_length=1)
    issuer_address: IssuerAddress = Field(default_factory=IssuerAddress, alias="issuerAddress")
    jurisdiction_of_inc: str | None = Field(default=None, alias="jurisdictionOfInc")
    year_of_inc: int | None = Field(default=None, alias="yearOfInc")
    entity_type: str | None = Field(default=None, alias="entityType")
    issuer_previous_name_list: list[str] = Field(
        default_factory=list, alias="issuerPreviousNameList"
    )


class NewOrAmendment(_Strict):
    is_amendment: bool = Field(alias="isAmendment")
    previous_accession_number: str | None = Field(default=None, alias="previousAccessionNumber")


class TypeOfFiling(_Strict):
    new_or_amendment: NewOrAmendment = Field(alias="newOrAmendment")
    date_of_first_sale: date | None = Field(default=None, alias="dateOfFirstSale")


class IndustryGroup(_Strict):
    industry_group_type: str | None = Field(default=None, alias="industryGroupType")


class OfferingSalesAmounts(_Strict):
    """Amounts in whole USD. ``Indefinite`` becomes ``None`` plus the matching flag.

    ``totalOfferingAmount`` and ``totalAmountSold`` are required keys (``null`` allowed):
    a filing that stops carrying them is drift and must fail validation.
    """

    total_offering_amount: int | None = Field(alias="totalOfferingAmount")
    total_offering_amount_indefinite: bool = Field(
        default=False, alias="totalOfferingAmountIndefinite"
    )
    total_amount_sold: int | None = Field(alias="totalAmountSold")
    total_remaining: int | None = Field(default=None, alias="totalRemaining")
    total_remaining_indefinite: bool = Field(default=False, alias="totalRemainingIndefinite")

    @pydantic.model_validator(mode="before")
    @classmethod
    def _indefinite_and_amounts(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        out = dict(data)
        for key, flag in (
            ("totalOfferingAmount", "totalOfferingAmountIndefinite"),
            ("totalRemaining", "totalRemainingIndefinite"),
        ):
            if key in out and _is_indefinite(out[key]):
                out[key] = None
                out[flag] = True
        for key in ("totalOfferingAmount", "totalAmountSold", "totalRemaining"):
            if key in out:
                out[key] = parse_amount(out[key])
        return out


class Investors(_Strict):
    total_number_already_invested: int | None = Field(
        default=None, alias="totalNumberAlreadyInvested"
    )


class OfferingData(_Strict):
    industry_group: IndustryGroup = Field(default_factory=IndustryGroup, alias="industryGroup")
    federal_exemptions_exclusions: list[str] = Field(
        default_factory=list, alias="federalExemptionsExclusions"
    )
    type_of_filing: TypeOfFiling = Field(alias="typeOfFiling")
    offering_sales_amounts: OfferingSalesAmounts = Field(alias="offeringSalesAmounts")
    investors: Investors = Field(default_factory=Investors)


class FormDFiling(_Strict):
    """One Form D or D/A: the parsed ``primary_doc.xml`` plus the filing's index metadata.

    This is the JSON rendering the fixtures store (``normal.json`` is a list of these).
    """

    accession_number: str = Field(alias="accessionNumber", pattern=_ACCESSION)
    cik: str = Field(pattern=r"^\d{1,10}$")
    form_type: Literal["D", "D/A"] = Field(alias="formType")
    date_filed: date = Field(alias="dateFiled")
    file_number: str | None = Field(default=None, alias="fileNumber")
    primary_issuer: PrimaryIssuer = Field(alias="primaryIssuer")
    offering_data: OfferingData = Field(alias="offeringData")

    @property
    def is_amendment(self) -> bool:
        return (
            self.form_type == "D/A"
            or self.offering_data.type_of_filing.new_or_amendment.is_amendment
        )

    def to_json_dict(self) -> dict[str, Any]:
        """The fixture rendering: provider element names, JSON types."""
        return self.model_dump(mode="json", by_alias=True)


class FormDFilings(pydantic.RootModel[list[FormDFiling]]):
    """``COLLECTOR.response_model``: a JSON list of :class:`FormDFiling` renderings."""


# --- primary_doc.xml -> FormDFiling --------------------------------------------------------------


_XML_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, remove_comments=True, huge_tree=False
)


def _local(element: etree._Element) -> str:
    tag = element.tag
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(parent: etree._Element | None, name: str) -> etree._Element | None:
    if parent is None:
        return None
    for element in parent:
        if _local(element) == name:
            return element
    return None


def _children(parent: etree._Element | None, name: str) -> Iterator[etree._Element]:
    if parent is None:
        return
    for element in parent:
        if _local(element) == name:
            yield element


def _text(parent: etree._Element | None, *path: str) -> str | None:
    node = parent
    for name in path:
        node = _child(node, name)
    if node is None:
        return None
    return _clean(node.text)


def _set(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _issuer_dict(issuer: etree._Element | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if issuer is None:
        return out
    _set(out, "cik", _text(issuer, "cik"))
    _set(out, "entityName", _text(issuer, "entityName"))
    address = _child(issuer, "issuerAddress")
    if address is not None:
        # Street lines are deliberately not read: city, state and zip only.
        out["issuerAddress"] = {
            "city": _text(address, "city"),
            "stateOrCountry": _text(address, "stateOrCountry"),
            "zipCode": _text(address, "zipCode"),
        }
    _set(out, "jurisdictionOfInc", _text(issuer, "jurisdictionOfInc"))
    _set(out, "entityType", _text(issuer, "entityType"))
    year = _text(issuer, "yearOfInc", "value")
    if year is not None and year.isdigit():
        out["yearOfInc"] = int(year)
    previous = _child(issuer, "issuerPreviousNameList")
    names = [_clean(el.text) for el in _children(previous, "previousName")]
    out["issuerPreviousNameList"] = [n for n in names if n and n.lower() not in _NONE_NAMES]
    return out


def _offering_dict(offering: etree._Element | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if offering is None:
        return out
    out["industryGroup"] = {
        "industryGroupType": _text(offering, "industryGroup", "industryGroupType")
    }
    exemptions = _child(offering, "federalExemptionsExclusions")
    out["federalExemptionsExclusions"] = [
        item for item in (_clean(el.text) for el in _children(exemptions, "item")) if item
    ]
    filing = _child(offering, "typeOfFiling")
    if filing is not None:
        new_or_amendment = _child(filing, "newOrAmendment")
        noa: dict[str, Any] = {}
        if new_or_amendment is not None:
            _set(noa, "isAmendment", _bool(_text(new_or_amendment, "isAmendment")))
            _set(
                noa,
                "previousAccessionNumber",
                _text(new_or_amendment, "previousAccessionNumber"),
            )
        out["typeOfFiling"] = {
            "newOrAmendment": noa,
            "dateOfFirstSale": _text(filing, "dateOfFirstSale", "value"),
        }
    amounts = _child(offering, "offeringSalesAmounts")
    if amounts is not None:
        sales: dict[str, Any] = {}
        for name in ("totalOfferingAmount", "totalAmountSold", "totalRemaining"):
            node = _child(amounts, name)
            if node is not None:
                sales[name] = _clean(node.text)
        out["offeringSalesAmounts"] = sales
    invested = _text(offering, "investors", "totalNumberAlreadyInvested")
    out["investors"] = {"totalNumberAlreadyInvested": invested}
    return out


def parse_formd_xml(
    xml: bytes | str,
    *,
    accession_number: str,
    cik: str,
    form_type: str,
    date_filed: date,
    file_number: str | None = None,
) -> FormDFiling:
    """Parse one ``primary_doc.xml`` into :class:`FormDFiling`.

    Only ``primaryIssuer`` and ``offeringData`` are visited; every other element of the
    document (signatures, the officers-and-directors list, sales compensation) is
    ignored. Raises :class:`pydantic.ValidationError` when a contract field is missing
    and :class:`ValueError` when the document is not well-formed XML.
    """
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    try:
        root = etree.fromstring(raw, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        msg = f"{accession_number}: primary_doc.xml is not well-formed: {exc}"
        raise ValueError(msg) from exc
    data = {
        "accessionNumber": accession_number,
        "cik": str(int(cik)),
        "formType": form_type,
        "dateFiled": date_filed.isoformat(),
        "fileNumber": file_number,
        "primaryIssuer": _issuer_dict(_child(root, "primaryIssuer")),
        "offeringData": _offering_dict(_child(root, "offeringData")),
    }
    return FormDFiling.model_validate(data)


# --- events --------------------------------------------------------------------------------------


def _occurred_at(day: date) -> datetime:
    """Filing date as midnight UTC: EDGAR gives a calendar date, never a time."""
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


def formd_payload(filing: FormDFiling, *, backfill: bool = False) -> dict[str, Any]:
    """Event payload: the contract fields in snake case, plus what Phase 5 links on."""
    issuer = filing.primary_issuer
    offering = filing.offering_data
    amounts = offering.offering_sales_amounts
    first_sale = offering.type_of_filing.date_of_first_sale
    payload: dict[str, Any] = {
        "accession_number": filing.accession_number,
        "cik": cik10(filing.cik),
        "form_type": filing.form_type,
        "date_filed": filing.date_filed.isoformat(),
        "file_number": filing.file_number,
        "is_amendment": filing.is_amendment,
        "previous_accession_number": (
            offering.type_of_filing.new_or_amendment.previous_accession_number
        ),
        "issuer_name": issuer.entity_name,
        "issuer_city": issuer.issuer_address.city,
        "issuer_state": issuer.issuer_address.state_or_country,
        "issuer_zip": issuer.issuer_address.zip_code,
        "previous_names": list(issuer.issuer_previous_name_list),
        "jurisdiction_of_inc": issuer.jurisdiction_of_inc,
        "year_of_inc": issuer.year_of_inc,
        "entity_type": issuer.entity_type,
        "industry_group_type": offering.industry_group.industry_group_type,
        "date_of_first_sale": first_sale.isoformat() if first_sale else None,
        "total_offering_amount": amounts.total_offering_amount,
        "total_offering_amount_indefinite": amounts.total_offering_amount_indefinite,
        "total_amount_sold": amounts.total_amount_sold,
        "total_remaining": amounts.total_remaining,
        "total_remaining_indefinite": amounts.total_remaining_indefinite,
        "total_number_already_invested": offering.investors.total_number_already_invested,
        "federal_exemptions_exclusions": list(offering.federal_exemptions_exclusions),
        "amendments": [],
    }
    if backfill:
        payload["backfill"] = True
    return payload


def formd_event(
    filing: FormDFiling,
    *,
    run_id: str,
    now: datetime,
    company_id: int | None = None,
    backfill: bool = False,
    payload: dict[str, Any] | None = None,
) -> Event:
    """``form_d_filed`` (new) or ``form_d_amendment`` (a D/A whose original is unknown).

    Key ``edgar:{accession}``: external id and version are both the accession number.
    Magnitude is ``totalOfferingAmount`` in USD, 0 when undisclosed (Phase 7 owns the
    log scaling).
    """
    amount = filing.offering_data.offering_sales_amounts.total_offering_amount
    return Event(
        source=SOURCE,
        type=FORM_D_AMENDMENT if filing.is_amendment else FORM_D_FILED,
        external_id=filing.accession_number,
        version=filing.accession_number,
        occurred_at=_occurred_at(filing.date_filed),
        observed_at=now,
        url=filing_folder_url(filing.cik, filing.accession_number),
        payload=payload if payload is not None else formd_payload(filing, backfill=backfill),
        run_id=run_id,
        magnitude=float(amount or 0),
        company_id=company_id,
    )


def amendment_entry(filing: FormDFiling) -> dict[str, Any]:
    """What one D/A contributes to the original's ``amendments`` list."""
    full = formd_payload(filing)
    entry = {key: full[key] for key in _AMOUNT_KEYS}
    entry["accession_number"] = filing.accession_number
    entry["date_filed"] = filing.date_filed.isoformat()
    return entry


def amendment_patch(original: Mapping[str, Any], filing: FormDFiling) -> dict[str, Any] | None:
    """The shallow patch a D/A applies to its original ``form_d_filed`` payload.

    Appends to ``amendments`` (ordered by filing date, then accession) and, when this
    D/A is the latest one, overwrites the amount fields and sets ``last_amended_on``.
    Returns ``None`` when this accession was merged before (replays are no-ops).
    """
    amendments = [dict(a) for a in original.get("amendments") or [] if isinstance(a, Mapping)]
    if any(a.get("accession_number") == filing.accession_number for a in amendments):
        return None
    entry = amendment_entry(filing)
    amendments.append(entry)
    amendments.sort(key=lambda a: (str(a.get("date_filed")), str(a.get("accession_number"))))
    patch: dict[str, Any] = {"amendments": amendments}
    if amendments[-1] is entry:
        patch.update({key: entry[key] for key in _AMOUNT_KEYS})
        patch["last_amended_on"] = entry["date_filed"]
    return patch


# --- database side ---------------------------------------------------------------------------------


def company_ids_by_cik(session: Session, ciks: Iterable[str]) -> dict[str, int]:
    """``{10-digit cik: companies.id}`` for the CIKs that belong to a known company."""
    wanted = {cik10(c) for c in ciks if str(c).strip().isdigit()}
    if not wanted:
        return {}
    candidates = wanted | {str(int(c)) for c in wanted}
    rows = session.execute(select(Company.id, Company.cik).where(Company.cik.in_(candidates)))
    out: dict[str, int] = {}
    for company_id, cik in rows:
        if cik is not None:
            out[cik10(cik)] = company_id
    return out


def _find_original(session: Session, filing: FormDFiling) -> tuple[str, dict[str, Any]] | None:
    """The ``form_d_filed`` event this D/A amends: by file number, else previous accession."""
    keys: list[tuple[str, str]] = []
    if filing.file_number:
        keys.append(("file_number", filing.file_number))
    previous = filing.offering_data.type_of_filing.new_or_amendment.previous_accession_number
    if previous:
        keys.append(("accession_number", previous))
    for key, value in keys:
        stmt = (
            select(EventRow.event_id, EventRow.payload)
            .where(
                EventRow.source == SOURCE,
                EventRow.type == FORM_D_FILED,
                EventRow.payload.op("->>")(key) == value,
            )
            .order_by(EventRow.occurred_at, EventRow.event_id)
            .limit(1)
        )
        row = session.execute(stmt).first()
        if row is not None:
            return str(row[0]), dict(row[1])
    return None


@dataclass(frozen=True)
class FormDIngest:
    """Outcome of :func:`ingest_formd_filings`."""

    stats: WriteStats
    events: list[Event]
    amended: int
    """Originals whose payload a D/A updated in place (no event written for those)."""


def ingest_formd_filings(
    session: Session, filings: Sequence[FormDFiling], *, run_id: str, now: datetime
) -> FormDIngest:
    """Write new Form D events, then merge each D/A into its original (SPEC section 5 rule 4).

    New filings go first so a D and its D/A listed in the same daily index link up.
    A D/A whose original is in ``events`` updates that payload with
    :func:`update_event_payload` and writes nothing; one whose original is unknown is
    kept as a ``form_d_amendment`` event, which Phase 7 never scores as funding.
    """
    company_ids = company_ids_by_cik(session, [f.cik for f in filings])
    originals = [f for f in filings if not f.is_amendment]
    amendments = sorted(
        (f for f in filings if f.is_amendment), key=lambda f: (f.date_filed, f.accession_number)
    )
    events = [
        formd_event(f, run_id=run_id, now=now, company_id=company_ids.get(cik10(f.cik)))
        for f in originals
    ]
    first = write_events(session, events)
    session.flush()

    amended = 0
    orphans: list[Event] = []
    for filing in amendments:
        found = _find_original(session, filing)
        if found is None:
            orphans.append(
                formd_event(
                    filing, run_id=run_id, now=now, company_id=company_ids.get(cik10(filing.cik))
                )
            )
            continue
        event_id, payload = found
        patch = amendment_patch(payload, filing)
        if patch is not None and update_event_payload(session, event_id, patch):
            amended += 1
    second = write_events(session, orphans)
    session.flush()
    stats = WriteStats(
        inserted=first.inserted + second.inserted, skipped=first.skipped + second.skipped
    )
    return FormDIngest(stats=stats, events=events + orphans, amended=amended)


# --- quarterly DERA Form D data sets (backtest backfill, SPEC section 13.2) -----------------


_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%Y%m%d", "%m/%d/%Y")


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    msg = f"unrecognised date {value!r}"
    raise ValueError(msg)


def _read_tsv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    return [
        {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()} for row in reader
    ]


def _quarterly_tables(path: Path) -> dict[str, list[dict[str, str]]]:
    """Read exactly :data:`QUARTERLY_FILES` from a data-set ZIP or an unpacked directory."""
    tables: dict[str, list[dict[str, str]]] = {}
    if path.is_dir():
        for name in QUARTERLY_FILES:
            matches = sorted(path.rglob(name))
            if not matches:
                msg = f"{name} not found under {path}"
                raise FileNotFoundError(msg)
            tables[name] = _read_tsv(matches[0].read_text(encoding="utf-8", errors="replace"))
        return tables
    with zipfile.ZipFile(path) as archive:
        by_base = {Path(info.filename).name: info.filename for info in archive.infolist()}
        for name in QUARTERLY_FILES:
            member = by_base.get(name)
            if member is None:
                msg = f"{name} not found in {path}"
                raise FileNotFoundError(msg)
            with archive.open(member) as fh:
                tables[name] = _read_tsv(fh.read().decode("utf-8", errors="replace"))
    return tables


def _primary_issuers(rows: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    """One issuer row per accession: the flagged primary issuer, else sequence 1, else first."""
    chosen: dict[str, dict[str, str]] = {}
    primary: set[str] = set()
    for row in rows:
        accession = row.get("ACCESSIONNUMBER", "")
        if not accession or accession in primary:
            continue
        flag = row.get("IS_PRIMARYISSUER_FLAG", "").upper()
        if flag in {"YES", "Y", "TRUE"} or (not flag and row.get("ISSUER_SEQ_KEY", "") == "1"):
            chosen[accession] = row
            primary.add(accession)
        elif accession not in chosen:
            chosen[accession] = row
    return chosen


def _quarterly_filing(
    submission: Mapping[str, str], issuer: Mapping[str, str], offering: Mapping[str, str]
) -> FormDFiling:
    accession = submission["ACCESSIONNUMBER"]
    filed = _parse_date(submission.get("FILING_DATE"))
    if filed is None:
        msg = f"{accession}: FILING_DATE missing"
        raise ValueError(msg)
    previous_names = [issuer.get(f"ISSUER_PREVIOUSNAME_{n}", "") for n in (1, 2, 3)]
    year = issuer.get("YEAROFINC_VALUE_ENTERED", "")
    exemptions = [
        part.strip() for part in offering.get("FEDERALEXEMPTIONS_ITEMS_LIST", "").split(",")
    ]
    sale = _parse_date(offering.get("SALE_DATE"))
    is_amendment = (
        bool(_bool(offering.get("ISAMENDMENT")))
        or submission.get("SUBMISSIONTYPE", "").upper() == "D/A"
    )
    invested = offering.get("TOTALNUMBERALREADYINVESTED", "")
    data: dict[str, Any] = {
        "accessionNumber": accession,
        "cik": str(int(issuer["CIK"])),
        "formType": "D/A" if is_amendment else "D",
        "dateFiled": filed.isoformat(),
        "fileNumber": submission.get("FILE_NUM") or None,
        "primaryIssuer": {
            "cik": str(int(issuer["CIK"])),
            "entityName": _clean(issuer.get("ENTITYNAME")),
            "issuerAddress": {
                "city": _clean(issuer.get("CITY")),
                "stateOrCountry": _clean(issuer.get("STATEORCOUNTRY")),
                "zipCode": _clean(issuer.get("ZIPCODE")),
            },
            "jurisdictionOfInc": _clean(issuer.get("JURISDICTIONOFINC")),
            "yearOfInc": int(year) if year.isdigit() else None,
            "entityType": _clean(issuer.get("ENTITYTYPE")),
            "issuerPreviousNameList": [
                n for n in (_clean(p) for p in previous_names) if n and n.lower() not in _NONE_NAMES
            ],
        },
        "offeringData": {
            "industryGroup": {"industryGroupType": _clean(offering.get("INDUSTRYGROUPTYPE"))},
            "federalExemptionsExclusions": [e for e in exemptions if e],
            "typeOfFiling": {
                "newOrAmendment": {
                    "isAmendment": is_amendment,
                    "previousAccessionNumber": offering.get("PREVIOUSACCESSIONNUMBER") or None,
                },
                "dateOfFirstSale": sale.isoformat() if sale else None,
            },
            "offeringSalesAmounts": {
                "totalOfferingAmount": offering.get("TOTALOFFERINGAMOUNT"),
                "totalAmountSold": offering.get("TOTALAMOUNTSOLD"),
                "totalRemaining": offering.get("TOTALREMAINING"),
            },
            "investors": {
                "totalNumberAlreadyInvested": int(invested) if invested.isdigit() else None
            },
        },
    }
    return FormDFiling.model_validate(data)


def load_formd_quarterly(path: Path | str, *, run_id: str, now: datetime) -> list[Event]:
    """Turn one quarterly Form D data set into events for backtests (``payload.backfill``).

    ``path`` is the SEC ZIP or a directory holding ``FORMDSUBMISSION.tsv``,
    ``ISSUERS.tsv`` and ``OFFERING.tsv``; nothing else in it is opened. New filings
    become ``form_d_filed``; a D/A whose original (same file number, else its previous
    accession) is in the same data set is merged into that payload exactly as the
    daily path does, and any other D/A becomes ``form_d_amendment``. Events carry no
    ``company_id`` (Phase 5 links them) and the same ``edgar:{accession}`` key as the
    daily collector, so a filing seen both ways is written once. Pure: no database.
    """
    tables = _quarterly_tables(Path(path))
    issuers = _primary_issuers(tables["ISSUERS.tsv"])
    offerings = {row.get("ACCESSIONNUMBER", ""): row for row in tables["OFFERING.tsv"]}
    filings: list[FormDFiling] = []
    for submission in tables["FORMDSUBMISSION.tsv"]:
        accession = submission.get("ACCESSIONNUMBER", "")
        if submission.get("SUBMISSIONTYPE", "D").upper() not in {"D", "D/A"}:
            continue
        if accession not in issuers or accession not in offerings:
            continue
        filings.append(_quarterly_filing(submission, issuers[accession], offerings[accession]))
    filings.sort(key=lambda f: (f.date_filed, f.accession_number))

    payloads: dict[str, dict[str, Any]] = {}
    by_file: dict[str, str] = {}
    originals: list[FormDFiling] = []
    orphans: list[FormDFiling] = []
    for filing in filings:
        if not filing.is_amendment:
            payloads[filing.accession_number] = formd_payload(filing, backfill=True)
            if filing.file_number:
                by_file.setdefault(filing.file_number, filing.accession_number)
            originals.append(filing)
            continue
        previous = filing.offering_data.type_of_filing.new_or_amendment.previous_accession_number
        target = by_file.get(filing.file_number or "") or (
            previous if previous in payloads else None
        )
        if target is None:
            orphans.append(filing)
            continue
        patch = amendment_patch(payloads[target], filing)
        if patch is not None:
            payloads[target].update(patch)
    events = [
        formd_event(f, run_id=run_id, now=now, payload=payloads[f.accession_number])
        for f in originals
    ]
    events += [formd_event(f, run_id=run_id, now=now, backfill=True) for f in orphans]
    return events
