"""Greenhouse Job Board API collector (spec section 3.1).

``GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true``
lists every published job on one board; no key is needed. This module is only
"fetch, parse with the pydantic contract, hand the postings to
:func:`tell.collectors.base.sync_board_postings`".

* The pydantic models below are the contract: every field spec section 3.1 lists is
  required, so a provider rename fails validation loudly instead of silently
  emitting empty postings. Unknown fields are allowed and ignored.
* ``content`` arrives entity-encoded (``&lt;p&gt;...``): it is decoded once to HTML,
  reduced to plain text, hashed into ``content_hash`` and discarded.
* ``remote`` comes from a ``Workplace Type`` metadata entry when the board has one,
  otherwise from the word "remote" in ``location.name``; ``None`` when neither says.
* ``location.name`` may be ``null`` (1 of 1,918 jobs on 1 of 36 boards, 2026-10-14):
  the posting's ``location`` is then ``NULL``, like a blank name.
* One bad job never fails a board: :func:`validate_board` (the poll path) checks the
  envelope, then each job on its own; a job that breaks :class:`GreenhouseJob` is
  skipped, logged at WARNING with the board and the field path, and counted in
  :attr:`GreenhouseJobsResponse.invalid_jobs` and ``BoardRunResult.errors``, which
  ``tell ingest`` adds to ``runs.errors``. Its id is passed to ``sync_board_postings``
  so a listed but invalid job is never closed. Only when every job of a non-empty
  board fails does the board fail, with the strict error.
* Idempotency key ``greenhouse:{board_token}:{job.id}:{updated_at}``: the posting key
  is built by ``base`` from ``vendor:identifier:external_id`` and
  ``ObservedPosting.version`` is ``updated_at`` (normalised to UTC by ``iso_utc``).
* Board liveness is the shared ADR 0015 rule: a 200 calls
  :func:`~tell.collectors.base.record_board_ok`, a 404 calls
  :func:`~tell.collectors.base.record_board_not_found` (``dead`` after two 404s at
  least 12 hours apart, plus one ``board_verify`` review item). A 404 never advances
  ``last_polled_at``; this module never writes ``last_verified_at``.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, override
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, PrivateAttr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    CollectorSpec,
    ObservedPosting,
    RunResult,
    iso_utc,
    listed_id,
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
    "SOURCE",
    "WORKPLACE_TYPE",
    "BoardNotFoundError",
    "BoardRunResult",
    "GreenhouseBoard",
    "GreenhouseDepartment",
    "GreenhouseJob",
    "GreenhouseJobsResponse",
    "GreenhouseLocation",
    "GreenhouseMetadata",
    "GreenhouseOffice",
    "board_url",
    "content_to_text",
    "fetch_board",
    "fetch_board_meta",
    "infer_remote",
    "ingest_board",
    "jobs_url",
    "parse",
    "run_fixture",
    "shared_budget",
    "validate_board",
]

SOURCE = "greenhouse"
API_BASE = "https://boards-api.greenhouse.io/v1/boards"
WORKPLACE_TYPE = "workplace type"
"""Metadata entry name (case-insensitive) that carries Remote / Hybrid / Onsite."""

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE
FIXTURE_DOMAIN = "greenhouse-fixture.test"
FIXTURE_BOARD = "tell-fixture"

_logger = logging.getLogger(__name__)
_REMOTE_WORD = re.compile(r"\bremote\b", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_REMOTE_VALUES = frozenset({"remote", "fully remote", "remote only"})
_NOT_REMOTE_VALUES = frozenset(
    {"hybrid", "onsite", "on-site", "on site", "in office", "in-office", "office"}
)


# --- the contract ----------------------------------------------------------------------


class GreenhouseLocation(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None  # present, but null on 1 of 1,918 jobs (2026-10-14)


class GreenhouseDepartment(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str


class GreenhouseOffice(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    location: str | None = None


class GreenhouseMetadata(BaseModel):
    """One custom field; ``value`` is a string, list, bool, number or object by type."""

    model_config = ConfigDict(extra="allow")

    name: str
    value: Any = None
    value_type: str | None = None


class GreenhouseJob(BaseModel):
    """One element of ``jobs[]`` as ``?content=true`` returns it."""

    model_config = ConfigDict(extra="allow")

    id: int
    title: str
    location: GreenhouseLocation
    absolute_url: str
    first_published: AwareDatetime | None
    updated_at: AwareDatetime
    departments: list[GreenhouseDepartment]
    offices: list[GreenhouseOffice]
    content: str
    metadata: list[GreenhouseMetadata] | None


class GreenhouseMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    total: int


class GreenhouseJobsResponse(BaseModel):
    """``GET /v1/boards/{board_token}/jobs?content=true``."""

    model_config = ConfigDict(extra="allow")

    jobs: list[GreenhouseJob]
    meta: GreenhouseMeta
    _invalid_ids: tuple[str | None, ...] = PrivateAttr(default=())

    @property
    def invalid_jobs(self) -> int:
        """Jobs :func:`validate_board` skipped because they broke :class:`GreenhouseJob`."""
        return len(self._invalid_ids)

    @property
    def invalid_ids(self) -> tuple[str | None, ...]:
        """The skipped jobs' ids (``None`` when unusable), for ``sync_board_postings``."""
        return self._invalid_ids


