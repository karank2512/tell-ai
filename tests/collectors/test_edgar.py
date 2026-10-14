"""SEC EDGAR collector: index parser, Form D XML contract, amendments, 8-K, tickers, backfill.

All HTTP goes through ``respx``; fixtures under ``tests/fixtures/edgar`` are hand-crafted
to the documented EDGAR shapes (see ``docs/sources/edgar.md``).
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pydantic
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

import tell.db
from tell.cli import app
from tell.collectors import edgar, edgar_formd
from tell.collectors.base import discover_collectors, start_run, write_events
from tell.collectors.edgar import (
    COLLECTOR,
    FIRST_RUN_DAYS_BACK,
    FORM_8K_FILED,
    FORM_S1_FILED,
    MAX_CATCHUP_DAYS_BACK,
    MAX_SINCE_DAYS_BACK,
    IndexEntry,
    SecClient,
    _index_is_listed,
    accession_from_file_name,
    collect,
    daily_index_url,
    default_dates,
    edgar_budget,
    edgar_event_id,
    form_8k_payload,
    known_accessions,
    last_index_date,
    load_ticker_map,
    parse_daily_index,
    parse_ticker_map,
    quarter_listing_url,
    run_fixture,
    since_dates,
    submissions_url,
)
from tell.collectors.edgar_formd import (
    FORM_D_AMENDMENT,
    FORM_D_FILED,
    FormDFiling,
    FormDFilings,
    amendment_patch,
    cik10,
    filing_folder_url,
    formd_event,
    formd_payload,
    load_formd_quarterly,
    parse_amount,
    parse_formd_xml,
    primary_doc_url,
)
from tell.config import get_settings
from tell.db import SessionLocal
from tell.http import RateBudget
from tell.models import Company, EventRow, Run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "edgar"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DAY = date(2026, 10, 1)

ACC_D1 = "0009990001-26-000001"
ACC_D2 = "0009990002-26-000001"
ACC_DA = "0009990001-26-000002"
ACC_8K = "0009990020-26-000012"
ACC_8K_UNMONITORED = "0009990030-26-000007"
ACC_S1 = "0009990040-26-000003"

# The Form D officers-and-directors element, spelled in pieces as tests/test_pii_guard.py does.
RELATED_ELEMENT = "related" + "PersonsList"
PLACEHOLDER_TOKENS = ("ZZPLACEHOLDERGIVEN", "ZZPLACEHOLDERFAMILY", "ZZPLACEHOLDERSTREET")


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _formd(
    name: str, accession: str, cik: str, form: str = "D", file_number: str | None = None
) -> FormDFiling:
    return parse_formd_xml(
        _read(name),
        accession_number=accession,
        cik=cik,
        form_type=form,
        date_filed=DAY,
        file_number=file_number,
    )


def _events(session: Session, type_: str | None = None) -> list[EventRow]:
    stmt = select(EventRow).order_by(EventRow.event_id)
    if type_ is not None:
        stmt = stmt.where(EventRow.type == type_)
    return list(session.scalars(stmt))


def _count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(EventRow)).scalar_one()


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


# --- registration ------------------------------------------------------------------------------


def test_collector_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELL_COLLECTORS_ONLY", "edgar")
    specs = discover_collectors()
    assert [s.source for s in specs] == ["edgar"]
    assert specs[0] is COLLECTOR
    assert COLLECTOR.kind == "filings"
    assert COLLECTOR.response_model is FormDFilings
    assert COLLECTOR.fixture_dir == FIXTURES
    assert COLLECTOR.drift_missing_field == "totalOfferingAmount"


# --- daily index -------------------------------------------------------------------------------


def test_index_parser_keeps_only_d_da_8k_s1() -> None:
    entries = parse_daily_index(_read("form.20261001.idx").decode())
    assert [(e.form_type, e.accession_number) for e in entries] == [
        ("8-K", ACC_8K),
        ("8-K", ACC_8K_UNMONITORED),
        ("D", ACC_D1),
        ("D", ACC_D2),
        ("D/A", ACC_DA),
        ("S-1", ACC_S1),
    ]
    first = entries[2]
    assert first.company_name == "Example Robotics, Inc."
    assert first.cik == "9990001"
    assert first.date_filed == DAY
    assert first.file_name == f"edgar/data/9990001/{ACC_D1}.txt"


def test_index_parser_all_forms_handles_spaces_in_form_type() -> None:
    entries = parse_daily_index(_read("form.20261001.idx").decode(), forms=None)
    assert len(entries) == 11
    forms = [e.form_type for e in entries]
    assert "SC 13G" in forms
    assert "8-K/A" in forms
    assert "S-1/A" in forms
    sc13g = entries[forms.index("SC 13G")]
    assert sc13g.company_name == "Example Capital Partners LP"


def test_index_parser_accepts_iso_dates_and_no_header_columns() -> None:
    text = (
        "Description: x\n"
        "-----\n"
        "D  Some Issuer LLC   123   2026-10-02   edgar/data/123/0000000123-26-000001.txt\n"
        "garbage line\n"
    )
    [entry] = parse_daily_index(text)
    assert entry.form_type == "D"
    assert entry.company_name == "Some Issuer LLC"
    assert entry.date_filed == date(2026, 10, 2)


def test_index_parser_requires_dashed_separator() -> None:
    with pytest.raises(ValueError, match="dashed"):
        parse_daily_index("Form Type   Company Name\nD x 1 20261001 edgar/data/1/a.txt\n")


def test_accession_from_file_name() -> None:
    assert accession_from_file_name(f"edgar/data/9990001/{ACC_D1}.txt") == ACC_D1
    with pytest.raises(ValueError, match="accession"):
        accession_from_file_name("edgar/data/1/nothing.txt")


# --- URL construction ----------------------------------------------------------------------------


def test_url_construction() -> None:
    assert daily_index_url(DAY) == (
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261001.idx"
    )
    assert daily_index_url(date(2026, 2, 27)).endswith("/2026/QTR1/form.20260227.idx")
    assert daily_index_url(date(2026, 6, 30)).endswith("/2026/QTR2/form.20260630.idx")
    assert daily_index_url(date(2026, 7, 1)).endswith("/2026/QTR3/form.20260701.idx")
    assert filing_folder_url("0009990001", ACC_D1) == (
        "https://www.sec.gov/Archives/edgar/data/9990001/000999000126000001/"
    )
    assert primary_doc_url(9990001, ACC_D1) == (
        "https://www.sec.gov/Archives/edgar/data/9990001/000999000126000001/primary_doc.xml"
    )
    assert submissions_url("320193") == "https://data.sec.gov/submissions/CIK0000320193.json"
    entry = IndexEntry("D", "x", "9990001", DAY, f"edgar/data/9990001/{ACC_D1}.txt")
    assert entry.folder_url == filing_folder_url("9990001", ACC_D1)
    assert entry.primary_doc_url.endswith("/000999000126000001/primary_doc.xml")
    assert cik10("320193") == "0000320193"


def _span(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def test_first_run_default_is_today_and_three_days_back_in_new_york() -> None:
    assert FIRST_RUN_DAYS_BACK == 3
    # 02:00 UTC on the 5th is still the 4th in New York.
    assert default_dates(datetime(2026, 10, 5, 2, 0, tzinfo=UTC)) == _span(
        date(2026, 10, 1), date(2026, 10, 4)
    )
    assert default_dates(NOW) == _span(date(2026, 10, 2), date(2026, 10, 5))


def test_default_dates_catch_up_after_the_last_index_read() -> None:
    assert default_dates(NOW, date(2026, 9, 29)) == _span(date(2026, 9, 30), date(2026, 10, 5))
    assert default_dates(NOW, date(2026, 10, 4)) == [date(2026, 10, 5)]
    assert default_dates(NOW, date(2026, 10, 5)) == []  # today's index is already read


def test_default_dates_are_capped_fourteen_days_back() -> None:
    assert MAX_CATCHUP_DAYS_BACK == 14
    capped = default_dates(NOW, date(2026, 7, 1))
    assert capped == _span(date(2026, 9, 21), date(2026, 10, 5))
    assert len(capped) == MAX_CATCHUP_DAYS_BACK + 1
    assert default_dates(NOW, date(2026, 9, 20)) == capped
    assert default_dates(NOW, date(2026, 9, 21))[0] == date(2026, 9, 22)


def test_since_dates_reach_back_at_most_120_days() -> None:
    assert since_dates(NOW, date(2026, 10, 3)) == _span(date(2026, 10, 3), date(2026, 10, 5))
    oldest = date(2026, 6, 7)  # 2026-10-05 minus 120 days
    assert len(since_dates(NOW, oldest)) == MAX_SINCE_DAYS_BACK + 1
    with pytest.raises(ValueError, match="more than 120 days back"):
        since_dates(NOW, oldest - timedelta(days=1))
    with pytest.raises(ValueError, match="after today"):
        since_dates(NOW, date(2026, 10, 6))


# --- Form D XML contract ------------------------------------------------------------------------


def test_formd_xml_parses_exact_amounts() -> None:
    filing = _formd("formd_1.xml", ACC_D1, "9990001", file_number="021-555001")
    issuer = filing.primary_issuer
    assert issuer.entity_name == "Example Robotics, Inc."
    assert issuer.cik == "0009990001"
    assert issuer.issuer_address.city == "SAN FRANCISCO"
    assert issuer.issuer_address.state_or_country == "CA"
    assert issuer.issuer_address.zip_code == "94107"
    assert issuer.jurisdiction_of_inc == "DELAWARE"
    assert issuer.year_of_inc == 2022
    assert issuer.entity_type == "Corporation"
    assert issuer.issuer_previous_name_list == ["Example Automation Inc."]
    offering = filing.offering_data
    assert offering.industry_group.industry_group_type == "Other Technology"
    assert offering.federal_exemptions_exclusions == ["06b", "3C", "3C.1"]
    assert offering.type_of_filing.new_or_amendment.is_amendment is False
    assert offering.type_of_filing.date_of_first_sale == date(2026, 9, 15)
    amounts = offering.offering_sales_amounts
    assert amounts.total_offering_amount == 18_000_000
    assert amounts.total_amount_sold == 12_500_000
    assert amounts.total_remaining == 5_500_000
    assert amounts.total_offering_amount_indefinite is False
    assert offering.investors.total_number_already_invested == 14
    assert filing.file_number == "021-555001"
    assert filing.is_amendment is False


def test_formd_xml_indefinite_becomes_none_plus_flag() -> None:
    filing = _formd("formd_2.xml", ACC_D2, "9990002")
    amounts = filing.offering_data.offering_sales_amounts
    assert amounts.total_offering_amount is None
    assert amounts.total_offering_amount_indefinite is True
    assert amounts.total_remaining is None
    assert amounts.total_remaining_indefinite is True
    assert amounts.total_amount_sold == 2_750_000
    assert filing.primary_issuer.year_of_inc is None
    assert filing.primary_issuer.issuer_previous_name_list == []
    event = formd_event(filing, run_id="r", now=NOW)
    assert event.magnitude == 0
    assert event.payload["total_offering_amount"] is None
    assert event.payload["total_offering_amount_indefinite"] is True


def test_formd_amendment_xml() -> None:
    filing = _formd("formd_amendment.xml", ACC_DA, "9990001", form="D/A")
    assert filing.is_amendment is True
    new_or_amendment = filing.offering_data.type_of_filing.new_or_amendment
    assert new_or_amendment.previous_accession_number == ACC_D1
    assert filing.offering_data.offering_sales_amounts.total_amount_sold == 16_000_000


def test_drift_xml_fails_naming_the_field() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        _formd("drift.xml", ACC_D1, "9990001")
    assert "totalOfferingAmount" in str(excinfo.value)


def test_malformed_xml_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="not well-formed"):
        parse_formd_xml(
            b"<edgarSubmission><primaryIssuer>",
            accession_number=ACC_D1,
            cik="1",
            form_type="D",
            date_filed=DAY,
        )


def test_json_renderings_match_the_xml_parser() -> None:
    normal = json.loads(_read("normal.json"))
    rendered = [
        _formd("formd_1.xml", ACC_D1, "9990001", file_number="021-555001").to_json_dict(),
        _formd("formd_2.xml", ACC_D2, "9990002", file_number="021-555002").to_json_dict(),
    ]
    assert rendered == normal
    amendment = json.loads(_read("amendment.json"))
    assert amendment == [
        _formd(
            "formd_amendment.xml", ACC_DA, "9990001", form="D/A", file_number="021-555001"
        ).to_json_dict()
    ]
    assert FormDFilings.model_validate_json(_read("empty.json")).root == []


def test_parse_amount() -> None:
    assert parse_amount("1,500,000.00") == 1_500_000
    assert parse_amount(" $250000 ") == 250_000
    assert parse_amount(42) == 42
    assert parse_amount("") is None
    assert parse_amount(None) is None
    assert parse_amount(True) is None
    with pytest.raises(ValueError, match="USD amount"):
        parse_amount("lots")


# --- PII guard -------------------------------------------------------------------------------------


def _xml_with_related_persons() -> bytes:
    """formd_1.xml with an officers-and-directors element of placeholder tokens injected."""
    given, family, street = PLACEHOLDER_TOKENS
    block = (
        f"<{RELATED_ELEMENT}><relatedPersonInfo><relatedPersonName>"
        f"<firstName>{given}</firstName><lastName>{family}</lastName></relatedPersonName>"
        f"<relatedPersonAddress><street1>{street}</street1><city>{street}</city>"
        f"</relatedPersonAddress><relatedPersonRelationshipList>"
        f"<relationship>Executive Officer</relationship></relatedPersonRelationshipList>"
        f"</relatedPersonInfo></{RELATED_ELEMENT}>"
    )
    xml = _read("formd_1.xml").decode()
    return xml.replace("<offeringData>", block + "<offeringData>", 1).encode()


@pytest.mark.db
def test_related_persons_never_reach_the_payload(db_session: Session) -> None:
    raw = _xml_with_related_persons()
    assert RELATED_ELEMENT.encode() in raw
    filing = parse_formd_xml(
        raw, accession_number=ACC_D1, cik="9990001", form_type="D", date_filed=DAY
    )
    dumped = filing.to_json_dict()
    assert RELATED_ELEMENT not in dumped
    assert all("related" not in s.lower() for s in _strings(dumped))

    event = formd_event(filing, run_id="r", now=NOW)
    write_events(db_session, [event])
    db_session.flush()
    [row] = _events(db_session)
    stored = json.dumps(row.payload)
    assert RELATED_ELEMENT not in row.payload
    assert "related" not in stored.lower()
    for token in PLACEHOLDER_TOKENS:
        assert token not in stored
        assert token not in json.dumps(dumped)


def test_json_rendering_with_related_persons_key_is_rejected() -> None:
    document = json.loads(_read("normal.json"))
    document[0][RELATED_ELEMENT] = [{"relationship": "Director"}]
    with pytest.raises(pydantic.ValidationError, match="Extra inputs"):
        FormDFilings.model_validate(document)


def test_fixtures_hold_no_related_persons_element() -> None:
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            assert RELATED_ELEMENT.lower() not in path.read_text().lower(), path.name


# --- events, run_fixture and amendments ------------------------------------------------------------


def test_formd_payload_fields() -> None:
    filing = _formd("formd_1.xml", ACC_D1, "9990001", file_number="021-555001")
    payload = formd_payload(filing)
    assert payload["is_amendment"] is False
    assert payload["file_number"] == "021-555001"
    assert payload["issuer_name"] == "Example Robotics, Inc."
    assert payload["issuer_city"] == "SAN FRANCISCO"
    assert payload["issuer_state"] == "CA"
    assert payload["previous_names"] == ["Example Automation Inc."]
    assert payload["cik"] == "0009990001"
    assert payload["total_offering_amount"] == 18_000_000
    assert payload["amendments"] == []
    assert "backfill" not in payload
    event = formd_event(filing, run_id="r", now=NOW)
    assert event.type == FORM_D_FILED
    assert event.external_id == ACC_D1
    assert event.version == ACC_D1
    assert event.magnitude == 18_000_000
    assert event.occurred_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert event.url == "https://www.sec.gov/Archives/edgar/data/9990001/000999000126000001/"


@pytest.mark.db
def test_run_fixture_writes_form_d_events_linked_by_cik(db_session: Session) -> None:
    company = Company(domain="example-robotics.test", name="Example Robotics", cik="0009990001")
    db_session.add(company)
    db_session.flush()
    run_id = start_run(db_session, "edgar", now=NOW)
    result = run_fixture(db_session, FIXTURES / "normal.json", run_id=run_id, now=NOW)
    assert (result.fetched, result.inserted, result.skipped) == (2, 2, 0)
    rows = {row.payload["accession_number"]: row for row in _events(db_session, FORM_D_FILED)}
    assert set(rows) == {ACC_D1, ACC_D2}
    assert rows[ACC_D1].company_id == company.id
    assert rows[ACC_D2].company_id is None
    assert float(rows[ACC_D1].magnitude) == 18_000_000
    assert float(rows[ACC_D2].magnitude) == 0
    assert rows[ACC_D1].payload["total_amount_sold"] == 12_500_000


@pytest.mark.db
def test_run_fixture_twice_inserts_nothing(db_session: Session) -> None:
    first = run_fixture(db_session, FIXTURES / "normal.json", run_id="edgar-a", now=NOW)
    second = run_fixture(db_session, FIXTURES / "normal.json", run_id="edgar-b", now=NOW)
    assert first.inserted == 2
    assert second.inserted == 0
    assert second.skipped == 2
    assert _count(db_session) == 2


@pytest.mark.db
def test_amendment_updates_original_and_inserts_nothing(db_session: Session) -> None:
    run_fixture(db_session, FIXTURES / "normal.json", run_id="edgar-a", now=NOW)
    before = _count(db_session)

    result = run_fixture(db_session, FIXTURES / "amendment.json", run_id="edgar-b", now=NOW)
    assert result.inserted == 0
    assert result.events == []
    assert _count(db_session) == before
    assert len(_events(db_session, FORM_D_FILED)) == 2
    assert _events(db_session, FORM_D_AMENDMENT) == []

    db_session.expire_all()
    original = next(
        r for r in _events(db_session, FORM_D_FILED) if r.payload["accession_number"] == ACC_D1
    )
    assert original.payload["total_amount_sold"] == 16_000_000
    assert original.payload["total_remaining"] == 2_000_000
    assert original.payload["total_number_already_invested"] == 19
    assert original.payload["is_amendment"] is False
    assert original.payload["last_amended_on"] == "2026-10-01"
    [entry] = original.payload["amendments"]
    assert entry["accession_number"] == ACC_DA
    assert entry["total_amount_sold"] == 16_000_000

    # Replaying the amendment changes nothing.
    again = run_fixture(db_session, FIXTURES / "amendment.json", run_id="edgar-c", now=NOW)
    assert again.inserted == 0
    db_session.expire_all()
    original = next(
        r for r in _events(db_session, FORM_D_FILED) if r.payload["accession_number"] == ACC_D1
    )
    assert len(original.payload["amendments"]) == 1
    assert _count(db_session) == before


@pytest.mark.db
def test_amendment_without_original_is_kept_as_form_d_amendment(db_session: Session) -> None:
    result = run_fixture(db_session, FIXTURES / "amendment.json", run_id="edgar-a", now=NOW)
    assert result.inserted == 1
    [row] = _events(db_session)
    assert row.type == FORM_D_AMENDMENT
    assert row.payload["is_amendment"] is True
    assert _events(db_session, FORM_D_FILED) == []
    again = run_fixture(db_session, FIXTURES / "amendment.json", run_id="edgar-b", now=NOW)
    assert (again.inserted, again.skipped) == (0, 1)


@pytest.mark.db
def test_amendment_falls_back_to_previous_accession(db_session: Session, tmp_path: Path) -> None:
    run_fixture(db_session, FIXTURES / "normal.json", run_id="edgar-a", now=NOW)
    document = json.loads(_read("amendment.json"))
    document[0]["fileNumber"] = None
    case = tmp_path / "amendment_no_file_number.json"
    case.write_text(json.dumps(document))
    result = run_fixture(db_session, case, run_id="edgar-b", now=NOW)
    assert result.inserted == 0
    db_session.expire_all()
    original = next(
        r for r in _events(db_session, FORM_D_FILED) if r.payload["accession_number"] == ACC_D1
    )
    assert original.payload["total_amount_sold"] == 16_000_000


def test_older_amendment_is_listed_but_does_not_overwrite_amounts() -> None:
    newer = _formd("formd_amendment.xml", ACC_DA, "9990001", form="D/A")
    original = formd_payload(_formd("formd_1.xml", ACC_D1, "9990001"))
    first = amendment_patch(original, newer)
    assert first is not None
    original.update(first)
    older_doc = newer.to_json_dict()
    older_doc["accessionNumber"] = "0009990001-26-000000"
    older_doc["dateFiled"] = "2026-09-30"
    older_doc["offeringData"]["offeringSalesAmounts"]["totalAmountSold"] = 1
    older = FormDFiling.model_validate(older_doc)
    patch = amendment_patch(original, older)
    assert patch is not None
    assert [a["accession_number"] for a in patch["amendments"]] == [
        "0009990001-26-000000",
        ACC_DA,
    ]
    assert "total_amount_sold" not in patch
    assert amendment_patch(original, newer) is None


# Form D file numbers are reused across an issuer's later offerings, so a D/A must land on
# its own round: previousAccessionNumber first, else the latest original filed on or before it.
FILE_NO = "021-555001"
ACC_2024 = "0009990001-24-000001"
ACC_2026 = "0009990001-26-000001"
ACC_LATE = "0009990001-26-000009"


def _variant(
    base: FormDFiling, accession: str, filed: str, *, previous: str | None = None
) -> FormDFiling:
    document = base.to_json_dict()
    document["accessionNumber"] = accession
    document["dateFiled"] = filed
    document["fileNumber"] = FILE_NO
    if base.is_amendment:
        new_or_amendment = document["offeringData"]["typeOfFiling"]["newOrAmendment"]
        new_or_amendment["previousAccessionNumber"] = previous
    return FormDFiling.model_validate(document)


def _two_rounds(db_session: Session, *extra: FormDFiling) -> None:
    original = _formd("formd_1.xml", ACC_D1, "9990001")
    filings = [
        _variant(original, ACC_2024, "2024-03-04"),
        _variant(original, ACC_2026, "2026-09-01"),
        *extra,
    ]
    edgar_formd.ingest_formd_filings(db_session, filings, run_id="edgar-a", now=NOW)


def _originals(db_session: Session) -> dict[str, dict[str, Any]]:
    db_session.expire_all()
    return {
        row.payload["accession_number"]: row.payload for row in _events(db_session, FORM_D_FILED)
    }


def _amend(previous: str | None, filed: str = "2026-10-01") -> FormDFiling:
    amendment = _formd("formd_amendment.xml", ACC_DA, "9990001", form="D/A")
    return _variant(amendment, ACC_DA, filed, previous=previous)


@pytest.mark.db
def test_amendment_follows_previous_accession_over_older_file_number_match(
    db_session: Session,
) -> None:
    _two_rounds(db_session)
    result = edgar_formd.ingest_formd_filings(
        db_session, [_amend(ACC_2026)], run_id="edgar-b", now=NOW
    )
    assert (result.amended, result.events) == (1, [])
    originals = _originals(db_session)
    assert originals[ACC_2024]["amendments"] == []
    assert originals[ACC_2024]["total_amount_sold"] == 12_500_000
    assert "last_amended_on" not in originals[ACC_2024]
    assert [a["accession_number"] for a in originals[ACC_2026]["amendments"]] == [ACC_DA]
    assert originals[ACC_2026]["total_amount_sold"] == 16_000_000
    assert _events(db_session, FORM_D_AMENDMENT) == []


@pytest.mark.db
def test_amendment_with_unknown_previous_takes_latest_original_on_or_before_it(
    db_session: Session,
) -> None:
    later = _variant(_formd("formd_1.xml", ACC_D1, "9990001"), ACC_LATE, "2026-11-02")
    _two_rounds(db_session, later)
    result = edgar_formd.ingest_formd_filings(
        db_session, [_amend("0009990001-25-999999")], run_id="edgar-b", now=NOW
    )
    assert result.amended == 1
    originals = _originals(db_session)
    assert originals[ACC_2024]["amendments"] == []
    assert originals[ACC_LATE]["amendments"] == []
    assert [a["accession_number"] for a in originals[ACC_2026]["amendments"]] == [ACC_DA]
    # Same with no previous accession at all.
    other = _amend(None).to_json_dict()
    other["accessionNumber"] = "0009990001-26-000003"
    edgar_formd.ingest_formd_filings(
        db_session, [FormDFiling.model_validate(other)], run_id="edgar-c", now=NOW
    )
    originals = _originals(db_session)
    assert len(originals[ACC_2026]["amendments"]) == 2
    assert originals[ACC_2024]["amendments"] == []


@pytest.mark.db
def test_amendment_older_than_every_original_is_kept_without_funding(
    db_session: Session,
) -> None:
    _two_rounds(db_session)
    result = edgar_formd.ingest_formd_filings(
        db_session, [_amend("0009990001-23-999999", filed="2023-05-01")], run_id="edgar-b", now=NOW
    )
    assert result.amended == 0
    [row] = _events(db_session, FORM_D_AMENDMENT)
    assert row.payload["accession_number"] == ACC_DA
    assert row.payload["is_amendment"] is True
    assert {a: p["amendments"] for a, p in _originals(db_session).items()} == {
        ACC_2024: [],
        ACC_2026: [],
    }


# --- 8-K -------------------------------------------------------------------------------------------


def test_8k_payload_flags_items() -> None:
    entry = IndexEntry(
        "8-K", "Monitored Software Inc", "9990020", DAY, f"edgar/data/9990020/{ACC_8K}.txt"
    )
    payload = form_8k_payload(entry, ["5.02", "9.01"], "001-49999")
    assert payload["items"] == ["5.02", "9.01"]
    assert payload["item_5_02"] is True
    assert payload["item_1_01"] is False
    assert payload["item_2_01"] is False
    assert payload["file_number"] == "001-49999"
    assert payload["cik"] == "0009990020"
    other = form_8k_payload(entry, ["1.01", "2.01"], None)
    assert (other["item_1_01"], other["item_2_01"], other["item_5_02"]) == (True, True, False)


def test_submissions_lookup() -> None:
    subs = edgar.Submissions.model_validate_json(_read("submissions_CIK.json"))
    assert subs.lookup(ACC_8K) == ("001-49999", ["5.02", "9.01"])
    assert subs.lookup("0009990020-26-000004") == ("001-49999", ["1.01", "2.01", "9.01"])
    assert subs.lookup("0000000000-00-000000") == (None, [])


# --- collect() end to end with respx ---------------------------------------------------------------


class FakeClock:
    """Monotonic clock that only moves when the rate budget sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _mock_sec(router: respx.MockRouter, *, index_text: str | None = None) -> dict[str, respx.Route]:
    archives = "https://www.sec.gov/Archives/edgar/data"
    return {
        "listing": router.get(quarter_listing_url(DAY)).respond(200, json=_listing(DAY)),
        "index": router.get(daily_index_url(DAY)).respond(
            200, text=index_text if index_text is not None else _read("form.20261001.idx").decode()
        ),
        "index_404": router.get(daily_index_url(date(2026, 10, 2))).respond(404),
        "d1": router.get(f"{archives}/9990001/000999000126000001/primary_doc.xml").respond(
            200, content=_read("formd_1.xml")
        ),
        "d2": router.get(f"{archives}/9990002/000999000226000001/primary_doc.xml").respond(
            200, content=_read("formd_2.xml")
        ),
        "da": router.get(f"{archives}/9990001/000999000126000002/primary_doc.xml").respond(
            200, content=_read("formd_amendment.xml")
        ),
        "subs_d1": router.get(submissions_url("9990001")).respond(
            200, content=_read("submissions_formd_issuer.json")
        ),
        "subs_d2": router.get(submissions_url("9990002")).respond(404),
        "subs_8k": router.get(submissions_url("9990020")).respond(
            200, content=_read("submissions_CIK.json")
        ),
        "subs_unmonitored": router.get(submissions_url("9990030")).respond(500),
    }


