"""Ashby Job Posting API collector (spec section 3.3).

One documented, unauthenticated endpoint per board::

    GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}?includeCompensation=true

``jobBoardName`` is the slug in ``jobs.ashbyhq.com/{name}``. The response is
``{"apiVersion": "1", "jobs": [...]}``; :class:`AshbyJobBoard` is the contract.

Mapping (spec section 3.3 "Fields used"):

* ``id`` -> ``external_id``; ``title`` (whitespace-stripped; Ashby boards do carry
  leading/trailing spaces) -> ``title``; ``department`` -> ``department``;
  ``location`` (the primary location) -> ``location``; ``jobUrl`` -> ``url``.
* ``publishedAt`` -> ``posted_at`` and, rendered with :func:`iso_utc`, the version
  marker (idempotency key ``ashby:{name}:{id}:{publishedAt}``).
* ``remote``: ``workplaceType`` decides (``Remote`` -> true, ``OnSite``/``Hybrid`` ->
  false); ``isRemote`` is only the fallback when ``workplaceType`` is null, because
  Ashby sets ``isRemote`` on hybrid roles that list a remote secondary location.
* ``descriptionPlain`` is hashed into ``content_hash`` and discarded.
* ``extra`` keeps ``team``, ``employmentType``, ``workplaceType``, ``isRemote``,
  ``secondaryLocations`` (location names only), ``applyUrl`` and a compensation
  summary (``compensationTierSummary``, ``scrapeableCompensationSalarySummary``,
  ``summaryComponents``) only when the employer displays compensation on the posting.
* Postings with ``isListed: false`` are skipped.

Board health: a 404 is not retried (``tell.http.fetch`` never retries 4xx). Liveness
is the shared ADR 0015 rule (``docs/decisions/0015-ats-404-and-reverification.md``):
a 200 calls :func:`~tell.collectors.base.record_board_ok`, a 404 calls
:func:`~tell.collectors.base.record_board_not_found` (``dead`` after two 404s at least
12 hours apart, plus one ``board_verify`` review item). A 404 never advances
``last_polled_at``, so the ``job_closed`` rule (two consecutive *real* polls without
a posting) never counts a 404 as an observation; this module never writes
``last_verified_at``.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    EXTRA_MAX_CHARS,
    CollectorSpec,
    ObservedPosting,
    RunResult,
    iso_utc,
    parse_iso,
    record_board_not_found,
    record_board_ok,
    sync_board_postings,
    write_events,
)
from tell.config import get_settings
from tell.http import RateBudget, Sleeper, fetch, make_client
from tell.models import AtsBoard, Company

__all__ = [
    "API_BASE",
    "COLLECTOR",
    "FIXTURE_BOARD_IDENTIFIER",
    "FIXTURE_DIR",
    "SOURCE",
    "AshbyAddress",
    "AshbyCompensation",
    "AshbyJob",
    "AshbyJobBoard",
    "board_url",
    "fetch_board",
    "ingest_board",
    "parse",
    "run_fixture",
]

SOURCE = "ashby"
API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
QUERY_PARAMS: dict[str, str] = {"includeCompensation": "true"}

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE
FIXTURE_BOARD_IDENTIFIER = "tell-fixture"
"""Synthetic board ``run_fixture`` creates once and then reuses (normal, then updated)."""
FIXTURE_COMPANY_DOMAIN = "ashby-fixture.invalid"

# --- the contract ----------------------------------------------------------------------


class _AshbyModel(BaseModel):
    """Nested Ashby objects: camelCase on the wire, unknown keys dropped."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class AshbyPostalAddress(_AshbyModel):
    address_locality: str | None = Field(default=None, alias="addressLocality")
    address_region: str | None = Field(default=None, alias="addressRegion")
    address_country: str | None = Field(default=None, alias="addressCountry")


class AshbyAddress(_AshbyModel):
    postal_address: AshbyPostalAddress | None = Field(default=None, alias="postalAddress")


class AshbySecondaryLocation(_AshbyModel):
    location: str
    address: AshbyAddress | None = None


class AshbyCompensationComponent(_AshbyModel):
    compensation_type: str | None = Field(default=None, alias="compensationType")
    interval: str | None = None
    currency_code: str | None = Field(default=None, alias="currencyCode")
    min_value: float | None = Field(default=None, alias="minValue")
    max_value: float | None = Field(default=None, alias="maxValue")


