"""The deeper page scan of ATS discovery (2026-10-14 recall audit).

When the first answering candidate page names no ATS, discovery also reads the homepage,
follows up to two same-site careers links (one hop) and, only when nothing at all was
found, up to five same-site JavaScript bundles. Further ATS vendors and YC Work at a
Startup are recorded as ``unsupported``; Notion, Google Docs/Forms and LinkedIn job
links are recorded on the ``no_board`` marker as hiring channels. Everything here is
mocked with ``respx`` against hand-crafted fixtures under ``tests/fixtures/discovery/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.discovery import ats
from tell.discovery.ats import (
    ASHBY_API,
    CANDIDATE_PATHS,
    GREENHOUSE_API,
    LEVER_API,
    LISTING_VENDORS,
    MAX_SCRIPTS,
    PAGE_CONFIDENCE,
    SMARTRECRUITERS_API,
    UNSUPPORTED_VENDORS,
    BoardHit,
    DiscoveryResult,
    channel_of,
    choose_hit,
    discover_domain,
    find_boards,
    find_boards_in_script,
)
from tell.http import make_client
from tell.models import AtsBoard

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DOMAIN = "acme.com"
BASE = "https://acme.com"
WWW = "https://www.acme.com"
SR_EMPTY = {"offset": 0, "limit": 1, "totalFound": 0, "content": []}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def page(name: str) -> httpx.Response:
    return httpx.Response(200, content=fixture(name))


def redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    # assert_all_mocked: any request this file did not expect (LinkedIn, Notion, YC, a
    # third-party script, a second-hop link) fails the test.
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
    robots: httpx.Response | None = None,
) -> None:
    """robots.txt plus every candidate page of acme.com; unspecified pages answer 404."""
    router.get(f"{BASE}/robots.txt").mock(return_value=robots or httpx.Response(404))
    for path in CANDIDATE_PATHS:
        router.get(f"{BASE}{path}").mock(return_value=(pages or {}).get(path, httpx.Response(404)))


def no_guesses(router: respx.MockRouter, label: str = "acme") -> None:
    for slug in (label, f"{label}inc", f"{label}-inc"):
        router.get(f"{GREENHOUSE_API}/{slug}/jobs").respond(404)
        router.get(f"{LEVER_API}/{slug}").respond(200, json=[])
        router.get(f"{ASHBY_API}/{slug}").respond(200, json={"apiVersion": "1", "jobs": []})
        router.get(f"{SMARTRECRUITERS_API}/{slug}/postings").respond(200, json=SR_EMPTY)


def requested(router: respx.MockRouter) -> list[str]:
    return [str(call.request.url) for call in router.calls]


def vendor_calls(router: respx.MockRouter) -> list[str]:
    hosts = ("api.lever.co", "api.ashbyhq.com", "boards-api.greenhouse", "api.smartrecruiters")
    return [url for url in requested(router) if any(host in url for host in hosts)]


def discover(session: Session, client: httpx.Client, name: str | None = None) -> DiscoveryResult:
    return discover_domain(session, client, DOMAIN, name, now=NOW, sleep=no_sleep)


def boards(session: Session) -> list[AtsBoard]:
    return list(session.scalars(select(AtsBoard).order_by(AtsBoard.id)))


# --- patterns ---------------------------------------------------------------------------------


UNSUPPORTED_CASES = [
    ("dover", "acmedover"),
    ("gem", "acmegem"),
    ("comeet", "acmecomeet"),
    ("recruitee", "acmerecruitee"),
    ("personio", "acmepersonio"),
    ("teamtailor", "acmetailor"),
    ("bamboohr", "acmebamboo"),
    ("rippling", "acmerippling"),
    ("breezy", "acmebreezy"),
    ("wellfound", "acmewellfound"),
    ("jobvite", "acmejobvite"),
    ("jazzhr", "acmejazz"),
    ("pinpoint", "acmepinpoint"),
    ("icims", "acmeicims"),
    ("homerun", "acmehomerun"),
    ("join", "acmejoin"),
    ("zohorecruit", "acmezoho"),
    ("polymer", "acmepolymer"),
    ("yc_waas", "acmeyc"),
]


@pytest.mark.parametrize(("vendor", "identifier"), UNSUPPORTED_CASES)
def test_every_unsupported_vendor_pattern(vendor: str, identifier: str) -> None:
    hits = find_boards(fixture("unsupported_vendors.html"))
    assert (vendor, identifier) in {(hit.vendor, hit.identifier) for hit in hits}
    assert vendor in UNSUPPORTED_VENDORS


def test_unsupported_fixture_covers_every_new_vendor() -> None:
    covered = {vendor for vendor, _ in UNSUPPORTED_CASES}
    assert covered == set(UNSUPPORTED_VENDORS) - {"workday", "oracle", "workable"}
    assert set(LISTING_VENDORS) == {"yc_waas"}


def test_comeet_embed_takes_the_company_uid() -> None:
    [hit] = find_boards(fixture("comeet_embed.html"))
    assert (hit.vendor, hit.identifier, hit.pattern) == ("comeet", "A1.000", "comeet_embed")


def test_further_supported_shapes() -> None:
    hits = {
        (hit.vendor, hit.identifier, hit.pattern)
        for hit in find_boards(fixture("supported_variants.html"))
    }
    assert hits == {
        ("lever", "acmeescaped", "lever"),
        ("greenhouse", "acmejobapp", "greenhouse_embed"),
        ("lever", "acmeleverapi", "lever_api"),
        ("smartrecruiters", "AcmeSR", "smartrecruiters_jobs"),
        ("smartrecruiters", "AcmeSRApi", "smartrecruiters_api"),
        ("ashby", "acmeunicode", "ashby_board"),
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://api.eu.lever.co/v0/postings/acme", ("lever", "eu:acme")),
        ("https://job-boards.greenhouse.io/embed/job_app?token=1&for=acme", ("greenhouse", "acme")),
        ("https://workatastartup.com/companies/acme", ("yc_waas", "acme")),
        ("https://app.dover.io/apply/acme/0000", ("dover", "acme")),
    ],
)
def test_further_pattern_variants(text: str, expected: tuple[str, str]) -> None:
    [hit] = find_boards(text)
    assert (hit.vendor, hit.identifier) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://www.ycombinator.com/companies/acme",  # the YC profile, not its jobs page
        "https://www.recruitee.com/pricing",
        "https://app.teamtailor.com/login",
        "https://careers.teamtailor.com/",
        "https://app.dover.com/login",
        "https://jobs.smartrecruiters.com/oneclick-ui/company/x",
        "https://notacme.homerun.company/",
        "COMEET.set('candidate-source-storage', true)",
    ],
)
def test_vendor_home_pages_and_lookalikes_do_not_match(text: str) -> None:
    assert find_boards(text) == []


def test_script_scan_undoes_json_escapes() -> None:
    [hit] = find_boards_in_script(fixture("bundle_ashby_api.js"))
    assert (hit.vendor, hit.identifier, hit.source) == ("ashby", "acme", "bundle")
    assert find_boards_in_script(fixture("bundle_plain.js")) == []
    [yc] = find_boards_in_script(fixture("bundle_yc_link.js").decode())
    assert (yc.vendor, yc.identifier) == ("yc_waas", "acme")


@pytest.mark.parametrize(
    ("url", "channel"),
    [
        ("https://acme.notion.site/Careers-0123", "notion"),
        ("https://www.notion.so/acme/We-re-Hiring-0123", "notion"),
        ("https://forms.gle/AbCd", "google_forms"),
        ("https://docs.google.com/forms/d/e/x/viewform", "google_forms"),
        ("https://docs.google.com/document/d/x/edit", "google_docs"),
        ("https://docs.google.com/spreadsheets/d/x", "google_docs"),
        ("https://www.linkedin.com/company/acme/jobs/", "linkedin"),
        ("https://in.linkedin.com/jobs/view/123", "linkedin"),
        ("https://www.linkedin.com/company/acme", None),
        ("https://docs.google.com/a/acme.com", None),
        ("https://acme.com/careers", None),
    ],
)
def test_channel_of(url: str, channel: str | None) -> None:
    assert channel_of(url) == channel


def test_choose_hit_ranks_ats_before_listings_before_short_links() -> None:
    yc = BoardHit("yc_waas", "acme", "", "a", "yc_company_jobs")
    workable = BoardHit("workable", "acme", "", "a", "workable")
    lever = BoardHit("lever", "acme", "", "a", "lever")
    short = BoardHit("greenhouse", "x1", "", "a", "grnh_se")
    assert choose_hit([short, yc, workable, lever]) == lever
    assert choose_hit([short, yc, workable]) == workable
    assert choose_hit([short, yc]) == yc
    assert choose_hit([short]) == short


# --- the deeper scan --------------------------------------------------------------------------


def test_yc_redirect_does_not_hide_the_homepage_board(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """/careers ends at the YC jobs page (never fetched); the homepage links the Ashby board."""
    site(router, {"/careers": redirect(f"{WWW}/careers"), "/": page("home_yc_and_ashby.html")})
    router.get(f"{WWW}/robots.txt").respond(404)
    router.get(f"{WWW}/careers").mock(
        return_value=redirect("https://www.ycombinator.com/companies/acme/jobs", 302)
    )
    router.get("https://jobs.ashbyhq.com/acmeinsure").mock(return_value=page("ashby_hosted.html"))

    result = discover(db_session, client)

    assert "ycombinator.com" not in " ".join(requested(router))
    assert result.outcome == "board"
    assert (result.vendor, result.identifier) == ("ashby", "acmeinsure")
    assert result.confidence == PAGE_CONFIDENCE
    assert result.discovered_from == f"{BASE}/"
    assert result.pages_checked == ("/careers", "/")
    assert vendor_calls(router) == []  # a page hit: no slug guesses


def test_a_yc_jobs_page_alone_is_recorded_unsupported_without_guessing(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    yc_only = b'<a href="https://www.ycombinator.com/companies/acme/jobs">Careers</a>'
    site(router, {"/careers": httpx.Response(200, content=yc_only)})

    result = discover(db_session, client)

    assert (result.outcome, result.vendor, result.identifier) == ("unsupported", "yc_waas", "acme")
    assert result.discovered_from == f"{BASE}/careers"
    assert result.pages_checked == ("/careers", "/")
    assert vendor_calls(router) == []
    [board] = boards(db_session)
    assert (board.vendor, board.identifier, board.status) == ("yc_waas", "acme", "unsupported")
    assert board.confidence == Decimal("0.95")


def test_an_ats_on_the_first_page_ends_the_scan(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("dover.html")})
    result = discover(db_session, client)
    assert (result.outcome, result.vendor, result.identifier) == ("unsupported", "dover", "acme")
    assert result.pages_checked == ("/careers",)
    assert requested(router) == [f"{BASE}/robots.txt", f"{BASE}/careers"]


def test_one_careers_link_is_followed_from_the_careers_page(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """A Comeet embed only on the position page that /careers links to."""
    site(router, {"/careers": page("careers_position_links.html")})
    position = router.get(f"{BASE}/careers/position-1a_001").mock(
        return_value=page("comeet_embed.html")
    )
    second = router.get(f"{BASE}/careers/position-2b_002").mock(return_value=page("no_board.html"))

    result = discover(db_session, client)

    assert position.called
    assert not second.called  # stops at the first ATS
    assert (result.outcome, result.vendor, result.identifier) == ("unsupported", "comeet", "A1.000")
    assert result.discovered_from == f"{BASE}/careers/position-1a_001"
    assert result.pages_checked == ("/careers", "/", "link:/careers/position-1a_001")
    assert vendor_calls(router) == []


def test_links_are_one_hop_same_site_and_careers_only(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/": page("home_careers_link.html")})
    team = router.get(f"{BASE}/company/team").mock(return_value=page("careers_position_links.html"))
    no_guesses(router)

    result = discover(db_session, client)

    # "Join us" is followed (fragment dropped); About/Blog, the off-site parent-company
    # link, mailto: and the #careers anchor on the homepage itself are not, and the
    # position links on the followed page are a second hop, never requested.
    assert team.call_count == 1
    assert result.outcome == "no_board"
    assert result.pages_checked == (*CANDIDATE_PATHS, "link:/company/team")
    [marker] = boards(db_session)
    assert marker.discovered_from == "checked:" + ",".join(result.pages_checked)
    assert result.channels == ()


def test_a_first_page_that_redirected_to_the_homepage_is_not_fetched_twice(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": redirect(f"{WWW}/", 302)})
    router.get(f"{WWW}/robots.txt").respond(404)
    home = router.get(f"{WWW}/").mock(return_value=page("no_board.html"))
    no_guesses(router)
    result = discover(db_session, client)
    assert home.call_count == 1
    assert result.pages_checked == ("/careers",)
    assert result.outcome == "no_board"


def test_robots_blocked_links_are_recorded(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /company/\n")
    site(router, {"/": page("home_careers_link.html")}, robots=robots)
    no_guesses(router)
    result = discover(db_session, client)
    assert result.pages_checked == (
        "/careers",
        "/jobs",
        "/join",
        "robots_blocked:/company/careers",
        "/",
        "robots_blocked:link:/company/team",
    )


# --- bundles -------------------------------------------------------------------------------------


def test_careers_bundle_is_scanned_first_and_third_party_scripts_never(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """The Ashby API URL is only in the careers page's own bundle."""
    site(router, {"/careers": page("careers_spa.html")})
    router.get(f"{WWW}/robots.txt").respond(404)
    bundle = f"{WWW}/_next/static/chunks/pages/careers-9d0e1f.js"
    router.get(bundle).mock(return_value=page("bundle_ashby_api.js"))
    main = router.get(f"{BASE}/_next/static/chunks/main-6a7b8c.js").respond(200, text="")
    router.get("https://jobs.ashbyhq.com/acme").mock(return_value=page("ashby_hosted.html"))

    result = discover(db_session, client)

    assert not main.called
    assert (result.outcome, result.vendor, result.identifier) == ("board", "ashby", "acme")
    assert result.confidence == PAGE_CONFIDENCE
    assert result.discovered_from == bundle
    assert result.pages_checked == (
        "/careers",
        "/",
        "script:/_next/static/chunks/pages/careers-9d0e1f.js",
    )


