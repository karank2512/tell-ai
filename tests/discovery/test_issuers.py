"""Issuer-first discovery (ADR 0035, ``tell.discovery.issuers``): contract, idempotency,
retries, conflicts and the tier C threshold, against ``tests/fixtures/issuer_sites``.

Every HTTP request is answered by ``respx`` (unmatched requests fail the test) and DNS by a
fake resolver, so nothing here touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.db import SessionLocal
from tell.discovery import cli, issuers
from tell.discovery.ats import ASHBY_API
from tell.discovery.issuers import (
    RETRY_AFTER_DAYS,
    SOURCE,
    TIER_C_REVIEW_MIN_SOLD,
    IssuerFinding,
    IssuerRun,
    discover_issuers,
    domain_guesses,
    select_issuers,
    slug_guesses,
)
from tell.http import make_client
from tell.models import (
    Company,
    CompanyAlias,
    EventRow,
    IssuerDiscovery,
    ReviewQueueItem,
    Run,
)
from tell.resolve.evidence import IssuerProfile, SiteEvidence, assess, decide, parse_page
from tell.resolve.match import AliasIndex, match_name

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "issuer_sites"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
CIK = "0002990001"
NAME = "Quillfeather Robotics, Inc."
DOMAIN = "quillfeather.com"
BASE = f"https://{DOMAIN}"
runner = CliRunner()


def fixture(case: str, name: str) -> bytes:
    return (FIXTURES / case / name).read_bytes()


def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        # Vendor APIs: no board anywhere unless a test says otherwise.
        mock.get(url__startswith="https://boards-api.greenhouse.io/").respond(404)
        mock.get(url__startswith="https://api.lever.co/").respond(404)
        mock.get(url__startswith="https://api.smartrecruiters.com/").respond(404)
        yield mock


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


def ashby_none(router: respx.MockRouter) -> None:
    router.get(url__startswith=f"{ASHBY_API}/").respond(404)


def site(router: respx.MockRouter, case: str = "normal", base: str = BASE) -> None:
    """Serve one fixture site: robots, homepage, the footer's terms page; the rest 404."""
    router.get(f"{base}/robots.txt").respond(200, content=fixture(case, "robots.txt"))
    router.get(f"{base}/").respond(200, content=fixture(case, "home.html"))
    if (FIXTURES / case / "terms.html").is_file():
        router.get(f"{base}/legal/terms-of-service").respond(
            200, content=fixture(case, "terms.html")
        )
    router.get(url__startswith=f"{base}/").respond(404)


def resolver(*domains: str) -> Any:
    known = set(domains)

    def resolve(host: str) -> bool:
        return host in known

    return resolve


