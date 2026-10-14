"""Lever Postings API collector (spec section 3.2).

``GET https://api.lever.co/v0/postings/{site}?mode=json`` (EU tenants:
``https://api.eu.lever.co/v0/postings/{site}?mode=json``) returns a JSON array of
every published posting on the board. ``ats_boards.identifier`` is either ``site``
(global host) or ``eu:site`` (EU host).

Fields used: ``id``, ``text`` (title), ``categories.team`` / ``department`` /
``location`` / ``commitment`` / ``allLocations``, ``workplaceType``, ``createdAt``
(epoch milliseconds), ``hostedUrl``, ``descriptionPlain`` (hashed into
``content_hash`` and discarded) and ``salaryRange`` when present. The provider change
marker is ``createdAt``: the idempotency key is ``lever:{site}:{id}:{createdAt}``,
so ``ObservedPosting.version == str(createdAt)``.

Optional in practice (2026-10-14, 380 postings on 14 boards): ``categories.department``
is absent on 33 postings (6 boards), ``categories.commitment`` on 2, ``salaryRange`` on
150 and ``country`` is null on 1; the model already allows all of them. One bad posting
never fails a board: :func:`validate_board` (the poll path) validates each posting on
its own, skips and logs (WARNING, board and field path) the ones that break
:class:`LeverPosting` and counts them in :attr:`LeverPostings.invalid_jobs` and
``BoardRunResult.errors`` (added to ``runs.errors`` by ``tell ingest``); their ids go to
``sync_board_postings`` so a listed but invalid posting is never closed. Only when every
posting of a non-empty board fails does the board fail, with the strict error.

Board liveness is the shared ADR 0015 rule
(``docs/decisions/0015-ats-404-and-reverification.md``): a 200 calls
:func:`~tell.collectors.base.record_board_ok`, a 404 calls
:func:`~tell.collectors.base.record_board_not_found` (``dead`` after two 404s at least
12 hours apart, plus one ``board_verify`` review item). A 404 never advances
``last_polled_at``; this module never writes ``last_verified_at``. ``active`` and
``dead`` boards are polled (a dead board that answers 200 again is revived); any
other status (``paused``, ``no_board``, ``unsupported``) is skipped without a request.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, RootModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    CollectorSpec,
    ObservedPosting,
    RunResult,
    from_epoch_ms,
    listed_id,
    record_board_not_found,
    record_board_ok,
    sync_board_postings,
    write_events,
)
from tell.config import get_settings
from tell.http import HostRateBudgets, RateBudget, fetch, make_client
from tell.models import AtsBoard, Company

__all__ = [
    "COLLECTOR",
    "EU_PREFIX",
    "FIXTURE_DIR",
    "FIXTURE_SITE",
    "SOURCE",
    "BoardNotFoundError",
    "BoardRunResult",
    "LeverCategories",
    "LeverPosting",
    "LeverPostings",
    "LeverSalaryRange",
    "board_url",
    "commitment_tags",
    "fetch_board",
    "infer_remote",
    "ingest_board",
    "parse",
    "parse_identifier",
    "run_fixture",
    "validate_board",
]

SOURCE = "lever"
GLOBAL_API = "https://api.lever.co/v0/postings"
EU_API = "https://api.eu.lever.co/v0/postings"
EU_PREFIX = "eu:"
QUERY: dict[str, str] = {"mode": "json"}

POLLED_STATUSES = frozenset({"active", "dead"})
"""Board statuses :func:`ingest_board` requests; any other status is skipped."""

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE
FIXTURE_SITE = "tell-fixture"
"""Synthetic board identifier ``run_fixture`` creates once and then reuses."""
FIXTURE_DOMAIN = "lever-fixture.example"

_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TAG_WORDS: tuple[str, ...] = ("intern", "contract")
_MAX_EXTRA_CHARS = 200
"""Clip for strings copied into ``extra`` (base rejects anything over 500)."""

Sleeper = Callable[[float], None]
_logger = logging.getLogger(__name__)


# --- the contract ---------------------------------------------------------------------------


class LeverCategories(BaseModel):
    """``categories``: every key is optional; boards omit the ones they do not use."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    team: str | None = None
    department: str | None = None
    location: str | None = None
    commitment: str | None = None
    all_locations: list[str] = Field(default_factory=list, alias="allLocations")


class LeverSalaryRange(BaseModel):
    """``salaryRange`` (only on boards that publish pay): currency, interval, min, max."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    currency: str | None = None
    interval: str | None = None
    minimum: float | None = Field(default=None, alias="min")
    maximum: float | None = Field(default=None, alias="max")


class LeverPosting(BaseModel):
    """One element of the postings array; the fields spec section 3.2 relies on are required."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    categories: LeverCategories
    created_at: int = Field(alias="createdAt", gt=0)
    hosted_url: str = Field(alias="hostedUrl", min_length=1)
    description_plain: str = Field(alias="descriptionPlain")
    workplace_type: str = Field(alias="workplaceType")
    country: str | None = None
    salary_range: LeverSalaryRange | None = Field(default=None, alias="salaryRange")