@pytest.mark.db
def test_collect_end_to_end(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "tell-test/0.1 (ops at example.invalid)")
    get_settings.cache_clear()
    monitored = Company(domain="monitored.test", name="Monitored Software", cik="0009990020")
    db_session.add(monitored)
    db_session.flush()
    clock = FakeClock()
    budget = RateBudget(5, clock=clock, sleep=clock.sleep)

    with respx.mock(assert_all_called=False) as router:
        routes = _mock_sec(router)
        result = collect(
            db_session,
            run_id="edgar-run",
            now=NOW,
            dates=[DAY, date(2026, 10, 2)],
            budget=budget,
            sleep=clock.sleep,
        )
        requests = [call.request for call in router.calls]

    assert result.fetched == 6
    assert result.errors == 0
    assert result.amended == 1
    assert result.inserted == 4
    assert result.notes["dates"] == ["2026-10-01", "2026-10-02"]
    assert result.notes["indexes"] == ["2026-10-01"]  # 10-02 is not in the quarter listing
    assert result.notes["last_index_date"] == "2026-10-01"
    assert not routes["index_404"].called
    by_type = {row.type: row for row in _events(db_session) if row.type != FORM_D_FILED}
    assert set(by_type) == {FORM_8K_FILED, FORM_S1_FILED}
    eight_k = by_type[FORM_8K_FILED]
    assert eight_k.company_id == monitored.id
    assert eight_k.payload["accession_number"] == ACC_8K
    assert eight_k.payload["item_5_02"] is True
    assert eight_k.payload["item_1_01"] is False
    assert eight_k.payload["items"] == ["5.02", "9.01"]
    assert eight_k.url == "https://www.sec.gov/Archives/edgar/data/9990020/000999002026000012/"
    assert by_type[FORM_S1_FILED].payload["accession_number"] == ACC_S1
    assert by_type[FORM_S1_FILED].company_id is None

    db_session.expire_all()
    formd = {r.payload["accession_number"]: r for r in _events(db_session, FORM_D_FILED)}
    assert set(formd) == {ACC_D1, ACC_D2}
    assert formd[ACC_D1].payload["file_number"] == "021-555001"
    assert formd[ACC_D1].payload["total_amount_sold"] == 16_000_000
    assert formd[ACC_D2].payload["file_number"] is None
    assert _events(db_session, FORM_D_AMENDMENT) == []

    # Only monitored 8-K filers get a submissions lookup.
    assert not routes["subs_unmonitored"].called
    assert routes["subs_d1"].call_count == 1  # cached across the D and the D/A

    # Every SEC request: declared User-Agent, gzip, one rate-budget token. One listing,
    # one index, three primary documents, three submissions documents.
    assert len(requests) == 8
    for request in requests:
        assert request.headers["User-Agent"] == "tell-test/0.1 (ops at example.invalid)"
        assert "gzip" in request.headers["Accept-Encoding"]
    assert len(clock.sleeps) == len(requests) - 1
    assert all(abs(s - 0.2) < 1e-9 for s in clock.sleeps)

    # A second identical run writes nothing new and fetches no known Form D again: the
    # D, 8-K and S-1 are known by event_id; only the D/A (it amends) is fetched.
    with respx.mock(assert_all_called=False) as router:
        rerun = _mock_sec(router)
        again = collect(
            db_session, run_id="edgar-run-2", now=NOW, dates=[DAY], budget=budget, sleep=clock.sleep
        )
    assert again.inserted == 0
    assert again.amended == 0
    assert again.skipped == 4
    assert again.notes["known"] == 4
    assert (rerun["d1"].call_count, rerun["d2"].call_count, rerun["da"].call_count) == (0, 0, 1)
    assert not rerun["subs_8k"].called


