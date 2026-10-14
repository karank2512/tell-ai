"""Ownership evidence for slug-guessed boards, ``reverify-guesses`` and shared destinations.

ADR 0031: a guessed board (confidence 0.60) is activated only when the board's own
public data points at the company's registrable domain. ``ownership_audit.json`` holds the
minimal public evidence (board name or page title, the sites the board links, one job URL
per host) of the wrong guesses the 2026-10-13 audit and the 2026-10-14 recount found and
of a sample of correct guesses, recorded 2026-10-14; every request is mocked here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.db import SessionLocal
from tell.discovery import ats, cli, domain_label
from tell.discovery.ats import (
    ASHBY_API,
    CANDIDATE_PATHS,
    GREENHOUSE_API,
    GUESS_CONFIDENCE,
    GUESS_EVIDENCE_REASON,
    LEVER_API,
    PAGE_CONFIDENCE,
    SMARTRECRUITERS_API,
    DiscoveryResult,
    OwnershipEvidence,
    discover_domain,
    guess_rejection,
    name_ratio,
    ownership_evidence,
    select_new_companies,
)
from tell.discovery.reverify import reverify_guesses, select_guessed_boards
from tell.http import RateBudget, make_client
from tell.models import AtsBoard, Company, ReviewQueueItem

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"
AUDIT = json.loads((FIXTURES / "ownership_audit.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DOMAIN = "acme.com"
BASE = "https://acme.com"
SR_EMPTY = {"offset": 0, "limit": 1, "totalFound": 0, "content": []}
GH_HOSTED = "https://job-boards.greenhouse.io"


def page(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


def site(router: respx.MockRouter, base: str = BASE) -> None:
    """A company site with nothing on it: every candidate page 404s."""
    router.get(f"{base}/robots.txt").respond(404)
    for path in CANDIDATE_PATHS:
        router.get(f"{base}{path}").respond(404)


def no_guesses(router: respx.MockRouter, label: str = "acme") -> None:
    for slug in (label, f"{label}inc", f"{label}-inc"):
        router.get(f"{GREENHOUSE_API}/{slug}/jobs").respond(404)
        router.get(f"{LEVER_API}/{slug}").respond(200, json=[])
        router.get(f"{ASHBY_API}/{slug}").respond(200, json={"apiVersion": "1", "jobs": []})
        router.get(f"{SMARTRECRUITERS_API}/{slug}/postings").respond(200, json=SR_EMPTY)


def requested(router: respx.MockRouter) -> list[str]:
    return [str(call.request.url) for call in router.calls]


def discover(
    session: Session, client: httpx.Client, domain: str = DOMAIN, name: str | None = None
) -> DiscoveryResult:
    return discover_domain(session, client, domain, name, now=NOW, sleep=no_sleep)


def boards(session: Session) -> list[AtsBoard]:
    return list(session.scalars(select(AtsBoard).order_by(AtsBoard.id)))


def reviews(session: Session) -> list[ReviewQueueItem]:
    return list(session.scalars(select(ReviewQueueItem).order_by(ReviewQueueItem.id)))


def greenhouse_guess(
    router: respx.MockRouter, jobs: list[dict[str, Any]] | None = None, content: str = ""
) -> None:
    """``acme`` is a Greenhouse board named "Acme" with one job."""
    no_guesses(router)
    payload = {"jobs": jobs or [{"id": 1, "absolute_url": f"{GH_HOSTED}/acme/jobs/1"}]}
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(200, json=payload)
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme", "content": content})


# --- the audit's boards ---------------------------------------------------------------------


def _mock_case(router: respx.MockRouter, case: dict[str, Any]) -> None:
    for url, doc in case["docs"].items():
        if "json" in doc:
            router.get(url).respond(200, json=doc["json"])
        else:
            router.get(url).respond(200, text=doc["html"])
    # Anything the fixture did not record was not requested when it was recorded.
    router.route().respond(404)


def _verdict(client: httpx.Client, case: dict[str, Any]) -> tuple[str | None, OwnershipEvidence]:
    profile = ownership_evidence(
        client, case["vendor"], case["identifier"], case["domain"], sleep=no_sleep
    )
    ratio = name_ratio(profile.name, [domain_label(case["domain"])])
    return guess_rejection(ratio, profile.evidence), profile.evidence


@pytest.mark.parametrize(
    "case", AUDIT["cases"], ids=lambda c: f"{c['domain']}-{c['vendor']}-{c['identifier']}"
)
def test_audit_boards(router: respx.MockRouter, client: httpx.Client, case: dict[str, Any]) -> None:
    _mock_case(router, case)
    reason, evidence = _verdict(client, case)
    if case["expected"] == "reject":
        assert reason is not None
        assert evidence.matched is None
        assert evidence.failed == ()
    else:
        assert reason is None, evidence
        assert evidence.matched is not None


def test_audit_counts(client: httpx.Client) -> None:
    """Every known wrong guess is rejected; every sampled correct guess is kept."""
    outcome: dict[str, list[bool]] = {"reject": [], "accept": []}
    for case in AUDIT["cases"]:
        with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
            _mock_case(router, case)
            reason, _ = _verdict(client, case)
        outcome[case["expected"]].append(reason is not None)
    assert outcome["reject"] == [True] * 11
    assert outcome["accept"] == [False] * 8


def test_audit_fixture_is_scrubbed() -> None:
    text = (FIXTURES / "ownership_audit.json").read_text(encoding="utf-8")
    assert "@" not in text
    assert "linkedin.com/in/" not in text


# --- evidence per vendor --------------------------------------------------------------------


def test_greenhouse_evidence_order_and_cost(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme", "content": ""})
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(
        200, json={"jobs": [{"id": 1, "absolute_url": f"{GH_HOSTED}/acme/jobs/1"}]}
    )
    router.get(f"{GH_HOSTED}/acme").mock(return_value=page("greenhouse_hosted.html"))
    profile = ownership_evidence(client, "greenhouse", "acme", DOMAIN, sleep=no_sleep)
    assert profile.name == "Acme"
    assert profile.evidence.matched == "greenhouse:hosted_page"
    assert profile.evidence.checked == (
        "greenhouse:board_content",
        "greenhouse:absolute_url",
        "greenhouse:hosted_page",
    )
    assert requested(router) == [
        f"{GREENHOUSE_API}/acme",
        f"{GREENHOUSE_API}/acme/jobs",
        f"{GH_HOSTED}/acme",
    ]


def test_greenhouse_intro_naming_the_domain_needs_no_more_requests(
    router: respx.MockRouter, client: httpx.Client
) -> None:
    content = "&lt;p&gt;Learn more at go.acme.com or on our blog.&lt;/p&gt;"
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme", "content": content})
    profile = ownership_evidence(client, "greenhouse", "acme", DOMAIN, sleep=no_sleep)
    assert profile.evidence.matched == "greenhouse:board_content"
    assert requested(router) == [f"{GREENHOUSE_API}/acme"]


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("visit acme.com today", True),
        ("visit careers.acme.com/jobs", True),
        ("visit ACME.COM", True),
        ("visit acme.com.au", False),
        ("visit notacme.com", False),
        ("visit acme.community", False),
        ("visit acme-com.net", False),
    ],
)
def test_domain_mentions(
    router: respx.MockRouter, client: httpx.Client, text: str, found: bool
) -> None:
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme", "content": text})
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(200, json={"jobs": []})
    router.get(f"{GH_HOSTED}/acme").respond(200, text="<html></html>")
    profile = ownership_evidence(client, "greenhouse", "acme", DOMAIN, sleep=no_sleep)
    assert profile.evidence.found is found


def test_ashby_public_website(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get("https://jobs.ashbyhq.com/acme").mock(return_value=page("ashby_hosted_app.html"))
    profile = ownership_evidence(
        client, "ashby", "acme", DOMAIN, probe={"jobs": []}, sleep=no_sleep
    )
    assert (profile.name, profile.evidence.matched) == ("Acme Jobs", "ashby:hosted_page")
    assert requested(router) == ["https://jobs.ashbyhq.com/acme"]


def test_ashby_job_urls_from_the_probe(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get("https://jobs.ashbyhq.com/acme").mock(return_value=page("ashby_hosted.html"))
    probe = {"jobs": [{"jobUrl": "https://jobs.ashbyhq.com/acme/1"}, {"applyUrl": 3}, "x"]}
    probe["jobs"].append({"applyUrl": "https://www.acme.com/careers/apply?ashby_jid=1"})
    profile = ownership_evidence(client, "ashby", "acme", DOMAIN, probe=probe, sleep=no_sleep)
    assert profile.evidence.matched == "ashby:job_urls"
    assert requested(router) == ["https://jobs.ashbyhq.com/acme"]


def test_ashby_without_a_probe_reads_the_posting_api(
    router: respx.MockRouter, client: httpx.Client
) -> None:
    router.get("https://jobs.ashbyhq.com/acme").respond(503)
    router.get(f"{ASHBY_API}/acme").respond(200, json={"jobs": []})
    profile = ownership_evidence(client, "ashby", "acme", DOMAIN, sleep=no_sleep)
    evidence = profile.evidence
    assert (evidence.found, evidence.conclusive) == (False, False)
    assert evidence.failed == ("ashby:hosted_page",)
    assert evidence.checked == ("ashby:job_urls",)


def test_lever_hosted_page_links(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get("https://jobs.eu.lever.co/acme").mock(return_value=page("lever_hosted_site.html"))
    profile = ownership_evidence(client, "lever", "eu:acme", DOMAIN, sleep=no_sleep)
    assert (profile.name, profile.evidence.matched) == ("Acme", "lever:hosted_page")


def test_smartrecruiters_without_a_probe_fetches_the_name(
    router: respx.MockRouter, client: httpx.Client
) -> None:
    postings = {"totalFound": 1, "content": [{"company": {"identifier": "acme", "name": "Acme"}}]}
    router.get(f"{SMARTRECRUITERS_API}/acme/postings").respond(200, json=postings)
    router.get("https://careers.smartrecruiters.com/acme").mock(
        return_value=page("smartrecruiters_careers.html")
    )
    profile = ownership_evidence(client, "smartrecruiters", "acme", DOMAIN, sleep=no_sleep)
    assert (profile.name, profile.evidence.matched) == ("Acme", "smartrecruiters:careers_page")
    assert profile.evidence.checked == ("smartrecruiters:postings", "smartrecruiters:careers_page")


def test_other_sites_are_kept_for_the_reviewer(
    router: respx.MockRouter, client: httpx.Client
) -> None:
    links = (
        "".join(f'<a href="https://site{i}.org/">s</a>' for i in range(8))
        + '<a href="https://www.lever.co/">Lever</a><a href="http://[::1">bad</a>'
    )
    router.get("https://jobs.lever.co/acme").respond(200, text=f"<html><body>{links}</body></html>")
    evidence = ownership_evidence(client, "lever", "acme", DOMAIN, sleep=no_sleep).evidence
    assert evidence.sites == tuple(f"site{i}.org" for i in range(5))
    assert evidence.payload() == {
        "evidence": None,
        "evidence_checked": ["lever:hosted_page"],
        "evidence_failed": [],
        "board_sites": [f"site{i}.org" for i in range(5)],
    }


def test_ownership_evidence_is_only_for_supported_vendors(client: httpx.Client) -> None:
    with pytest.raises(ValueError, match="workday"):
        ownership_evidence(client, "workday", "acme.wd5/x", DOMAIN)


def test_guess_rejection_reasons() -> None:
    found = OwnershipEvidence(DOMAIN, matched="lever:hosted_page", checked=("lever:hosted_page",))
    missing = OwnershipEvidence(DOMAIN, checked=("lever:hosted_page",))
    assert guess_rejection(100.0, found) is None
    assert guess_rejection(100.0, missing) == GUESS_EVIDENCE_REASON
    assert guess_rejection(50.0, found) == "name_mismatch"
    assert missing.conclusive
    assert not OwnershipEvidence(DOMAIN).conclusive


# --- discovery ------------------------------------------------------------------------------


def test_guess_without_domain_evidence_goes_to_review_paused(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    greenhouse_guess(router)
    router.get(f"{GH_HOSTED}/acme").respond(
        200, text='<html><body><a class="logo" href="https://www.globex.com/">x</a></body></html>'
    )

    result = discover(db_session, client)

    assert (result.outcome, result.confidence, result.ratio) == ("review", GUESS_CONFIDENCE, 100.0)
    [board] = boards(db_session)
    assert (board.vendor, board.identifier, board.status) == ("greenhouse", "acme", "paused")
    assert board.last_verified_at == NOW
    [item] = reviews(db_session)
    assert (item.kind, item.status) == ("board_verify", "open")
    assert item.payload == {
        "domain": DOMAIN,
        "vendor": "greenhouse",
        "identifier": "acme",
        "board_name": "Acme",
        "ratio": 100.0,
        "discovered_from": f"{GREENHOUSE_API}/acme/jobs",
        "reason": GUESS_EVIDENCE_REASON,
        "evidence": None,
        "evidence_checked": [
            "greenhouse:board_content",
            "greenhouse:absolute_url",
            "greenhouse:hosted_page",
        ],
        "evidence_failed": [],
        "board_sites": ["globex.com"],
    }
    # A rerun reuses the item and the paused row; the company stays out of new batches.
    assert discover(db_session, client).outcome == "review"
    assert len(reviews(db_session)) == 1
    assert len(boards(db_session)) == 1
    assert select_new_companies(db_session, 10) == []


def test_guess_with_a_job_on_the_company_domain_is_activated(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    greenhouse_guess(router, jobs=[{"id": 1, "absolute_url": "https://www.acme.com/jobs?gh_jid=1"}])

    result = discover(db_session, client)

    assert (result.outcome, result.confidence) == ("board", GUESS_CONFIDENCE)
    [board] = boards(db_session)
    assert (board.status, board.confidence) == ("active", Decimal("0.60"))
    assert reviews(db_session) == []
    # The probe's job list is reused: no second /jobs request, no hosted page.
    assert requested(router).count(f"{GREENHOUSE_API}/acme/jobs") == 1
    assert f"{GH_HOSTED}/acme" not in requested(router)


def test_rejected_guess_pauses_the_companys_active_board(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """Re-verification of a board activated under the old name-only rule."""
    company = Company(domain=DOMAIN, name="Acme")
    db_session.add(company)
    db_session.flush()
    db_session.add(
        AtsBoard(
            company_id=company.id,
            vendor="greenhouse",
            identifier="acme",
            confidence=GUESS_CONFIDENCE,
            status="active",
            last_verified_at=NOW - timedelta(days=40),
        )
    )
    db_session.flush()
    site(router)
    greenhouse_guess(router)
    router.get(f"{GH_HOSTED}/acme").respond(404)

    result = discover(db_session, client, name="Acme")

    assert result.outcome == "review"
    [board] = boards(db_session)
    assert (board.status, board.last_verified_at) == ("paused", NOW)
    [item] = reviews(db_session)
    assert item.payload["reason"] == GUESS_EVIDENCE_REASON
    assert item.payload["evidence_failed"] == ["greenhouse:hosted_page"]


def test_rejected_guess_pauses_an_unowned_board_without_claiming_it(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """An orphan or hand-added row (no company) is not left polling after a failed guess."""
    db_session.add(
        AtsBoard(
            company_id=None,
            vendor="greenhouse",
            identifier="acme",
            confidence=GUESS_CONFIDENCE,
            status="active",
        )
    )
    db_session.flush()
    site(router)
    greenhouse_guess(router)
    router.get(f"{GH_HOSTED}/acme").respond(404)

    result = discover(db_session, client, name="Acme")

    assert result.outcome == "review"
    [board] = boards(db_session)
    assert (board.company_id, board.status, board.last_verified_at) == (None, "paused", NOW)
    [item] = reviews(db_session)
    assert item.payload["reason"] == GUESS_EVIDENCE_REASON


def _human_activated(session: Session) -> AtsBoard:
    """A guessed board a person activated after resolving its evidence review item."""
    company = Company(domain=DOMAIN, name="Acme")
    session.add(company)
    session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor="greenhouse",
        identifier="acme",
        confidence=GUESS_CONFIDENCE,
        status="active",
        last_verified_at=NOW - timedelta(days=40),
    )
    session.add(board)
    session.add(
        ReviewQueueItem(
            kind="board_verify",
            status="resolved",
            payload={
                "domain": DOMAIN,
                "vendor": "greenhouse",
                "identifier": "acme",
                "reason": GUESS_EVIDENCE_REASON,
            },
            created_at=NOW - timedelta(days=45),
            resolved_at=NOW - timedelta(days=41),
        )
    )
    session.flush()
    return board


def test_a_board_a_person_confirmed_is_not_paused_again(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    _human_activated(db_session)
    site(router)
    greenhouse_guess(router)
    router.get(f"{GH_HOSTED}/acme").respond(200, text="<html></html>")

    result = discover(db_session, client, name="Acme")

    assert result.outcome == "board"
    [board] = boards(db_session)
    assert (board.status, board.last_verified_at) == ("active", NOW)
    assert [item.status for item in reviews(db_session)] == ["resolved"]


def test_a_review_discovery_resolved_itself_is_not_a_confirmation(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    _human_activated(db_session)
    [item] = reviews(db_session)
    item.payload = {**item.payload, "resolved_by": "discovery"}
    db_session.flush()
    site(router)
    greenhouse_guess(router)
    router.get(f"{GH_HOSTED}/acme").respond(200, text="<html></html>")

    assert discover(db_session, client, name="Acme").outcome == "review"
    assert boards(db_session)[0].status == "paused"


def test_evidence_found_later_resolves_the_review(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    greenhouse_guess(router)
    hosted = router.get(f"{GH_HOSTED}/acme")
    hosted.respond(200, text="<html></html>")
    assert discover(db_session, client).outcome == "review"
    hosted.mock(return_value=page("greenhouse_hosted.html"))

    # Paused boards are not re-verified by batches; a manual rerun of discovery finds the
    # evidence, activates the board and closes the item it raised.
    result = discover(db_session, client)

    assert result.outcome == "board"
    assert boards(db_session)[0].status == "active"
    [item] = reviews(db_session)
    assert item.status == "resolved"
    assert item.payload["resolved_by"] == "discovery"


def test_page_linked_boards_need_no_domain_evidence(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    router.get(f"{BASE}/robots.txt").respond(404)
    router.get(f"{BASE}/careers").mock(return_value=page("greenhouse_link.html"))
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme", "content": ""})

    result = discover(db_session, client)

    assert (result.outcome, result.confidence) == ("board", PAGE_CONFIDENCE)
    assert f"{GH_HOSTED}/acme" not in requested(router)


# --- shared unsupported destinations (ownership collision) --------------------------------------


def gem_page(router: respx.MockRouter, base: str) -> None:
    router.get(f"{base}/robots.txt").respond(404)
    router.get(f"{base}/careers").respond(
        200, text='<a href="https://jobs.gem.com/sharedco">Open roles</a>'
    )


def test_two_companies_sharing_a_gem_board_both_get_an_outcome(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    gem_page(router, BASE)
    gem_page(router, "https://globex.com")
    first = discover(db_session, client)
    [owned] = boards(db_session)
    before = (owned.company_id, owned.discovered_from, owned.confidence, owned.last_verified_at)

    later = NOW + timedelta(hours=1)
    second = discover_domain(db_session, client, "globex.com", None, now=later, sleep=no_sleep)

    assert (first.outcome, first.identifier) == ("unsupported", "sharedco")
    assert (second.outcome, second.identifier) == ("unsupported", "sharedco#globex.com")
    owned_again, other = boards(db_session)
    # The first company's row is untouched...
    assert (
        owned_again.company_id,
        owned_again.discovered_from,
        owned_again.confidence,
        owned_again.last_verified_at,
    ) == before
    # ...and the second company has its own outcome row, so it is not picked again.
    globex = db_session.scalars(select(Company).where(Company.domain == "globex.com")).one()
    assert (other.company_id, other.vendor, other.status) == (globex.id, "gem", "unsupported")
    assert other.discovered_from == "https://globex.com/careers"
    assert other.last_verified_at == later
    assert select_new_companies(db_session, 10) == []

    # Reruns are idempotent for both companies.
    discover(db_session, client)
    discover_domain(db_session, client, "globex.com", None, now=later, sleep=no_sleep)
    assert [(b.identifier, b.company_id) for b in boards(db_session)] == [
        ("sharedco", owned.company_id),
        ("sharedco#globex.com", globex.id),
    ]


def test_writing_over_another_companys_row_is_refused(
    db_session: Session,
) -> None:
    mine = Company(domain=DOMAIN, name="Acme")
    other = Company(domain="globex.com", name="Globex")
    db_session.add_all([mine, other])
    db_session.flush()
    db_session.add(
        AtsBoard(
            company_id=other.id,
            vendor="gem",
            identifier="x",
            confidence=PAGE_CONFIDENCE,
            status="unsupported",
        )
    )
    db_session.flush()
    with pytest.raises(ValueError, match="belongs to company"):
        ats._write_board(
            db_session,
            mine,
            vendor="gem",
            identifier="x",
            discovered_from="",
            confidence=PAGE_CONFIDENCE,
            status="unsupported",
            now=NOW,
        )


# --- reverify-guesses -----------------------------------------------------------------------


def _guessed(
    session: Session,
    domain: str,
    vendor: str,
    identifier: str,
    *,
    confidence: Decimal = GUESS_CONFIDENCE,
    status: str = "active",
    verified: datetime | None = None,
) -> AtsBoard:
    company = session.scalars(select(Company).where(Company.domain == domain)).first()
    if company is None:
        company = Company(domain=domain, name=domain_label(domain).title())
        session.add(company)
        session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=vendor,
        identifier=identifier,
        confidence=confidence,
        status=status,
        discovered_from=f"guess:{identifier}",
        last_verified_at=verified,
    )
    session.add(board)
    session.flush()
    return board


def _audit_case(domain: str, vendor: str) -> dict[str, Any]:
    return next(c for c in AUDIT["cases"] if c["domain"] == domain and c["vendor"] == vendor)


def test_reverify_guesses_keeps_pauses_and_skips(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    kept = _guessed(db_session, "zapier.com", "ashby", "zapier")
    wrong = _guessed(db_session, "stream.claims", "ashby", "stream")
    flaky = _guessed(db_session, "tilt.bike", "ashby", "tilt")
    page_linked = _guessed(db_session, "acme.com", "lever", "acme", confidence=PAGE_CONFIDENCE)
    paused = _guessed(db_session, "globex.com", "lever", "globex", status="paused")
    for domain in ("zapier.com", "stream.claims"):
        for url, doc in _audit_case(domain, "ashby")["docs"].items():
            router.get(url).respond(
                200, **({"json": doc["json"]} if "json" in doc else {"text": doc["html"]})
            )
    router.get("https://jobs.ashbyhq.com/tilt").respond(503)
    router.get(f"{ASHBY_API}/tilt").respond(200, json={"jobs": []})

    budget = RateBudget(1000.0, sleep=no_sleep)
    result = reverify_guesses(db_session, client, now=NOW, budget=budget, sleep=no_sleep)

    assert result.outcomes == {"kept": 1, "paused": 1, "error": 1}
    by_id = {check.board_id: check for check in result.checks}
    assert set(by_id) == {kept.id, wrong.id, flaky.id}
    assert (kept.status, kept.last_verified_at) == ("active", NOW)
    assert by_id[kept.id].evidence == "ashby:hosted_page"
    assert (wrong.status, wrong.last_verified_at) == ("paused", NOW)
    assert by_id[wrong.id].reason == GUESS_EVIDENCE_REASON
    assert (flaky.status, flaky.last_verified_at) == ("active", None)
    assert by_id[flaky.id].reason == "inconclusive"
    assert page_linked.last_verified_at is None
    assert paused.last_verified_at is None
    [item] = reviews(db_session)
    assert item.payload["domain"] == "stream.claims"
    assert item.payload["board_sites"] == ["getstream.io"]
    assert by_id[wrong.id].review_id == item.id

    # Idempotent: the paused board is not selected again, the kept one only re-stamped.
    later = NOW + timedelta(days=1)
    again = reverify_guesses(db_session, client, now=later, budget=budget, sleep=no_sleep)
    assert sorted(check.board_id for check in again.checks) == sorted([kept.id, flaky.id])
    assert kept.last_verified_at == later
    assert len(reviews(db_session)) == 1


def test_reverify_guesses_name_mismatch_and_limit(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    older = _guessed(db_session, "acme.com", "lever", "acme", verified=NOW - timedelta(days=9))
    newer = _guessed(db_session, "globex.com", "lever", "globex", verified=NOW - timedelta(days=1))
    router.get("https://jobs.lever.co/acme").respond(
        200, text='<html><head><title>Initech</title></head><a href="https://acme.com">h</a></html>'
    )
    assert select_guessed_boards(db_session, 1) == [older]

    with caplog.at_level(logging.INFO, logger="tell.discovery"):
        result = reverify_guesses(
            db_session, client, now=NOW, limit=1, budget=RateBudget(1000.0), sleep=no_sleep
        )

    [check] = result.checks
    assert (check.board_id, check.outcome, check.reason) == (older.id, "paused", "name_mismatch")
    assert check.evidence == "lever:hosted_page"
    assert newer.last_verified_at == NOW - timedelta(days=1)
    [line] = [
        json.loads(r.getMessage()) for r in caplog.records if "reverify_guess" in r.getMessage()
    ]
    assert line["outcome"] == "paused"


def test_reverify_guesses_keeps_a_board_a_person_confirmed(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    board = _human_activated(db_session)
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme", "content": ""})
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(200, json={"jobs": []})
    router.get(f"{GH_HOSTED}/acme").respond(200, text="<html></html>")
    result = reverify_guesses(
        db_session, client, now=NOW, budget=RateBudget(1000.0), sleep=no_sleep
    )
    assert [check.outcome for check in result.checks] == ["kept"]
    assert (board.status, board.last_verified_at) == ("active", NOW)


def test_reverify_guesses_requires_an_aware_now(db_session: Session, client: httpx.Client) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        reverify_guesses(db_session, client, now=datetime(2026, 10, 14))


# --- CLI ------------------------------------------------------------------------------------


def test_cli_reverify_guesses(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, router: respx.MockRouter
) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    with SessionLocal(bind=engine) as session:
        _guessed(session, "stream.claims", "ashby", "stream")
        session.commit()
    for url, doc in _audit_case("stream.claims", "ashby")["docs"].items():
        router.get(url).respond(
            200, **({"json": doc["json"]} if "json" in doc else {"text": doc["html"]})
        )

    result = CliRunner().invoke(cli.app, ["reverify-guesses", "--limit", "5", "--rps", "100"])

    assert result.exit_code == 0, result.output
    *lines, summary = [json.loads(line) for line in result.output.splitlines()]
    assert [line["outcome"] for line in lines] == ["paused"]
    assert summary == {"checked": 1, "outcomes": {"error": 0, "kept": 0, "paused": 1}}
    with SessionLocal(bind=engine) as session:
        [board] = session.scalars(select(AtsBoard)).all()
        assert board.status == "paused"