class _GreenhouseEnvelope(BaseModel):
    """The jobs response with each job left raw, so one bad job cannot fail the board."""

    model_config = ConfigDict(extra="allow")

    jobs: list[Any]
    meta: GreenhouseMeta


@dataclass(frozen=True)
class BoardRunResult(RunResult):
    """One board poll; ``errors`` counts jobs skipped because they failed validation."""


def _error_paths(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<job>'} ({err['type']})"
        for err in exc.errors()
    )


def validate_board(content: str | bytes, *, identifier: str) -> GreenhouseJobsResponse:
    """Validate a jobs response job by job; invalid jobs are skipped, logged and counted.

    The envelope (``jobs``, ``meta.total``) is strict. A job failing
    :class:`GreenhouseJob` is dropped with one WARNING naming ``identifier``, the job id
    and the field paths, and counted in :attr:`GreenhouseJobsResponse.invalid_jobs`.
    When every job of a non-empty board fails, the strict board-level error is raised.
    """
    envelope = _GreenhouseEnvelope.model_validate_json(content)
    valid: list[GreenhouseJob] = []
    invalid_ids: list[str | None] = []
    for index, item in enumerate(envelope.jobs):
        try:
            valid.append(GreenhouseJob.model_validate(item))
        except ValidationError as exc:
            job_id = listed_id(item)
            invalid_ids.append(job_id)
            _logger.warning(
                "greenhouse board %s: skipped invalid job %s (jobs.%d): %s",
                identifier,
                job_id,
                index,
                _error_paths(exc),
            )
    if envelope.jobs and not valid:
        GreenhouseJobsResponse.model_validate_json(content)  # raises the board-level error
    response = GreenhouseJobsResponse(jobs=valid, meta=envelope.meta)
    response._invalid_ids = tuple(invalid_ids)  # set once, right after build
    return response


class GreenhouseBoard(BaseModel):
    """``GET /v1/boards/{board_token}``: the board's display name (discovery verification)."""

    model_config = ConfigDict(extra="allow")

    name: str
    content: str | None = None


# --- fetching --------------------------------------------------------------------------


class BoardNotFoundError(LookupError):
    """The board token answered 404 (usually a renamed or removed board)."""

    def __init__(self, board_token: str) -> None:
        super().__init__(f"greenhouse board {board_token!r} returned 404")
        self.board_token = board_token


def board_url(board_token: str) -> str:
    """``https://boards-api.greenhouse.io/v1/boards/{board_token}`` (token path-quoted)."""
    return f"{API_BASE}/{quote(board_token, safe='')}"


def jobs_url(board_token: str) -> str:
    """The jobs list URL without its query string (``content=true`` is passed as a param)."""
    return f"{board_url(board_token)}/jobs"