def test_bundles_are_capped_in_number_and_ranked(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/": page("spa_shell_many_scripts.html")})
    router.get("https://framerusercontent.com/robots.txt").respond(404)
    plain = page("bundle_plain.js")
    router.get(url__regex=r"https://acme\.com/assets/.*").mock(return_value=plain)
    router.get("https://framerusercontent.com/sites/abc123/script_main.mjs").mock(
        return_value=plain
    )
    no_guesses(router)

    result = discover(db_session, client)

    scripts = [entry for entry in result.pages_checked if entry.startswith("script:")]
    assert len(scripts) == MAX_SCRIPTS
    assert scripts == [
        "script:/assets/index-a1.js",
        "script:framerusercontent.com/sites/abc123/script_main.mjs",
        "script:/assets/chunk-b2.js",
        "script:/assets/chunk-c3.js",
        "script:/assets/chunk-d4.js",
    ]
    assert "https://framerusercontent.com/modules/xyz/other.js" not in requested(router)
    assert result.outcome == "no_board"


def test_bundle_bytes_are_capped(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = fixture("bundle_ashby_api.js")
    # The cap ends before the Ashby URL inside the bundle, so the board is not seen.
    monkeypatch.setattr(ats, "MAX_SCRIPT_BYTES", body.index(b"api.ashbyhq") - 1)
    site(router, {"/": page("spa_shell_many_scripts.html")})
    router.get("https://framerusercontent.com/robots.txt").respond(404)
    router.get(url__regex=r"https://acme\.com/assets/.*").mock(
        return_value=httpx.Response(200, content=body)
    )
    no_guesses(router)

    result = discover(db_session, client)

    assert [e for e in result.pages_checked if e.startswith("script:")] == [
        "script:/assets/index-a1.js"
    ]
    assert result.outcome == "no_board"


def test_failed_bundles_are_skipped(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("careers_spa.html")})
    router.get(f"{WWW}/robots.txt").respond(404)
    router.get(f"{WWW}/_next/static/chunks/pages/careers-9d0e1f.js").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    router.get(f"{BASE}/_next/static/chunks/main-6a7b8c.js").respond(404)
    router.get(f"{BASE}/_next/static/chunks/webpack-0a1b2c.js").respond(200, text="")
    router.get(f"{BASE}/_next/static/chunks/framework-3d4e5f.js").mock(
        return_value=page("bundle_yc_link.js")
    )
    result = discover(db_session, client)
    assert (result.outcome, result.vendor) == ("unsupported", "yc_waas")
    assert result.discovered_from == f"{BASE}/_next/static/chunks/framework-3d4e5f.js"
    assert vendor_calls(router) == []


def test_robots_blocked_bundles_are_recorded(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /assets/\n")
    site(router, {"/": page("spa_shell_many_scripts.html")}, robots=robots)
    router.get("https://framerusercontent.com/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /\n"
    )
    no_guesses(router)
    result = discover(db_session, client)
    assert "robots_blocked:script:/assets/index-a1.js" in result.pages_checked
    assert not [e for e in result.pages_checked if e.startswith("script:")]


def test_deeper_scan_requests_are_capped(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ats, "MAX_DEEP_REQUESTS", 2)
    site(router, {"/careers": page("careers_spa.html"), "/": redirect(f"{BASE}/home")})
    router.get(f"{BASE}/home").mock(return_value=redirect(f"{BASE}/home/"))
    router.get(f"{WWW}/robots.txt").respond(404)
    router.get(f"{WWW}/_next/static/chunks/pages/careers-9d0e1f.js").mock(
        return_value=page("bundle_plain.js")
    )
    no_guesses(router)

    result = discover(db_session, client)

    # Two deep requests: the homepage and one redirect hop (the meter leaves no room
    # for a second hop), then nothing for the bundles.
    deep = [u for u in requested(router) if u.startswith(BASE) and "/careers" not in u]
    assert deep == [f"{BASE}/robots.txt", f"{BASE}/", f"{BASE}/home"]
    assert result.pages_checked == ("/careers", "/")


# --- hiring channels -------------------------------------------------------------------------


def test_careers_link_to_notion_is_a_channel_and_skips_guesses(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/": page("careers_notion_link.html")})

    result = discover(db_session, client)

    # Only the careers link counts: the press-kit Notion page, the waitlist form and the
    # LinkedIn company page are not hiring channels.
    assert result.outcome == "no_board"
    assert result.channels == ("notion",)
    assert vendor_calls(router) == []
    [marker] = boards(db_session)
    assert (marker.vendor, marker.status) == ("none", "no_board")
    assert marker.discovered_from == "checked:/careers,/jobs,/join,/company/careers,/;via:notion"


def test_every_channel_on_a_careers_page_is_recorded(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": page("careers_linkedin_forms.html")})
    result = discover(db_session, client)
    assert result.channels == ("linkedin", "google_docs", "google_forms")
    assert result.discovered_from.endswith(";via:linkedin,google_docs,google_forms")
    assert vendor_calls(router) == []
    assert result.log_record()["channels"] == ["linkedin", "google_docs", "google_forms"]


def test_a_candidate_redirect_to_a_channel_is_never_fetched(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    site(router, {"/careers": redirect("https://www.linkedin.com/company/acme/jobs/", 302)})
    result = discover(db_session, client)
    assert not [u for u in requested(router) if "linkedin" in u]
    assert result.channels == ("linkedin",)
    assert result.pages_checked == ("/careers", "/")
    assert result.outcome == "no_board"


def test_a_channel_does_not_block_a_short_link_from_guessing(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    content = (
        b'<a href="https://grnh.se/abc123">Engineer</a>'
        b'<a href="https://acme.notion.site/Careers-1">Careers</a>'
    )
    site(router, {"/careers": httpx.Response(200, content=content)})
    router.get("https://grnh.se/abc123").respond(404)
    no_guesses(router)
    result = discover(db_session, client)
    assert result.channels == ("notion",)
    assert len(vendor_calls(router)) == 12  # the unresolved short link still guesses
    assert result.outcome == "no_board"


def test_odd_markup_and_an_exhausted_meter(
    db_session: Session,
    router: respx.MockRouter,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ats, "MAX_DEEP_REQUESTS", 1)
    home = b'<a href="">empty</a><a href="/jobs-board">Jobs</a>'
    site(
        router,
        {
            "/careers": httpx.Response(200, content=b"<!-- nothing -->"),
            "/": httpx.Response(200, content=home),
        },
    )
    link = router.get(f"{BASE}/jobs-board").respond(200, text="")
    no_guesses(router)
    result = discover(db_session, client)
    # The homepage used the only deep request: the careers link is never fetched.
    assert not link.called
    assert result.pages_checked == ("/careers", "/")
    assert result.outcome == "no_board"


def test_inline_and_non_http_scripts_are_skipped(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    home = (
        b"<script>var inline = 1;</script>"
        b'<script src="data:text/javascript,1"></script>'
        b'<script src="/app.js"></script>'
    )
    site(router, {"/": httpx.Response(200, content=home)})
    router.get(f"{BASE}/app.js").mock(return_value=page("bundle_plain.js"))
    no_guesses(router)
    result = discover(db_session, client)
    assert result.pages_checked == (*CANDIDATE_PATHS, "script:/app.js")


class _Slots:
    def acquire(self) -> float:
        return 0.0


def test_script_read_stops_at_the_wall_clock_deadline(
    router: respx.MockRouter, client: httpx.Client
) -> None:
    chunks = [b"var a = 1;", b"var b = 2;", b'"https://jobs.ashbyhq.com/acme"']
    router.get(f"{BASE}/drip.js").mock(return_value=httpx.Response(200, content=iter(chunks)))
    ticks = iter([0.0, 1.0, ats.SCRIPT_DEADLINE_S, 999.0])
    body = ats._get_script(
        client, f"{BASE}/drip.js", budget=_Slots(), limit=10_000, clock=lambda: next(ticks)
    )
    # Two chunks arrive before the deadline passes; the third is never read.
    assert body == b"var a = 1;var b = 2;"