class AshbyCompensation(_AshbyModel):
    """Present only with ``includeCompensation=true``; tiers are parsed but not kept."""

    compensation_tier_summary: str | None = Field(default=None, alias="compensationTierSummary")
    scrapeable_compensation_salary_summary: str | None = Field(
        default=None, alias="scrapeableCompensationSalarySummary"
    )
    summary_components: list[AshbyCompensationComponent] = Field(
        default_factory=list, alias="summaryComponents"
    )


class AshbyJob(_AshbyModel):
    """One entry of ``jobs[]``; every field spec section 3.3 uses is required."""

    id: str = Field(min_length=1)
    title: str
    department: str | None
    team: str | None
    employment_type: str | None = Field(alias="employmentType")
    location: str
    secondary_locations: list[AshbySecondaryLocation] = Field(alias="secondaryLocations")
    is_remote: bool = Field(alias="isRemote")
    workplace_type: str | None = Field(alias="workplaceType")
    published_at: datetime = Field(alias="publishedAt")
    is_listed: bool = Field(alias="isListed")
    job_url: str = Field(alias="jobUrl", min_length=1)
    apply_url: str = Field(alias="applyUrl")
    description_plain: str | None = Field(alias="descriptionPlain", repr=False)
    should_display_compensation: bool | None = Field(
        default=None, alias="shouldDisplayCompensationOnJobPostings"
    )
    compensation: AshbyCompensation | None = None
    address: AshbyAddress | None = None