@pytest.mark.db
def test_collect_counts_per_filing_errors(db_session: Session) -> None:
    index = _read("form.20261001.idx").decode()
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_sec(router, index_text=index)
        routes["d2"].respond(404)
        routes["d1"].respond(200, content=b"<edgarSubmission><broken>")
        result = collect(
            db_session, run_id="edgar-run", now=NOW, dates=[DAY], budget=RateBudget(1000, burst=100)
        )
    assert result.errors == 2
    # The D/A lost its original (unparseable) and is kept as form_d_amendment.
    assert [r.type for r in _events(db_session, FORM_D_AMENDMENT)] == [FORM_D_AMENDMENT]
    assert _events(db_session, FORM_D_FILED) == []


@pytest.mark.db
def test_collect_first_run_reads_the_last_three_days(db_session: Session) -> None:
    """No earlier run: today and the 3 days before it, through the listing filter."""
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(DAY)).respond(
            200, json=_listing(date(2026, 10, 1), date(2026, 10, 2))
        )
        router.get(daily_index_url(date(2026, 10, 2))).respond(404)  # listed, then 404
        result = collect(db_session, run_id="edgar-run", now=NOW, budget=RateBudget(1000, burst=10))
    assert (result.fetched, result.inserted, result.errors) == (0, 0, 0)
    assert result.notes["dates"] == ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
    assert result.notes["indexes"] == []
    assert result.notes["last_index_date"] is None
    assert result.notes["previous_index_date"] is None


