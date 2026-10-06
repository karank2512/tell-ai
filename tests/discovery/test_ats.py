"""ATS discovery (spec section 4): page scan, robots, redirects, guesses, verification, batches."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import utcnow
from tell.config import get_settings
from tell.discovery import ats, domain_label, registrable_domain
from tell.discovery.ats import (
    ASHBY_API,
    CANDIDATE_PATHS,
    GREENHOUSE_API,
    GUESS_CONFIDENCE,
    LEVER_API,
    PAGE_CONFIDENCE,
    SMARTRECRUITERS_API,
    BoardHit,
    DiscoveryResult,
    RobotsCache,
    board_name,
    choose_hit,
    discover_batch,
    discover_domain,
    find_boards,
    guess_slugs,
    hosted_board_url,
    name_ratio,
    reverify_due,
    select_due_boards,
    select_new_companies,
)
from tell.http import RateBudget, make_client
from tell.models import AtsBoard, Company, ReviewQueueItem, Run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DOMAIN = "acme.com"
BASE = "https://acme.com"
UA = "tell/0.1 (+https://github.com/karank2512/tell-ai)"
SR_EMPTY = {"offset": 0, "limit": 1, "totalFound": 0, "content": []}


def page(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


def redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


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


def site(
    router: respx.MockRouter,
    pages: dict[str, httpx.Response] | None = None,
    *,
    base: str = BASE,
    robots: httpx.Response | None = None,
) -> None:
    """Mock robots.txt and every candidate page; unspecified pages answer 404."""
    router.get(f"{base}/robots.txt").mock(return_value=robots or httpx.Response(404))
    for path in CANDIDATE_PATHS:
        response = (pages or {}).get(path, httpx.Response(404))
        router.get(f"{base}{path}").mock(return_value=response)


def no_guesses(router: respx.MockRouter, label: str = "acme") -> None:
    """Every slug guess misses, each vendor in its own way (404, empty list, zero total)."""
    for slug in (label, f"{label}inc", f"{label}-inc"):
        router.get(f"{GREENHOUSE_API}/{slug}/jobs").respond(404)
        router.get(f"{LEVER_API}/{slug}").respond(200, json=[])
        router.get(f"{ASHBY_API}/{slug}").respond(200, json={"apiVersion": "1", "jobs": []})
        router.get(f"{SMARTRECRUITERS_API}/{slug}/postings").respond(200, json=SR_EMPTY)


def greenhouse_name(router: respx.MockRouter, token: str = "acme", name: str = "Acme") -> None:
    router.get(f"{GREENHOUSE_API}/{token}").respond(200, json={"name": name, "content": ""})


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


# --- fixtures --------------------------------------------------------------------------------


def test_fixtures_are_small_and_scrubbed() -> None:
    for path in FIXTURES.iterdir():
        assert path.stat().st_size < 50_000, path.name
        assert "@" not in path.read_text(encoding="utf-8"), path.name


# --- patterns ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "vendor", "identifier", "source"),
    [
        ("greenhouse_link.html", "greenhouse", "acme", "a"),
        ("greenhouse_iframe.html", "greenhouse", "acme", "iframe"),
        ("greenhouse_script.html", "greenhouse", "acme", "script"),
        ("greenhouse_job_boards.html", "greenhouse", "acme", "a"),
        ("greenhouse_api.html", "greenhouse", "acme", "text"),
        ("greenhouse_grnh.html", "greenhouse", "7f3a2b1c8", "a"),
        ("lever.html", "lever", "acme", "a"),
        ("lever_eu.html", "lever", "eu:acme", "a"),
        ("ashby_script.html", "ashby", "acme", "script"),
        ("ashby_api.html", "ashby", "acme", "text"),
        ("smartrecruiters.html", "smartrecruiters", "AcmeCorp", "a"),
        ("workday.html", "workday", "acme.wd5/AcmeCareers", "a"),
        ("oracle.html", "oracle", "ekzt.fa.us2.oraclecloud.com", "a"),
        ("workable.html", "workable", "acme", "a"),
    ],
)
def test_every_pattern_extracts_its_identifier(
    fixture: str, vendor: str, identifier: str, source: str
) -> None:
    hits = find_boards((FIXTURES / fixture).read_bytes())
    assert len(hits) == 1
    assert (hits[0].vendor, hits[0].identifier, hits[0].source) == (vendor, identifier, source)
    assert hits[0].pattern


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://boards-api.greenhouse.io/v1/boards/acme", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/embed/job_board?for=acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_board?b=x&amp;for=acme", ("greenhouse", "acme")),
        ("https://JOBS.LEVER.CO/acme.", ("lever", "acme")),
        ("https://jobs.ashbyhq.com/Acme%20Labs/embed", ("ashby", "Acme Labs")),
        ("https://api.ashbyhq.com/posting-api/job-board/acme", ("ashby", "acme")),
        ("https://Acme.wd1.myworkdayjobs.com/External", ("workday", "acme.wd1/External")),
        ("https://careers.smartrecruiters.com/AcmeCorp/", ("smartrecruiters", "AcmeCorp")),
    ],
)
def test_pattern_variants(text: str, expected: tuple[str, str]) -> None:
    hits = find_boards(f'<script>var u = "{text}";</script>')
    assert [(h.vendor, h.identifier) for h in hits] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "https://boards.greenhouse.io/embed/job_board",
        "https://apply.workable.com/j/ABC123",
        "https://apply.workable.com/api/v1/widget",
        "https://notboards.greenhouse.io/acme",
        "https://myjobs.lever.co/acme",
        "https://acme.wd9.myworkdayjobs.com/External",
        "https://acme.com/hcmUI/Other",
    ],
)
def test_lookalikes_do_not_match(text: str) -> None:
    assert find_boards(f'<a href="{text}">x</a>') == []


def test_no_board_page_and_garbage_have_no_hits() -> None:
    assert find_boards((FIXTURES / "no_board.html").read_bytes()) == []
    assert find_boards(b"") == []
    assert find_boards("   ") == []


def test_redirect_targets_come_first_and_hits_are_deduplicated() -> None:
    content = b'<a href="https://jobs.lever.co/acme">a</a> https://jobs.lever.co/acme/123'
    hits = find_boards(content, redirects=["https://boards.greenhouse.io/acme"])
    assert [(h.vendor, h.identifier, h.source) for h in hits] == [
        ("greenhouse", "acme", "redirect"),
        ("lever", "acme", "a"),
    ]


def test_choose_hit_prefers_supported_vendors() -> None:
    hits = find_boards((FIXTURES / "mixed.html").read_bytes())
    assert [h.vendor for h in hits] == ["workday", "lever"]
    chosen = choose_hit(hits)
    assert chosen is not None
    assert (chosen.vendor, chosen.identifier) == ("lever", "acme")
    workday = BoardHit("workday", "acme.wd1/External", "x", "a")
    assert choose_hit([workday]) == workday
    assert choose_hit([]) is None


def test_choose_hit_ranks_grnh_se_short_links_last() -> None:
    hits = find_boards((FIXTURES / "greenhouse_grnh_mixed.html").read_bytes())
    assert [(h.pattern, h.identifier) for h in hits] == [
        ("grnh_se", "7f3a2b1c8"),
        ("workday", "acme.wd5/AcmeCareers"),
        ("lever", "acme"),
    ]
    chosen = choose_hit(hits)
    assert chosen is not None
    assert (chosen.vendor, chosen.identifier) == ("lever", "acme")
    # Below unsupported vendors too: a short link is not a board token.
    chosen = choose_hit(hits[:2])
    assert chosen is not None
    assert chosen.vendor == "workday"
    assert choose_hit(hits[:1]) == hits[0]


def test_guess_slugs_and_domain_helpers() -> None:
    assert guess_slugs("acme.com") == ("acme", "acmeinc", "acme-inc")
    assert guess_slugs("acme.co.uk") == ("acme", "acmeinc", "acme-inc")
    assert guess_slugs("") == ()
    assert registrable_domain("https://www.Acme.com/careers") == "acme.com"
    assert registrable_domain("acme.vercel.app") == "acme.vercel.app"
    assert registrable_domain("localhost") is None
    assert registrable_domain("  ") is None
    assert domain_label("jobs.acme.co.uk") == "acme"


def test_name_ratio() -> None:
    assert name_ratio("Acme", ["acme"]) == 100.0
    assert name_ratio("Acme Jobs", ["acme"]) == 100.0
    assert name_ratio("Acme, Inc.", ["acme"]) == 100.0
    assert name_ratio("Globex Corporation", ["acme", "Acme"]) < 85
    assert name_ratio("Acme", ["getacme", None, "Acme"]) == 100.0
    assert name_ratio("Acme", ["getacme"]) < 85
    assert name_ratio(None, ["acme"]) == 0.0
    assert name_ratio("!!!", ["acme"]) == 0.0
    assert name_ratio("Acme", [None, ""]) == 0.0


def test_hosted_board_urls() -> None:
    assert hosted_board_url("greenhouse", "acme") == "https://job-boards.greenhouse.io/acme"
    assert hosted_board_url("lever", "acme") == "https://jobs.lever.co/acme"
    assert hosted_board_url("lever", "eu:acme") == "https://jobs.eu.lever.co/acme"
    assert hosted_board_url("ashby", "Acme Labs") == "https://jobs.ashbyhq.com/Acme%20Labs"
    assert hosted_board_url("smartrecruiters", "X") == "https://careers.smartrecruiters.com/X"
    with pytest.raises(ValueError, match="workday"):
        hosted_board_url("workday", "acme.wd5/x")


# --- robots ------------------------------------------------------------------------------------


def test_robots_rules_are_parsed(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get(f"{BASE}/robots.txt").mock(return_value=page("robots_disallow_careers.txt"))
    robots = RobotsCache(client, user_agent=UA)
    assert robots.allowed(f"{BASE}/careers") is False
    assert robots.allowed(f"{BASE}/jobs") is True
    assert robots.allowed(f"{BASE}/admin/x") is False
    assert len(router.calls) == 1  # cached per origin


@pytest.mark.parametrize(
    ("response", "allowed"),
    [
        (httpx.Response(404), True),
        (httpx.Response(410), True),
        (httpx.Response(401), False),
        (httpx.Response(403), False),
        (httpx.Response(503), False),
        (httpx.Response(200, text=""), True),
    ],
)
def test_robots_status_rules(
    router: respx.MockRouter, client: httpx.Client, response: httpx.Response, allowed: bool
) -> None:
    router.get(f"{BASE}/robots.txt").mock(return_value=response)
    assert RobotsCache(client, user_agent=UA).allowed(f"{BASE}/careers") is allowed


def test_robots_unreachable_is_disallow_all(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get(f"{BASE}/robots.txt").mock(side_effect=httpx.ConnectError("down"))
    assert RobotsCache(client, user_agent=UA).allowed(f"{BASE}/careers") is False


def test_robots_follows_redirects(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get(f"{BASE}/robots.txt").mock(return_value=redirect("https://www.acme.com/robots.txt"))
    router.get("https://www.acme.com/robots.txt").mock(
        return_value=page("robots_disallow_careers.txt")
    )
    assert RobotsCache(client, user_agent=UA).allowed(f"{BASE}/careers") is False


def test_robots_blocked_page_is_skipped_and_recorded(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(
        router,
        {"/careers": page("lever.html"), "/jobs": page("greenhouse_link.html")},
        robots=page("robots_disallow_careers.txt"),
    )
    greenhouse_name(router)
    result = discover(db_session, client)
    assert f"{BASE}/careers" not in requested(router)
    assert result.pages_checked == ("robots_blocked:/careers", "/jobs")
    assert (result.outcome, result.vendor, result.identifier) == ("board", "greenhouse", "acme")


def test_robots_disallow_all_falls_through_to_guesses_and_no_board(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, robots=httpx.Response(403))
    no_guesses(router)
    result = discover(db_session, client)
    assert result.outcome == "no_board"
    assert result.pages_checked == tuple(f"robots_blocked:{p}" for p in CANDIDATE_PATHS)
    assert not [u for u in requested(router) if u.startswith(BASE) and "robots" not in u]


# --- page order and redirects ---------------------------------------------------------------


def test_pages_in_order_and_stop_at_first_200(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/jobs": page("greenhouse_link.html"), "/join": page("lever.html")})
    greenhouse_name(router)
    result = discover(db_session, client)

    assert requested(router) == [
        f"{BASE}/robots.txt",
        f"{BASE}/careers",
        f"{BASE}/jobs",
        f"{GREENHOUSE_API}/acme",
    ]
    assert result.pages_checked == ("/careers", "/jobs")
    assert result.outcome == "board"
    assert result.confidence == PAGE_CONFIDENCE
    assert result.discovered_from == f"{BASE}/jobs"
    assert (result.board_name, result.ratio) == ("Acme", 100.0)

    [board] = boards(db_session)
    assert (board.vendor, board.identifier, board.status) == ("greenhouse", "acme", "active")
    assert board.confidence == Decimal("0.95")
    assert board.discovered_from == f"{BASE}/jobs"
    assert board.last_verified_at == NOW
    company = db_session.get(Company, board.company_id)
    assert company is not None
    assert (company.domain, company.name) == (DOMAIN, DOMAIN)


def test_homepage_is_the_last_candidate(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/": page("ashby_script.html")})
    router.get("https://jobs.ashbyhq.com/acme").mock(return_value=page("ashby_hosted.html"))
    result = discover(db_session, client, name="Acme")
    assert result.pages_checked == CANDIDATE_PATHS
    assert result.discovered_from == f"{BASE}/"
    assert (result.outcome, result.vendor, result.board_name) == ("board", "ashby", "Acme Jobs")


def test_two_redirects_are_followed(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": redirect("https://www.acme.com/careers")})
    router.get("https://www.acme.com/robots.txt").respond(404)
    router.get("https://www.acme.com/careers").mock(return_value=redirect("/careers/", 308))
    router.get("https://www.acme.com/careers/").mock(return_value=page("lever.html"))
    router.get("https://jobs.lever.co/acme").mock(return_value=page("lever_hosted.html"))

    result = discover(db_session, client)
    assert result.outcome == "board"
    assert (result.vendor, result.identifier) == ("lever", "acme")
    assert result.discovered_from == "https://www.acme.com/careers/"
    assert result.pages_checked == ("/careers",)


def test_a_third_redirect_is_not_followed(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(
        router,
        {"/careers": redirect(f"{BASE}/careers/a"), "/jobs": page("greenhouse_link.html")},
    )
    router.get(f"{BASE}/careers/a").mock(return_value=redirect(f"{BASE}/careers/b"))
    router.get(f"{BASE}/careers/b").mock(return_value=redirect(f"{BASE}/careers/c"))
    final = router.get(f"{BASE}/careers/c").mock(return_value=page("lever.html"))
    greenhouse_name(router)

    result = discover(db_session, client)
    assert not final.called
    assert result.pages_checked == ("/careers", "/jobs")
    assert (result.vendor, result.identifier) == ("greenhouse", "acme")


def test_redirect_to_a_board_is_a_hit_without_fetching_it(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": redirect("https://boards.greenhouse.io/acme", 302)})
    greenhouse_name(router)
    result = discover(db_session, client)
    assert "https://boards.greenhouse.io/acme" not in requested(router)
    assert (result.outcome, result.vendor, result.identifier) == ("board", "greenhouse", "acme")
    assert result.confidence == PAGE_CONFIDENCE
    assert result.discovered_from == f"{BASE}/careers"


def test_redirect_into_a_robots_blocked_path_stops(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(
        router,
        {"/jobs": redirect(f"{BASE}/careers/all"), "/join": page("greenhouse_link.html")},
        robots=page("robots_disallow_careers.txt"),
    )
    blocked = router.get(f"{BASE}/careers/all").mock(return_value=page("lever.html"))
    greenhouse_name(router)
    result = discover(db_session, client)
    assert not blocked.called
    assert result.pages_checked == ("robots_blocked:/careers", "/jobs", "/join")
    assert result.vendor == "greenhouse"


def test_transport_errors_and_empty_pages(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/jobs": httpx.Response(200, content=b"")})
    router.get(f"{BASE}/careers").mock(side_effect=httpx.ConnectTimeout("slow"))
    no_guesses(router)
    result = discover(db_session, client)
    # /careers failed, /jobs answered 200 with nothing on it: scanning stops there.
    assert result.pages_checked == ("/careers", "/jobs")
    assert result.outcome == "no_board"


# --- slug guesses -----------------------------------------------------------------------------


def test_guesses_run_in_order_only_after_the_page_scan_fails(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("no_board.html")})
    no_guesses(router)
    router.get(f"{LEVER_API}/acmeinc").respond(200, json=[{"id": "x1", "text": "Engineer"}])
    router.get("https://jobs.lever.co/acmeinc").mock(return_value=page("lever_hosted.html"))

    result = discover(db_session, client)
    api_calls = [u for u in requested(router) if not u.startswith(BASE)]
    assert api_calls == [
        f"{GREENHOUSE_API}/acme/jobs",
        f"{LEVER_API}/acme?mode=json&limit=1",
        f"{ASHBY_API}/acme",
        f"{SMARTRECRUITERS_API}/acme/postings?limit=1",
        f"{GREENHOUSE_API}/acmeinc/jobs",
        f"{LEVER_API}/acmeinc?mode=json&limit=1",
        "https://jobs.lever.co/acmeinc",
    ]
    assert result.outcome == "board"
    assert (result.vendor, result.identifier) == ("lever", "acmeinc")
    assert result.confidence == GUESS_CONFIDENCE
    assert result.discovered_from == f"{LEVER_API}/acmeinc"
    [board] = boards(db_session)
    assert board.confidence == Decimal("0.60")


def test_page_hit_never_guesses(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("lever.html")})
    router.get("https://jobs.lever.co/acme").mock(return_value=page("lever_hosted.html"))
    discover(db_session, client)
    assert not [u for u in requested(router) if "api." in u or "boards-api" in u]


def test_smartrecruiters_guess_takes_the_name_from_the_payload(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    no_guesses(router)
    router.get(f"{SMARTRECRUITERS_API}/acme/postings").respond(
        200,
        json={
            "offset": 0,
            "limit": 1,
            "totalFound": 12,
            "content": [{"id": "1", "company": {"identifier": "acme", "name": "Acme"}}],
        },
    )
    result = discover(db_session, client)
    assert (result.outcome, result.vendor, result.board_name) == (
        "board",
        "smartrecruiters",
        "Acme",
    )
    assert requested(router)[-1] == f"{SMARTRECRUITERS_API}/acme/postings?limit=1"


def test_guess_with_jobs_but_unverifiable_name_goes_to_review(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    no_guesses(router)
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(200, json={"jobs": [{"id": 1}], "meta": {}})
    greenhouse_name(router, name="Globex")
    result = discover(db_session, client)
    assert result.outcome == "review"
    assert result.confidence == GUESS_CONFIDENCE


def test_malformed_vendor_json_is_a_miss(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    no_guesses(router)
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(200, content=b"<html>not json</html>")
    router.get(f"{ASHBY_API}/acme").respond(200, json=["not", "a", "board"])
    router.get(f"{SMARTRECRUITERS_API}/acme/postings").respond(200, json=[1])
    router.get(f"{LEVER_API}/acmeinc").mock(side_effect=httpx.ConnectError("refused"))
    result = discover(db_session, client)
    assert result.outcome == "no_board"


# --- verification -----------------------------------------------------------------------------


def test_failed_verification_queues_review_and_does_not_activate(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("greenhouse_iframe.html")})
    greenhouse_name(router, name="Globex Corporation")
    result = discover(db_session, client)

    assert result.outcome == "review"
    [board] = boards(db_session)
    assert (board.vendor, board.identifier, board.status) == ("greenhouse", "acme", "paused")
    assert board.last_verified_at is None
    [item] = reviews(db_session)
    assert item.kind == "board_verify"
    assert item.status == "open"
    assert item.payload == {
        "domain": DOMAIN,
        "vendor": "greenhouse",
        "identifier": "acme",
        "board_name": "Globex Corporation",
        "ratio": result.ratio,
        "discovered_from": f"{BASE}/careers",
        "reason": "name_mismatch",
    }
    assert result.review_id == item.id
    assert result.ratio is not None
    assert result.ratio < 85

    # A rerun reuses the open item and the paused row.
    again = discover(db_session, client)
    assert again.outcome == "review"
    assert len(reviews(db_session)) == 1
    assert len(boards(db_session)) == 1


def test_company_name_rescues_a_label_mismatch(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("lever.html")}, base="https://getacme.com")
    router.get("https://jobs.lever.co/acme").mock(return_value=page("lever_hosted.html"))
    without = discover(db_session, client, "getacme.com")
    assert without.outcome == "review"
    with_name = discover(db_session, client, "getacme.com", "Acme")
    assert with_name.outcome == "board"
    [board] = boards(db_session)
    assert (board.status, board.last_verified_at) == ("active", NOW)
    [item] = reviews(db_session)
    assert (item.status, item.resolved_at) == ("resolved", NOW)


def test_unreachable_board_name_goes_to_review(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("greenhouse_link.html")})
    router.get(f"{GREENHOUSE_API}/acme").respond(503)
    result = discover(db_session, client)
    assert result.outcome == "review"
    assert (result.board_name, result.ratio) == (None, 0.0)


def test_board_owned_by_another_company_goes_to_review(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    other = Company(domain="acme-holdings.com", name="Acme Holdings")
    db_session.add(other)
    db_session.flush()
    db_session.add(
        AtsBoard(
            company_id=other.id,
            vendor="greenhouse",
            identifier="acme",
            confidence=Decimal("0.95"),
            status="active",
        )
    )
    db_session.flush()
    site(router, {"/careers": page("greenhouse_link.html")})
    greenhouse_name(router)

    result = discover(db_session, client)
    assert result.outcome == "review"
    [board] = boards(db_session)
    assert board.company_id == other.id
    [item] = reviews(db_session)
    assert item.payload["reason"] == "owned_by_other_company"
    # The open review item keeps the domain out of the next batch.
    assert [c.domain for c in select_new_companies(db_session, 10)] == []


def test_board_name_lookups(router: respx.MockRouter, client: httpx.Client) -> None:
    greenhouse_name(router)
    router.get("https://jobs.eu.lever.co/acme").mock(return_value=page("lever_hosted.html"))
    router.get("https://jobs.ashbyhq.com/acme").respond(404)
    router.get(f"{SMARTRECRUITERS_API}/AcmeCorp/postings").respond(200, json=SR_EMPTY)
    router.get(f"{GREENHOUSE_API}/blank").respond(200, json={"name": "  "})
    router.get("https://jobs.lever.co/untitled").respond(200, content=b"<html><body/></html>")
    router.get("https://jobs.lever.co/empty").respond(200, content=b"")
    assert board_name(client, "greenhouse", "acme") == "Acme"
    assert board_name(client, "lever", "eu:acme") == "Acme"
    assert board_name(client, "ashby", "acme") is None
    assert board_name(client, "smartrecruiters", "AcmeCorp") is None
    assert board_name(client, "greenhouse", "blank") is None
    assert board_name(client, "lever", "untitled") is None
    assert board_name(client, "lever", "empty") is None
    with pytest.raises(ValueError, match="workday"):
        board_name(client, "workday", "acme.wd5/x")


def test_board_name_transport_error_is_none(router: respx.MockRouter, client: httpx.Client) -> None:
    router.get("https://jobs.lever.co/acme").mock(side_effect=httpx.ConnectError("refused"))
    assert board_name(client, "lever", "acme", sleep=no_sleep) is None


# --- unsupported vendors and no_board ------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "vendor", "identifier"),
    [
        ("workday.html", "workday", "acme.wd5/AcmeCareers"),
        ("oracle.html", "oracle", "ekzt.fa.us2.oraclecloud.com"),
        ("workable.html", "workable", "acme"),
    ],
)
def test_unsupported_vendors_are_recorded_and_never_fetched(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    fixture: str,
    vendor: str,
    identifier: str,
) -> None:
    site(router, {"/careers": page(fixture)})
    result = discover(db_session, client)
    assert requested(router) == [f"{BASE}/robots.txt", f"{BASE}/careers"]
    assert (result.outcome, result.vendor, result.identifier) == ("unsupported", vendor, identifier)
    [board] = boards(db_session)
    assert (board.vendor, board.identifier, board.status) == (vendor, identifier, "unsupported")
    assert board.confidence == Decimal("0.95")
    assert board.last_verified_at == NOW


def test_no_board_row_lists_pages_checked_and_is_replaced_by_a_later_hit(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router)
    no_guesses(router)
    result = discover(db_session, client)
    assert result.outcome == "no_board"
    assert result.pages_checked == CANDIDATE_PATHS
    [row] = boards(db_session)
    assert (row.vendor, row.identifier, row.status) == ("none", DOMAIN, "no_board")
    assert row.discovered_from == "checked:/careers,/jobs,/join,/company/careers,/"
    assert row.confidence == Decimal("0.00")
    assert row.last_verified_at == NOW

    # Rerun: still one marker row.
    discover(db_session, client)
    assert len(boards(db_session)) == 1

    router.get(f"{BASE}/careers").mock(return_value=page("greenhouse_link.html"))
    greenhouse_name(router)
    found = discover(db_session, client)
    assert found.outcome == "board"
    assert [(b.vendor, b.identifier) for b in boards(db_session)] == [("greenhouse", "acme")]


def test_invalid_domain_and_naive_now(db_session: Session, client: httpx.Client) -> None:
    with pytest.raises(ValueError, match="registrable"):
        discover_domain(db_session, client, "localhost", now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        discover_domain(db_session, client, DOMAIN, now=datetime(2026, 10, 5))


def test_existing_company_is_reused_and_domain_normalised(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    company = Company(domain=DOMAIN, name="Acme")
    db_session.add(company)
    db_session.flush()
    site(router, {"/careers": page("workable.html")})
    result = discover(db_session, client, "https://www.ACME.com/about")
    assert result.domain == DOMAIN
    assert result.company_id == company.id
    assert db_session.scalar(select(func.count()).select_from(Company)) == 1


def test_every_result_is_one_json_log_line(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    site(router, {"/careers": page("workday.html")})
    caplog.set_level(logging.INFO, logger="tell.discovery")
    discover(db_session, client)
    lines = [r.getMessage() for r in caplog.records if r.name == "tell.discovery"]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "domain": DOMAIN,
        "outcome": "unsupported",
        "vendor": "workday",
        "identifier": "acme.wd5/AcmeCareers",
        "confidence": 0.95,
        "pages_checked": ["/careers"],
    }


# --- re-verification -----------------------------------------------------------------------------


def _board(**kwargs: Any) -> AtsBoard:
    defaults: dict[str, Any] = {
        "vendor": "greenhouse",
        "identifier": "acme",
        "confidence": Decimal("0.95"),
        "status": "active",
    }
    return AtsBoard(**{**defaults, **kwargs})


@freeze_time("2026-10-05 12:00:00", tz_offset=0)
def test_reverify_due() -> None:
    now = utcnow()
    assert not reverify_due(_board(last_verified_at=now - timedelta(days=29)), now)
    assert reverify_due(_board(last_verified_at=now - timedelta(days=30)), now)
    assert reverify_due(_board(last_verified_at=now - timedelta(days=45)), now)
    assert reverify_due(_board(last_verified_at=None), now)
    # Two consecutive 404s: every ATS collector marks the board dead. A dead board is
    # due when never verified, 30 days after its last verification, or once polled
    # after that verification; not again right after discovery has looked at it.
    assert not reverify_due(_board(status="dead", last_verified_at=now), now)
    assert not reverify_due(
        _board(status="dead", last_verified_at=now, last_polled_at=now - timedelta(hours=1)),
        now,
    )
    assert reverify_due(
        _board(status="dead", last_verified_at=now - timedelta(days=2), last_polled_at=now),
        now,
    )
    assert reverify_due(_board(status="dead", last_verified_at=None), now)
    assert not reverify_due(_board(status="dead", last_verified_at=now - timedelta(days=29)), now)
    assert reverify_due(_board(status="dead", last_verified_at=now - timedelta(days=30)), now)
    # A poll after verification only matters for dead boards.
    assert not reverify_due(
        _board(last_verified_at=now - timedelta(days=2), last_polled_at=now), now
    )
    # Paused boards wait for their review item.
    assert not reverify_due(_board(status="paused", last_verified_at=None), now)
    assert reverify_due(_board(status="no_board", last_verified_at=now - timedelta(days=31)), now)
    with pytest.raises(ValueError, match="timezone-aware"):
        reverify_due(_board(), datetime(2026, 10, 5))


def _company(session: Session, domain: str, age_days: int) -> Company:
    company = Company(domain=domain, name=domain, created_at=NOW - timedelta(days=age_days))
    session.add(company)
    session.flush()
    return company


def test_select_due_boards(db_session: Session) -> None:
    a = _company(db_session, "a.com", 5)
    b = _company(db_session, "b.com", 5)
    c = _company(db_session, "c.com", 5)
    d = _company(db_session, "d.com", 5)
    e = _company(db_session, "e.com", 5)
    old = _board(company_id=a.id, identifier="a", last_verified_at=NOW - timedelta(days=31))
    a_dup = _board(company_id=a.id, identifier="a2", last_verified_at=NOW - timedelta(days=40))
    fresh = _board(company_id=b.id, identifier="b", last_verified_at=NOW - timedelta(days=1))
    dead = _board(
        company_id=c.id,
        identifier="c",
        status="dead",
        last_verified_at=NOW - timedelta(days=1),
        last_polled_at=NOW - timedelta(hours=1),
    )
    paused = _board(company_id=d.id, identifier="d", status="paused")
    never = _board(company_id=e.id, vendor="workday", identifier="e", status="unsupported")
    orphan = _board(company_id=None, identifier="orphan")
    f = _company(db_session, "f.com", 5)
    checked_dead = _board(company_id=f.id, identifier="f", status="dead", last_verified_at=NOW)
    db_session.add_all([old, a_dup, fresh, dead, paused, never, orphan, checked_dead])
    db_session.flush()

    due = select_due_boards(db_session, NOW, 10)
    assert [board.identifier for board in due] == ["e", "a2", "c"]
    assert [board.identifier for board in select_due_boards(db_session, NOW, 2)] == ["e", "a2"]
    assert select_due_boards(db_session, NOW, 0) == []


def test_select_new_companies_oldest_first(db_session: Session) -> None:
    newest = _company(db_session, "new.com", 1)
    oldest = _company(db_session, "old.com", 9)
    middle = _company(db_session, "mid.com", 5)
    boarded = _company(db_session, "boarded.com", 20)
    db_session.add(_board(company_id=boarded.id, identifier="boarded", last_verified_at=NOW))
    db_session.flush()
    assert select_new_companies(db_session, 10) == [oldest, middle, newest]
    assert select_new_companies(db_session, 2) == [oldest, middle]
    assert select_new_companies(db_session, 0) == []


# --- batches ------------------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only moves when the rate budget sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_batch_respects_limit_and_rps(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    for domain, age in (("alpha.com", 9), ("bravo.com", 5), ("charlie.com", 1)):
        _company(db_session, domain, age)
        site(router, {"/careers": page("workday.html")}, base=f"https://{domain}")
    clock = FakeClock()

    batch = discover_batch(
        db_session, client, now=NOW, limit=2, rps=4.0, clock=clock, sleep=clock.sleep
    )

    assert [r.domain for r in batch.results] == ["alpha.com", "bravo.com"]
    assert not [u for u in requested(router) if "charlie" in u]
    assert (batch.new_domains, batch.reverified, batch.errors) == (2, 0, [])
    assert batch.outcomes == {"board": 0, "unsupported": 2, "review": 0, "no_board": 0}
    # Four requests (robots + /careers per domain) at 4 rps, burst 1: three 0.25 s waits.
    assert len(router.calls) == 4
    assert clock.sleeps == pytest.approx([0.25, 0.25, 0.25])

    run = db_session.scalars(select(Run).where(Run.run_id == batch.run_id)).one()
    assert (run.source, run.status, run.fetched, run.inserted, run.skipped, run.errors) == (
        "discovery",
        "ok",
        2,
        2,
        0,
        0,
    )
    assert run.notes == {
        "outcomes": {"board": 0, "unsupported": 2, "review": 0, "no_board": 0},
        "new_domains": 2,
        "reverified": 0,
    }


def test_batch_defaults_come_from_settings(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELL_DISCOVERY_BUDGET", "1")
    monkeypatch.setenv("TELL_DISCOVERY_RPS", "2")
    get_settings.cache_clear()
    for domain, age in (("alpha.com", 9), ("bravo.com", 5)):
        _company(db_session, domain, age)
        site(router, {"/careers": page("oracle.html")}, base=f"https://{domain}")
    clock = FakeClock()
    batch = discover_batch(db_session, client, now=NOW, clock=clock, sleep=clock.sleep)
    assert [r.domain for r in batch.results] == ["alpha.com"]
    assert clock.sleeps == pytest.approx([0.5])


def test_batch_reverifies_due_boards(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    stale = _company(db_session, "acme.com", 30)
    dead = _company(db_session, "dead.com", 30)
    db_session.add_all(
        [
            _board(company_id=stale.id, last_verified_at=NOW - timedelta(days=31)),
            _board(
                company_id=dead.id,
                identifier="deadco",
                status="dead",
                last_verified_at=NOW - timedelta(days=3),
                last_polled_at=NOW - timedelta(days=1),
            ),
        ]
    )
    db_session.flush()
    site(router, {"/careers": page("greenhouse_link.html")})
    greenhouse_name(router)
    site(router, {"/jobs": page("greenhouse_job_boards.html")}, base="https://dead.com")
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme"})

    batch = discover_batch(db_session, client, now=NOW, limit=0, reverify_limit=5, sleep=no_sleep)

    assert (batch.new_domains, batch.reverified) == (0, 2)
    assert {r.domain: r.outcome for r in batch.results} == {
        "dead.com": "review",
        "acme.com": "board",
    }
    rows = {b.identifier: b for b in boards(db_session)}
    assert rows["acme"].last_verified_at == NOW
    assert rows["acme"].status == "active"
    assert rows["deadco"].status == "dead"
    assert rows["deadco"].last_verified_at == NOW


def test_batch_reactivates_a_dead_board_found_again(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    company = _company(db_session, "acme.com", 30)
    db_session.add(
        _board(
            company_id=company.id,
            status="dead",
            last_verified_at=NOW - timedelta(days=2),
            last_polled_at=NOW - timedelta(days=1),
        )
    )
    db_session.flush()
    site(router, {"/careers": page("greenhouse_script.html")})
    greenhouse_name(router)
    batch = discover_batch(db_session, client, now=NOW, sleep=no_sleep)
    assert batch.reverified == 1
    [board] = boards(db_session)
    assert (board.status, board.last_verified_at) == ("active", NOW)


def test_batch_isolates_failures(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _company(db_session, "alpha.com", 9)
    _company(db_session, "bravo.com", 5)
    site(router, {"/careers": page("workable.html")}, base="https://bravo.com")
    real = ats.discover_domain

    def flaky(session: Session, client: httpx.Client, domain: str, *a: Any, **k: Any) -> Any:
        if domain == "alpha.com":
            msg = "boom"
            raise RuntimeError(msg)
        return real(session, client, domain, *a, **k)

    monkeypatch.setattr(ats, "discover_domain", flaky)
    batch = discover_batch(db_session, client, now=NOW, sleep=no_sleep)
    assert batch.errors == ["alpha.com"]
    assert [r.domain for r in batch.results] == ["bravo.com"]
    run = db_session.scalars(select(Run).where(Run.run_id == batch.run_id)).one()
    assert run.errors == 1


def test_batch_with_injected_budget(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    _company(db_session, "alpha.com", 9)
    site(router, {"/careers": page("workday.html")}, base="https://alpha.com")
    budget = RateBudget(1000.0, burst=100, clock=lambda: 0.0, sleep=no_sleep)
    batch = discover_batch(db_session, client, now=NOW, budget=budget)
    assert batch.outcomes["unsupported"] == 1


# --- regression: dead boards are not re-verified on every batch ------------------------------


def _dead_board(company_id: int, **kwargs: Any) -> AtsBoard:
    """A board two collector 404s marked dead: polled after its last verification."""
    defaults: dict[str, Any] = {
        "company_id": company_id,
        "vendor": "lever",
        "identifier": "oldacme",
        "status": "dead",
        "last_verified_at": NOW - timedelta(days=3),
        "last_polled_at": NOW - timedelta(days=1),
    }
    return _board(**{**defaults, **kwargs})


def _mock_outcome(router: respx.MockRouter, outcome: str) -> None:
    if outcome == "board":
        site(router, {"/careers": page("greenhouse_link.html")})
        greenhouse_name(router)
    elif outcome == "review":
        site(router, {"/careers": page("greenhouse_link.html")})
        greenhouse_name(router, name="Globex Corporation")
    elif outcome == "unsupported":
        site(router, {"/careers": page("workday.html")})
    else:
        site(router)
        no_guesses(router)


@pytest.mark.parametrize("outcome", ["board", "unsupported", "review", "no_board"])
def test_every_outcome_stamps_the_companys_dead_boards(
    db_session: Session, router: respx.MockRouter, client: httpx.Client, outcome: str
) -> None:
    company = _company(db_session, DOMAIN, 30)
    dead = _dead_board(company.id)
    db_session.add(dead)
    db_session.flush()
    assert reverify_due(dead, NOW)
    _mock_outcome(router, outcome)

    result = discover(db_session, client)

    assert result.outcome == outcome
    assert (dead.status, dead.last_verified_at) == ("dead", NOW)
    assert not reverify_due(dead, NOW)
    assert select_due_boards(db_session, NOW, 10) == []


def test_review_for_a_board_owned_elsewhere_stamps_the_no_board_marker(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    company = _company(db_session, DOMAIN, 30)
    other = _company(db_session, "acme-holdings.com", 30)
    marker = _board(
        company_id=company.id,
        vendor="none",
        identifier=DOMAIN,
        confidence=Decimal("0.00"),
        status="no_board",
        last_verified_at=NOW - timedelta(days=40),
    )
    owned = _board(company_id=other.id, identifier="acme", last_verified_at=NOW)
    db_session.add_all([marker, owned])
    db_session.flush()
    site(router, {"/careers": page("greenhouse_link.html")})
    greenhouse_name(router)

    result = discover(db_session, client)

    assert result.outcome == "review"
    assert (marker.status, marker.last_verified_at) == ("no_board", NOW)
    assert not reverify_due(marker, NOW)


def test_dead_board_is_reverified_once_then_waits_for_a_poll_or_30_days(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    company = _company(db_session, "dead.com", 30)
    dead = _dead_board(company.id, vendor="greenhouse", identifier="deadco")
    db_session.add(dead)
    db_session.flush()
    site(router, base="https://dead.com")
    no_guesses(router, "dead")
    budget = RateBudget(1000.0, burst=100, clock=lambda: 0.0, sleep=no_sleep)

    def run() -> int:
        batch = discover_batch(
            db_session,
            client,
            now=utcnow(),
            limit=0,
            reverify_limit=5,
            budget=budget,
            sleep=no_sleep,
        )
        return batch.reverified

    with freeze_time(NOW):
        assert run() == 1
    assert (dead.status, dead.last_verified_at) == ("dead", NOW)
    [marker] = [b for b in boards(db_session) if b.vendor == "none"]
    assert marker.last_verified_at == NOW
    calls = len(router.calls)

    # The next batches leave it alone: no requests, nothing re-verified.
    for days in (1, 29):
        with freeze_time(NOW + timedelta(days=days)):
            assert run() == 0
    assert len(router.calls) == calls

    # A poll after the verification (the collector saw another 404) makes it due again.
    dead.last_polled_at = NOW + timedelta(days=2)
    db_session.flush()
    with freeze_time(NOW + timedelta(days=3)):
        assert [b.id for b in select_due_boards(db_session, utcnow(), 10)] == [dead.id]

    # Without a newer poll, 30 days after the verification.
    dead.last_polled_at = NOW - timedelta(days=1)
    db_session.flush()
    with freeze_time(NOW + timedelta(days=29, hours=23)):
        assert select_due_boards(db_session, utcnow(), 10) == []
    with freeze_time(NOW + timedelta(days=30)):
        assert [b.company_id for b in select_due_boards(db_session, utcnow(), 10)] == [company.id]


# --- regression: grnh.se short links ---------------------------------------------------------

SHORT_LINK = "https://grnh.se/7f3a2b1c8"


@pytest.mark.parametrize(
    "location",
    [
        "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=abc",
        "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "https://boards.greenhouse.io/acme",
    ],
)
def test_grnh_se_hit_resolves_through_its_redirect(
    db_session: Session, router: respx.MockRouter, client: httpx.Client, location: str
) -> None:
    site(router, {"/careers": page("greenhouse_grnh.html")})
    router.get(SHORT_LINK).respond(301, headers={"Location": location})
    greenhouse_name(router)

    result = discover(db_session, client)

    assert (result.outcome, result.vendor, result.identifier) == ("board", "greenhouse", "acme")
    assert result.confidence == PAGE_CONFIDENCE
    assert result.discovered_from == f"{BASE}/careers"
    urls = requested(router)
    # One GET, redirect not followed, no slug guesses.
    assert urls.count(SHORT_LINK) == 1
    assert location not in urls
    assert not [u for u in urls if u.endswith("/jobs") or "api.lever" in u]
    [board] = boards(db_session)
    assert (board.identifier, board.status) == ("acme", "active")


def test_resolve_short_link_directly(router: respx.MockRouter, client: httpx.Client) -> None:
    hit = BoardHit("greenhouse", "7f3a2b1c8", "grnh.se/7f3a2b1c8", "a", "grnh_se")
    router.get(SHORT_LINK).respond(
        302, headers={"Location": "https://job-boards.greenhouse.io/acme"}
    )
    budget = RateBudget(1000.0, burst=100, clock=lambda: 0.0, sleep=no_sleep)
    resolved = ats.resolve_short_link(client, hit, budget=budget)
    assert resolved is not None
    assert (resolved.vendor, resolved.identifier, resolved.source, resolved.pattern) == (
        "greenhouse",
        "acme",
        "a",
        "greenhouse_board",
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>posting</html>"),
        httpx.Response(404),
        redirect("https://acme.com/careers?gh_jid=4012345"),
        redirect("https://notboards.greenhouse.io/acme"),
        redirect("https://acme.com/?next=https://boards.greenhouse.io/acme"),
        redirect("https://boards.greenhouse.io/embed/job_app?token=4012345"),
    ],
)
def test_unresolvable_grnh_se_falls_through_to_guesses_not_review(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    response: httpx.Response,
) -> None:
    site(router, {"/careers": page("greenhouse_grnh.html")})
    router.get(SHORT_LINK).mock(return_value=response)
    no_guesses(router)

    result = discover(db_session, client)

    assert result.outcome == "no_board"
    assert requested(router).count(SHORT_LINK) == 1
    assert f"{GREENHOUSE_API}/acme/jobs" in requested(router)
    assert reviews(db_session) == []
    assert [(b.vendor, b.status) for b in boards(db_session)] == [("none", "no_board")]


def test_grnh_se_transport_error_falls_through_to_a_guess(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("greenhouse_grnh.html")})
    router.get(SHORT_LINK).mock(side_effect=httpx.ConnectError("refused"))
    no_guesses(router)
    router.get(f"{GREENHOUSE_API}/acme/jobs").respond(200, json={"jobs": [{"id": 1}], "meta": {}})
    greenhouse_name(router)

    result = discover(db_session, client)

    assert (result.outcome, result.vendor, result.identifier) == ("board", "greenhouse", "acme")
    assert result.confidence == GUESS_CONFIDENCE
    assert reviews(db_session) == []