class LeverPostings(RootModel[list[LeverPosting]]):
    """The whole response: a bare JSON array of postings (``[]`` for an empty board)."""

    _invalid_ids: tuple[str | None, ...] = PrivateAttr(default=())

    @property
    def invalid_jobs(self) -> int:
        """Postings :func:`validate_board` skipped because they broke :class:`LeverPosting`."""
        return len(self._invalid_ids)

    @property
    def invalid_ids(self) -> tuple[str | None, ...]:
        """The skipped ids (``None`` when unusable), for ``sync_board_postings``."""
        return self._invalid_ids


class _LeverEnvelope(RootModel[list[Any]]):
    """The response array with each posting left raw, so one bad posting cannot fail it."""


@dataclass(frozen=True)
class BoardRunResult(RunResult):
    """One board poll; ``errors`` counts postings skipped because they failed validation."""


def _error_paths(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<posting>'} ({err['type']})"
        for err in exc.errors()
    )


def validate_board(content: str | bytes, *, identifier: str) -> LeverPostings:
    """Validate a response posting by posting; invalid ones are skipped, logged and counted.

    The response must be a JSON array. A posting failing :class:`LeverPosting` is
    dropped with one WARNING naming ``identifier``, the posting id and the field paths,
    and counted in :attr:`LeverPostings.invalid_jobs`. When every posting of a non-empty
    board fails, the strict :class:`LeverPostings` error is raised instead.
    """
    items = _LeverEnvelope.model_validate_json(content).root
    valid: list[LeverPosting] = []
    invalid_ids: list[str | None] = []
    for index, item in enumerate(items):
        try:
            valid.append(LeverPosting.model_validate(item))
        except ValidationError as exc:
            posting_id = listed_id(item)
            invalid_ids.append(posting_id)
            _logger.warning(
                "lever board %s: skipped invalid posting %s (%d): %s",
                identifier,
                posting_id,
                index,
                _error_paths(exc),
            )
    if items and not valid:
        LeverPostings.model_validate_json(content)  # raises the board-level error
    postings = LeverPostings(valid)
    postings._invalid_ids = tuple(invalid_ids)  # set once, right after construction
    return postings


# --- identifiers and URLs -------------------------------------------------------------------


def parse_identifier(identifier: str) -> tuple[str, str]:
    """``"site"`` or ``"eu:site"`` -> ``(api_base, site)``; rejects anything else."""
    text = identifier.strip()
    base = GLOBAL_API
    if text.lower().startswith(EU_PREFIX):
        base = EU_API
        text = text[len(EU_PREFIX) :]
    if not _SITE_RE.fullmatch(text):
        msg = f"not a Lever site identifier: {identifier!r} (expected 'site' or 'eu:site')"
        raise ValueError(msg)
    return base, text


def board_url(identifier: str) -> str:
    """The documented list URL for a board, without the query string."""
    base, site = parse_identifier(identifier)
    return f"{base}/{site}"


# --- fetch ----------------------------------------------------------------------------------


class BoardNotFoundError(LookupError):
    """The board answered 404: the site was renamed, removed or never existed."""

    def __init__(self, identifier: str, url: str) -> None:
        super().__init__(f"Lever board {identifier!r} returned 404 at {url}")
        self.identifier = identifier
        self.url = url


@lru_cache(maxsize=4)
def _host_budgets(rps: float) -> HostRateBudgets:
    """One token bucket per Lever host, shared by every board in the process."""
    return HostRateBudgets(rps, burst=1)


def _default_budget(url: str) -> RateBudget:
    return _host_budgets(get_settings().tell_rate_ats_rps).for_url(url)