def _listing(*days: date) -> dict[str, object]:
    return {"directory": {"item": [{"name": f"form.{d:%Y%m%d}.idx"} for d in days]}}


def test_quarter_listing_url() -> None:
    assert quarter_listing_url(date(2026, 10, 12)) == (
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/index.json"
    )
    assert quarter_listing_url(date(2026, 3, 31)).endswith("/2026/QTR1/index.json")


def test_quarter_listing_is_fetched_once_and_answers_per_day() -> None:
    """sec.gov returns 403 for a file that does not exist (2026-10-12 was a federal holiday);
    the quarter listing tells that apart from a block, and is fetched once per quarter."""
    holiday = date(2026, 10, 12)
    with respx.mock(assert_all_called=True) as router:
        listing = router.get(quarter_listing_url(holiday)).respond(
            200, json=_listing(date(2026, 10, 9), date(2026, 10, 13))
        )
        cache: dict[str, set[str]] = {}
        with SecClient(budget=RateBudget(1000, burst=10)) as sec:
            assert _index_is_listed(sec, holiday, cache) is False
            assert _index_is_listed(sec, date(2026, 10, 13), cache) is True
    assert listing.call_count == 1


def test_missing_quarter_listing_lists_nothing() -> None:
    day = date(2027, 1, 4)
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(day)).respond(404)
        with SecClient(budget=RateBudget(1000, burst=10)) as sec:
            assert _index_is_listed(sec, day, {}) is False