class AshbyJobBoard(BaseModel):
    """``GET /posting-api/job-board/{name}``: the whole response."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, frozen=True)

    api_version: str = Field(alias="apiVersion")
    jobs: list[AshbyJob]


# --- fetch -----------------------------------------------------------------------------


def board_url(identifier: str) -> str:
    """The documented list URL for ``identifier`` (query string added by :func:`fetch_board`)."""
    if not identifier:
        msg = "identifier must be a non-empty Ashby job board name"
        raise ValueError(msg)
    return f"{API_BASE}/{quote(identifier, safe='')}"


@cache
def _default_budget() -> RateBudget:
    """One process-wide bucket for api.ashbyhq.com at ``TELL_RATE_ATS_RPS``."""
    return RateBudget(get_settings().tell_rate_ats_rps)


def fetch_board(
    client: httpx.Client,
    identifier: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> AshbyJobBoard | None:
    """Fetch and validate one board; ``None`` when Ashby answers 404 (unknown board).

    429/5xx are retried by :func:`tell.http.fetch` (1, 2, 4, 8 s or ``Retry-After``)
    and raise :class:`tell.http.RetryExhausted` when they persist; any other non-2xx
    raises :class:`httpx.HTTPStatusError`. A drifted response raises
    :class:`pydantic.ValidationError`.
    """
    response = fetch(
        board_url(identifier),
        client=client,
        params=QUERY_PARAMS,
        budget=budget if budget is not None else _default_budget(),
        sleep=sleep,
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    response.raise_for_status()
    return AshbyJobBoard.model_validate_json(response.content)


# --- parse -----------------------------------------------------------------------------


def _clip(value: str | None) -> str | None:
    return None if value is None else value[:EXTRA_MAX_CHARS]


def _remote(job: AshbyJob) -> bool:
    workplace = (job.workplace_type or "").strip().lower()
    if workplace == "remote":
        return True
    if workplace in {"onsite", "hybrid"}:
        return False
    return job.is_remote


def _compensation(job: AshbyJob) -> dict[str, Any] | None:
    comp = job.compensation
    if comp is None or job.should_display_compensation is False:
        return None
    summary = {
        "compensationTierSummary": _clip(comp.compensation_tier_summary),
        "scrapeableCompensationSalarySummary": _clip(comp.scrapeable_compensation_salary_summary),
        "summaryComponents": [
            {
                "compensationType": _clip(part.compensation_type),
                "interval": _clip(part.interval),
                "currencyCode": _clip(part.currency_code),
                "minValue": part.min_value,
                "maxValue": part.max_value,
            }
            for part in comp.summary_components
        ],
    }
    if summary["compensationTierSummary"] is None and not summary["summaryComponents"]:
        return None
    return summary


def _extra(job: AshbyJob) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "team": _clip(job.team),
        "employmentType": _clip(job.employment_type),
        "workplaceType": _clip(job.workplace_type),
        "isRemote": job.is_remote,
        "secondaryLocations": [_clip(loc.location) for loc in job.secondary_locations],
        "applyUrl": _clip(job.apply_url),
    }
    compensation = _compensation(job)
    if compensation is not None:
        extra["compensation"] = compensation
    return extra


def _observe(job: AshbyJob) -> ObservedPosting | None:
    title = job.title.strip()
    if not job.is_listed or not title:
        return None
    posted_at = parse_iso(job.published_at.isoformat())
    return ObservedPosting(
        external_id=job.id,
        title=title,
        url=job.job_url,
        version=iso_utc(posted_at),
        department=(job.department or "").strip() or None,
        location=job.location.strip() or None,
        remote=_remote(job),
        posted_at=posted_at,
        description_plain=job.description_plain,
        extra=_extra(job),
    )


def parse(raw: AshbyJobBoard | str | bytes) -> list[ObservedPosting]:
    """Listed postings of one board response (raw JSON text or an already-validated model)."""
    board = raw if isinstance(raw, AshbyJobBoard) else AshbyJobBoard.model_validate_json(raw)
    observed = (_observe(job) for job in board.jobs)
    return [posting for posting in observed if posting is not None]


# --- ingest ----------------------------------------------------------------------------


def _sync(
    session: Session, board: AtsBoard, response: AshbyJobBoard, *, run_id: str, now: datetime
) -> RunResult:
    events = sync_board_postings(session, board, parse(response), run_id=run_id, now=now)
    stats = write_events(session, events)
    record_board_ok(session, board, now=now)
    return RunResult(
        fetched=len(response.jobs), inserted=stats.inserted, skipped=stats.skipped, events=events
    )


def _require_ashby(board: AtsBoard) -> None:
    if board.vendor != SOURCE:
        msg = f"ashby collector cannot poll a {board.vendor!r} board ({board.identifier!r})"
        raise ValueError(msg)


def ingest_board(
    session: Session,
    board: AtsBoard,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> RunResult:
    """Poll one Ashby board, reconcile its postings and write the resulting events.

    The caller owns the run (``start_run`` / ``finish_run``) and the transaction.
    A 404 returns an empty :class:`RunResult` and goes to ``record_board_not_found``
    (ADR 0015). Errors other than 404 propagate.
    """
    _require_ashby(board)
    own_client = client is None
    http = client if client is not None else make_client()
    try:
        response = fetch_board(http, board.identifier, budget=budget, sleep=sleep)
    finally:
        if own_client:
            http.close()
    if response is None:
        record_board_not_found(session, board, now=now)
        return RunResult(fetched=0, inserted=0, skipped=0)
    return _sync(session, board, response, run_id=run_id, now=now)


# --- fixture replay ---------------------------------------------------------------------


def _fixture_board(session: Session) -> AtsBoard:
    board = session.scalars(
        select(AtsBoard).where(
            AtsBoard.vendor == SOURCE, AtsBoard.identifier == FIXTURE_BOARD_IDENTIFIER
        )
    ).one_or_none()
    if board is not None:
        return board
    company = session.scalars(
        select(Company).where(Company.domain == FIXTURE_COMPANY_DOMAIN)
    ).one_or_none()
    if company is None:
        company = Company(domain=FIXTURE_COMPANY_DOMAIN, name="Ashby fixture company")
        session.add(company)
        session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=SOURCE,
        identifier=FIXTURE_BOARD_IDENTIFIER,
        discovered_from=f"https://jobs.ashbyhq.com/{FIXTURE_BOARD_IDENTIFIER}",
        confidence=Decimal("1.00"),
        status="active",
    )
    session.add(board)
    session.flush()
    return board


def run_fixture(session: Session, case_path: Path, /, *, run_id: str, now: datetime) -> RunResult:
    """Replay one fixture file through parse + ``sync_board_postings`` with no network."""
    response = AshbyJobBoard.model_validate_json(case_path.read_bytes())
    return _sync(session, _fixture_board(session), response, run_id=run_id, now=now)


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="ats",
    response_model=AshbyJobBoard,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="publishedAt",
    run_fixture=run_fixture,
)
