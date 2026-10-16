"""HubSpot write-back (ADR 0037): setup, check and push against hand-authored fixtures.

No network: every HubSpot endpoint is a ``respx`` route on ``hubspot.invalid``. The
database part (``load_receipt``, the ``push`` command) uses the temporary test database
with one synthetic company and one stored score; nothing names a person.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

import tell.db
from tell.cli import app
from tell.config import get_settings
from tell.crm.hubspot import (
    ACTIVITY_PROPERTIES,
    CRM_ACTIVITY_WINDOW,
    LIST_PAGE_CAP,
    LIST_PAGE_SIZE,
    NOTE_TO_COMPANY,
    PROPERTIES,
    SCOPES,
    TASK_TO_COMPANY,
    HubSpotClient,
    HubSpotError,
    Receipt,
    check,
    company_properties,
    crm_status,
    ensure_properties,
    load_receipt,
    note_body,
    push_company,
    why_now,
)
from tell.db import SessionLocal
from tell.models import Company, Score
from tell.signals.profiles import get_profile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hubspot"
BASE = "https://hubspot.invalid"
TOKEN = "pat-na1-placeholder-token-value"  # a placeholder, not a credential
DOMAIN = "alpha.example"
NAME = "Alpha Example Inc."
AS_OF = date(2026, 10, 6)
NOW = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
PAGES = "https://pages.example/tell-data"
FORM_D_URL = "https://www.sec.gov/Archives/edgar/data/0000000001/0001.txt"
BOARD_URL = "https://boards.greenhouse.invalid/alpha"

CONTRIBUTIONS: list[dict[str, Any]] = [
    {
        "signal_id": 1,
        "signal_type": "funding",
        "role_family": None,
        "contribution": 24.6,
        "url": FORM_D_URL,
        "occurred_at": "2026-10-01T00:00:00+00:00",
        "summary": "Form D: $10,000,000 offering",
    },
    {
        "signal_id": 2,
        "signal_type": "hiring_velocity",
        "role_family": "sales",
        "contribution": 5.5,
        "url": BOARD_URL,
        "occurred_at": "2026-10-03T18:00:00+00:00",
        "summary": "4 new sales roles in 7 days vs 0.5 baseline",
    },
    {
        "signal_id": 3,
        "signal_type": "launch",
        "role_family": None,
        "contribution": 0.0,
        "url": "",
        "occurred_at": "2026-09-20T00:00:00+00:00",
        "summary": "dropped: no receipt",
    },
]

runner = CliRunner()


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _receipt() -> Receipt:
    return Receipt(
        company_id=1,
        domain=DOMAIN,
        name=NAME,
        profile="default",
        as_of=AS_OF,
        score=30.1,
        contributions=[c for c in CONTRIBUTIONS if c["url"]],
    )


def _client() -> HubSpotClient:
    return HubSpotClient(TOKEN, base_url=BASE, sleep=lambda _s: None)


@pytest.fixture
def hubspot() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _route_properties(router: respx.MockRouter, *, missing: set[str]) -> None:
    for spec in PROPERTIES:
        if spec.name in missing:
            router.get(f"/crm/v3/properties/companies/{spec.name}").mock(
                return_value=httpx.Response(404, json=_fixture("property_not_found.json"))
            )
        else:
            router.get(f"/crm/v3/properties/companies/{spec.name}").mock(
                return_value=httpx.Response(200, json=_fixture("property_tell_score.json"))
            )


# --- pure helpers -------------------------------------------------------------------------------


def test_why_now_matches_the_digest_rule() -> None:
    assert why_now(CONTRIBUTIONS) == (
        "Why now: Form D: $10,000,000 offering and 4 new sales roles in 7 days vs 0.5 baseline."
    )
    assert why_now([]) == "Why now: the score crossed the alert threshold."


def test_note_body_is_company_level_only_with_one_line_per_receipt() -> None:
    body = note_body(_receipt(), base_url=PAGES + "/")
    lines = body.splitlines()
    assert lines[0] == "tell score 30.1 (default) as of 2026-10-06"
    assert lines[1] == f"Form D: $10,000,000 offering · 2026-10-01 · {FORM_D_URL}"
    assert lines[2] == f"4 new sales roles in 7 days vs 0.5 baseline · 2026-10-03 · {BOARD_URL}"
    assert lines[3].startswith("Why now: ")
    assert lines[4] == f"Account page: {PAGES}/companies/{DOMAIN}.html"
    assert "dropped: no receipt" not in body
    # The whole body is these five lines: score, two receipts, why now, page. Nothing else
    # from the stored row (ids, families, weights) and nothing that could name a person.
    assert len(lines) == 5
    assert "signal_id" not in body
    assert "@" not in body


def test_company_properties_take_the_top_contribution() -> None:
    props = company_properties(_receipt())
    assert props == {
        "tell_score": "30.1",
        "tell_top_signal": "Form D: $10,000,000 offering",
        "tell_receipt_url": FORM_D_URL,
        "tell_scored_at": "2026-10-06",
        "tell_profile": "default",
    }


# --- client and operations ----------------------------------------------------------------------


def test_setup_creates_only_the_missing_properties(hubspot: respx.MockRouter) -> None:
    _route_properties(hubspot, missing={"tell_profile", "tell_scored_at"})
    created = hubspot.post("/crm/v3/properties/companies").mock(
        return_value=httpx.Response(201, json=_fixture("property_created.json"))
    )
    with _client() as client:
        result = ensure_properties(client)
    assert result == {
        "created": ["tell_scored_at", "tell_profile"],
        "existing": ["tell_score", "tell_top_signal", "tell_receipt_url"],
    }
    assert created.call_count == 2
    bodies = [json.loads(c.request.content) for c in created.calls]
    assert {b["name"] for b in bodies} == {"tell_profile", "tell_scored_at"}
    assert all(b["groupName"] == "companyinformation" for b in bodies)
    assert {(b["type"], b["fieldType"]) for b in bodies} == {("string", "text"), ("date", "date")}
    first = created.calls[0].request
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"
    assert first.headers["User-Agent"].startswith("tell/")


def test_setup_is_a_no_op_when_everything_exists(hubspot: respx.MockRouter) -> None:
    _route_properties(hubspot, missing=set())
    created = hubspot.post("/crm/v3/properties/companies")
    with _client() as client:
        result = ensure_properties(client)
    assert result["created"] == []
    assert len(result["existing"]) == len(PROPERTIES)
    assert not created.called


def test_check_reports_the_probe_and_the_properties(hubspot: respx.MockRouter) -> None:
    probe = hubspot.get("/crm/v3/objects/companies", params={"limit": "1"}).mock(
        return_value=httpx.Response(200, json=_fixture("companies_probe.json"))
    )
    _route_properties(hubspot, missing={"tell_profile"})
    with _client() as client:
        result = check(client)
    assert probe.called
    assert result["ok"] is True
    assert result["setup_needed"] is True
    assert result["properties"]["tell_profile"] is False
    assert result["properties"]["tell_score"] is True


def test_push_creates_company_note_and_task_when_search_is_empty(
    hubspot: respx.MockRouter,
) -> None:
    search = hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_empty.json"))
    )
    listing = hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_list_end.json"))
    )
    create = hubspot.post("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(201, json=_fixture("company_created.json"))
    )
    note = hubspot.post("/crm/v3/objects/notes").mock(
        return_value=httpx.Response(201, json=_fixture("note_created.json"))
    )
    task = hubspot.post("/crm/v3/objects/tasks").mock(
        return_value=httpx.Response(201, json=_fixture("task_created.json"))
    )
    with _client() as client:
        outcome = push_company(
            client,
            _receipt(),
            now=NOW,
            base_url=PAGES,
            threshold=6.0,
            task=True,
            owner_id="555",
        )
    assert outcome.as_record() == {
        "action": "created",
        "domain": DOMAIN,
        "hubspot_company_id": "2002",
        "note_id": "3003",
        "task_id": "4004",
        "listed": {"pages": 1, "capped": False},
    }
    assert listing.call_count == 1
    listed = listing.calls[0].request.url.params
    assert listed["limit"] == str(LIST_PAGE_SIZE)
    assert set(listed["properties"].split(",")) >= {"domain", *(p.name for p in PROPERTIES)}
    query = json.loads(search.calls[0].request.content)
    assert query["filterGroups"] == [
        {"filters": [{"propertyName": "domain", "operator": "EQ", "value": DOMAIN}]}
    ]
    assert set(query["properties"]) >= {p.name for p in PROPERTIES}
    created = json.loads(create.calls[0].request.content)["properties"]
    assert created["name"] == NAME
    assert created["domain"] == DOMAIN
    assert created["tell_score"] == "30.1"
    note_body_sent = json.loads(note.calls[0].request.content)
    assert note_body_sent["associations"] == [
        {
            "to": {"id": "2002"},
            "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_COMPANY}
            ],
        }
    ]
    assert note_body_sent["properties"]["hs_timestamp"] == "2026-10-15T18:00:00+00:00"
    assert note_body_sent["properties"]["hs_note_body"].startswith("tell score 30.1 (default)")
    task_sent = json.loads(task.calls[0].request.content)
    assert task_sent["associations"][0]["types"][0]["associationTypeId"] == TASK_TO_COMPANY
    assert task_sent["properties"]["hs_task_subject"] == f"tell: {NAME} crossed 6"
    assert task_sent["properties"]["hs_task_status"] == "NOT_STARTED"
    assert task_sent["properties"]["hs_timestamp"] == "2026-10-17T18:00:00+00:00"
    assert task_sent["properties"]["hubspot_owner_id"] == "555"


def test_push_updates_an_existing_company_and_skips_the_task_by_default(
    hubspot: respx.MockRouter,
) -> None:
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_found.json"))
    )
    listing = hubspot.get("/crm/v3/objects/companies")
    create = hubspot.post("/crm/v3/objects/companies")
    update = hubspot.patch("/crm/v3/objects/companies/1001").mock(
        return_value=httpx.Response(200, json=_fixture("company_updated.json"))
    )
    hubspot.post("/crm/v3/objects/notes").mock(
        return_value=httpx.Response(201, json=_fixture("note_created.json"))
    )
    task = hubspot.post("/crm/v3/objects/tasks")
    with _client() as client:
        outcome = push_company(client, _receipt(), now=NOW, base_url=PAGES, threshold=6.0)
    assert outcome.action == "updated"
    assert outcome.hubspot_company_id == "1001"
    assert outcome.note_id == "3003"
    assert outcome.task_id is None
    assert "listed" not in outcome.as_record()
    assert not listing.called
    assert not create.called
    assert not task.called
    patched = json.loads(update.calls[0].request.content)["properties"]
    assert set(patched) == {p.name for p in PROPERTIES}
    assert "name" not in patched


def test_push_is_unchanged_when_this_score_was_pushed_unless_forced(
    hubspot: respx.MockRouter,
) -> None:
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_found_same_push.json"))
    )
    update = hubspot.patch("/crm/v3/objects/companies/1001").mock(
        return_value=httpx.Response(200, json=_fixture("company_updated.json"))
    )
    note = hubspot.post("/crm/v3/objects/notes").mock(
        return_value=httpx.Response(201, json=_fixture("note_created.json"))
    )
    with _client() as client:
        first = push_company(client, _receipt(), now=NOW, base_url=PAGES, threshold=6.0)
        assert first.action == "unchanged"
        assert first.hubspot_company_id == "1001"
        assert not update.called
        assert not note.called
        forced = push_company(
            client, _receipt(), now=NOW, base_url=PAGES, threshold=6.0, force=True
        )
    assert forced.action == "updated"
    assert update.called
    assert note.called


def test_crm_status_reads_sales_activity_never_the_note_date() -> None:
    """ADR 0040: ``notes_last_updated`` is set by tell's own note, so it is not read."""
    assert crm_status(None, now=NOW) == "absent"
    quiet = _fixture("search_found.json")["results"][0]
    assert crm_status(quiet, now=NOW) == "quiet"
    active = _fixture("search_found_active.json")["results"][0]
    assert crm_status(active, now=NOW) == "active"
    assert crm_status(active, now=NOW + CRM_ACTIVITY_WINDOW + timedelta(days=1)) == "quiet"
    only_note = {"properties": {"notes_last_updated": "2026-10-15T10:00:00.000Z"}}
    assert crm_status(only_note, now=NOW) == "quiet"
    garbage = {"properties": {"hs_last_sales_activity_timestamp": "not a date"}}
    assert crm_status(garbage, now=NOW) == "quiet"
    naive = {"properties": {"notes_last_contacted": "2026-10-14T10:00:00"}}
    assert crm_status(naive, now=NOW) == "active"
    assert "notes_last_updated" not in ACTIVITY_PROPERTIES