@pytest.mark.db
def test_collect_skips_a_holiday_index(db_session: Session) -> None:
    """An unlisted day is never requested (sec.gov would answer 403)."""
    holiday = date(2026, 10, 12)
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(holiday)).respond(200, json=_listing(date(2026, 10, 13)))
        result = collect(
            db_session,
            run_id="edgar-run",
            now=NOW,
            dates=[holiday],
            budget=RateBudget(1000, burst=10),
        )
    assert (result.fetched, result.inserted, result.errors) == (0, 0, 0)


@pytest.mark.db
def test_forbidden_index_fails_the_run(db_session: Session) -> None:
    """A 403 on a file the SEC lists as published is a real block: the run fails loudly."""
    with respx.mock(assert_all_called=False) as router:
        router.get(daily_index_url(DAY)).respond(403)
        router.get(quarter_listing_url(DAY)).respond(200, json=_listing(DAY))
        with pytest.raises(httpx.HTTPStatusError):
            collect(
                db_session,
                run_id="edgar-run",
                now=NOW,
                dates=[DAY],
                budget=RateBudget(1000, burst=10),
            )


# --- catch-up: dates from runs.notes, listing filter, known accessions, transactions ------------

EMPTY_INDEX = "Form Type   Company Name   CIK   Date Filed   File Name\n---------\n"


