"""GitHub org collector (spec section 3.8; v1.1, config-gated per company).

Only companies whose ``companies.github_org`` is set are polled. Two documented REST
endpoints, both public and readable without a token:

* ``GET https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=100`` (every page,
  following ``Link: rel="next"``, at most :data:`MAX_REPO_PAGES`);
* ``GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=20`` for the
  org's flagship repo plus its next most-starred live repos (at most
  :data:`RELEASE_REPOS_PER_ORG` in total).

Events (``source = "github"``):

``github_repo_created``
    ``external_id = f"{org}:{repo}"``, ``version = "created"`` (natural key
    ``github:{org}:{repo}:created``); ``occurred_at`` is the repo's ``created_at``.
    Payload: ``org``, ``name``, ``language``, ``stars``, ``fork``, ``is_flagship``.
    Never the description.
``github_release``
    ``external_id = f"{owner}/{repo}:{release.id}"``, ``version = str(release.id)``
    (natural key ``github:{repo}:{release.id}``); ``occurred_at`` is ``published_at``.
    Payload: ``owner``, ``repo``, ``tag_name``, ``name``, ``prerelease``, ``stars``,
    ``is_flagship``. Drafts and releases without ``published_at`` are skipped. Release
    notes (``body``), ``author`` and asset ``uploader`` objects are never read.

The flagship repo is the org's most-starred non-fork repo (ties broken by name);
``payload.is_flagship`` lets Phase 7 count only flagship releases as ``launch``.

Conditional requests: every first-page URL the run fully processed is stored with its
``ETag`` in ``runs.notes["etags"]`` (``{url: etag}``), and the next run sends it back as
``If-None-Match``, read from the latest *successful* github run. A ``304`` costs no
rate limit and is skipped without counting (neither ``fetched`` nor ``skipped``). A
``304`` on the repo listing reuses the per-org summary (owner, flagship, release
repos with stars) stored in ``runs.notes["orgs"]`` so releases are still polled.

Rate limit: ``X-RateLimit-Remaining`` is read on every response; once it drops below
:data:`RATE_LIMIT_FLOOR` the response in hand is finished and the run stops, noting
``rate_limited`` in ``runs.notes``. A ``403``/``429`` rate-limit answer stops it too.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    CollectorSpec,
    Event,
    RunResult,
    finish_run,
    start_run,
    utcnow,
    write_events,
)
from tell.config import get_settings
from tell.http import RateBudget, fetch, make_client
from tell.models import Company, Run

__all__ = [
    "ACCEPT",
    "API_ROOT",
    "COLLECTOR",
    "FIXTURE_DIR",
    "GITHUB_RELEASE",
    "GITHUB_REPO_CREATED",
    "MAX_REPO_PAGES",
    "RATE_LIMIT_FLOOR",
    "RELEASE_REPOS_PER_ORG",
    "SOURCE",
    "GithubClient",
    "GithubRelease",
    "GithubReleases",
    "GithubRepo",
    "GithubRepos",
    "GithubRunResult",
    "OrgSummary",
    "RateLimitStopError",
    "collect",
    "configured_orgs",
    "flagship",
    "ingest",
    "list_releases",
    "list_repos",
    "normalize_org",
    "release_candidates",
    "release_events",
    "releases_url",
    "repo_created_events",
    "repos_url",
    "run_fixture",
]

SOURCE = "github"
API_ROOT = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"

GITHUB_RELEASE = "github_release"
GITHUB_REPO_CREATED = "github_repo_created"

RATE_LIMIT_FLOOR = 50
"""Stop the run once ``X-RateLimit-Remaining`` drops below this."""
MAX_REPO_PAGES = 10
"""Pages of 100 repos read per org (1,000 most recently pushed repos)."""
RELEASE_REPOS_PER_ORG = 5
"""Repos per org whose releases are polled: the flagship plus the next most-starred."""
_MAX_TEXT = 200
"""Clip for release names and tags copied into payloads."""

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE
FIXTURE_DOMAIN = "github-fixture.example"
FIXTURE_RELEASES = "releases.json"

_ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_ORG_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)

Sleeper = Callable[[float], None]
_log = logging.getLogger(__name__)


# --- the contract ---------------------------------------------------------------------------


class GithubRepo(BaseModel):
    """One element of the org repos listing; the fields the events use are required.

    Unknown keys (``owner``, the ``*_url`` templates, ``description``) are ignored and
    never kept.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str = Field(min_length=1)
    full_name: str = Field(min_length=3)
    html_url: str = Field(min_length=1)
    fork: bool
    created_at: AwareDatetime
    pushed_at: AwareDatetime | None = None
    stargazers_count: int = Field(ge=0)
    language: str | None = None
    archived: bool = False

    @property
    def owner(self) -> str:
        """The org login as GitHub spells it (left of ``/`` in ``full_name``)."""
        return self.full_name.split("/", 1)[0]