def test_lookups_ask_for_the_activity_properties(hubspot: respx.MockRouter) -> None:
    search = hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_empty.json"))
    )
    listing = hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_list_end.json"))
    )
    with _client() as client:
        assert client.lookup_company(DOMAIN).company is None
    asked = set(json.loads(search.calls[0].request.content)["properties"])
    assert asked >= set(ACTIVITY_PROPERTIES)
    listed = set(listing.calls[0].request.url.params["properties"].split(","))
    assert listed >= set(ACTIVITY_PROPERTIES)


def test_push_finds_a_company_the_search_index_does_not_have_yet(
    hubspot: respx.MockRouter,
) -> None:
    """Issue #5: a company created seconds ago is missing from the search index; the
    object-store listing still has it (whatever the case of its ``domain``), so the push
    updates instead of creating a duplicate."""
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_empty.json"))
    )
    listing = hubspot.get("/crm/v3/objects/companies").mock(
        side_effect=[
            httpx.Response(200, json=_fixture("companies_list_more.json")),
            httpx.Response(200, json=_fixture("companies_list_found.json")),
        ]
    )
    create = hubspot.post("/crm/v3/objects/companies")
    update = hubspot.patch("/crm/v3/objects/companies/1001").mock(
        return_value=httpx.Response(200, json=_fixture("company_updated.json"))
    )
    hubspot.post("/crm/v3/objects/notes").mock(
        return_value=httpx.Response(201, json=_fixture("note_created.json"))
    )
    with _client() as client:
        outcome = push_company(client, _receipt(), now=NOW, base_url=PAGES, threshold=6.0)
    assert outcome.action == "updated"
    assert outcome.hubspot_company_id == "1001"
    assert outcome.as_record()["listed"] == {"pages": 2, "capped": False}
    assert not create.called
    assert update.called
    assert "after" not in listing.calls[0].request.url.params
    assert listing.calls[1].request.url.params["after"] == "1000"