def _run(
    run_id: str, notes: dict[str, Any], *, status: str = "ok", source: str = "edgar", days: int = 0
) -> Run:
    return Run(
        run_id=run_id,
        source=source,
        started_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=days),
        status=status,
        notes=notes,
    )


@pytest.mark.db
def test_last_index_date_reads_successful_edgar_runs(db_session: Session) -> None:
    assert last_index_date(db_session) is None
    db_session.add_all(
        [
            _run("legacy", {"dates": ["2026-09-27", "2026-09-28"], "amended": 0}, days=1),
            _run("ok", {"last_index_date": "2026-09-30"}, days=2),
            _run("failed", {"last_index_date": "2026-10-03"}, status="failed", days=3),
            _run("running", {"last_index_date": "2026-10-03"}, status="running", days=4),
            _run("other", {"last_index_date": "2026-10-04"}, source="news", days=5),
            _run("nothing-yet", {"last_index_date": None}, days=6),
        ]
    )
    db_session.flush()
    assert last_index_date(db_session) == date(2026, 9, 30)


@pytest.mark.db
def test_last_index_date_falls_back_to_legacy_dates(db_session: Session) -> None:
    """Runs from before ``last_index_date`` was recorded read yesterday and today."""
    db_session.add(_run("legacy", {"dates": ["2026-10-02", "2026-10-03"], "amended": 0}))
    db_session.add(_run("no-notes", {"error": "x"}, days=1))
    db_session.flush()
    assert last_index_date(db_session) == date(2026, 10, 2)


def test_quarter_listing_403_lists_nothing_until_the_quarter_exists() -> None:
    """Before a quarter's first index the directory is missing (403); its parent tells."""
    day = date(2027, 1, 4)
    root = {"directory": {"item": [{"name": "2026", "type": "dir"}]}}
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(day)).respond(403)
        router.get(f"{edgar.DAILY_INDEX}/2027/index.json").respond(403)
        router.get(f"{edgar.DAILY_INDEX}/index.json").respond(200, json=root)
        with SecClient(budget=RateBudget(1000, burst=10)) as sec:
            assert _index_is_listed(sec, day, {}) is False


def test_quarter_listing_403_on_an_existing_quarter_is_a_block() -> None:
    day = date(2026, 10, 13)
    year = {"directory": {"item": [{"name": "QTR3/", "type": "dir"}, {"name": "QTR4/"}]}}
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(day)).respond(403)
        router.get(f"{edgar.DAILY_INDEX}/2026/index.json").respond(200, json=year)
        with (
            SecClient(budget=RateBudget(1000, burst=10)) as sec,
            pytest.raises(httpx.HTTPStatusError),
        ):
            _index_is_listed(sec, day, {})
    with respx.mock() as router:  # everything forbidden, root included: blocked
        router.get(host="www.sec.gov").respond(403)
        with (
            SecClient(budget=RateBudget(1000, burst=10)) as sec,
            pytest.raises(httpx.HTTPStatusError),
        ):
            _index_is_listed(sec, day, {})


@pytest.mark.db
def test_collect_reads_only_listed_days(db_session: Session) -> None:
    """Weekend, holiday and not-yet-published days are never requested."""
    days = _span(date(2026, 10, 9), date(2026, 10, 14))
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        listing = router.get(quarter_listing_url(DAY)).respond(
            200, json=_listing(date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 13))
        )
        friday = router.get(daily_index_url(date(2026, 10, 9))).respond(200, text=EMPTY_INDEX)
        tuesday = router.get(daily_index_url(date(2026, 10, 13))).respond(200, text=EMPTY_INDEX)
        result = collect(
            db_session, run_id="edgar-run", now=NOW, dates=days, budget=RateBudget(1000, burst=10)
        )
        assert len(router.calls) == 3
    assert (listing.call_count, friday.call_count, tuesday.call_count) == (1, 1, 1)
    assert result.notes["indexes"] == ["2026-10-09", "2026-10-13"]
    assert result.notes["last_index_date"] == "2026-10-13"


@pytest.mark.db
def test_collect_counts_an_accession_once_across_rows_and_days(db_session: Session) -> None:
    """Co-issuers repeat one accession under several CIKs; a later day may repeat it too."""
    row = f"S-1   Example Co   9990040   20261001   edgar/data/9990040/{ACC_S1}.txt\n"
    twin = f"S-1   Example Co Twin   9990041   20261001   edgar/data/9990041/{ACC_S1}.txt\n"
    second = date(2026, 10, 2)
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(DAY)).respond(200, json=_listing(DAY, second))
        router.get(daily_index_url(DAY)).respond(200, text=EMPTY_INDEX + row + twin)
        router.get(daily_index_url(second)).respond(200, text=EMPTY_INDEX + row)
        result = collect(
            db_session,
            run_id="edgar-run",
            now=NOW,
            dates=[second, DAY],
            budget=RateBudget(1000, burst=10),
        )
    assert (result.fetched, result.inserted, result.skipped) == (1, 1, 0)
    [event] = _events(db_session, FORM_S1_FILED)
    assert event.payload["cik"] == "0009990040"


@pytest.mark.db
def test_known_accessions(db_session: Session) -> None:
    run_fixture(db_session, FIXTURES / "normal.json", run_id="edgar-a", now=NOW)
    filing = _formd("formd_1.xml", ACC_D1, "9990001")
    assert edgar_event_id(ACC_D1) == formd_event(filing, run_id="r", now=NOW).event_id
    assert known_accessions(db_session, [ACC_D1, ACC_8K]) == {ACC_D1}
    assert known_accessions(db_session, []) == set()