@lru_cache(maxsize=1)
def shared_budget() -> RateBudget:
    """One token bucket for the Greenhouse host at ``TELL_RATE_ATS_RPS``, shared by workers."""
    return RateBudget(get_settings().tell_rate_ats_rps)


def _get(
    client: httpx.Client,
    url: str,
    board_token: str,
    *,
    params: Mapping[str, str] | None,
    budget: RateBudget | None,
    sleep: Sleeper,
) -> httpx.Response:
    response = fetch(
        url,
        client=client,
        params=params,
        budget=budget if budget is not None else shared_budget(),
        sleep=sleep,
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        raise BoardNotFoundError(board_token)
    response.raise_for_status()
    return response


def fetch_board(
    client: httpx.Client,
    board_token: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> GreenhouseJobsResponse:
    """Fetch and validate one board's jobs (``?content=true``).

    Retries 429/5xx with the shared backoff (:func:`tell.http.fetch`), raises
    :class:`BoardNotFoundError` on 404, :class:`httpx.HTTPStatusError` on any other
    4xx. Jobs that break the contract are skipped and counted (:func:`validate_board`);
    a drifted envelope, or a board on which every job fails, raises
    :class:`pydantic.ValidationError`.
    """
    response = _get(
        client,
        jobs_url(board_token),
        board_token,
        params={"content": "true"},
        budget=budget,
        sleep=sleep,
    )
    return validate_board(response.content, identifier=board_token)


def fetch_board_meta(
    client: httpx.Client,
    board_token: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> str:
    """The board's company name from ``GET /v1/boards/{token}`` (spec section 4 step 4)."""
    response = _get(
        client, board_url(board_token), board_token, params=None, budget=budget, sleep=sleep
    )
    return GreenhouseBoard.model_validate_json(response.content).name


# --- parsing ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Collects text nodes, skipping ``<script>``/``<style>``; entities are decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    @override
    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def content_to_text(content: str) -> str:
    """Plain text of Greenhouse's entity-encoded ``content``.

    The field is HTML that has been entity-encoded once more (``&lt;p&gt;A &amp;amp;
    B&lt;/p&gt;``): ``html.unescape`` once yields the HTML, the parser drops the tags
    and decodes the HTML's own entities, and whitespace is collapsed.
    """
    parser = _TextExtractor()
    parser.feed(html.unescape(content))
    parser.close()
    return _WHITESPACE.sub(" ", " ".join(parser.parts)).strip()


def _metadata_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _workplace_type(metadata: Sequence[GreenhouseMetadata] | None) -> str | None:
    for entry in metadata or ():
        if entry.name.strip().lower() == WORKPLACE_TYPE:
            values = [v.strip() for v in _metadata_values(entry.value) if v.strip()]
            if values:
                return ", ".join(values)
    return None


def infer_remote(workplace_type: str | None, location: str | None) -> bool | None:
    """``True``/``False`` from the Workplace Type value, else ``True`` if the location says remote.

    A Workplace Type of Remote means remote; Hybrid / Onsite mean not remote. With no
    usable Workplace Type, a location naming "remote" (``US-Remote``, ``Remote in
    the US``) means remote; otherwise the flag is unknown (``None``).
    """
    if workplace_type is not None:
        values = {part.strip().lower() for part in workplace_type.split(",")}
        if values & _REMOTE_VALUES:
            return True
        if values & _NOT_REMOTE_VALUES:
            return False
    if location and _REMOTE_WORD.search(location):
        return True
    return None


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = _WHITESPACE.sub(" ", text).strip()
    return cleaned or None


def _observe(job: GreenhouseJob) -> ObservedPosting:
    departments = [name for d in job.departments if (name := _clean(d.name))]
    offices = [name for o in job.offices if (name := _clean(o.name))]
    location = _clean(job.location.name)
    workplace_type = _workplace_type(job.metadata)
    return ObservedPosting(
        external_id=str(job.id),
        title=_clean(job.title) or job.title,
        url=job.absolute_url,
        version=iso_utc(job.updated_at),
        department=departments[0] if departments else None,
        location=location,
        remote=infer_remote(workplace_type, location),
        posted_at=job.first_published,
        description_plain=content_to_text(job.content),
        extra={
            "departments": departments,
            "offices": offices,
            "workplace_type": workplace_type,
        },
    )


def parse(
    raw: GreenhouseJobsResponse | Mapping[str, Any] | str | bytes,
) -> list[ObservedPosting]:
    """Validate one jobs response (model, JSON text or decoded dict) into postings."""
    if isinstance(raw, GreenhouseJobsResponse):
        response = raw
    elif isinstance(raw, str | bytes):
        response = GreenhouseJobsResponse.model_validate_json(raw)
    else:
        response = GreenhouseJobsResponse.model_validate(raw)
    return [_observe(job) for job in response.jobs]


# --- ingesting -------------------------------------------------------------------------


def _sync(
    session: Session,
    board: AtsBoard,
    response: GreenhouseJobsResponse,
    *,
    run_id: str,
    now: datetime,
) -> BoardRunResult:
    observed = parse(response)
    events = sync_board_postings(
        session, board, observed, run_id=run_id, now=now, invalid=response.invalid_ids
    )
    stats = write_events(session, events)
    record_board_ok(session, board, now=now)
    return BoardRunResult(
        fetched=len(response.jobs) + response.invalid_jobs,
        inserted=stats.inserted,
        skipped=stats.skipped,
        events=events,
        errors=response.invalid_jobs,
    )


def ingest_board(
    session: Session,
    board: AtsBoard,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> BoardRunResult:
    """Poll one Greenhouse board and write its ``job_*`` events (no commit).

    On success: ``sync_board_postings`` + ``write_events`` + ``record_board_ok``
    (``last_polled_at = now``, 404 streak reset, a ``dead`` board revived). On 404:
    nothing fetched, ``record_board_not_found`` (ADR 0015) and an empty result.
    Other HTTP failures propagate to the caller, which counts them as run errors.
    Jobs that fail validation are skipped and counted in ``errors``.
    """
    if board.vendor != SOURCE:
        msg = f"ingest_board expects a {SOURCE!r} board, got vendor {board.vendor!r}"
        raise ValueError(msg)
    own_client = client is None
    http = client if client is not None else make_client()
    try:
        response = fetch_board(http, board.identifier, budget=budget, sleep=sleep)
    except BoardNotFoundError:
        record_board_not_found(session, board, now=now)
        return BoardRunResult(fetched=0, inserted=0, skipped=0, events=[])
    finally:
        if own_client:
            http.close()
    return _sync(session, board, response, run_id=run_id, now=now)


# --- fixture replay (collector contract) -----------------------------------------------


def _fixture_board(session: Session) -> AtsBoard:
    board = session.scalars(
        select(AtsBoard).where(AtsBoard.vendor == SOURCE, AtsBoard.identifier == FIXTURE_BOARD)
    ).one_or_none()
    if board is not None:
        return board
    company = session.scalars(select(Company).where(Company.domain == FIXTURE_DOMAIN)).one_or_none()
    if company is None:
        company = Company(domain=FIXTURE_DOMAIN, name="Greenhouse Fixture")
        session.add(company)
        session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=SOURCE,
        identifier=FIXTURE_BOARD,
        discovered_from=board_url(FIXTURE_BOARD),
        confidence=Decimal("0.95"),
        status="active",
    )
    session.add(board)
    session.flush()
    return board


def run_fixture(session: Session, case_path: Path, /, *, run_id: str, now: datetime) -> RunResult:
    """Replay one recorded jobs response through the poll path and the state machine."""
    response = validate_board(case_path.read_text(encoding="utf-8"), identifier=FIXTURE_BOARD)
    return _sync(session, _fixture_board(session), response, run_id=run_id, now=now)


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="ats",
    response_model=GreenhouseJobsResponse,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="updated_at",
    run_fixture=run_fixture,
)