def test_push_lookup_is_unchanged_through_the_listing_too(hubspot: respx.MockRouter) -> None:
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_empty.json"))
    )
    hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_list_found.json"))
    )
    update = hubspot.patch("/crm/v3/objects/companies/1001")
    note = hubspot.post("/crm/v3/objects/notes")
    receipt = Receipt(
        company_id=1,
        domain=DOMAIN,
        name=NAME,
        profile="default",
        as_of=date(2026, 10, 13),
        score=12.4,
        contributions=[c for c in CONTRIBUTIONS if c["url"]],
    )
    with _client() as client:
        outcome = push_company(client, receipt, now=NOW, base_url=PAGES, threshold=6.0)
    assert outcome.action == "unchanged"
    assert outcome.as_record()["listed"] == {"pages": 1, "capped": False}
    assert not update.called
    assert not note.called


def test_push_stops_listing_at_the_page_cap_and_says_so(hubspot: respx.MockRouter) -> None:
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_empty.json"))
    )
    listing = hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_list_more.json"))
    )
    hubspot.post("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(201, json=_fixture("company_created.json"))
    )
    hubspot.post("/crm/v3/objects/notes").mock(
        return_value=httpx.Response(201, json=_fixture("note_created.json"))
    )
    with _client() as client:
        outcome = push_company(client, _receipt(), now=NOW, base_url=PAGES, threshold=6.0)
    assert outcome.action == "created"
    assert listing.call_count == LIST_PAGE_CAP
    assert outcome.as_record()["listed"] == {"pages": LIST_PAGE_CAP, "capped": True}