def form_d(
    session: Session,
    *,
    event_id: str = "edgar:0002990001-26-000001",
    cik: str = CIK,
    name: str = NAME,
    kind: str = "form_d_filed",
    filed: datetime = NOW - timedelta(days=3),
    observed: datetime | None = None,
    company_id: int | None = None,
    **payload: Any,
) -> EventRow:
    body: dict[str, Any] = {
        "cik": cik,
        "issuer_name": name,
        "issuer_city": "AUSTIN",
        "issuer_state": "TX",
        "previous_names": [],
        "entity_type": "Corporation",
        "jurisdiction_of_inc": "DELAWARE",
        "year_of_inc": 2025,
        "industry_group_type": "Other Technology",
        "total_amount_sold": 2_500_000,
        "date_filed": filed.date().isoformat(),
    }
    body.update(payload)
    row = EventRow(
        event_id=event_id,
        company_id=company_id,
        source="edgar",
        type=kind,
        occurred_at=filed,
        observed_at=observed or filed + timedelta(hours=6),
        magnitude=Decimal(1),
        url=f"https://www.sec.gov/{event_id}",
        payload=body,
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row


def run(session: Session, client: httpx.Client, *, now: datetime = NOW, **kw: Any) -> IssuerRun:
    kw.setdefault("apply", True)
    kw.setdefault("resolve_host", resolver(DOMAIN))
    kw.setdefault("sleep", no_sleep)
    kw.setdefault("concurrency", 1)
    return discover_issuers(session, client, now=now, **kw)


def count(session: Session, model: Any) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- contract ------------------------------------------------------------------------------


PROFILE = IssuerProfile(cik=CIK, name=NAME, city="AUSTIN", state="TX")


def test_contract_normal_site_states_the_legal_name() -> None:
    home = parse_page(fixture("normal", "home.html"), "/")
    terms = parse_page(fixture("normal", "terms.html"), "/legal/terms-of-service")
    assert home.title.startswith("Quillfeather Robotics")
    assert "window.__APP__" not in home.text  # scripts are never page text
    found = assess(PROFILE, DOMAIN, [home, terms])
    assert (found.tier, found.path, found.span) == (
        "A",
        "/legal/terms-of-service",
        "Quillfeather Robotics, Inc.",
    )
    assert found.record() == {
        "path": "/legal/terms-of-service",
        "span": "Quillfeather Robotics, Inc.",
    }


def test_contract_empty_board_site_names_someone_else() -> None:
    home = parse_page(fixture("empty_board", "home.html"), "/")
    found = assess(PROFILE, DOMAIN, [home])
    assert found.tier is None
    assert found.reasons == ("no_brand",)


def test_contract_drift_fails_loudly() -> None:
    """The legal entity line moved into a script and the title: no tier A, and the reason
    names what went missing."""
    home = parse_page(fixture("drift", "home.html"), "/")
    terms = parse_page(fixture("drift", "terms.html"), "/legal/terms-of-service")
    found = assess(PROFILE, DOMAIN, [home, terms])
    assert found.tier == "C"
    assert "title_only" in found.reasons


def test_fixtures_are_small() -> None:
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            assert path.stat().st_size < 50_000, path


# --- names ---------------------------------------------------------------------------------


def test_guesses() -> None:
    assert slug_guesses(NAME) == ["quillfeatherrobotics", "quillfeather-robotics", "quillfeather"]
    assert domain_guesses(NAME) == [
        "quillfeather.com",
        "quillfeather.ai",
        "quillfeather.io",
        "quillfeather.co",
        "getquillfeather.com",
        "quillfeatherhq.com",
        "quillfeatherrobotics.com",
        "quillfeatherrobotics.ai",
    ]
    assert domain_guesses("Uviquity, Inc.") == [
        "uviquity.com",
        "uviquity.ai",
        "uviquity.io",
        "uviquity.co",
        "getuviquity.com",
        "uviquityhq.com",
    ]
    assert slug_guesses("AB, Inc.") == []
    assert domain_guesses("Caf\u00e9 Lumi\u00e8re, Inc.")[0] == "cafelumiere.com"  # accents folded
    assert domain_guesses("\u03bc\u03b9\u03b1, Inc.") == []  # no ASCII label at all


# --- input ---------------------------------------------------------------------------------


def test_select_issuers_applies_the_input_filters(db_session: Session) -> None:
    form_d(db_session)
    form_d(
        db_session,
        event_id="e-fund",
        cik="0002990002",
        name="Acme Fund I, LP",
        industry_group_type="Pooled Investment Fund",
    )
    form_d(
        db_session,
        event_id="e-llc",
        cik="0002990003",
        name="Acme LLC",
        entity_type="Limited Liability Company",
    )
    form_d(
        db_session,
        event_id="e-nv",
        cik="0002990004",
        name="Nevada Robotics, Inc.",
        jurisdiction_of_inc="NEVADA",
    )
    form_d(
        db_session, event_id="e-old", cik="0002990005", name="Old Robotics, Inc.", year_of_inc=None
    )
    form_d(
        db_session,
        event_id="e-early",
        cik="0002990006",
        name="Early Robotics, Inc.",
        filed=NOW - timedelta(days=200),
    )
    form_d(db_session, event_id="e-nocik", cik="", name="No Cik Robotics, Inc.")
    known = Company(domain="known.com", name="Known", cik="0002990007")
    db_session.add(known)
    db_session.flush()
    form_d(db_session, event_id="e-known", cik="0002990007", name="Known Robotics, Inc.")
    form_d(
        db_session,
        event_id="e-linked",
        cik="0002990008",
        name="Linked Robotics, Inc.",
        company_id=known.id,
    )
    form_d(
        db_session,
        event_id="e-amend",
        cik="0002990009",
        name="Amended Robotics, Inc.",
        kind="form_d_amendment",
    )
    selected = select_issuers(db_session, now=NOW)
    assert [i.cik for i in selected] == [CIK]
    [issuer] = selected
    assert (issuer.name, issuer.city, issuer.state, issuer.amount_sold) == (
        NAME,
        "AUSTIN",
        "TX",
        2_500_000,
    )
    assert {
        i.cik for i in select_issuers(db_session, now=NOW, since=NOW - timedelta(days=365))
    } == {
        CIK,
        "0002990006",
    }
    assert select_issuers(db_session, now=NOW, limit=0) == []
    assert select_issuers(db_session, now=NOW, retry_due=True) == []


def test_select_issuers_takes_the_newest_filing_and_the_largest_amount(db_session: Session) -> None:
    form_d(
        db_session,
        event_id="e-1",
        filed=NOW - timedelta(days=40),
        total_amount_sold=3_000_000,
        previous_names=["Quillfeather Labs, Inc.", ""],
    )
    form_d(
        db_session,
        event_id="e-2",
        filed=NOW - timedelta(days=2),
        total_amount_sold=None,
        issuer_city="ROUND ROCK",
    )
    [issuer] = select_issuers(db_session, now=NOW)
    assert issuer.event_id == "e-2"
    assert issuer.city == "ROUND ROCK"
    assert issuer.amount_sold == 3_000_000
    assert issuer.previous_names == ()


# --- the main path -------------------------------------------------------------------------


def test_apply_creates_the_company_and_links_by_cik(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    event = form_d(db_session, previous_names=["Quillfeather Labs, Inc."])
    amendment = form_d(
        db_session, event_id="e-amend", kind="form_d_amendment", filed=NOW - timedelta(days=1)
    )
    eight_k = form_d(db_session, event_id="e-8k", kind="form_8k_filed", company_name=NAME)
    event.payload = {
        **event.payload,
        "match": {"rule": "R5", "resolved_at": "2026-10-13T00:00:00+00:00"},
    }
    open_item = ReviewQueueItem(
        kind="entity_match", payload={"event_id": event.event_id, "name": NAME}
    )
    db_session.add(open_item)
    db_session.flush()

    result = run(db_session, client)

    assert result.outcomes["linked"] == 1
    [line] = result.results
    assert (line.outcome, line.tier, line.domain) == ("linked", "A", DOMAIN)
    assert line.evidence == {
        "path": "/legal/terms-of-service",
        "span": "Quillfeather Robotics, Inc.",
    }
    assert line.linked_events == 3
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    assert (company.source, company.cik, company.cik_source) == (
        "issuer_first",
        CIK,
        "issuer_first_a",
    )
    assert (company.name, company.legal_name) == ("Quillfeather Robotics", NAME)
    assert (company.hq_city, company.hq_state, company.hq_country) == ("Austin", "TX", "US")
    assert company.yc_status is None
    aliases = {
        (a.alias, a.source)
        for a in db_session.scalars(
            select(CompanyAlias).where(CompanyAlias.company_id == company.id)
        )
    }
    # "Quillfeather Labs, Inc." normalizes to "quillfeather" (labs is cut); the brand and the
    # domain label repeat the filing's names, so every alias here is a trusted edgar one.
    assert aliases == {("quillfeather robotics", "edgar"), ("quillfeather", "edgar")}
    for row in (event, amendment, eight_k):
        db_session.refresh(row)
        assert row.company_id == company.id
        assert row.payload["match"]["rule"] == "R0"
        assert row.payload["match"]["linked_by"] == SOURCE
        assert row.payload["match"]["confidence"] == 0.99
    assert event.payload["match"]["previous"]["rule"] == "R5"
    db_session.refresh(open_item)
    assert open_item.status == "resolved"
    assert open_item.payload["resolved_by"] == SOURCE
    record = db_session.get(IssuerDiscovery, CIK)
    assert record is not None
    assert (record.status, record.tier, record.domain, record.attempts) == (
        "linked",
        "A",
        DOMAIN,
        1,
    )
    assert record.evidence == {
        "path": "/legal/terms-of-service",
        "span": "Quillfeather Robotics, Inc.",
    }
    assert record.next_retry_at is None
    run_row = db_session.get(Run, result.run_id)
    assert run_row is not None
    assert (run_row.source, run_row.status, run_row.fetched, run_row.inserted) == (
        SOURCE,
        "ok",
        1,
        1,
    )
    # The next filing links by R0 through the resolver, guards and all.
    index = AliasIndex.from_session(db_session)
    decision = match_name(db_session, "Quillfeather Robotics Inc", index=index, cik=CIK)
    assert (decision.rule, decision.company_id) == ("R0", company.id)


def test_rerun_creates_nothing(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    form_d(db_session)
    run(db_session, client)
    before = {
        m: count(db_session, m) for m in (Company, CompanyAlias, IssuerDiscovery, ReviewQueueItem)
    }
    calls = len(router.calls)
    again = run(db_session, client)
    assert again.selected == 0
    assert again.results == []
    assert len(router.calls) == calls
    assert {m: count(db_session, m) for m in before} == before


def test_shadow_writes_nothing(
    db_session: Session,
    client: httpx.Client,
    router: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ashby_none(router)
    site(router)
    form_d(db_session)
    with caplog.at_level("INFO", logger="tell.discovery.issuers"):
        result = run(db_session, client, apply=False)
    assert [r.outcome for r in result.results] == ["linked"]
    assert result.results[0].company_id is None
    assert count(db_session, Company) == 0
    assert count(db_session, IssuerDiscovery) == 0
    assert count(db_session, Run) == 0
    line = json.loads(caplog.records[-1].getMessage())
    assert {
        "run_id",
        "source",
        "started_at",
        "finished_at",
        "fetched",
        "inserted",
        "skipped",
        "errors",
    } <= set(line)
    assert (line["source"], line["mode"], line["fetched"]) == (SOURCE, "shadow", 1)
    assert line["requests"] == len(router.calls)


def test_a_redirect_keys_the_company_by_its_final_domain(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    router.get("https://quillfeatherrobotics.com/robots.txt").respond(404)
    router.get("https://quillfeatherrobotics.com/").respond(
        301, headers={"Location": "https://www.quillfeather.com/"}
    )
    site(router, base="https://www.quillfeather.com")
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver("quillfeatherrobotics.com"))
    [line] = result.results
    assert (line.outcome, line.domain) == ("linked", DOMAIN)
    assert db_session.scalars(select(Company.domain)).all() == [DOMAIN]


# --- outcomes ------------------------------------------------------------------------------


def test_existing_domain_opens_a_review_item_and_changes_nothing(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    owner = Company(domain=DOMAIN, name="Quillfeather", yc_status="Active", yc_batch="Summer 2025")
    db_session.add(owner)
    db_session.flush()
    event = form_d(db_session)
    result = run(db_session, client)
    [line] = result.results
    assert (line.outcome, line.company_id) == ("conflict", owner.id)
    db_session.refresh(owner)
    assert (owner.cik, owner.cik_source, owner.legal_name) == (None, None, None)
    db_session.refresh(event)
    assert event.company_id is None
    item = db_session.get(ReviewQueueItem, line.review_id)
    assert item is not None
    assert (item.kind, item.status) == ("entity_match", "open")
    assert item.payload["event_id"] == event.event_id
    assert item.payload["candidates"][0]["company_id"] == owner.id
    info = item.payload[SOURCE]
    assert (info["reason"], info["cik"], info["proposed_company_id"]) == (
        "domain_exists",
        CIK,
        owner.id,
    )
    assert info["evidence"] == {
        "path": "/legal/terms-of-service",
        "span": "Quillfeather Robotics, Inc.",
    }
    record = db_session.get(IssuerDiscovery, CIK)
    assert record is not None
    assert record.status == "conflict"
    # Rerun: not selected again, no second item.
    run(db_session, client)
    assert count(db_session, ReviewQueueItem) == 1


def test_conflict_merges_into_the_resolvers_open_item(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    owner = Company(domain=DOMAIN, name="Quillfeather")
    db_session.add(owner)
    db_session.flush()
    event = form_d(db_session)
    item = ReviewQueueItem(
        kind="entity_match", payload={"event_id": event.event_id, "name": NAME, "candidates": []}
    )
    db_session.add(item)
    db_session.flush()
    run(db_session, client)
    assert count(db_session, ReviewQueueItem) == 1
    db_session.refresh(item)
    assert item.payload[SOURCE]["reason"] == "domain_exists"
    assert item.payload["candidates"][0]["company_id"] == owner.id


def test_two_qualifying_domains_are_a_conflict(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    site(router, base="https://quillfeather.ai")
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver(DOMAIN, "quillfeather.ai"))
    [line] = result.results
    assert (line.outcome, line.domains) == ("conflict", (DOMAIN, "quillfeather.ai"))
    assert count(db_session, Company) == 0
    item = db_session.get(ReviewQueueItem, line.review_id)
    assert item is not None
    assert item.payload[SOURCE]["reason"] == "several_domains"
    assert item.payload[SOURCE]["domains"] == [DOMAIN, "quillfeather.ai"]


@pytest.mark.parametrize(("sold", "reviewed"), [(TIER_C_REVIEW_MIN_SOLD, True), (999_999, False)])
def test_tier_c_threshold(
    db_session: Session, client: httpx.Client, router: respx.MockRouter, sold: int, reviewed: bool
) -> None:
    ashby_none(router)
    router.get(f"{BASE}/robots.txt").respond(404)
    router.get(f"{BASE}/").respond(
        200,
        content=b"<html><head><title>Quillfeather | robots</title></head><body><p>Robots.</p></body></html>",
    )
    router.get(url__startswith=f"{BASE}/").respond(404)
    form_d(db_session, total_amount_sold=sold)
    result = run(db_session, client)
    [line] = result.results
    assert (line.outcome, line.tier, line.domains) == ("tier_c", "C", (DOMAIN,))
    assert (line.review_id is not None) is reviewed
    assert count(db_session, ReviewQueueItem) == int(reviewed)
    assert count(db_session, Company) == 0
    record = db_session.get(IssuerDiscovery, CIK)
    assert record is not None
    assert (record.status, record.tier, record.domain, record.evidence) == (
        "tier_c",
        "C",
        DOMAIN,
        None,
    )
    assert record.next_retry_at is None


def test_no_domain_retries_after_30_and_90_days_and_on_the_next_filing(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    """The shipped daily step: no ``--since``, so the default 90-day filing window."""
    ashby_none(router)
    form_d(db_session)  # filed NOW - 3 days; first tried at NOW
    nowhere = resolver()

    first = run(db_session, client, resolve_host=nowhere)
    assert [r.outcome for r in first.results] == ["no_domain"]
    record = db_session.get(IssuerDiscovery, CIK)
    assert record is not None
    assert (record.status, record.attempts) == ("no_domain", 1)
    assert record.next_retry_at == NOW + timedelta(days=RETRY_AFTER_DAYS[0])

    assert run(db_session, client, now=NOW + timedelta(days=1), resolve_host=nowhere).selected == 0
    assert select_issuers(db_session, now=NOW + timedelta(days=29), retry_due=True) == []

    later = NOW + timedelta(days=31)
    due = select_issuers(db_session, now=later, retry_due=True)
    assert [(i.cik, i.retry) for i in due] == [(CIK, True)]
    second = run(db_session, client, now=later, resolve_host=nowhere)
    assert [(r.outcome, r.retry) for r in second.results] == [("no_domain", True)]
    db_session.refresh(record)
    assert record.attempts == 2
    assert record.next_retry_at == NOW + timedelta(days=RETRY_AFTER_DAYS[1])

    # The 90-day retry falls 93 days after the filing, outside the default window: it is
    # still selected, by a plain run and by --retry-due alike.
    third_at = NOW + timedelta(days=RETRY_AFTER_DAYS[1], hours=1)
    assert select_issuers(db_session, now=third_at - timedelta(hours=2)) == []
    plain = select_issuers(db_session, now=third_at)
    assert [(i.cik, i.event_id, i.retry, i.amount_sold) for i in plain] == [
        (CIK, "edgar:0002990001-26-000001", True, 2_500_000)
    ]
    third = run(db_session, client, now=third_at, resolve_host=nowhere, retry_due=True)
    assert [(r.outcome, r.retry) for r in third.results] == [("no_domain", True)]
    db_session.refresh(record)
    assert (record.attempts, record.next_retry_at) == (3, None)
    assert select_issuers(db_session, now=NOW + timedelta(days=400)) == []
    assert select_issuers(db_session, now=NOW + timedelta(days=400), retry_due=True) == []

    # The issuer's next filing triggers a retry, whatever the schedule says.
    form_d(
        db_session,
        event_id="e-next",
        filed=third_at + timedelta(days=2),
        observed=third_at + timedelta(days=2),
    )
    again = select_issuers(db_session, now=third_at + timedelta(days=3))
    assert [(i.event_id, i.retry) for i in again] == [("e-next", True)]


def test_a_scheduled_retry_outside_the_window_reads_the_cik_filings_only(
    db_session: Session,
) -> None:
    """Out-of-window due retries take their newest eligible filing and largest amount; an
    ineligible, linked or other issuer's old filing is never pulled in, nor a CIK a company
    took since."""
    old = NOW - timedelta(days=200)
    form_d(db_session, event_id="e-1", filed=old, total_amount_sold=4_000_000)
    form_d(db_session, event_id="e-2", filed=old + timedelta(days=10), total_amount_sold=1_000)
    form_d(db_session, event_id="e-fund", filed=old + timedelta(days=20), entity_type="LLC")
    form_d(
        db_session, event_id="e-stranger", cik="0002990020", name="Stranger Labs, Inc.", filed=old
    )
    form_d(db_session, event_id="e-taken", cik="0002990021", name="Taken Labs, Inc.", filed=old)
    db_session.add(Company(domain="taken.com", name="Taken", cik="0002990021"))
    for cik in (CIK, "0002990021", "0002990022"):
        db_session.add(
            IssuerDiscovery(
                cik=cik,
                status="no_domain",
                attempts=1,
                last_tried_at=old + timedelta(days=30),
                next_retry_at=NOW - timedelta(days=1),
                created_at=old + timedelta(days=30),
            )
        )
    db_session.flush()

    picked = select_issuers(db_session, now=NOW)
    assert [(i.cik, i.event_id, i.amount_sold, i.retry) for i in picked] == [
        (CIK, "e-2", 4_000_000, True)
    ]
    assert select_issuers(db_session, now=NOW, retry_due=True) == picked
    # an explicit --since narrower than the filing still leaves the scheduled retry due
    assert select_issuers(db_session, now=NOW, since=NOW - timedelta(days=5)) == picked


def test_new_issuers_come_before_due_retries(db_session: Session) -> None:
    form_d(db_session)
    form_d(db_session, event_id="e-other", cik="0002990010", name="Other Robotics, Inc.")
    db_session.add(
        IssuerDiscovery(
            cik="0002990010",
            status="no_domain",
            attempts=1,
            last_tried_at=NOW - timedelta(days=40),
            next_retry_at=NOW - timedelta(days=10),
            created_at=NOW - timedelta(days=40),
        )
    )
    db_session.add(IssuerDiscovery(cik="0002990011", status="linked", attempts=1))
    db_session.flush()
    assert [(i.cik, i.retry) for i in select_issuers(db_session, now=NOW)] == [
        (CIK, False),
        ("0002990010", True),
    ]
    assert [i.cik for i in select_issuers(db_session, now=NOW, limit=1)] == [CIK]


# --- the network half ----------------------------------------------------------------------


def test_ats_board_site_is_a_candidate_domain(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    jobs = {
        "jobs": [
            {"title": "Robotics engineer", "jobUrl": "https://jobs.ashbyhq.com/quillfeather/1"}
        ]
    }
    router.get(f"{ASHBY_API}/quillfeatherrobotics").respond(200, json=jobs)
    router.get(url__startswith=f"{ASHBY_API}/").respond(404)
    hosted = (
        b"<html><head><title>Quillfeather Robotics Jobs</title></head><body>"
        b'<script>window.__appData = {"organization": {"publicWebsite": "https://qfrobotics.io"}};</script>'
        b"</body></html>"
    )
    router.get("https://jobs.ashbyhq.com/robots.txt").respond(404)
    router.get("https://jobs.ashbyhq.com/quillfeatherrobotics").respond(200, content=hosted)
    site(router, base="https://qfrobotics.io")
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver("qfrobotics.io"))
    [line] = result.results
    assert (line.outcome, line.domain) == ("linked", "qfrobotics.io")
    assert line.candidates[0] == "qfrobotics.io"
    assert line.boards[0]["vendor"] == "ashby"
    assert line.boards[0]["sites"] == ["qfrobotics.io"]


def test_a_robots_blocked_hosted_board_page_is_not_fetched(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    jobs = {"jobs": [{"title": "Robotics engineer", "jobUrl": "https://example.test/1"}]}
    router.get(f"{ASHBY_API}/quillfeatherrobotics").respond(200, json=jobs)
    router.get(url__startswith=f"{ASHBY_API}/").respond(404)
    router.get("https://jobs.ashbyhq.com/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /quillfeatherrobotics\n"
    )
    hosted = router.get("https://jobs.ashbyhq.com/quillfeatherrobotics").respond(200)
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver())
    [line] = result.results
    assert line.outcome == "no_domain"
    assert hosted.call_count == 0
    assert line.boards[0]["vendor"] == "ashby"
    assert line.boards[0]["sites"] == []


def test_robots_and_unreachable_domains_are_skipped(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    router.get(f"{BASE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    router.get("https://quillfeather.ai/robots.txt").respond(404)
    router.get("https://quillfeather.ai/").mock(side_effect=httpx.ConnectError("refused"))
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver(DOMAIN, "quillfeather.ai"))
    [line] = result.results
    assert line.outcome == "no_domain"
    homepage_calls = [c for c in router.calls if c.request.url == httpx.URL(f"{BASE}/")]
    assert homepage_calls == []


def test_platform_and_parked_sites_give_no_evidence(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    router.get(f"{BASE}/robots.txt").respond(404)
    router.get(f"{BASE}/").respond(301, headers={"Location": "https://quillfeather.vercel.app/"})
    router.get("https://quillfeather.vercel.app/robots.txt").respond(404)
    router.get("https://quillfeather.vercel.app/").respond(
        200, content=fixture("normal", "terms.html")
    )
    router.get("https://quillfeather.ai/robots.txt").respond(404)
    router.get("https://quillfeather.ai/").respond(
        200, content=b"<html><head><title>quillfeather.ai is for sale</title></head></html>"
    )
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver(DOMAIN, "quillfeather.ai"))
    assert [r.outcome for r in result.results] == ["no_domain"]


def test_requests_are_rate_limited_per_host_and_the_hook_is_removed(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    form_d(db_session)
    waits: list[float] = []
    run(db_session, client, sleep=waits.append, clock=lambda: 0.0)
    assert waits  # same-host requests waited for their slot
    assert all(w > 0 for w in waits)
    assert client.event_hooks["request"] == []


def test_one_failing_issuer_does_not_stop_the_run(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    form_d(db_session)
    form_d(db_session, event_id="e-bad", cik="0002990012", name="Brokenwing Robotics, Inc.")

    def resolve(host: str) -> bool:
        if host.startswith("brokenwing"):
            raise RuntimeError("resolver down")
        return host == DOMAIN

    result = run(db_session, client, resolve_host=resolve, concurrency=2)
    assert result.errors == ["0002990012"]
    assert [r.outcome for r in result.results] == ["linked"]
    run_row = db_session.get(Run, result.run_id)
    assert run_row is not None
    assert run_row.errors == 1
    # The failed issuer is recorded as tried, with the usual retry date, so the next run does
    # not select and fetch it again (shadow run 2026-10-15: 8 issuers re-fetched every run).
    row = db_session.get(IssuerDiscovery, "0002990012")
    assert row is not None
    assert (row.status, row.attempts, row.tier, row.domain, row.evidence) == (
        "no_domain",
        1,
        None,
        None,
        None,
    )
    assert row.next_retry_at == NOW + timedelta(days=RETRY_AFTER_DAYS[0])
    later = NOW + timedelta(days=1)
    assert select_issuers(db_session, now=later) == []
    due = select_issuers(db_session, now=NOW + timedelta(days=RETRY_AFTER_DAYS[0]))
    assert [(i.cik, i.retry) for i in due] == [("0002990012", True)]


def test_record_error_never_overwrites_another_outcome(db_session: Session) -> None:
    issuer = issuers.Issuer(cik=CIK, name=NAME, event_id="e1", filed_on="2026-10-12")
    db_session.add(
        IssuerDiscovery(cik=CIK, status="tier_c", attempts=1, created_at=NOW, domain="q.com")
    )
    db_session.flush()
    assert issuers.record_error(db_session, issuer, now=NOW) is False
    row = db_session.get(IssuerDiscovery, CIK)
    assert row is not None
    assert (row.status, row.attempts, row.domain) == ("tier_c", 1, "q.com")
    db_session.add(Company(domain="held.example", name="Held", cik=CIK))
    db_session.flush()
    other = issuers.Issuer(cik=CIK, name=NAME, event_id="e1", filed_on="2026-10-12")
    assert issuers.record_error(db_session, other, now=NOW) is False


def test_record_error_failing_write_is_logged_not_raised(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr(issuers, "_company_by_cik", boom)
    issuer = issuers.Issuer(cik=CIK, name=NAME, event_id="e1", filed_on="2026-10-12")
    assert issuers.record_error(db_session, issuer, now=NOW) is False
    assert db_session.get(IssuerDiscovery, CIK) is None


def test_a_page_with_control_characters_is_read_not_an_error(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    """lxml refused control characters ("All strings must be XML compatible") and the
    issuer was never recorded; the page is now read without them."""
    ashby_none(router)
    home = fixture("normal", "home.html").replace(b"<main>", b"<main>\x01\x00<!-- x -->\x0b")
    terms = fixture("normal", "terms.html").replace(b"</p>", b"</p>\x1f", 1)
    router.get(f"{BASE}/robots.txt").respond(200, content=fixture("normal", "robots.txt"))
    router.get(f"{BASE}/").respond(200, content=home)
    router.get(f"{BASE}/legal/terms-of-service").respond(200, content=terms)
    router.get(url__startswith=f"{BASE}/").respond(404)
    form_d(db_session)
    result = run(db_session, client)
    assert result.errors == []
    assert [(r.outcome, r.tier) for r in result.results] == [("linked", "A")]


def test_the_filing_years_reach_the_evidence_rule(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    """An older company with the issuer's exact legal name (shadow run 2026-10-15, Core
    Automation) is tier C: the profile carries the filing's year of incorporation."""
    ashby_none(router)
    home = fixture("normal", "home.html").replace(b"&copy; 2026", b"&copy; 2001 - 2022")
    router.get(f"{BASE}/robots.txt").respond(200, content=fixture("normal", "robots.txt"))
    router.get(f"{BASE}/").respond(200, content=home)
    router.get(f"{BASE}/legal/terms-of-service").respond(
        200, content=fixture("normal", "terms.html")
    )
    router.get(url__startswith=f"{BASE}/").respond(404)
    form_d(db_session, year_of_inc=2025)
    [issuer] = select_issuers(db_session, now=NOW)
    assert (issuer.year_of_inc, issuer.profile().filed_year) == (2025, 2026)
    result = run(db_session, client)
    assert [(r.outcome, r.tier) for r in result.results] == [("tier_c", "C")]


def test_out_of_time_defers_issuers(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    form_d(db_session)
    ticks = iter([0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    result = run(db_session, client, clock=lambda: next(ticks, 1000.0), max_minutes=1)
    assert (result.selected, result.deferred, result.results) == (1, 1, [])
    assert not router.calls


def test_bad_arguments(db_session: Session, client: httpx.Client) -> None:
    with pytest.raises(ValueError, match="aware"):
        discover_issuers(db_session, client, now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="aware"):
        discover_issuers(db_session, client, now=NOW, since=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="concurrency"):
        discover_issuers(db_session, client, now=NOW, concurrency=0)
    with pytest.raises(ValueError, match="max_minutes"):
        discover_issuers(db_session, client, now=NOW, max_minutes=0)


def test_commit_is_called_after_the_read_and_every_issuer(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    site(router)
    form_d(db_session)
    commits: list[int] = []
    run(db_session, client, commit=lambda s: commits.append(len(router.calls)))
    # Before any request (start + selection), after the issuer, after the run row.
    assert commits[0] == 0
    assert len(commits) == 3


# --- CLI -----------------------------------------------------------------------------------


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    return engine


def test_cli_issuers_shadow_and_apply(
    bound: Engine, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ashby_none(router)
    site(router)
    monkeypatch.setattr(issuers, "dns_resolves", resolver(DOMAIN))
    monkeypatch.setattr(issuers.time, "sleep", no_sleep)  # the per-host limiter's waits
    with SessionLocal(bind=bound) as session:
        form_d(session, filed=datetime.now(UTC) - timedelta(days=1))
        session.commit()

    shadow = runner.invoke(cli.app, ["issuers", "--since", "2026-01-01", "--concurrency", "1"])
    assert shadow.exit_code == 0, shadow.output
    lines = [json.loads(line) for line in shadow.output.splitlines()]
    assert lines[0]["outcome"] == "linked"
    assert (lines[-1]["mode"], lines[-1]["fetched"], lines[-1]["inserted"]) == ("shadow", 1, 0)
    with SessionLocal(bind=bound) as session:
        assert count(session, Company) == 0

    applied = runner.invoke(cli.app, ["issuers", "--apply", "--limit", "5"])
    assert applied.exit_code == 0, applied.output
    last = json.loads(applied.output.splitlines()[-1])
    assert (last["mode"], last["inserted"]) == ("apply", 1)
    with SessionLocal(bind=bound) as session:
        assert count(session, Company) == 1
        assert session.scalar(select(Run.source)) == SOURCE

    help_text = runner.invoke(cli.app, ["issuers", "--help"]).output
    for option in ("--since", "--limit", "--shadow", "--apply", "--retry-due"):
        assert option in help_text


# --- smaller paths -------------------------------------------------------------------------


def test_dns_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(host: str, port: int, **_: Any) -> list[Any]:
        if host == "gone.example":
            raise OSError("NXDOMAIN")
        return [("addr",)]

    monkeypatch.setattr(issuers.socket, "getaddrinfo", answer)
    assert issuers.dns_resolves("quillfeather.com")
    assert not issuers.dns_resolves("gone.example")


def test_record_never_takes_a_cik_another_company_holds(db_session: Session) -> None:
    form_d(db_session)
    [issuer] = select_issuers(db_session, now=NOW)
    db_session.add(Company(domain="other.com", name="Other", cik=CIK))
    db_session.flush()
    evidence = SiteEvidence(DOMAIN, "A", "/terms", NAME, "Quillfeather Robotics")
    finding = IssuerFinding(issuer, decide([evidence]))
    result = issuers.record(db_session, finding, now=NOW)
    assert result.outcome == "known_cik"
    assert count(db_session, IssuerDiscovery) == 0
    assert db_session.scalars(select(Company.domain)).all() == ["other.com"]


def test_shadow_reports_conflicts_and_tier_c(db_session: Session) -> None:
    form_d(db_session)
    [issuer] = select_issuers(db_session, now=NOW)
    a = SiteEvidence("a.com", "A", "/", NAME)
    b = SiteEvidence("b.com", "B", "/", "Quillfeather Robotics")
    c = SiteEvidence("c.com", "C", "/", "Quillfeather")
    assert issuers._planned(IssuerFinding(issuer, decide([a, b]))) == "conflict"
    assert issuers._planned(IssuerFinding(issuer, decide([c]))) == "tier_c"
    assert issuers._planned(IssuerFinding(issuer, decide([]))) == "no_domain"


def test_redirect_duplicates_and_blocked_legal_pages(
    db_session: Session, client: httpx.Client, router: respx.MockRouter
) -> None:
    ashby_none(router)
    for host in (DOMAIN, "quillfeather.ai"):
        router.get(f"https://{host}/robots.txt").respond(404)
        router.get(f"https://{host}/").respond(
            301, headers={"Location": "https://www.quillfeather.com/"}
        )
    www = "https://www.quillfeather.com"
    router.get(f"{www}/robots.txt").respond(200, text="User-agent: *\nDisallow: /legal/\n")
    router.get(f"{www}/").respond(200, content=fixture("normal", "home.html"))
    router.get(f"{www}/terms").respond(301, headers={"Location": "https://elsewhere.example/terms"})
    router.get("https://elsewhere.example/robots.txt").respond(404)
    router.get("https://elsewhere.example/terms").respond(
        200, content=fixture("normal", "terms.html")
    )
    router.get(url__startswith=f"{www}/").respond(404)
    form_d(db_session)
    result = run(db_session, client, resolve_host=resolver(DOMAIN, "quillfeather.ai"))
    [line] = result.results
    # The legal pages are robots-blocked or leave the site, so the homepage's brand is all.
    assert (line.outcome, line.tier, line.domains) == ("tier_c", "C", (DOMAIN,))
    assert not [c for c in router.calls if c.request.url.path.startswith("/legal/")]
