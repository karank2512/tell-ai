"""GitHub org collector: contract, event keys, flagship, pagination, ETags, rate limit, runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pydantic
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors import github_org
from tell.collectors.base import sha256_hex, start_run
from tell.collectors.github_org import (
    COLLECTOR,
    GITHUB_RELEASE,
    GITHUB_REPO_CREATED,
    GithubClient,
    GithubRelease,
    GithubReleases,
    GithubRepo,
    GithubRepos,
    OrgSummary,
    RateLimitStopError,
    configured_orgs,
    flagship,
    ingest,
    list_releases,
    list_repos,
    normalize_org,
    release_candidates,
    release_events,
    releases_url,
    repo_created_events,
    repos_url,
    run_fixture,
)
from tell.config import get_settings
from tell.models import Company, EventRow, Run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
REPOS = FIXTURES / "repos.json"
RELEASES = FIXTURES / "releases.json"
NORMAL = FIXTURES / "normal.json"
EMPTY = FIXTURES / "empty.json"
DRIFT = FIXTURES / "drift.json"

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)

ORG = "astral-sh"
REPOS_URL = "https://api.github.com/orgs/astral-sh/repos?sort=pushed&per_page=100"
UV_RELEASES = "https://api.github.com/repos/astral-sh/uv/releases?per_page=20"
RUFF_RELEASES = "https://api.github.com/repos/astral-sh/ruff/releases?per_page=20"
PAGE_2 = "https://api.github.com/organizations/115962839/repos?sort=pushed&per_page=100&page=2"
OTHER_ORG = "other-org"
OTHER_REPOS_URL = "https://api.github.com/orgs/other-org/repos?sort=pushed&per_page=100"

RELEASE_IDS = [402618784, 401449793, 399533040]
REPOS_ETAG = 'W/"repos-etag-1"'
UV_ETAG = 'W/"uv-etag-1"'
RUFF_ETAG = 'W/"ruff-etag-1"'


def _repos_raw() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(REPOS.read_text(encoding="utf-8"))
    return data


def _releases_raw() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(RELEASES.read_text(encoding="utf-8"))
    return data


def _repos() -> list[GithubRepo]:
    return GithubRepos.model_validate_json(REPOS.read_text(encoding="utf-8")).root


def _releases() -> list[GithubRelease]:
    return GithubReleases.model_validate_json(RELEASES.read_text(encoding="utf-8")).root


def _repo(name: str, stars: int, *, fork: bool = False, archived: bool = False) -> GithubRepo:
    return GithubRepo(
        id=abs(hash(name)) % 10_000,
        name=name,
        full_name=f"{ORG}/{name}",
        html_url=f"https://github.com/{ORG}/{name}",
        fork=fork,
        created_at=NOW,
        stargazers_count=stars,
        archived=archived,
    )


def _ok(
    body: Any, *, etag: str | None = None, remaining: int = 4000, link: str | None = None
) -> httpx.Response:
    headers = {"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset": "1791219415"}
    if etag is not None:
        headers["ETag"] = etag
    if link is not None:
        headers["Link"] = link
    return httpx.Response(200, json=body, headers=headers)


def _not_modified() -> httpx.Response:
    return httpx.Response(304, headers={"X-RateLimit-Remaining": "4000"})


@pytest.fixture
def mocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def _no_sleep(_: float) -> None:
    raise AssertionError("no retry sleep expected")


# --- fixtures and contract --------------------------------------------------------------------


def test_collector_spec() -> None:
    assert COLLECTOR.source == "github"
    assert COLLECTOR.kind == "github"
    assert COLLECTOR.response_model is GithubRepos
    assert COLLECTOR.fixture_dir == FIXTURES
    assert COLLECTOR.drift_missing_field == "created_at"


def test_fixtures_are_small_and_scrubbed() -> None:
    for path in FIXTURES.iterdir():
        text = path.read_text(encoding="utf-8")
        assert path.stat().st_size < 50_000, path.name
        assert "@" not in text, path.name
        for key in ('"author"', '"uploader"', '"owner"', '"body"', '"assets"'):
            assert key not in text, (path.name, key)


def test_normal_is_the_recorded_repo_listing() -> None:
    assert json.loads(NORMAL.read_text(encoding="utf-8")) == _repos_raw()
    repos = _repos()
    assert [r.name for r in repos] == ["uv", "ruff", "pubgrub"]
    assert [r.fork for r in repos] == [False, False, True]
    assert all(r.owner == ORG for r in repos)
    assert GithubRepos.model_validate_json(EMPTY.read_text(encoding="utf-8")).root == []


def test_releases_fixture_parses() -> None:
    releases = _releases()
    assert [r.id for r in releases] == RELEASE_IDS
    assert all(r.published_at is not None and not r.draft for r in releases)


def test_drift_raises_naming_created_at() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        GithubRepos.model_validate_json(DRIFT.read_text(encoding="utf-8"))
    assert "created_at" in str(excinfo.value)


def test_release_without_published_at_key_fails() -> None:
    raw = _releases_raw()
    del raw[0]["published_at"]
    with pytest.raises(pydantic.ValidationError, match="published_at"):
        GithubReleases.model_validate(raw)


# --- identifiers and URLs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("astral-sh", "astral-sh"),
        ("  Astral-SH ", "astral-sh"),
        ("https://github.com/astral-sh", "astral-sh"),
        ("https://www.github.com/astral-sh/", "astral-sh"),
        ("github.com/astral-sh/uv", "astral-sh"),
    ],
)
def test_normalize_org(raw: str, expected: str) -> None:
    assert normalize_org(raw) == expected


@pytest.mark.parametrize("raw", ["", "bad org", "-leading", "a" * 40, "../x", "org?x=1"])
def test_normalize_org_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="GitHub org"):
        normalize_org(raw)


def test_urls_are_exact() -> None:
    assert repos_url("Astral-SH") == REPOS_URL
    assert releases_url(ORG, "uv") == UV_RELEASES
    for bad in ("..", "a/b", "x y", ""):
        with pytest.raises(ValueError, match="repo name"):
            releases_url(ORG, bad)


# --- flagship and candidates ------------------------------------------------------------------


def test_flagship_is_most_starred_non_fork() -> None:
    recorded = flagship(_repos())
    assert recorded is not None
    assert recorded.name == "uv"
    repos = [_repo("big-fork", 999, fork=True), _repo("beta", 5), _repo("Alpha", 5)]
    top = flagship(repos)
    assert top is not None
    assert top.name == "Alpha"  # tie on stars: name, case-insensitive
    assert flagship([_repo("f", 1, fork=True)]) is None
    assert flagship([]) is None


def test_flagship_may_be_archived_but_candidates_skip_other_archived() -> None:
    repos = [
        _repo("old", 100, archived=True),
        _repo("a", 50),
        _repo("b", 40, archived=True),
        _repo("c", 30, fork=True),
        _repo("d", 20),
    ]
    assert [r.name for r in release_candidates(repos)] == ["old", "a", "d"]
    assert [r.name for r in release_candidates(repos, limit=2)] == ["old", "a"]
    assert release_candidates(repos, limit=0) == []
    assert [r.name for r in release_candidates(_repos())] == ["uv", "ruff"]


def test_org_summary_round_trip() -> None:
    summary = OrgSummary.from_repos(ORG, _repos())
    assert summary == OrgSummary(owner=ORG, flagship="uv", repos=(("uv", 90411), ("ruff", 49914)))
    assert OrgSummary.from_json(json.loads(json.dumps(summary.to_json()))) == summary
    assert OrgSummary.from_repos(ORG, []) == OrgSummary(owner=ORG, flagship=None)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"owner": ORG},
        {"owner": 1, "flagship": None, "repos": []},
        {"owner": ORG, "flagship": 3, "repos": []},
        {"owner": ORG, "flagship": None, "repos": ["uv"]},
        {"owner": ORG, "flagship": None, "repos": [{"name": "uv", "stars": "1"}]},
    ],
)
def test_org_summary_rejects_malformed(data: Any) -> None:
    assert OrgSummary.from_json(data) is None


# --- events -----------------------------------------------------------------------------------


def test_repo_created_event_keys_and_payload() -> None:
    events = repo_created_events(_repos(), flagship_name="uv", company_id=7, run_id="r1", now=NOW)
    assert [(e.external_id, e.version) for e in events] == [
        ("astral-sh:uv", "created"),
        ("astral-sh:ruff", "created"),
        ("astral-sh:pubgrub", "created"),
    ]
    uv = events[0]
    assert uv.event_id == sha256_hex("github|astral-sh:uv|created")
    assert uv.source == "github"
    assert uv.type == GITHUB_REPO_CREATED
    assert uv.occurred_at == datetime(2023, 10, 2, 20, 24, 11, tzinfo=UTC)
    assert uv.observed_at == NOW
    assert uv.url == "https://github.com/astral-sh/uv"
    assert uv.magnitude == 1
    assert uv.company_id == 7
    assert uv.payload == {
        "org": ORG,
        "name": "uv",
        "language": "Rust",
        "stars": 90411,
        "fork": False,
        "is_flagship": True,
    }
    assert [e.payload["is_flagship"] for e in events] == [True, False, False]
    assert events[2].payload["fork"] is True
    assert all("description" not in e.payload for e in events)


def test_release_event_keys_and_payload() -> None:
    events = release_events(
        _releases(),
        owner=ORG,
        repo="uv",
        stars=90411,
        is_flagship=True,
        company_id=7,
        run_id="r1",
        now=NOW,
    )
    assert [(e.external_id, e.version) for e in events] == [
        (f"astral-sh/uv:{rid}", str(rid)) for rid in RELEASE_IDS
    ]
    first = events[0]
    assert first.event_id == sha256_hex("github|astral-sh/uv:402618784|402618784")
    assert first.type == GITHUB_RELEASE
    assert first.occurred_at == datetime(2026, 10, 3, 17, 34, 11, tzinfo=UTC)
    assert first.url == "https://github.com/astral-sh/uv/releases/tag/0.12.23"
    assert first.magnitude == 1
    assert first.payload == {
        "owner": ORG,
        "repo": "uv",
        "tag_name": "0.12.23",
        "name": "0.12.23",
        "prerelease": False,
        "stars": 90411,
        "is_flagship": True,
    }


def test_release_events_skip_drafts_and_unpublished() -> None:
    raw = _releases_raw()
    raw[0]["draft"] = True
    raw[1]["published_at"] = None
    raw[2]["name"] = "x" * 500
    releases = GithubReleases.model_validate(raw).root
    events = release_events(
        releases,
        owner=ORG,
        repo="uv",
        stars=1,
        is_flagship=False,
        company_id=None,
        run_id="r1",
        now=NOW,
    )
    assert [e.version for e in events] == [str(RELEASE_IDS[2])]
    assert len(events[0].payload["name"]) == 200
    assert events[0].payload["is_flagship"] is False


# --- HTTP: headers, pagination, ETags, rate limit ---------------------------------------------


def test_headers_without_token(mocked: respx.MockRouter) -> None:
    route = mocked.get(UV_RELEASES).mock(return_value=_ok([]))
    with httpx.Client() as http:
        assert list_releases(http, ORG, "uv") == []
    request = route.calls.last.request
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert "Authorization" not in request.headers
    assert "If-None-Match" not in request.headers


def test_headers_with_token_from_settings(
    mocked: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")
    get_settings.cache_clear()
    route = mocked.get(UV_RELEASES).mock(return_value=_ok([]))
    with GithubClient(sleep=_no_sleep) as api:
        list_releases(api, ORG, "uv")
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-token-not-real"
    assert request.headers["User-Agent"] == "tell/0.1 (+https://github.com/karankapur/tell)"


def test_list_repos_follows_link_next(mocked: respx.MockRouter) -> None:
    raw = _repos_raw()
    link = f'<{PAGE_2}>; rel="next", <{PAGE_2}>; rel="last"'
    first = mocked.get(REPOS_URL).mock(return_value=_ok(raw[:2], etag=REPOS_ETAG, link=link))
    second = mocked.get(PAGE_2).mock(return_value=_ok(raw[2:], etag='W/"page-2"'))
    api = GithubClient(httpx.Client(), etags={REPOS_URL: 'W/"stale"', PAGE_2: 'W/"p2"'})
    repos = list_repos(api, ORG)
    assert repos is not None
    assert [r.name for r in repos] == ["uv", "ruff", "pubgrub"]
    assert first.calls.last.request.headers["If-None-Match"] == 'W/"stale"'
    assert "If-None-Match" not in second.calls.last.request.headers
    assert api.etags == {REPOS_URL: REPOS_ETAG}  # later pages are never stored
    assert api.requests == 2


def test_list_repos_stops_at_page_cap(
    mocked: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_org, "MAX_REPO_PAGES", 1)
    link = f'<{PAGE_2}>; rel="next"'
    mocked.get(REPOS_URL).mock(return_value=_ok(_repos_raw()[:1], link=link))
    page_2 = mocked.get(PAGE_2).mock(return_value=_ok([]))
    repos = list_repos(httpx.Client(), ORG)
    assert repos is not None
    assert len(repos) == 1
    assert not page_2.called


def test_conditional_304_returns_none_and_keeps_etag(mocked: respx.MockRouter) -> None:
    route = mocked.get(UV_RELEASES).mock(return_value=_not_modified())
    api = GithubClient(httpx.Client(), etags={UV_RELEASES: UV_ETAG})
    assert list_releases(api, ORG, "uv") is None
    assert route.calls.last.request.headers["If-None-Match"] == UV_ETAG
    assert api.not_modified == 1
    assert api.etags == {UV_RELEASES: UV_ETAG}

    repos_route = mocked.get(REPOS_URL).mock(return_value=_not_modified())
    api = GithubClient(httpx.Client(), etags={REPOS_URL: REPOS_ETAG})
    assert list_repos(api, ORG) is None
    assert repos_route.calls.last.request.headers["If-None-Match"] == REPOS_ETAG


def test_unconditional_request_sends_no_etag(mocked: respx.MockRouter) -> None:
    route = mocked.get(REPOS_URL).mock(return_value=_ok([]))
    api = GithubClient(httpx.Client(), etags={REPOS_URL: REPOS_ETAG})
    assert list_repos(api, ORG, conditional=False) == []
    assert "If-None-Match" not in route.calls.last.request.headers


def test_rate_limit_floor_finishes_response_then_stops(mocked: respx.MockRouter) -> None:
    mocked.get(UV_RELEASES).mock(return_value=_ok(_releases_raw(), remaining=49))
    ruff = mocked.get(RUFF_RELEASES).mock(return_value=_ok([]))
    api = GithubClient(httpx.Client())
    releases = list_releases(api, ORG, "uv")
    assert releases is not None
    assert len(releases) == 3
    assert api.stopped
    assert api.rate_remaining == 49
    assert api.rate_reset == 1791219415
    with pytest.raises(RateLimitStopError) as excinfo:
        list_releases(api, ORG, "ruff")
    assert excinfo.value.remaining == 49
    assert not ruff.called


def test_rate_limit_403_stops(mocked: respx.MockRouter) -> None:
    mocked.get(UV_RELEASES).mock(
        return_value=httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "17"}
        )
    )
    api = GithubClient(httpx.Client())
    with pytest.raises(RateLimitStopError) as excinfo:
        list_releases(api, ORG, "uv")
    assert (excinfo.value.remaining, excinfo.value.reset) == (0, 17)
    assert api.stopped


def test_other_403_is_an_http_error(mocked: respx.MockRouter) -> None:
    mocked.get(UV_RELEASES).mock(
        return_value=httpx.Response(403, headers={"X-RateLimit-Remaining": "bogus"})
    )
    api = GithubClient(httpx.Client())
    with pytest.raises(httpx.HTTPStatusError):
        list_releases(api, ORG, "uv")
    assert not api.stopped
    assert api.rate_remaining is None


# --- runs against the database ----------------------------------------------------------------


def _company(session: Session, domain: str, org: str | None) -> Company:
    company = Company(domain=domain, name=domain.split(".", maxsplit=1)[0], github_org=org)
    session.add(company)
    session.flush()
    return company


def _mock_astral(
    router: respx.MockRouter,
    *,
    repos: Callable[[httpx.Request], httpx.Response] | None = None,
    uv: Callable[[httpx.Request], httpx.Response] | None = None,
    remaining: int = 4000,
) -> dict[str, respx.Route]:
    def default_repos(_: httpx.Request) -> httpx.Response:
        return _ok(_repos_raw(), etag=REPOS_ETAG, remaining=remaining)

    def default_uv(_: httpx.Request) -> httpx.Response:
        return _ok(_releases_raw(), etag=UV_ETAG, remaining=remaining)

    return {
        "repos": router.get(REPOS_URL).mock(side_effect=repos or default_repos),
        "uv": router.get(UV_RELEASES).mock(side_effect=uv or default_uv),
        "ruff": router.get(RUFF_RELEASES).mock(
            return_value=_ok([], etag=RUFF_ETAG, remaining=remaining)
        ),
    }


def _events(session: Session) -> list[EventRow]:
    return list(session.scalars(select(EventRow).order_by(EventRow.event_id)))


def _count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(EventRow)).scalar_one()


@pytest.mark.db
def test_configured_orgs(db_session: Session) -> None:
    first = _company(db_session, "astral.example", "Astral-SH")
    _company(db_session, "twin.example", "https://github.com/astral-sh")
    _company(db_session, "none.example", None)
    _company(db_session, "blank.example", "  ")
    _company(db_session, "bad.example", "not an org")
    other = _company(db_session, "other.example", OTHER_ORG)
    assert configured_orgs(db_session) == {ORG: first.id, OTHER_ORG: other.id}


@pytest.mark.db
def test_ingest_writes_repo_and_release_events(
    db_session: Session, mocked: respx.MockRouter
) -> None:
    company = _company(db_session, "astral.example", ORG)
    _company(db_session, "nogithub.example", None)
    routes = _mock_astral(mocked)

    run = ingest(db_session, now=NOW, client=httpx.Client(), sleep=_no_sleep)

    assert run.status == "ok"
    assert (run.fetched, run.inserted, run.skipped, run.errors) == (6, 6, 0, 0)
    assert all(route.call_count == 1 for route in routes.values())
    events = _events(db_session)
    assert {e.company_id for e in events} == {company.id}
    by_type: dict[str, list[EventRow]] = {}
    for event in events:
        by_type.setdefault(event.type, []).append(event)
    assert len(by_type[GITHUB_REPO_CREATED]) == 3
    assert len(by_type[GITHUB_RELEASE]) == 3
    assert all(e.payload["is_flagship"] for e in by_type[GITHUB_RELEASE])
    assert {e.event_id for e in by_type[GITHUB_RELEASE]} == {
        sha256_hex(f"github|astral-sh/uv:{rid}|{rid}") for rid in RELEASE_IDS
    }
    assert run.notes is not None
    assert run.notes["etags"] == {
        REPOS_URL: REPOS_ETAG,
        UV_RELEASES: UV_ETAG,
        RUFF_RELEASES: RUFF_ETAG,
    }
    assert run.notes["orgs"][ORG] == {
        "owner": ORG,
        "flagship": "uv",
        "repos": [{"name": "uv", "stars": 90411}, {"name": "ruff", "stars": 49914}],
    }
    assert run.notes["requests"] == 3
    assert run.notes["not_modified"] == 0
    assert run.notes["rate_limited"] is False


@pytest.mark.db
def test_etag_round_trip_and_304(db_session: Session, mocked: respx.MockRouter) -> None:
    _company(db_session, "astral.example", ORG)
    _mock_astral(mocked)
    first = ingest(db_session, now=NOW, client=httpx.Client(), sleep=_no_sleep)
    assert first.inserted == 6
    assert first.notes is not None
    total = _count(db_session)

    seen: dict[str, str | None] = {}

    def conditional(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("If-None-Match")
        return _not_modified()

    mocked.routes.clear()
    mocked.get(REPOS_URL).mock(side_effect=conditional)
    mocked.get(UV_RELEASES).mock(side_effect=conditional)
    mocked.get(RUFF_RELEASES).mock(side_effect=conditional)
    second = ingest(db_session, now=LATER, client=httpx.Client(), sleep=_no_sleep)

    assert seen == {REPOS_URL: REPOS_ETAG, UV_RELEASES: UV_ETAG, RUFF_RELEASES: RUFF_ETAG}
    assert (second.fetched, second.inserted, second.skipped, second.errors) == (0, 0, 0, 0)
    assert second.status == "ok"
    assert second.notes is not None
    assert second.notes["not_modified"] == 3
    assert second.notes["etags"] == first.notes["etags"]
    assert second.notes["orgs"] == first.notes["orgs"]
    assert _count(db_session) == total

    # Third run: the listing is unchanged (304, summary reused) but uv shipped a release.
    new_release = {**_releases_raw()[0], "id": 403000000, "tag_name": "0.12.24"}
    new_release["html_url"] = "https://github.com/astral-sh/uv/releases/tag/0.12.24"
    mocked.routes.clear()
    mocked.get(REPOS_URL).mock(return_value=_not_modified())
    mocked.get(UV_RELEASES).mock(
        return_value=_ok([new_release, *_releases_raw()], etag='W/"uv-etag-2"')
    )
    mocked.get(RUFF_RELEASES).mock(return_value=_not_modified())
    third = ingest(db_session, now=LATER + timedelta(hours=6), client=httpx.Client())
    assert (third.fetched, third.inserted, third.skipped) == (4, 1, 3)
    assert third.notes is not None
    assert third.notes["etags"][UV_RELEASES] == 'W/"uv-etag-2"'
    added = db_session.scalars(
        select(EventRow).where(
            EventRow.event_id == sha256_hex("github|astral-sh/uv:403000000|403000000")
        )
    ).one()
    assert added.payload["stars"] == 90411
    assert added.payload["is_flagship"] is True


@pytest.mark.db
def test_without_etags_second_run_inserts_nothing(
    db_session: Session, mocked: respx.MockRouter
) -> None:
    _company(db_session, "astral.example", ORG)
    mocked.get(REPOS_URL).mock(return_value=_ok(_repos_raw()))
    mocked.get(UV_RELEASES).mock(return_value=_ok(_releases_raw()))
    mocked.get(RUFF_RELEASES).mock(return_value=_ok([]))
    first = ingest(db_session, now=NOW, client=httpx.Client())
    second = ingest(db_session, now=LATER, client=httpx.Client())
    assert (first.inserted, first.skipped) == (6, 0)
    assert (second.fetched, second.inserted, second.skipped) == (6, 0, 6)
    assert second.notes is not None
    assert second.notes["etags"] == {}
    assert _count(db_session) == 6


@pytest.mark.db
def test_rate_limit_floor_stops_the_run(db_session: Session, mocked: respx.MockRouter) -> None:
    _company(db_session, "astral.example", ORG)
    _company(db_session, "other.example", OTHER_ORG)
    routes = _mock_astral(mocked, remaining=12)
    other = mocked.get(OTHER_REPOS_URL).mock(return_value=_ok([]))

    run = ingest(db_session, now=NOW, client=httpx.Client())

    assert run.status == "ok"
    assert routes["repos"].call_count == 1
    assert not routes["uv"].called
    assert not routes["ruff"].called
    assert not other.called
    assert (run.fetched, run.inserted) == (3, 3)
    assert run.notes is not None
    assert run.notes["rate_limited"] is True
    assert run.notes["rate_limit_remaining"] == 12
    assert run.notes["stopped_at_org"] == ORG
    assert run.notes["etags"] == {REPOS_URL: REPOS_ETAG}


@pytest.mark.db
def test_rate_limit_stop_keeps_previous_etags_and_summaries(
    db_session: Session, mocked: respx.MockRouter
) -> None:
    _company(db_session, "astral.example", ORG)
    _mock_astral(mocked)
    first = ingest(db_session, now=NOW, client=httpx.Client())
    assert first.notes is not None

    mocked.routes.clear()
    mocked.get(REPOS_URL).mock(
        return_value=httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
    )
    second = ingest(db_session, now=LATER, client=httpx.Client())
    assert second.status == "ok"
    assert second.notes is not None
    assert second.notes["rate_limited"] is True
    assert second.notes["etags"] == first.notes["etags"]
    assert second.notes["orgs"] == first.notes["orgs"]


@pytest.mark.db
def test_missing_org_is_an_error_and_the_run_continues(
    db_session: Session, mocked: respx.MockRouter
) -> None:
    _company(db_session, "gone.example", OTHER_ORG)
    _company(db_session, "astral.example", ORG)
    mocked.get(OTHER_REPOS_URL).mock(return_value=httpx.Response(404))
    _mock_astral(mocked)
    run = ingest(db_session, now=NOW, client=httpx.Client())
    assert run.status == "ok"
    assert run.errors == 1
    assert run.inserted == 6
    assert run.notes is not None
    assert run.notes["missing_orgs"] == [OTHER_ORG]


@pytest.mark.db
def test_transport_failure_is_an_error(db_session: Session, mocked: respx.MockRouter) -> None:
    _company(db_session, "astral.example", ORG)
    mocked.get(REPOS_URL).mock(side_effect=httpx.ConnectError("boom"))
    run = ingest(db_session, now=NOW, client=httpx.Client(), sleep=lambda _: None)
    assert run.status == "ok"
    assert run.errors == 1
    assert run.inserted == 0


@pytest.mark.db
def test_drift_fails_the_run_and_its_etags_are_not_reused(
    db_session: Session, mocked: respx.MockRouter
) -> None:
    _company(db_session, "astral.example", ORG)
    drift = json.loads(DRIFT.read_text(encoding="utf-8"))
    mocked.get(REPOS_URL).mock(return_value=_ok(drift, etag=REPOS_ETAG))
    run = ingest(db_session, now=NOW, client=httpx.Client())
    assert run.status == "failed"
    assert run.errors == 1
    assert run.notes is not None
    assert run.notes["drift"].startswith(ORG)
    assert run.notes["etags"] == {}

    seen: list[str | None] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("If-None-Match"))
        return _ok([], etag=REPOS_ETAG)

    mocked.routes.clear()
    mocked.get(REPOS_URL).mock(side_effect=capture)
    ingest(db_session, now=LATER, client=httpx.Client())
    assert seen == [None]


@pytest.mark.db
def test_previous_notes_come_from_the_latest_ok_run(
    db_session: Session, mocked: respx.MockRouter
) -> None:
    _company(db_session, "astral.example", ORG)
    for run_id, status, started, etag in [
        ("github-old", "ok", NOW - timedelta(days=2), 'W/"old"'),
        ("github-new", "ok", NOW - timedelta(days=1), 'W/"new"'),
        ("github-bad", "failed", NOW - timedelta(hours=1), 'W/"bad"'),
    ]:
        db_session.add(
            Run(
                run_id=run_id,
                source="github",
                started_at=started,
                status=status,
                notes={
                    "etags": {REPOS_URL: etag, "https://api.github.com/orgs/gone/repos": "x"},
                    "orgs": {ORG: {"owner": ORG, "flagship": None, "repos": []}},
                },
            )
        )
    db_session.flush()
    route = mocked.get(REPOS_URL).mock(return_value=_not_modified())
    run = ingest(db_session, now=NOW, client=httpx.Client())
    assert route.calls.last.request.headers["If-None-Match"] == 'W/"new"'
    assert run.notes is not None
    assert run.notes["etags"] == {REPOS_URL: 'W/"new"'}  # orgs no longer configured dropped


@pytest.mark.db
def test_run_fixture_twice_is_idempotent(db_session: Session) -> None:
    run_a = start_run(db_session, "github", now=NOW)
    first = run_fixture(db_session, NORMAL, run_id=run_a, now=NOW)
    assert (first.fetched, first.inserted, first.skipped) == (6, 6, 0)
    company = db_session.scalars(
        select(Company).where(Company.domain == "github-fixture.example")
    ).one()
    assert company.github_org == ORG
    assert {e.company_id for e in first.events} == {company.id}
    assert sorted(e.external_id for e in first.events if e.type == GITHUB_RELEASE) == sorted(
        f"astral-sh/uv:{rid}" for rid in RELEASE_IDS
    )

    run_b = start_run(db_session, "github", now=LATER)
    second = run_fixture(db_session, NORMAL, run_id=run_b, now=LATER)
    assert (second.fetched, second.inserted, second.skipped) == (6, 0, 6)
    assert _count(db_session) == 6


@pytest.mark.db
def test_run_fixture_empty(db_session: Session) -> None:
    run_id = start_run(db_session, "github", now=NOW)
    result = run_fixture(db_session, EMPTY, run_id=run_id, now=NOW)
    assert (result.fetched, result.inserted, result.skipped, result.events) == (0, 0, 0, [])