def test_list_company_honours_a_smaller_cap(hubspot: respx.MockRouter) -> None:
    listing = hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_list_more.json"))
    )
    with _client() as client:
        found = client.list_company(DOMAIN, page_cap=2)
    assert found.company is None
    assert found.via == "list_capped"
    assert found.pages == 2
    assert listing.call_count == 2


def test_dry_run_builds_every_payload_and_sends_nothing(hubspot: respx.MockRouter) -> None:
    outcome = push_company(
        None, _receipt(), now=NOW, base_url=PAGES, threshold=6.0, task=True, dry_run=True
    )
    assert outcome.action == "dry_run"
    assert set(outcome.payloads) == {"company", "note", "task"}
    assert outcome.payloads["company"]["properties"]["domain"] == DOMAIN
    assert outcome.payloads["note"]["associations"][0]["to"] == {"id": "<company id>"}
    assert "hubspot_owner_id" not in outcome.payloads["task"]["properties"]
    assert not hubspot.calls


@pytest.mark.parametrize(
    ("status", "fixture", "expected_scopes"),
    [
        (401, "unauthorized_401.json", ()),
        (403, "missing_scopes_403.json", ("crm.objects.companies.write",)),
    ],
)
def test_auth_errors_carry_the_status_category_and_scopes(
    hubspot: respx.MockRouter, status: int, fixture: str, expected_scopes: tuple[str, ...]
) -> None:
    hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(status, json=_fixture(fixture))
    )
    with _client() as client, pytest.raises(HubSpotError) as info:
        check(client)
    err = info.value
    assert err.status == status
    assert err.category in {"INVALID_AUTHENTICATION", "MISSING_SCOPES"}
    assert err.missing_scopes == expected_scopes
    assert "docs/crm/hubspot.md" in err.hint
    if status == 403:
        assert "crm.objects.companies.write" in err.hint
    else:
        assert "HUBSPOT_PRIVATE_APP_TOKEN" in err.hint
    assert TOKEN not in err.hint