class GithubRepos(RootModel[list[GithubRepo]]):
    """``/orgs/{org}/repos``: a bare JSON array (``[]`` for an org with no public repos)."""


class GithubRelease(BaseModel):
    """One element of a repo's releases listing. ``author``/``assets``/``body`` are ignored."""

    model_config = ConfigDict(extra="ignore")

    id: int
    tag_name: str = Field(min_length=1)
    name: str | None = None
    html_url: str = Field(min_length=1)
    draft: bool
    prerelease: bool
    created_at: AwareDatetime
    published_at: AwareDatetime | None


class GithubReleases(RootModel[list[GithubRelease]]):
    """``/repos/{owner}/{repo}/releases``: a bare JSON array, newest first."""


# --- identifiers and URLs -------------------------------------------------------------------


def normalize_org(value: str) -> str:
    """``companies.github_org`` to a lowercase org login; accepts a ``github.com/`` URL."""
    text = _ORG_URL_PREFIX.sub("", value.strip()).strip("/").split("/", 1)[0]
    if not _ORG_RE.fullmatch(text):
        msg = f"not a GitHub org login: {value!r}"
        raise ValueError(msg)
    return text.lower()


def _check_repo(name: str) -> str:
    if not _REPO_RE.fullmatch(name) or name in {".", ".."}:
        msg = f"not a GitHub repo name: {name!r}"
        raise ValueError(msg)
    return name


def repos_url(org: str) -> str:
    """First page of the org repos listing, exactly as spec section 3.8 writes it."""
    return f"{API_ROOT}/orgs/{normalize_org(org)}/repos?sort=pushed&per_page=100"


def releases_url(owner: str, repo: str) -> str:
    """The releases listing of one repo (first 20, newest first)."""
    return f"{API_ROOT}/repos/{normalize_org(owner)}/{_check_repo(repo)}/releases?per_page=20"


# --- HTTP: auth, ETags, rate limit ----------------------------------------------------------


class RateLimitStopError(RuntimeError):
    """The rate-limit floor was reached (or GitHub refused for rate); the run stops here."""

    def __init__(self, remaining: int | None, reset: int | None) -> None:
        super().__init__(f"GitHub rate limit floor reached (remaining={remaining})")
        self.remaining = remaining
        self.reset = reset


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