def fetch_board(
    client: httpx.Client | None,
    identifier: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> LeverPostings:
    """GET one board and validate it against :class:`LeverPostings`.

    Rate-limited per host by ``TELL_RATE_ATS_RPS`` unless ``budget`` is given; 429
    and 5xx are retried by :func:`tell.http.fetch` (``sleep`` is injectable).
    Raises :class:`BoardNotFoundError` on 404 and :class:`httpx.HTTPStatusError` on any
    other non-2xx status. Postings that break the contract are skipped and counted
    (:func:`validate_board`); a non-array response, or a board on which every posting
    fails, raises :class:`pydantic.ValidationError`.
    """
    url = board_url(identifier)
    own_client = client is None
    http = client if client is not None else make_client()
    try:
        response = fetch(
            url,
            client=http,
            params=QUERY,
            budget=budget if budget is not None else _default_budget(url),
            sleep=sleep,
        )
        if response.status_code == 404:
            raise BoardNotFoundError(identifier, str(response.request.url))
        response.raise_for_status()
        return validate_board(response.content, identifier=identifier)
    finally:
        if own_client:
            http.close()


# --- parse ----------------------------------------------------------------------------------


def infer_remote(workplace_type: str | None, *locations: str | None) -> bool | None:
    """``remote`` -> True, ``onsite``/``hybrid`` -> False, else "remote" in the location text."""
    kind = (workplace_type or "").strip().lower().replace("-", "").replace("_", "")
    if kind == "remote":
        return True
    if kind in {"onsite", "hybrid"}:
        return False
    if any(loc and "remote" in loc.lower() for loc in locations):
        return True
    return None


def commitment_tags(commitment: str | None) -> list[str]:
    """``["intern"]`` / ``["contract"]`` when the commitment says so (Phase 6 tags is_intern)."""
    lowered = (commitment or "").lower()
    return [word for word in _TAG_WORDS if word in lowered]


def _clip(value: str | None) -> str | None:
    return None if value is None else value[:_MAX_EXTRA_CHARS]


def _salary(salary: LeverSalaryRange | None) -> dict[str, Any] | None:
    if salary is None:
        return None
    return {
        "currency": _clip(salary.currency),
        "interval": _clip(salary.interval),
        "min": salary.minimum,
        "max": salary.maximum,
    }


def _observe(posting: LeverPosting) -> ObservedPosting:
    cats = posting.categories
    extra: dict[str, Any] = {
        "team": _clip(cats.team),
        "department": _clip(cats.department),
        "commitment": _clip(cats.commitment),
        "commitment_tags": commitment_tags(cats.commitment),
        "workplace_type": _clip(posting.workplace_type),
        "all_locations": [_clip(loc) for loc in cats.all_locations],
        "country": _clip(posting.country),
        "salary_range": _salary(posting.salary_range),
    }
    return ObservedPosting(
        external_id=posting.id,
        title=posting.text.strip(),
        url=posting.hosted_url,
        version=str(posting.created_at),
        department=cats.department or cats.team,
        location=cats.location,
        remote=infer_remote(posting.workplace_type, cats.location, *cats.all_locations),
        posted_at=from_epoch_ms(posting.created_at),
        description_plain=posting.description_plain,
        extra=extra,
    )


def parse(raw: LeverPostings | str | bytes) -> list[ObservedPosting]:
    """Map a Lever response (validated model or raw JSON) to observed postings."""
    postings = raw if isinstance(raw, LeverPostings) else LeverPostings.model_validate_json(raw)
    return [_observe(posting) for posting in postings.root]


# --- ingest ---------------------------------------------------------------------------------


def _apply_poll(
    session: Session, board: AtsBoard, raw: LeverPostings, *, run_id: str, now: datetime
) -> BoardRunResult:
    """Parse, reconcile, write: the shared tail of :func:`ingest_board` and :func:`run_fixture`."""
    observed = parse(raw)
    events = sync_board_postings(
        session, board, observed, run_id=run_id, now=now, invalid=raw.invalid_ids
    )
    stats = write_events(session, events)
    record_board_ok(session, board, now=now)
    return BoardRunResult(
        fetched=len(raw.root) + raw.invalid_jobs,
        inserted=stats.inserted,
        skipped=stats.skipped,
        events=events,
        errors=raw.invalid_jobs,
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
    """Poll one Lever board and write its events; the caller owns the run and the commit.

    Boards whose status is not ``active`` or ``dead`` are skipped without a request.
    A 404 returns an empty result and goes to ``record_board_not_found`` (ADR 0015);
    other HTTP failures propagate so the run records an error. Postings that fail
    validation are skipped and counted in ``errors``.
    """
    if board.vendor != SOURCE:
        msg = f"ingest_board expects a {SOURCE!r} board, got vendor {board.vendor!r}"
        raise ValueError(msg)
    if board.status not in POLLED_STATUSES:
        return BoardRunResult(fetched=0, inserted=0, skipped=0)
    try:
        raw = fetch_board(client, board.identifier, budget=budget, sleep=sleep)
    except BoardNotFoundError:
        record_board_not_found(session, board, now=now)
        return BoardRunResult(fetched=0, inserted=0, skipped=0)
    return _apply_poll(session, board, raw, run_id=run_id, now=now)


# --- fixture replay ---------------------------------------------------------------------------


def _fixture_board(session: Session) -> AtsBoard:
    board = session.scalars(
        select(AtsBoard).where(AtsBoard.vendor == SOURCE, AtsBoard.identifier == FIXTURE_SITE)
    ).first()
    if board is not None:
        return board
    company = session.scalars(select(Company).where(Company.domain == FIXTURE_DOMAIN)).first()
    if company is None:
        company = Company(domain=FIXTURE_DOMAIN, name="Lever Fixture Co")
        session.add(company)
        session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=SOURCE,
        identifier=FIXTURE_SITE,
        discovered_from="tests/fixtures/lever",
        confidence=Decimal("1.00"),
        status="active",
    )
    session.add(board)
    session.flush()
    return board


def run_fixture(session: Session, case_path: Path, /, *, run_id: str, now: datetime) -> RunResult:
    """Replay one fixture file through parse + ``sync_board_postings`` on a synthetic board."""
    raw = validate_board(case_path.read_text(encoding="utf-8"), identifier=FIXTURE_SITE)
    return _apply_poll(session, _fixture_board(session), raw, run_id=run_id, now=now)


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="ats",
    response_model=LeverPostings,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="createdAt",
    run_fixture=run_fixture,
)