def test_forbidden_without_a_named_scope_hints_every_scope(hubspot: respx.MockRouter) -> None:
    hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(403, content=b"forbidden")
    )
    with _client() as client, pytest.raises(HubSpotError) as info:
        check(client)
    assert all(scope in info.value.hint for scope in SCOPES)


def test_other_errors_keep_hubspots_message(hubspot: respx.MockRouter) -> None:
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(
            400, json={"status": "error", "message": "bad filter", "category": "VALIDATION_ERROR"}
        )
    )
    with _client() as client, pytest.raises(HubSpotError, match="HTTP 400: bad filter"):
        client.find_company(DOMAIN)


def test_requests_are_paced_by_the_shared_budget(hubspot: respx.MockRouter) -> None:
    _route_properties(hubspot, missing=set())
    waits: list[float] = []
    with HubSpotClient(TOKEN, base_url=BASE, sleep=waits.append) as client:
        ensure_properties(client)
    # Five GETs through a 4 rps bucket with burst 1: every call after the first waits.
    assert len(waits) == len(PROPERTIES) - 1
    assert all(w > 0 for w in waits)


# --- database: the receipt and the command -------------------------------------------------------


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """``session_scope()`` opens on the test database; the fake HubSpot is the API base."""
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    monkeypatch.setenv("HUBSPOT_API_BASE", BASE)
    monkeypatch.setenv("TELL_BASE_URL", PAGES)
    get_settings.cache_clear()
    return engine


def _seed(session: Session) -> None:
    company = Company(domain=DOMAIN, name=NAME)
    session.add(company)
    session.flush()
    session.add(
        Score(
            company_id=company.id,
            icp_profile="default",
            as_of=date(2026, 10, 5),
            score=Decimal("12.4"),
            contributions=CONTRIBUTIONS[:1],
        )
    )
    session.add(
        Score(
            company_id=company.id,
            icp_profile="default",
            as_of=AS_OF,
            score=Decimal("30.06"),
            contributions=CONTRIBUTIONS,
        )
    )
    session.commit()


def test_load_receipt_takes_the_newest_score_and_drops_receiptless_rows(bound: Engine) -> None:
    with SessionLocal(bind=bound) as session:
        _seed(session)
        receipt = load_receipt(session, DOMAIN, get_profile("default"))
        assert receipt.as_of == AS_OF
        assert receipt.score == 30.1
        assert [c["signal_type"] for c in receipt.contributions] == ["funding", "hiring_velocity"]
        with pytest.raises(LookupError, match="no company"):
            load_receipt(session, "nobody.example", get_profile("default"))
        with pytest.raises(LookupError, match="no 'job-seeker' score"):
            load_receipt(session, DOMAIN, get_profile("job-seeker"))