class GithubClient:
    """An ``httpx.Client`` plus the per-run GitHub state: token, ETags, rate limit.

    ``etags`` are the previous successful run's ``{url: etag}``; :meth:`get` sends the
    matching ``If-None-Match`` when ``conditional`` and returns ``None`` on ``304``.
    :meth:`remember` records a URL's ETag once the caller has fully processed the
    response, so an interrupted listing is never marked as seen.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        token: str | None = None,
        etags: Mapping[str, str] | None = None,
        floor: int = RATE_LIMIT_FLOOR,
        budget: RateBudget | None = None,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self._own = client is None
        self.http = client if client is not None else make_client()
        if token is None:
            secret = get_settings().github_token
            token = secret.get_secret_value() if secret is not None else None
        self._token = token or None
        self.previous_etags: dict[str, str] = dict(etags or {})
        self.etags: dict[str, str] = {}
        self.floor = floor
        self._budget = budget
        self._sleep = sleep
        self.requests = 0
        self.not_modified = 0
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None
        self.stopped = False

    def __enter__(self) -> GithubClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._own:
            self.http.close()

    def headers(self, url: str, *, conditional: bool) -> dict[str, str]:
        headers = {"Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        etag = self.previous_etags.get(url) if conditional else None
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _observe_rate(self, response: httpx.Response) -> None:
        remaining = _int_header(response, "X-RateLimit-Remaining")
        if remaining is not None:
            self.rate_remaining = remaining
            self.rate_reset = _int_header(response, "X-RateLimit-Reset")

    def get(self, url: str, *, conditional: bool = True) -> httpx.Response | None:
        """GET ``url``; ``None`` on ``304``. Raises :class:`RateLimitStopError` when stopped.

        Any other non-2xx raises :class:`httpx.HTTPStatusError`. After a response that
        leaves fewer than ``floor`` requests, the run is marked stopped: the caller
        still processes this response, and the next call raises.
        """
        if self.stopped:
            raise RateLimitStopError(self.rate_remaining, self.rate_reset)
        response = fetch(
            url,
            client=self.http,
            headers=self.headers(url, conditional=conditional),
            budget=self._budget,
            sleep=self._sleep,
        )
        self.requests += 1
        self._observe_rate(response)
        if response.status_code in (403, 429) and (
            self.rate_remaining == 0 or "Retry-After" in response.headers
        ):
            self.stopped = True
            raise RateLimitStopError(self.rate_remaining, self.rate_reset)
        if self.rate_remaining is not None and self.rate_remaining < self.floor:
            self.stopped = True
        if response.status_code == 304:
            self.not_modified += 1
            self.remember(url, self.previous_etags.get(url))
            return None
        response.raise_for_status()
        return response

    def remember(self, url: str, etag: str | None) -> None:
        if etag:
            self.etags[url] = etag


def _as_api(client: GithubClient | httpx.Client | None) -> GithubClient:
    return client if isinstance(client, GithubClient) else GithubClient(client)


def list_repos(
    client: GithubClient | httpx.Client | None, org: str, *, conditional: bool = True
) -> list[GithubRepo] | None:
    """Every repo of ``org`` (most recently pushed first), following ``Link: rel="next"``.

    Returns ``None`` when the first page answers ``304`` (nothing changed since the
    ETag the previous run stored). Only the first page is conditional; later pages
    are always read in full once the first page changed. Stops after
    :data:`MAX_REPO_PAGES` pages.
    """
    api = _as_api(client)
    first = repos_url(org)
    url: str | None = first
    repos: list[GithubRepo] = []
    first_etag: str | None = None
    pages = 0
    while url is not None:
        response = api.get(url, conditional=conditional and url == first)
        if response is None:
            return None
        if url == first:
            first_etag = response.headers.get("ETag")
        repos.extend(GithubRepos.model_validate_json(response.content).root)
        pages += 1
        url = response.links.get("next", {}).get("url")
        if url is not None and pages >= MAX_REPO_PAGES:
            _log.warning("github: %s has more than %d pages of repos", org, MAX_REPO_PAGES)
            break
    api.remember(first, first_etag)
    return repos


def list_releases(
    client: GithubClient | httpx.Client | None, owner: str, repo: str, *, conditional: bool = True
) -> list[GithubRelease] | None:
    """The 20 newest releases of ``owner/repo``; ``None`` on ``304``."""
    api = _as_api(client)
    url = releases_url(owner, repo)
    response = api.get(url, conditional=conditional)
    if response is None:
        return None
    releases = GithubReleases.model_validate_json(response.content).root
    api.remember(url, response.headers.get("ETag"))
    return releases


# --- derive -----------------------------------------------------------------------------------


def flagship(repos: Iterable[GithubRepo]) -> GithubRepo | None:
    """The most-starred non-fork repo (ties: name, case-insensitive); ``None`` if all forks."""
    candidates = [repo for repo in repos if not repo.fork]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (-r.stargazers_count, r.name.lower()))


def release_candidates(
    repos: Sequence[GithubRepo], limit: int = RELEASE_REPOS_PER_ORG
) -> list[GithubRepo]:
    """Repos whose releases are polled: the flagship first, then live non-forks by stars."""
    top = flagship(repos)
    ranked = sorted(
        (r for r in repos if not r.fork and not r.archived and r is not top),
        key=lambda r: (-r.stargazers_count, r.name.lower()),
    )
    chosen = ([top] if top is not None else []) + ranked
    return chosen[: max(limit, 0)]


@dataclass(frozen=True)
class OrgSummary:
    """What a ``304`` on the repo listing needs from the last full read (``runs.notes``)."""

    owner: str
    flagship: str | None
    repos: tuple[tuple[str, int], ...] = ()
    """``(name, stars)`` of the release candidates, flagship first."""

    @classmethod
    def from_repos(cls, org: str, repos: Sequence[GithubRepo]) -> OrgSummary:
        top = flagship(repos)
        return cls(
            owner=repos[0].owner if repos else org,
            flagship=top.name if top is not None else None,
            repos=tuple((r.name, r.stargazers_count) for r in release_candidates(repos)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "flagship": self.flagship,
            "repos": [{"name": name, "stars": stars} for name, stars in self.repos],
        }

    @classmethod
    def from_json(cls, data: Any) -> OrgSummary | None:
        """Parse a stored summary; ``None`` when it is missing or malformed."""
        if not isinstance(data, Mapping):
            return None
        owner = data.get("owner")
        top = data.get("flagship")
        repos = data.get("repos")
        if not isinstance(owner, str) or not isinstance(repos, list):
            return None
        if top is not None and not isinstance(top, str):
            return None
        pairs: list[tuple[str, int]] = []
        for item in repos:
            if not isinstance(item, Mapping):
                return None
            name, stars = item.get("name"), item.get("stars")
            if not isinstance(name, str) or not isinstance(stars, int):
                return None
            pairs.append((name, stars))
        return cls(owner=owner, flagship=top, repos=tuple(pairs))


def repo_created_events(
    repos: Iterable[GithubRepo],
    *,
    flagship_name: str | None,
    company_id: int | None,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """One ``github_repo_created`` per repo, keyed ``github:{org}:{repo}:created``."""
    return [
        Event(
            source=SOURCE,
            type=GITHUB_REPO_CREATED,
            external_id=f"{repo.owner}:{repo.name}",
            version="created",
            occurred_at=repo.created_at,
            observed_at=now,
            url=repo.html_url,
            payload={
                "org": repo.owner,
                "name": repo.name,
                "language": repo.language,
                "stars": repo.stargazers_count,
                "fork": repo.fork,
                "is_flagship": repo.name == flagship_name,
            },
            magnitude=1,
            company_id=company_id,
            run_id=run_id,
        )
        for repo in repos
    ]


def _clip(value: str | None) -> str | None:
    return None if value is None else value[:_MAX_TEXT]


def release_events(
    releases: Iterable[GithubRelease],
    *,
    owner: str,
    repo: str,
    stars: int,
    is_flagship: bool,
    company_id: int | None,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """One ``github_release`` per published release, keyed ``github:{repo}:{release.id}``."""
    return [
        Event(
            source=SOURCE,
            type=GITHUB_RELEASE,
            external_id=f"{owner}/{repo}:{release.id}",
            version=str(release.id),
            occurred_at=release.published_at,
            observed_at=now,
            url=release.html_url,
            payload={
                "owner": owner,
                "repo": repo,
                "tag_name": _clip(release.tag_name),
                "name": _clip(release.name),
                "prerelease": release.prerelease,
                "stars": stars,
                "is_flagship": is_flagship,
            },
            magnitude=1,
            company_id=company_id,
            run_id=run_id,
        )
        for release in releases
        if release.published_at is not None and not release.draft
    ]


# --- run ------------------------------------------------------------------------------------------


@dataclass
class GithubRunResult:
    """What :func:`collect` did; :attr:`notes` is what ``runs.notes`` stores."""

    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    events: list[Event] = field(default_factory=list)
    etags: dict[str, str] = field(default_factory=dict)
    orgs: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: int = 0
    not_modified: int = 0
    rate_limited: dict[str, Any] | None = None
    missing: list[str] = field(default_factory=list)
    drift: str | None = None

    def record(self, session: Session, fetched: int, events: list[Event]) -> None:
        stats = write_events(session, events)
        self.fetched += fetched
        self.inserted += stats.inserted
        self.skipped += stats.skipped
        self.events.extend(events)

    @property
    def notes(self) -> dict[str, Any]:
        notes: dict[str, Any] = {
            "etags": dict(self.etags),
            "orgs": dict(self.orgs),
            "requests": self.requests,
            "not_modified": self.not_modified,
            "rate_limited": self.rate_limited is not None,
        }
        if self.rate_limited is not None:
            notes.update(self.rate_limited)
        if self.missing:
            notes["missing_orgs"] = list(self.missing)
        if self.drift is not None:
            notes["drift"] = self.drift
        return notes

    def as_run_result(self) -> RunResult:
        return RunResult(
            fetched=self.fetched, inserted=self.inserted, skipped=self.skipped, events=self.events
        )


def _previous_notes(session: Session, run_id: str | None) -> Mapping[str, Any]:
    """``notes`` of the latest successful github run other than ``run_id`` ({} if none)."""
    stmt = select(Run.notes).where(Run.source == SOURCE, Run.status == "ok", Run.notes.is_not(None))
    if run_id is not None:
        stmt = stmt.where(Run.run_id != run_id)
    notes = session.scalars(stmt.order_by(Run.started_at.desc()).limit(1)).first()
    return notes if isinstance(notes, Mapping) else {}


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def configured_orgs(session: Session) -> dict[str, int]:
    """``{org (lowercase): company_id}`` over companies with ``github_org`` set.

    An unparseable value is logged and skipped; when two companies claim one org the
    lowest ``id`` wins (and the clash is logged).
    """
    orgs: dict[str, int] = {}
    stmt = select(Company.id, Company.github_org).where(Company.github_org.is_not(None))
    for company_id, raw in session.execute(stmt.order_by(Company.id)):
        if not raw or not raw.strip():
            continue
        try:
            org = normalize_org(raw)
        except ValueError:
            _log.warning("github: company %s has an invalid github_org %r", company_id, raw)
            continue
        if org in orgs:
            _log.warning(
                "github: org %s claimed by companies %s and %s", org, orgs[org], company_id
            )
            continue
        orgs[org] = company_id
    return orgs


def _url_belongs_to(url: str, orgs: Iterable[str]) -> bool:
    lowered = url.lower()
    return any(
        lowered.startswith((f"{API_ROOT}/orgs/{org}/", f"{API_ROOT}/repos/{org}/")) for org in orgs
    )


def _collect_org(
    session: Session,
    api: GithubClient,
    org: str,
    company_id: int,
    *,
    previous: OrgSummary | None,
    result: GithubRunResult,
    run_id: str,
    now: datetime,
) -> None:
    repos = list_repos(api, org, conditional=previous is not None)
    if repos is None:
        if previous is None:  # pragma: no cover - a 304 needs a stored ETag, stored with a summary
            return
        summary = previous
    else:
        summary = OrgSummary.from_repos(org, repos)
        events = repo_created_events(
            repos, flagship_name=summary.flagship, company_id=company_id, run_id=run_id, now=now
        )
        result.record(session, len(repos), events)
    result.orgs[org] = summary.to_json()
    for name, stars in summary.repos:
        releases = list_releases(api, summary.owner, name)
        if releases is None:
            continue
        events = release_events(
            releases,
            owner=summary.owner,
            repo=name,
            stars=stars,
            is_flagship=name == summary.flagship,
            company_id=company_id,
            run_id=run_id,
            now=now,
        )
        result.record(session, len(releases), events)


def collect(
    session: Session,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    token: str | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> GithubRunResult:
    """Poll every configured org and write its events; flushes, never commits.

    ETags come from the latest successful github run's notes. A 404 org is counted as
    an error and noted in ``missing_orgs``; a response that fails the contract is an
    error noted as ``drift``; other HTTP failures are errors; each skips to the next
    org. Reaching the rate-limit floor stops the run (``rate_limited``). The caller
    (normally :func:`ingest`) closes the run with :attr:`GithubRunResult.notes`.
    """
    orgs = configured_orgs(session)
    prev_notes = _previous_notes(session, run_id)
    prev_etags = _string_map(prev_notes.get("etags"))
    prev_orgs = prev_notes.get("orgs")
    prev_orgs = prev_orgs if isinstance(prev_orgs, Mapping) else {}
    result = GithubRunResult()
    with GithubClient(client, token=token, etags=prev_etags, budget=budget, sleep=sleep) as api:
        for org, company_id in orgs.items():
            previous = OrgSummary.from_json(prev_orgs.get(org))
            try:
                _collect_org(
                    session,
                    api,
                    org,
                    company_id,
                    previous=previous,
                    result=result,
                    run_id=run_id,
                    now=now,
                )
            except RateLimitStopError as stop:
                result.rate_limited = {
                    "rate_limit_remaining": stop.remaining,
                    "rate_limit_reset": stop.reset,
                    "stopped_at_org": org,
                }
                _log.warning("github: rate limit floor reached at %s; stopping", org)
                break
            except ValidationError as exc:
                result.errors += 1
                result.drift = f"{org}: {exc.error_count()} validation errors"
                _log.error("github: %s response failed the contract: %s", org, exc)
            except httpx.HTTPStatusError as exc:
                result.errors += 1
                if exc.response.status_code == 404:
                    result.missing.append(org)
                _log.warning("github: %s failed: %s", org, exc)
            except httpx.HTTPError as exc:
                result.errors += 1
                _log.warning("github: %s failed: %s", org, exc)
            session.flush()
        result.requests = api.requests
        result.not_modified = api.not_modified
        kept = {u: e for u, e in prev_etags.items() if _url_belongs_to(u, orgs)}
        result.etags = {**kept, **api.etags}
        for org in orgs:
            if org not in result.orgs and isinstance(prev_orgs.get(org), Mapping):
                result.orgs[org] = dict(prev_orgs[org])
    return result


def ingest(
    session: Session,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    token: str | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> Run:
    """One github run: ``start_run``, :func:`collect`, ``finish_run`` with the notes.

    Finishes ``ok`` unless a response failed the contract (then ``failed``, so its
    ETags are not reused). A rate-limit stop still finishes ``ok``. Flushes, never commits.
    """
    started = now or utcnow()
    run_id = start_run(session, SOURCE, now=started)
    result = collect(
        session, run_id=run_id, now=started, client=client, token=token, budget=budget, sleep=sleep
    )
    return finish_run(
        session,
        run_id,
        status="failed" if result.drift is not None else "ok",
        fetched=result.fetched,
        inserted=result.inserted,
        skipped=result.skipped,
        errors=result.errors,
        notes=result.notes,
        now=now,
    )


# --- fixture replay ---------------------------------------------------------------------------


def _fixture_company(session: Session, org: str) -> Company:
    company = session.scalars(select(Company).where(Company.domain == FIXTURE_DOMAIN)).first()
    if company is None:
        company = Company(domain=FIXTURE_DOMAIN, name="GitHub Fixture Co", github_org=org)
        session.add(company)
        session.flush()
    return company


def run_fixture(session: Session, case_path: Path, /, *, run_id: str, now: datetime) -> RunResult:
    """Replay a recorded org repo listing (and the flagship's releases) with no network.

    ``case_path`` is a raw ``/orgs/{org}/repos`` response. When the listing has a
    flagship and ``releases.json`` sits beside it, that file is replayed as the
    flagship's releases. ``fetched`` counts repos plus releases.
    """
    repos = GithubRepos.model_validate_json(case_path.read_text(encoding="utf-8")).root
    result = GithubRunResult()
    if not repos:
        return result.as_run_result()
    summary = OrgSummary.from_repos(repos[0].owner, repos)
    company_id = _fixture_company(session, summary.owner.lower()).id
    events = repo_created_events(
        repos, flagship_name=summary.flagship, company_id=company_id, run_id=run_id, now=now
    )
    result.record(session, len(repos), events)
    releases_path = case_path.parent / FIXTURE_RELEASES
    if summary.flagship is not None and releases_path.is_file():
        releases = GithubReleases.model_validate_json(releases_path.read_text(encoding="utf-8"))
        stars = dict(summary.repos).get(summary.flagship, 0)
        events = release_events(
            releases.root,
            owner=summary.owner,
            repo=summary.flagship,
            stars=stars,
            is_flagship=True,
            company_id=company_id,
            run_id=run_id,
            now=now,
        )
        result.record(session, len(releases.root), events)
    session.flush()
    return result.as_run_result()


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="github",
    response_model=GithubRepos,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="created_at",
    run_fixture=run_fixture,
)