@pytest.mark.db
def test_collect_catches_up_without_holding_a_transaction_across_http(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dates come from runs.notes; each day commits; no SEC request runs inside a transaction."""
    with SessionLocal(bind=engine) as setup:
        setup.add(_run("earlier", {"last_index_date": "2026-09-30"}))
        setup.add(Company(domain="monitored.test", name="Monitored Software", cik="0009990020"))
        setup.commit()

    session = SessionLocal(bind=engine)
    open_during_http: list[bool] = []
    real_get = SecClient.get

    def spy(self: SecClient, url: str) -> httpx.Response | None:
        open_during_http.append(session.in_transaction())
        return real_get(self, url)

    monkeypatch.setattr(SecClient, "get", spy)
    try:
        with respx.mock(assert_all_called=False) as router:
            routes = _mock_sec(router)
            result = collect(
                session, run_id="edgar-run", now=NOW, budget=RateBudget(1000, burst=100)
            )
        assert not session.in_transaction()  # the day's writes are already committed
    finally:
        session.close()

    assert result.notes["previous_index_date"] == "2026-09-30"
    assert result.notes["dates"] == [d.isoformat() for d in _span(DAY, date(2026, 10, 5))]
    assert result.notes["indexes"] == ["2026-10-01"]
    assert result.notes["last_index_date"] == "2026-10-01"
    assert routes["listing"].call_count == 1
    assert len(open_during_http) == 8
    assert not any(open_during_http)
    with SessionLocal(bind=engine) as check:
        assert _count(check) == 4


@pytest.mark.db
def test_collect_with_since_records_it(db_session: Session) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(quarter_listing_url(DAY)).respond(200, json=_listing())
        result = collect(
            db_session,
            run_id="edgar-run",
            now=NOW,
            since=date(2026, 10, 3),
            budget=RateBudget(1000, burst=10),
        )
    assert result.notes["dates"] == ["2026-10-03", "2026-10-04", "2026-10-05"]
    assert result.notes["since"] == "2026-10-03"
    with pytest.raises(ValueError, match="120 days"):
        collect(db_session, run_id="edgar-run", now=NOW, since=date(2026, 1, 1))


@pytest.mark.db
def test_collect_with_nothing_to_catch_up_makes_no_request(db_session: Session) -> None:
    db_session.add(_run("earlier", {"last_index_date": "2026-10-05"}))
    db_session.flush()
    with respx.mock(assert_all_mocked=True) as router:
        result = collect(db_session, run_id="edgar-run", now=NOW)
    assert len(router.calls) == 0
    assert result.notes["dates"] == []
    assert result.notes["last_index_date"] == "2026-10-05"


# --- tell ingest --source edgar [--since] -------------------------------------------------------

CLI_NOW = "2026-10-14 16:00:00"  # noon in New York; 2026-10-12 was Columbus Day


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Every ``session_scope()`` the CLI opens lands on the test database."""
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    monkeypatch.setenv("TELL_RATE_EDGAR_RPS", "1000")
    get_settings.cache_clear()
    return engine


def _ingest(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(app, ["ingest", "--source", *args])
    assert result.exit_code == code, (result.output, result.exception)
    return result


def _edgar_runs(engine: Engine) -> list[Run]:
    with SessionLocal(bind=engine) as session:
        stmt = select(Run).where(Run.source == "edgar").order_by(Run.started_at, Run.run_id)
        return list(session.scalars(stmt))


def _mock_october(router: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "listing": router.get(quarter_listing_url(DAY)).respond(
            200, json=_listing(date(2026, 10, 9), date(2026, 10, 13))
        ),
        "fri": router.get(daily_index_url(date(2026, 10, 9))).respond(200, text=EMPTY_INDEX),
        "tue": router.get(daily_index_url(date(2026, 10, 13))).respond(200, text=EMPTY_INDEX),
    }


@pytest.mark.db
def test_cli_ingest_edgar_catches_up_from_the_last_run(bound: Engine) -> None:
    with SessionLocal(bind=bound) as setup:
        setup.add(_run("earlier", {"last_index_date": "2026-10-09"}))
        setup.commit()
    with freeze_time(CLI_NOW, tick=True), respx.mock(assert_all_called=False) as router:
        routes = _mock_october(router)
        _ingest("edgar")
        _ingest("edgar")  # caught up: only today, not published yet
    assert not routes["fri"].called
    assert routes["tue"].call_count == 1
    first, second = (r for r in _edgar_runs(bound) if r.run_id != "earlier")
    assert first.notes is not None
    assert second.notes is not None
    assert first.notes["dates"] == [
        "2026-10-10",
        "2026-10-11",
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
    ]
    assert (first.notes["indexes"], first.notes["last_index_date"]) == (
        ["2026-10-13"],
        "2026-10-13",
    )
    assert second.notes["dates"] == ["2026-10-14"]
    assert (second.notes["indexes"], second.notes["last_index_date"]) == ([], "2026-10-13")
    assert (first.status, second.status) == ("ok", "ok")


@pytest.mark.db
def test_cli_ingest_edgar_since(bound: Engine) -> None:
    with freeze_time(CLI_NOW, tick=True), respx.mock(assert_all_mocked=True) as router:
        routes = _mock_october(router)
        _ingest("edgar", "--since", "2026-10-08")
    assert (routes["fri"].call_count, routes["tue"].call_count) == (1, 1)
    (run,) = _edgar_runs(bound)
    assert run.notes is not None
    assert run.notes["since"] == "2026-10-08"
    assert run.notes["dates"][0] == "2026-10-08"
    assert run.notes["last_index_date"] == "2026-10-13"


@pytest.mark.db
@pytest.mark.parametrize(
    "args",
    [
        ["edgar", "--since", "2026-06-15"],  # 121 days back
        ["edgar", "--since", "2026-10-15"],  # tomorrow
        ["edgar", "--since", "14/10/2026"],
        ["news", "--since", "2026-10-13"],
    ],
)
def test_cli_ingest_since_is_checked_before_any_run(bound: Engine, args: list[str]) -> None:
    with freeze_time(CLI_NOW, tick=True), respx.mock(assert_all_mocked=True) as router:
        _ingest(*args, code=2)
        assert len(router.calls) == 0
    assert _edgar_runs(bound) == []


def test_edgar_budget_uses_the_configured_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    assert edgar_budget().rps == 5.0
    monkeypatch.setenv("TELL_RATE_EDGAR_RPS", "2")
    get_settings.cache_clear()
    assert edgar_budget().rps == 2.0


# --- CIK -> ticker --------------------------------------------------------------------------------


def test_load_ticker_map() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://www.sec.gov/files/company_tickers_exchange.json").respond(
            200, content=_read("company_tickers_exchange.json")
        )
        tickers = load_ticker_map(budget=RateBudget(1000, burst=10))
    assert route.calls.last.request.headers["User-Agent"] == get_settings().sec_user_agent
    assert len(tickers) == 10
    assert tickers["0000320193"] == ("AAPL", "Nasdaq", "Apple Inc.")
    assert tickers["0001652044"].ticker == "GOOGL"
    assert tickers["0001108524"].exchange == "NYSE"
    assert tickers["0009990020"].exchange is None


def test_load_ticker_map_404_and_bad_columns() -> None:
    with respx.mock() as router:
        router.get("https://www.sec.gov/files/company_tickers_exchange.json").respond(404)
        with pytest.raises(LookupError):
            load_ticker_map(budget=RateBudget(1000, burst=10))
    with pytest.raises(ValueError, match="exchange"):
        parse_ticker_map('{"fields": ["cik", "name", "ticker"], "data": []}')
    assert (
        parse_ticker_map(
            '{"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "x", null, null]]}'
        )
        == {}
    )


# --- quarterly TSV backfill -----------------------------------------------------------------------


def test_quarterly_loader_builds_backfill_events() -> None:
    events = load_formd_quarterly(FIXTURES / "quarterly", run_id="backfill", now=NOW)
    assert [(e.type, e.external_id) for e in events] == [
        (FORM_D_FILED, "0009990101-26-000001"),
        (FORM_D_FILED, "0009990102-26-000001"),
    ]
    first, second = events
    assert all(e.payload["backfill"] is True for e in events)
    assert first.payload["issuer_name"] == "Example Quarterly Labs Inc"
    assert first.payload["file_number"] == "021-560101"
    assert first.payload["previous_names"] == ["Quarterly Labs Inc"]
    assert first.payload["year_of_inc"] == 2023
    assert first.payload["federal_exemptions_exclusions"] == ["06b", "3C"]
    assert first.payload["total_offering_amount"] == 9_000_000
    # The D/A in the same data set is merged, never a second event.
    assert first.payload["total_amount_sold"] == 7_500_000
    assert [a["accession_number"] for a in first.payload["amendments"]] == ["0009990101-26-000002"]
    assert first.occurred_at == datetime(2026, 1, 12, tzinfo=UTC)
    assert first.magnitude == 9_000_000
    assert first.company_id is None
    assert second.payload["total_offering_amount"] is None
    assert second.payload["total_offering_amount_indefinite"] is True
    assert second.magnitude == 0


def _copy_quarterly(tmp_path: Path) -> Path:
    target = tmp_path / "2026q1_d"
    shutil.copytree(FIXTURES / "quarterly", target)
    return target


def test_quarterly_loader_never_opens_related_persons_tsv(tmp_path: Path) -> None:
    folder = _copy_quarterly(tmp_path)
    forbidden = folder / "RELATEDPERSONS.tsv"
    forbidden.write_text("unreadable\n")
    os.chmod(forbidden, 0)
    try:
        events = load_formd_quarterly(folder, run_id="backfill", now=NOW)
    finally:
        os.chmod(forbidden, 0o600)
    assert len(events) == 2


def test_quarterly_loader_reads_zip_without_related_persons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "2026q1_d.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted((FIXTURES / "quarterly").iterdir()):
            zf.write(path, f"2026Q1_d/{path.name}")
        zf.writestr("2026Q1_d/RELATEDPERSONS.tsv", "unread\n")
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(name))
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    events = load_formd_quarterly(str(archive), run_id="backfill", now=NOW)
    assert len(events) == 2
    assert sorted(opened) == [
        "2026Q1_d/FORMDSUBMISSION.tsv",
        "2026Q1_d/ISSUERS.tsv",
        "2026Q1_d/OFFERING.tsv",
    ]