def _invoke(args: list[str], code: int = 0) -> Result:
    result = runner.invoke(app, ["crm", "hubspot", *args])
    assert result.exit_code == code, (result.output, result.exception)
    return result


def _json(result: Result) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return record


def test_push_command_without_a_token_exits_1(bound: Engine) -> None:
    with SessionLocal(bind=bound) as session:
        _seed(session)
    record = _json(_invoke(["push", DOMAIN], code=1))
    assert record == {
        "error": "HUBSPOT_PRIVATE_APP_TOKEN is not set (docs/crm/hubspot.md)",
        "source": "hubspot",
    }


def test_push_command_dry_run_needs_no_token(bound: Engine) -> None:
    with SessionLocal(bind=bound) as session:
        _seed(session)
    record = _json(_invoke(["push", f"WWW.{DOMAIN}", "--dry-run", "--task"]))
    assert record["action"] == "dry_run"
    assert record["domain"] == DOMAIN
    assert set(record["payloads"]) == {"company", "note", "task"}
    assert record["payloads"]["company"]["properties"]["tell_score"] == "30.1"
    assert (
        f"{PAGES}/companies/{DOMAIN}.html"
        in record["payloads"]["note"]["properties"]["hs_note_body"]
    )


def test_push_command_unknown_domain_exits_1(bound: Engine) -> None:
    record = _json(_invoke(["push", "nobody.example"], code=1))
    assert record["source"] == "hubspot"
    assert "no company" in record["error"]


def test_push_command_unknown_profile_is_a_usage_error(bound: Engine) -> None:
    _invoke(["push", DOMAIN, "--profile", "nope"], code=2)


@freeze_time(NOW)
def test_push_command_end_to_end(
    bound: Engine, hubspot: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", TOKEN)
    get_settings.cache_clear()
    with SessionLocal(bind=bound) as session:
        _seed(session)
    hubspot.post("/crm/v3/objects/companies/search").mock(
        return_value=httpx.Response(200, json=_fixture("search_empty.json"))
    )
    hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_list_end.json"))
    )
    hubspot.post("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(201, json=_fixture("company_created.json"))
    )
    note = hubspot.post("/crm/v3/objects/notes").mock(
        return_value=httpx.Response(201, json=_fixture("note_created.json"))
    )
    record = _json(_invoke(["push", DOMAIN]))
    assert record == {
        "action": "created",
        "domain": DOMAIN,
        "hubspot_company_id": "2002",
        "note_id": "3003",
        "task_id": None,
        "listed": {"pages": 1, "capped": False},
    }
    sent = json.loads(note.calls[0].request.content)["properties"]
    assert sent["hs_timestamp"] == "2026-10-15T18:00:00+00:00"
    assert TOKEN not in record.values()


def test_check_and_setup_commands_report_hints_on_auth_failure(
    bound: Engine, hubspot: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", TOKEN)
    get_settings.cache_clear()
    hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(401, json=_fixture("unauthorized_401.json"))
    )
    record = _json(_invoke(["check"], code=1))
    assert record["error"].startswith("HubSpot rejected the token (401)")
    assert TOKEN not in record["error"]
    hubspot.get("/crm/v3/properties/companies/tell_score").mock(
        return_value=httpx.Response(403, json=_fixture("missing_scopes_403.json"))
    )
    record = _json(_invoke(["setup"], code=1))
    assert "crm.objects.companies.write" in record["error"]


def test_check_and_setup_commands_succeed(
    bound: Engine, hubspot: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", TOKEN)
    get_settings.cache_clear()
    hubspot.get("/crm/v3/objects/companies").mock(
        return_value=httpx.Response(200, json=_fixture("companies_probe.json"))
    )
    _route_properties(hubspot, missing={"tell_profile"})
    hubspot.post("/crm/v3/properties/companies").mock(
        return_value=httpx.Response(201, json=_fixture("property_created.json"))
    )
    checked = _json(_invoke(["check"]))
    assert checked["setup_needed"] is True
    created = _json(_invoke(["setup"]))
    assert created["created"] == ["tell_profile"]