def test_quarterly_orphan_amendment_and_missing_files(tmp_path: Path) -> None:
    folder = _copy_quarterly(tmp_path)
    submission = folder / "FORMDSUBMISSION.tsv"
    lines = submission.read_text().splitlines()
    submission.write_text("\n".join([lines[0], lines[3]]) + "\n")
    [event] = load_formd_quarterly(folder, run_id="backfill", now=NOW)
    assert event.type == FORM_D_AMENDMENT
    assert event.payload["backfill"] is True
    assert event.payload["is_amendment"] is True

    (folder / "OFFERING.tsv").unlink()
    with pytest.raises(FileNotFoundError):
        load_formd_quarterly(folder, run_id="backfill", now=NOW)
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("README.txt", "nothing")
    with pytest.raises(FileNotFoundError):
        load_formd_quarterly(empty_zip, run_id="backfill", now=NOW)


def _add_quarterly_round(folder: Path, accession: str, filed: str) -> None:
    """Copy the first original's rows under a new accession and filing date."""
    template = "0009990101-26-000001"
    for name in ("FORMDSUBMISSION.tsv", "ISSUERS.tsv", "OFFERING.tsv"):
        path = folder / name
        lines = path.read_text().splitlines()
        row = next(line for line in lines if line.startswith(template + "\t"))
        row = row.replace(template, accession, 1)
        if name == "FORMDSUBMISSION.tsv":
            row = row.replace("12-JAN-2026", filed)
        path.write_text("\n".join([*lines, row]) + "\n")


@pytest.mark.parametrize("previous", ["0009990101-26-000001", ""])
def test_quarterly_amendment_links_to_its_own_round(tmp_path: Path, previous: str) -> None:
    folder = _copy_quarterly(tmp_path)
    # An older round and a later round reuse the file number of 0009990101-26-000001.
    _add_quarterly_round(folder, "0009990101-25-000001", "10-JAN-2025")
    _add_quarterly_round(folder, "0009990101-26-000005", "01-APR-2026")
    offering = folder / "OFFERING.tsv"
    offering.write_text(
        offering.read_text().replace("\ttrue\t0009990101-26-000001\t", f"\ttrue\t{previous}\t")
    )
    events = load_formd_quarterly(folder, run_id="backfill", now=NOW)
    amendments = {e.external_id: e.payload["amendments"] for e in events}
    assert all(e.type == FORM_D_FILED for e in events)
    assert amendments == {
        "0009990101-25-000001": [],
        "0009990101-26-000001": [amendments["0009990101-26-000001"][0]],
        "0009990102-26-000001": [],
        "0009990101-26-000005": [],
    }
    assert amendments["0009990101-26-000001"][0]["accession_number"] == "0009990101-26-000002"


def test_quarterly_amendment_older_than_every_original_is_an_orphan(tmp_path: Path) -> None:
    folder = _copy_quarterly(tmp_path)
    submission = folder / "FORMDSUBMISSION.tsv"
    submission.write_text(submission.read_text().replace("16-MAR-2026", "02-JAN-2026"))
    offering = folder / "OFFERING.tsv"
    offering.write_text(offering.read_text().replace("\t0009990101-26-000001\t", "\t\t"))
    events = load_formd_quarterly(folder, run_id="backfill", now=NOW)
    by_id = {e.external_id: e for e in events}
    assert by_id["0009990101-26-000002"].type == FORM_D_AMENDMENT
    assert by_id["0009990101-26-000001"].payload["amendments"] == []


@pytest.mark.db
def test_quarterly_events_write_and_dedupe_with_daily_keys(db_session: Session) -> None:
    events = load_formd_quarterly(FIXTURES / "quarterly", run_id="backfill", now=NOW)
    first = write_events(db_session, events)
    again = write_events(
        db_session, load_formd_quarterly(FIXTURES / "quarterly", run_id="b2", now=NOW)
    )
    assert (first.inserted, again.inserted, again.skipped) == (2, 0, 2)
    assert edgar_formd.SOURCE == "edgar"
