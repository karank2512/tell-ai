"""Collector base: the ``Event`` shape, idempotent writes, run lifecycle, posting state machine.

Everything a source adapter needs that is not source-specific lives here, so each
collector module (``greenhouse.py``, ``edgar.py``, ...) is only "fetch, parse with a
pydantic model, hand the results to this module".

Collector registration contract (binding for Phase 2 onwards)
==============================================================

Every collector module under ``tell/collectors/`` exposes a module-level
``COLLECTOR: CollectorSpec`` with:

``source``
    The ``events.source`` value and the fixture folder name, e.g. ``"greenhouse"``.
``kind``
    One of ``"ats"``, ``"filings"``, ``"news"``, ``"github"``. ATS collectors drive
    :func:`sync_board_postings`; the generic idempotency test applies the
    ``job_updated`` check only to ``kind == "ats"``.
``response_model``
    The pydantic v2 model that parses one raw provider response. It *is* the
    contract: a provider field that disappears must fail validation.
``fixture_dir``
    ``tests/fixtures/<source>``. Files the shared tests read, all JSON:
    ``normal*.json`` and ``empty*.json`` (must parse), ``drift.json`` (must raise
    ``pydantic.ValidationError`` mentioning ``drift_missing_field``) and, for ATS
    collectors, an optional ``updated.json`` (``normal`` with one provider version
    marker changed; must yield exactly one ``job_updated`` and no new ``job_opened``).
    Collectors with non-JSON upstreams (XML, RSS) keep their raw fixtures alongside
    under other extensions for their own tests, plus a JSON rendering for these.
``drift_missing_field``
    The field name ``drift.json`` omits; the contract test asserts it appears in the
    ``ValidationError`` text.
``run_fixture(session, case_path, *, run_id, now) -> RunResult``
    Replays one fixture file through the real parse + write path (``write_events``,
    and for ATS ``sync_board_postings``) with no network. ``fetched`` is the number
    of provider records in the file; ``inserted``/``skipped`` come from
    :class:`WriteStats`; ``events`` is the list handed to ``write_events``. It must
    flush but need not commit. What the generic idempotency test expects of an
    unchanged rerun differs by kind: a non-ATS collector re-derives the same events,
    so they are all skipped (``skipped == first.inserted + first.skipped``); an ATS
    collector's state machine returns no events at all, so ``events == []`` and
    ``inserted == skipped == 0``.

:func:`discover_collectors` finds these objects with ``pkgutil`` so
``tests/test_contracts.py`` and ``tests/test_idempotency.py`` never change when a
source is added. ``TELL_COLLECTORS_ONLY=greenhouse,lever`` narrows discovery to the
named modules (a local aid for parallel agents; see the function docstring).

Conventions every collector follows
-----------------------------------

* ``Event.event_id = sha256(f"{source}|{external_id}|{version}")`` where ``version``
  is the provider's own change marker. Re-fetching unchanged data yields the same id
  and ``write_events`` skips it (``ON CONFLICT DO NOTHING``).
* All datetimes are timezone-aware; versions derived from datetimes use
  :func:`iso_utc` so they are stable across session time zones.
* Payloads hold provider fields only, never personal data: no names, emails or
  phone numbers, and for Form D never the related-persons list. Keys named like PII
  are rejected at construction time, at any nesting depth. Do not spell the Form D
  element name for that list anywhere outside a ``#`` comment;
  ``tests/test_pii_guard.py`` greps for it.
* Posting descriptions are hashed into ``content_hash``, scanned for the tech keyword
  table (``tell/signals/tech_keywords.py``) and discarded; only the hash and the
  keyword names survive. ``ObservedPosting.extra`` rejects description-like keys and
  long strings so raw text cannot be smuggled into event payloads through it.
* ``postings.version_marker`` holds the provider marker last seen for each posting
  (set on insert and refreshed on every poll); ``job_updated`` fires when the
  incoming marker differs from it, never by looking earlier events up
  (``docs/decisions/0013-postings-version-marker.md``). Event payloads name the
  board by ``vendor`` + ``board_identifier``, never by the database-internal id.
* Reposts (spec section 7): a *new* ``external_id`` whose requisition fingerprint
  matches a posting on the same board that closed within 30 days, or that has the same
  fingerprint and ``content_hash`` as a posting seen (open or closed) within 7 days,
  gets ``postings.repost_of`` and emits ``job_reposted`` instead of ``job_opened``.
  Every job event payload carries ``is_intern`` and ``repost_of_external_id`` so
  velocity can exclude interns and reposts from the payload alone.
* Tech keywords: a keyword found in a new (or newly re-worded) posting's description
  that the company (or, for a board not yet resolved to a company, the board) has never
  had a ``tech_mentioned_first_time`` event for emits one. This is the one place the
  state machine reads ``events`` back.
* Form D amendments (``D/A``) call :func:`update_event_payload` on the original
  ``form_d_filed`` event and never write a second funding event.
* One run = ``start_run`` ... ``finish_run``; ``finish_run`` emits exactly one JSON
  log line (spec section 1) on the ``tell.runs`` logger.
* Board liveness is one rule for every ATS collector
  (``docs/decisions/0015-ats-404-and-reverification.md``): a poll that answers 200
  calls :func:`record_board_ok` after :func:`sync_board_postings`; a poll that answers
  404 calls :func:`record_board_not_found` and nothing else. A 404 never advances
  ``last_polled_at``, so the ``job_closed`` rule counts only real polls. Collectors
  never write ``ats_boards.last_verified_at``: that column belongs to discovery's
  ownership verification (spec section 4 step 5).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import pkgutil
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import pydantic
from sqlalchemy import bindparam, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tell.db import upsert
from tell.models import AtsBoard, EventRow, Posting, PostingObservation, ReviewQueueItem, Run
from tell.signals.taxonomy import fingerprint as make_fingerprint
from tell.signals.taxonomy import is_intern, normalize_title, role_family
from tell.signals.tech_keywords import extract_tech_keywords

__all__ = [
    "BOARD_VERIFY_KIND",
    "CLOSE_GAP",
    "COLLECTORS_ONLY_ENV",
    "COLLECTOR_KINDS",
    "COMMITMENT_KEYS",
    "EXCLUDED_AFTER",
    "EXTRA_MAX_CHARS",
    "EXTRA_TEXT_KEYS",
    "JOB_CLOSED",
    "JOB_OPENED",
    "JOB_REPOSTED",
    "JOB_UPDATED",
    "NOT_FOUND_DEAD_AFTER",
    "NOT_FOUND_DEAD_GAP",
    "NOT_FOUND_REASON",
    "PII_KEYS",
    "REPOST_CLOSED_WINDOW",
    "REPOST_SAME_CONTENT_WINDOW",
    "STALE_AFTER",
    "STALE_WEIGHT",
    "TECH_MENTIONED_FIRST_TIME",
    "WRITE_BATCH",
    "CollectorKind",
    "CollectorSpec",
    "Event",
    "ObservedPosting",
    "RunFixture",
    "RunResult",
    "WriteStats",
    "content_hash",
    "discover_collectors",
    "finish_run",
    "from_epoch_ms",
    "iso_utc",
    "new_run_id",
    "parse_iso",
    "record_board_not_found",
    "record_board_ok",
    "sha256_hex",
    "stale_weight",
    "start_run",
    "sync_board_postings",
    "update_event_payload",
    "utcnow",
    "write_events",
]

CollectorKind = Literal["ats", "filings", "news", "github"]
COLLECTOR_KINDS: tuple[str, ...] = ("ats", "filings", "news", "github")

CLOSE_GAP = timedelta(hours=12)
"""Minimum gap between two consecutive polls without a posting before ``job_closed``."""

WRITE_BATCH = 500
"""Rows per ``INSERT ... ON CONFLICT DO NOTHING`` statement in :func:`write_events`."""

PII_KEYS: frozenset[str] = frozenset({"email", "phone", "first_name", "last_name"})
"""Payload keys rejected outright, at any depth (plus keys ending in ``_email`` / ``_phone``)."""

EXTRA_TEXT_KEYS: frozenset[str] = frozenset({"content", "body"})
"""``ObservedPosting.extra`` keys rejected outright, plus any key containing ``description``."""

EXTRA_MAX_CHARS = 500
"""Longest string allowed anywhere inside ``ObservedPosting.extra``; descriptions are longer."""

NOT_FOUND_DEAD_AFTER = 2
"""404 polls in a row (with no 200 in between) before a board can be marked ``dead``."""

NOT_FOUND_DEAD_GAP = timedelta(hours=12)
"""Minimum span from the first 404 of a streak to the one that marks the board ``dead``."""

BOARD_VERIFY_KIND = "board_verify"
"""``review_queue.kind`` opened when a board dies (shared with discovery)."""

NOT_FOUND_REASON = "not_found_twice"
"""``review_queue.payload.reason`` of the item :func:`record_board_not_found` opens."""

JOB_OPENED = "job_opened"
JOB_UPDATED = "job_updated"
JOB_CLOSED = "job_closed"
JOB_REPOSTED = "job_reposted"
TECH_MENTIONED_FIRST_TIME = "tech_mentioned_first_time"

REPOST_CLOSED_WINDOW = timedelta(days=30)
"""A new posting whose fingerprint closed on the same board this recently is a repost."""

REPOST_SAME_CONTENT_WINDOW = timedelta(days=7)
"""A new posting with the same fingerprint and ``content_hash`` as a posting seen this
recently (open or closed) on the same board is a repost."""

STALE_AFTER = timedelta(days=90)
"""A posting open longer than this weighs :data:`STALE_WEIGHT` in any magnitude it feeds."""

EXCLUDED_AFTER = timedelta(days=180)
"""A posting open longer than this weighs 0 (excluded)."""

STALE_WEIGHT = 0.25

COMMITMENT_KEYS: tuple[str, ...] = ("commitment", "employmentType", "type_of_employment")
"""``ObservedPosting.extra`` keys holding the provider's employment type (Lever, Ashby,
SmartRecruiters), read by :func:`tell.signals.taxonomy.is_intern`."""

_REPOST_CLOSED = "closed_within_30d"
_REPOST_SAME_CONTENT = "same_content_within_7d"

COLLECTORS_ONLY_ENV = "TELL_COLLECTORS_ONLY"
"""Comma-separated collector module names that :func:`discover_collectors` limits itself to."""

_WHITESPACE = re.compile(r"\s+")
_run_logger = logging.getLogger("tell.runs")


# --- shared helpers --------------------------------------------------------------


def utcnow() -> datetime:
    """Timezone-aware ``now`` in UTC."""
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    """ISO 8601 rendering in UTC; the only way datetimes become version strings."""
    _require_aware(value, "value")
    return value.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string (``Z`` accepted; naive means UTC) to an aware UTC datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def from_epoch_ms(milliseconds: int | float) -> datetime:
    """Epoch milliseconds (Lever's ``createdAt``) to an aware UTC datetime."""
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def sha256_hex(text: str) -> str:
    """Hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(description_plain: str | None) -> str | None:
    """SHA-256 of a posting description with whitespace collapsed; ``None`` when empty."""
    if description_plain is None:
        return None
    normalized = _WHITESPACE.sub(" ", description_plain).strip()
    return sha256_hex(normalized) if normalized else None


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be a timezone-aware datetime, got naive {value!r}"
        raise ValueError(msg)


def _require_json(value: Any, name: str) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be JSON-serialisable: {exc}"
        raise TypeError(msg) from exc


def _walk_json(value: Any, path: str) -> Iterable[tuple[str, str | None, Any]]:
    """Yield ``(path, key, value)`` for every node of a JSON-like value, depth first.

    ``key`` is the mapping key that holds ``value`` (``None`` for the root and for
    list items), ``path`` is a dotted/indexed locator for error messages.
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _walk_json(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, None, child
            yield from _walk_json(child, child_path)


def _is_pii_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in PII_KEYS or lowered.endswith(("_email", "_phone"))


def _reject_pii_keys(mapping: Mapping[str, Any], name: str) -> None:
    """Reject ``mapping`` when any key, however deeply nested, is named like personal data."""
    for path, key, _ in _walk_json(mapping, name):
        if key is not None and _is_pii_key(key):
            msg = f"{name} must not carry personal data; key {path!r} is not allowed"
            raise ValueError(msg)


def _reject_posting_text(extra: Mapping[str, Any], name: str) -> None:
    """Reject ``extra`` when it looks like it carries a posting description.

    Descriptions are hashed into ``content_hash`` and discarded; a key containing
    ``description`` (``descriptionPlain``, ``jobDescription``), a ``content`` /
    ``body`` key, or any string longer than :data:`EXTRA_MAX_CHARS` is refused at
    any depth so raw text cannot reach event payloads through ``extra``.
    """
    for path, key, value in _walk_json(extra, name):
        if key is not None:
            lowered = key.lower()
            if lowered in EXTRA_TEXT_KEYS or "description" in lowered:
                msg = f"{name} must not carry posting text; key {path!r} is not allowed"
                raise ValueError(msg)
        if isinstance(value, str) and len(value) > EXTRA_MAX_CHARS:
            msg = (
                f"{name} must not carry posting text; {path!r} is {len(value)} characters "
                f"(limit {EXTRA_MAX_CHARS})"
            )
            raise ValueError(msg)


def _event_id(source: str, external_id: str, version: str) -> str:
    return sha256_hex(f"{source}|{external_id}|{version}")


# --- the one event shape -------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Event:
    """One normalized observation from any source (spec section 8, table ``events``).

    ``occurred_at`` is when the thing happened per the provider (posting published,
    filing accepted); ``observed_at`` is when this run saw it. ``payload`` holds the
    provider fields we keep (JSON-serialisable, no PII). ``raw_ref`` is the
    ``tell-data`` JSONL path once published.
    """

    source: str
    type: str
    external_id: str
    version: str
    occurred_at: datetime
    observed_at: datetime
    url: str
    payload: dict[str, Any]
    run_id: str
    magnitude: float = 1
    company_id: int | None = None
    raw_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("source", "type", "external_id", "version", "url", "run_id"):
            if not getattr(self, name):
                msg = f"Event.{name} must be a non-empty string"
                raise ValueError(msg)
        _require_aware(self.occurred_at, "Event.occurred_at")
        _require_aware(self.observed_at, "Event.observed_at")
        _require_json(self.payload, "Event.payload")
        _reject_pii_keys(self.payload, "Event.payload")

    @property
    def event_id(self) -> str:
        """``sha256(source|external_id|version)``: the natural key of ``events``."""
        return _event_id(self.source, self.external_id, self.version)

    def to_row(self) -> dict[str, Any]:
        """Column values for ``events`` (keys match :class:`tell.models.EventRow`)."""
        return {
            "event_id": self.event_id,
            "company_id": self.company_id,
            "source": self.source,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "magnitude": self.magnitude,
            "url": self.url,
            "payload": self.payload,
            "raw_ref": self.raw_ref,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class WriteStats:
    """Outcome of :func:`write_events`: exact counts, as the ``runs`` row reports them."""

    inserted: int
    skipped: int

    @property
    def total(self) -> int:
        return self.inserted + self.skipped


def write_events(session: Session, events: Iterable[Event]) -> WriteStats:
    """Insert events with ``ON CONFLICT (event_id) DO NOTHING`` and report exact counts.

    Duplicate ids inside one call count as skipped, as do rows already present.
    ``inserted`` comes from ``RETURNING`` so it is exact regardless of batching.
    """
    rows: dict[str, dict[str, Any]] = {}
    skipped = 0
    for event in events:
        if event.event_id in rows:
            skipped += 1
            continue
        rows[event.event_id] = event.to_row()
    values = list(rows.values())
    inserted = 0
    for start in range(0, len(values), WRITE_BATCH):
        chunk = values[start : start + WRITE_BATCH]
        inserted += upsert(session, EventRow, chunk, conflict_columns=["event_id"])
    return WriteStats(inserted=inserted, skipped=skipped + len(values) - inserted)


def update_event_payload(session: Session, event_id: str, patch: Mapping[str, Any]) -> bool:
    """Shallow-merge ``patch`` into ``events.payload`` (JSONB ``||``) for one event.

    Returns ``True`` when a row was updated and ``False`` when no such event exists.
    Never inserts: this is how a Form D amendment refreshes the original
    ``form_d_filed`` event instead of becoming a second funding signal.
    """
    patch_dict = dict(patch)
    _require_json(patch_dict, "patch")
    _reject_pii_keys(patch_dict, "patch")
    merged = EventRow.payload.op("||", return_type=JSONB())(
        bindparam("payload_patch", patch_dict, type_=JSONB())
    )
    stmt = update(EventRow).where(EventRow.event_id == event_id).values(payload=merged)
    # Session.execute() is typed as a generic Result; an UPDATE always yields a CursorResult.
    result = cast("CursorResult[Any]", session.execute(stmt))
    return result.rowcount == 1


# --- runs lifecycle --------------------------------------------------------------------


def new_run_id(source: str, now: datetime | None = None) -> str:
    """``<source>-<UTC yyyymmddThhmmss>-<6 hex>``: sortable, unique, readable in logs."""
    stamp = (now or utcnow()).astimezone(UTC)
    return f"{source}-{stamp:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def start_run(session: Session, source: str, *, now: datetime | None = None) -> str:
    """Insert a ``runs`` row with ``status='running'`` and return its ``run_id``."""
    if not source:
        msg = "source must be a non-empty string"
        raise ValueError(msg)
    started = now or utcnow()
    _require_aware(started, "now")
    run_id = new_run_id(source, started)
    session.add(
        Run(
            run_id=run_id,
            source=source,
            started_at=started,
            status="running",
            fetched=0,
            inserted=0,
            skipped=0,
            errors=0,
        )
    )
    session.flush()
    return run_id


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: Literal["ok", "failed"],
    fetched: int,
    inserted: int,
    skipped: int,
    errors: int = 0,
    notes: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Run:
    """Close the ``runs`` row and emit the one JSON log line per run (spec section 1).

    The line carries exactly ``run_id, source, started_at, finished_at, fetched,
    inserted, skipped, errors`` on logger ``tell.runs`` at INFO.
    """
    if status not in ("ok", "failed"):
        msg = f"status must be 'ok' or 'failed', got {status!r}"
        raise ValueError(msg)
    run = session.get(Run, run_id)
    if run is None:
        msg = f"unknown run_id {run_id!r}; call start_run first"
        raise LookupError(msg)
    finished = now or utcnow()
    _require_aware(finished, "now")
    notes_dict = dict(notes) if notes is not None else None
    if notes_dict is not None:
        _require_json(notes_dict, "notes")
    run.finished_at = finished
    run.fetched = fetched
    run.inserted = inserted
    run.skipped = skipped
    run.errors = errors
    run.status = status
    run.notes = notes_dict
    session.flush()
    _run_logger.info(
        json.dumps(
            {
                "run_id": run.run_id,
                "source": run.source,
                "started_at": iso_utc(run.started_at),
                "finished_at": iso_utc(finished),
                "fetched": fetched,
                "inserted": inserted,
                "skipped": skipped,
                "errors": errors,
            },
            sort_keys=True,
        )
    )
    return run


# --- ATS posting state machine -------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ObservedPosting:
    """One posting as a provider lists it right now, already parsed and scrubbed.

    ``version`` is the provider's change marker (``updated_at``, ``createdAt``,
    ``publishedAt``). ``description_plain`` is hashed into ``content_hash`` and scanned
    into ``tech_keywords`` (keyword names only) at construction, then discarded; it is
    never stored. ``extra`` is merged into
    event payloads under ``"extra"`` and must carry neither personal data nor
    posting text: description-like keys and strings over :data:`EXTRA_MAX_CHARS`
    are rejected (see :func:`_reject_posting_text`).
    """

    external_id: str
    title: str
    url: str
    version: str
    department: str | None = None
    location: str | None = None
    remote: bool | None = None
    posted_at: datetime | None = None
    description_plain: str | None = field(default=None, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = field(init=False, default=None)
    tech_keywords: tuple[str, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        for name in ("external_id", "title", "url", "version"):
            if not getattr(self, name):
                msg = f"ObservedPosting.{name} must be a non-empty string"
                raise ValueError(msg)
        if self.posted_at is not None:
            _require_aware(self.posted_at, "ObservedPosting.posted_at")
        _require_json(self.extra, "ObservedPosting.extra")
        _reject_pii_keys(self.extra, "ObservedPosting.extra")
        _reject_posting_text(self.extra, "ObservedPosting.extra")
        object.__setattr__(self, "content_hash", content_hash(self.description_plain))
        object.__setattr__(self, "tech_keywords", extract_tech_keywords(self.description_plain))
        object.__setattr__(self, "description_plain", None)

    @property
    def commitment(self) -> str | None:
        """The provider's employment type from ``extra`` (first of :data:`COMMITMENT_KEYS`)."""
        for key in COMMITMENT_KEYS:
            value = self.extra.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None


_Pair = tuple[Posting, ObservedPosting]


@dataclass(frozen=True)
class _Repost:
    """A new posting recognised as a repost of ``original`` (spec section 7)."""

    posting: Posting
    obs: ObservedPosting
    original: Posting
    reason: str


@dataclass
class _Reconciled:
    """What one poll did to ``postings`` (see :func:`_reconcile_postings`)."""

    existing: dict[str, Posting]
    opened: list[_Pair] = field(default_factory=list)
    reposted: list[_Repost] = field(default_factory=list)
    changed: list[_Pair] = field(default_factory=list)
    worded: list[tuple[Posting, ObservedPosting, datetime]] = field(default_factory=list)
    """New postings and postings whose ``content_hash`` changed, with the time their
    description is dated to (``posted_at`` for a new posting, else ``now``)."""


def stale_weight(posting: Posting, now: datetime) -> float:
    """Weight of an open posting in any magnitude it feeds (spec section 7, stale requisitions).

    Age is ``now - posted_at`` (``first_seen_at`` when the provider gave no date):
    ``1.0`` up to and including 90 days, :data:`STALE_WEIGHT` (``0.25``) beyond 90
    days, ``0.0`` (excluded) beyond 180 days.
    """
    _require_aware(now, "now")
    age = now - (posting.posted_at or posting.first_seen_at)
    if age > EXCLUDED_AFTER:
        return 0.0
    if age > STALE_AFTER:
        return STALE_WEIGHT
    return 1.0


def _posting_key(board: AtsBoard, external_id: str) -> str:
    return f"{board.vendor}:{board.identifier}:{external_id}"


def _opened_version(posting: Posting) -> str:
    """``job_opened`` is keyed on the first ``posted_at`` seen, else the first sighting."""
    return iso_utc(posting.posted_at or posting.first_seen_at)


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else iso_utc(value)


def _job_event(
    board: AtsBoard,
    posting: Posting,
    *,
    type_: str,
    version: str,
    occurred_at: datetime,
    now: datetime,
    run_id: str,
    marker: str | None,
    extra: Mapping[str, Any],
    commitment: str | None,
    repost_of_external_id: str | None,
    repost_reason: str | None = None,
) -> Event:
    # The board is named by vendor + identifier, never by its database-internal id,
    # so payloads stay meaningful in the JSONL archive and across database rebuilds.
    payload: dict[str, Any] = {
        "vendor": board.vendor,
        "board_identifier": board.identifier,
        "posting_external_id": posting.external_id,
        "title": posting.title,
        "title_norm": posting.title_norm,
        "role_family": posting.role_family,
        "department": posting.department,
        "location": posting.location,
        "remote": posting.remote,
        "posted_at": _optional_iso(posting.posted_at),
        "first_seen_at": iso_utc(posting.first_seen_at),
        "last_seen_at": iso_utc(posting.last_seen_at),
        "closed_at": _optional_iso(posting.closed_at),
        "fingerprint": posting.fingerprint,
        "content_hash": posting.content_hash,
        "version_marker": marker,
        "is_intern": is_intern(posting.title, commitment),
        "repost_of_external_id": repost_of_external_id,
        "repost_reason": repost_reason,
        "extra": dict(extra),
    }
    return Event(
        source=board.vendor,
        type=type_,
        external_id=_posting_key(board, posting.external_id),
        version=version,
        occurred_at=occurred_at,
        observed_at=now,
        url=posting.url,
        payload=payload,
        company_id=board.company_id,
        run_id=run_id,
    )


def _apply_observation(posting: Posting, obs: ObservedPosting, now: datetime) -> None:
    title_norm = normalize_title(obs.title)
    posting.title = obs.title
    posting.title_norm = title_norm
    posting.role_family = role_family(obs.title, obs.department)
    posting.fingerprint = make_fingerprint(title_norm, obs.department, obs.location, obs.remote)
    posting.department = obs.department
    posting.location = obs.location
    posting.remote = obs.remote
    if posting.posted_at is None:
        # Keep the first posted_at seen: job_opened is keyed on it.
        posting.posted_at = obs.posted_at
    posting.url = obs.url
    posting.content_hash = obs.content_hash
    posting.version_marker = obs.version
    posting.last_seen_at = now
    posting.closed_at = None


def _ensure_persistent(session: Session, board: AtsBoard) -> None:
    if not sa_inspect(board).persistent:
        session.add(board)
        session.flush()


def _repost_original(
    candidates: Iterable[Posting],
    seen: Mapping[str, ObservedPosting],
    posting: Posting,
    now: datetime,
) -> tuple[Posting, str] | None:
    """The posting that a *new* ``posting`` reposts, and why; ``None`` when it is new.

    Candidates are the board's postings that are *absent* from this poll: a repost
    replaces an earlier listing, it does not sit beside it. Two postings listed side
    by side with one fingerprint are one requisition, which velocity handles by
    counting distinct fingerprints, not by calling either one a repost.

    Rule 1 (``closed_within_30d``): same fingerprint, ``closed_at`` within
    :data:`REPOST_CLOSED_WINDOW` of ``now``; the most recently closed wins. Rule 2
    (``same_content_within_7d``): same fingerprint and the same non-null
    ``content_hash``, ``last_seen_at`` within :data:`REPOST_SAME_CONTENT_WINDOW`, open
    or closed; the most recently seen wins. Rule 1 is tried first.
    """
    closed: list[Posting] = []
    same_content: list[Posting] = []
    for candidate in candidates:
        if candidate.external_id in seen or candidate.fingerprint != posting.fingerprint:
            continue
        if candidate.closed_at is not None and now - candidate.closed_at <= REPOST_CLOSED_WINDOW:
            closed.append(candidate)
        elif (
            posting.content_hash is not None
            and candidate.content_hash == posting.content_hash
            and now - candidate.last_seen_at <= REPOST_SAME_CONTENT_WINDOW
        ):
            same_content.append(candidate)
    if closed:
        return max(closed, key=lambda p: (p.closed_at or now, p.external_id)), _REPOST_CLOSED
    if same_content:
        return max(
            same_content, key=lambda p: (p.last_seen_at, p.external_id)
        ), _REPOST_SAME_CONTENT
    return None


def _reconcile_postings(
    session: Session, board: AtsBoard, seen: Mapping[str, ObservedPosting], now: datetime
) -> _Reconciled:
    """Upsert ``postings`` for one poll and classify what happened.

    * ``opened``: new ``external_id`` values that are not reposts.
    * ``reposted``: new ``external_id`` values recognised by :func:`_repost_original`;
      their ``repost_of`` is set to the original posting's id.
    * ``changed``: revisited postings whose incoming provider marker differs from the
      ``version_marker`` stored by the previous poll, decided *before* the row is
      overwritten. A stored marker of ``NULL`` (a row written before migration
      ``0002``) has no previous value to compare against: the marker is recorded and
      nothing is reported as changed.
    * ``worded``: new postings plus revisited postings whose ``content_hash`` changed,
      i.e. the descriptions the tech-keyword scan has not seen yet.

    Repost candidates are the postings already on the board that this poll does not
    list (see :func:`_repost_original`).
    """
    existing: dict[str, Posting] = {
        posting.external_id: posting
        for posting in session.scalars(select(Posting).where(Posting.board_id == board.id))
    }
    result = _Reconciled(existing=existing)
    for external_id, obs in seen.items():
        posting = existing.get(external_id)
        if posting is None:
            posting = Posting(
                board_id=board.id,
                external_id=external_id,
                fingerprint="",
                title=obs.title,
                title_norm="",
                role_family="",
                first_seen_at=now,
                last_seen_at=now,
                url=obs.url,
            )
            _apply_observation(posting, obs, now)
            original = _repost_original(existing.values(), seen, posting, now)
            session.add(posting)
            existing[external_id] = posting
            if original is None:
                result.opened.append((posting, obs))
            else:
                result.reposted.append(_Repost(posting, obs, original[0], original[1]))
            result.worded.append((posting, obs, posting.posted_at or now))
        else:
            previous_marker = posting.version_marker
            previous_hash = posting.content_hash
            _apply_observation(posting, obs, now)
            if previous_marker is not None and previous_marker != obs.version:
                result.changed.append((posting, obs))
            if obs.content_hash is not None and obs.content_hash != previous_hash:
                result.worded.append((posting, obs, now))
    session.flush()
    for repost in result.reposted:
        repost.posting.repost_of = repost.original.id
    if result.reposted:
        session.flush()
    return result


def _record_observations(
    session: Session, postings: Sequence[Posting], *, run_id: str, now: datetime
) -> None:
    if not postings:
        return
    upsert(
        session,
        PostingObservation,
        [{"posting_id": p.id, "observed_at": now, "run_id": run_id} for p in postings],
        conflict_columns=["posting_id", "observed_at"],
    )


def _repost_external_ids(existing: Mapping[str, Posting]) -> dict[int, str]:
    """``postings.id -> external_id`` for the board, to name ``repost_of`` in payloads."""
    return {posting.id: posting.external_id for posting in existing.values()}


def _opened_events(
    board: AtsBoard, opened: Sequence[_Pair], *, run_id: str, now: datetime
) -> list[Event]:
    return [
        _job_event(
            board,
            posting,
            type_=JOB_OPENED,
            version=_opened_version(posting),
            occurred_at=posting.posted_at or now,
            now=now,
            run_id=run_id,
            marker=obs.version,
            extra=obs.extra,
            commitment=obs.commitment,
            repost_of_external_id=None,
        )
        for posting, obs in opened
    ]


def _reposted_events(
    board: AtsBoard, reposted: Sequence[_Repost], *, run_id: str, now: datetime
) -> list[Event]:
    """``job_reposted`` in place of ``job_opened``: same key, never counted as velocity."""
    return [
        _job_event(
            board,
            repost.posting,
            type_=JOB_REPOSTED,
            version=_opened_version(repost.posting),
            occurred_at=repost.posting.posted_at or now,
            now=now,
            run_id=run_id,
            marker=repost.obs.version,
            extra=repost.obs.extra,
            commitment=repost.obs.commitment,
            repost_of_external_id=repost.original.external_id,
            repost_reason=repost.reason,
        )
        for repost in reposted
    ]


def _updated_events(
    board: AtsBoard,
    changed: Sequence[_Pair],
    names: Mapping[int, str],
    *,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """``job_updated`` for each posting whose provider marker changed since the last poll.

    The previous marker is ``postings.version_marker`` (ADR 0013), compared by
    :func:`_reconcile_postings` before the row is refreshed, so the decision never
    depends on which events still exist: pruning ``events`` cannot cause a spurious
    ``job_updated``, and the caller need not have written the previous poll's events
    for the next poll to behave. A marker that reverts to an earlier value (v1 -> v2
    -> v1) is a change like any other; if that exact ``job_updated`` was written once
    before, ``write_events`` skips it on conflict.
    """
    return [
        _job_event(
            board,
            posting,
            type_=JOB_UPDATED,
            version=obs.version,
            occurred_at=now,
            now=now,
            run_id=run_id,
            marker=obs.version,
            extra=obs.extra,
            commitment=obs.commitment,
            repost_of_external_id=names.get(posting.repost_of) if posting.repost_of else None,
        )
        for posting, obs in changed
    ]


def _closed_events(
    board: AtsBoard,
    existing: Mapping[str, Posting],
    seen: Mapping[str, ObservedPosting],
    names: Mapping[int, str],
    *,
    previous_poll: datetime | None,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """Close postings absent now and at the previous poll, when the polls are >= 12h apart.

    No observation is at hand for a closed posting, so its payload's ``is_intern`` is
    decided from the title alone (the provider's employment type is not stored).
    """
    if previous_poll is None or now - previous_poll < CLOSE_GAP:
        return []
    events: list[Event] = []
    for external_id, posting in existing.items():
        if external_id in seen or posting.closed_at is not None:
            continue
        if posting.last_seen_at >= previous_poll:
            continue  # it was present at the previous poll: first absence only
        posting.closed_at = now
        events.append(
            _job_event(
                board,
                posting,
                type_=JOB_CLOSED,
                version=iso_utc(now),
                occurred_at=now,
                now=now,
                run_id=run_id,
                marker=None,
                extra={},
                commitment=None,
                repost_of_external_id=names.get(posting.repost_of) if posting.repost_of else None,
            )
        )
    return events


def _tech_scope(board: AtsBoard) -> str:
    """Whose "first time" a keyword is: the company id, else ``vendor:identifier``."""
    if board.company_id is not None:
        return str(board.company_id)
    return f"{board.vendor}:{board.identifier}"


def _known_tech_keywords(session: Session, board: AtsBoard, keywords: Iterable[str]) -> set[str]:
    """Keywords that already have a ``tech_mentioned_first_time`` event in this scope."""
    keyword = EventRow.payload["keyword"].astext
    stmt = select(keyword).where(
        EventRow.type == TECH_MENTIONED_FIRST_TIME, keyword.in_(sorted(set(keywords)))
    )
    if board.company_id is not None:
        stmt = stmt.where(EventRow.company_id == board.company_id)
    else:
        stmt = stmt.where(
            EventRow.company_id.is_(None),
            EventRow.payload.contains({"scope": _tech_scope(board)}),
        )
    return set(session.scalars(stmt))


def _tech_events(
    session: Session,
    board: AtsBoard,
    worded: Sequence[tuple[Posting, ObservedPosting, datetime]],
    *,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """``tech_mentioned_first_time`` for keywords this scope has never had an event for.

    ``external_id = f"{scope}:{keyword}"`` and ``version`` is the posting's external
    id, where the scope is the company id, or ``vendor:identifier`` for a board not yet
    resolved to a company. The first posting of this poll (in provider order) that
    mentions the keyword is the receipt: the event's ``url`` is its URL and the payload
    names it by title and external id. No description text is ever included.

    Known limitation: events pruned to the JSONL archive (180 days) are no longer seen
    here, so a keyword that resurfaces after its first event was pruned emits again.
    """
    receipts: dict[str, tuple[Posting, datetime]] = {}
    for posting, obs, dated in worded:
        for keyword in obs.tech_keywords:
            receipts.setdefault(keyword, (posting, dated))
    if not receipts:
        return []
    known = _known_tech_keywords(session, board, receipts)
    scope = _tech_scope(board)
    return [
        Event(
            source=board.vendor,
            type=TECH_MENTIONED_FIRST_TIME,
            external_id=f"{scope}:{keyword}",
            version=posting.external_id,
            occurred_at=dated,
            observed_at=now,
            url=posting.url,
            payload={
                "keyword": keyword,
                "posting_title": posting.title,
                "posting_external_id": posting.external_id,
                "vendor": board.vendor,
                "board_identifier": board.identifier,
                "scope": scope,
            },
            company_id=board.company_id,
            run_id=run_id,
        )
        for keyword, (posting, dated) in receipts.items()
        if keyword not in known
    ]


def sync_board_postings(
    session: Session,
    board: AtsBoard,
    observed: Sequence[ObservedPosting],
    *,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """Reconcile one poll of ``board`` against ``postings`` and return the events to write.

    * Upserts ``postings`` on ``(board_id, external_id)``: ``first_seen_at`` on insert,
      ``last_seen_at = now`` and ``version_marker = <provider marker>`` always,
      ``closed_at = NULL`` when a posting reappears, ``posted_at`` kept from the first
      sighting that carried one.
    * Appends one ``posting_observations`` row per observed posting (idempotent on
      ``(posting_id, observed_at)``).
    * Emits ``job_opened`` once per posting (version = first ``posted_at`` seen, else
      ``first_seen_at``); ``job_updated`` when the provider marker differs from the
      ``version_marker`` stored by the previous poll (version = the new marker), never
      a second ``job_opened``; ``job_closed`` only when a posting is absent now, was
      absent at the previous poll too, and ``now - previous poll >= 12h`` (no closes
      on a first poll).
    * A new ``external_id`` that reposts an earlier requisition (spec section 7; see
      :func:`_repost_original`) gets ``repost_of`` and emits ``job_reposted`` (same key
      a ``job_opened`` would have had) instead of ``job_opened``. The same
      ``external_id`` coming back after a close is the same posting, not a repost: its
      ``closed_at`` is cleared and nothing is emitted.
    * Emits ``tech_mentioned_first_time`` for keywords in new or re-worded descriptions
      that the company (or board) has no such event for yet (:func:`_tech_events`).
    * Reads ``board.last_polled_at`` as the previous poll time, then sets it to ``now``.
      Only polls that answered 200 reach this function (a 404 goes to
      :func:`record_board_not_found`, which leaves ``last_polled_at`` alone), so the
      previous poll time is always a real observation of the board.

    The caller writes the returned events with :func:`write_events` in the same
    transaction. Only the tech-keyword check reads ``events`` back; an unchanged rerun
    returns no events at all, whatever ``events`` holds.
    """
    _require_aware(now, "now")
    _ensure_persistent(session, board)
    previous_poll = board.last_polled_at

    seen: dict[str, ObservedPosting] = {}
    for obs in observed:
        seen.setdefault(obs.external_id, obs)

    done = _reconcile_postings(session, board, seen, now)
    existing = done.existing
    _record_observations(
        session, [existing[external_id] for external_id in seen], run_id=run_id, now=now
    )
    names = _repost_external_ids(existing)

    events = _opened_events(board, done.opened, run_id=run_id, now=now)
    events += _reposted_events(board, done.reposted, run_id=run_id, now=now)
    events += _updated_events(board, done.changed, names, run_id=run_id, now=now)
    events += _closed_events(
        board, existing, seen, names, previous_poll=previous_poll, run_id=run_id, now=now
    )
    events += _tech_events(session, board, done.worded, run_id=run_id, now=now)

    board.last_polled_at = now
    session.flush()
    return events


# --- board liveness (ADR 0015) ---------------------------------------------------------


def _open_board_verify(session: Session, board: AtsBoard) -> list[ReviewQueueItem]:
    """Open ``board_verify`` items about ``board`` (vendor + identifier), whoever raised them."""
    stmt = (
        select(ReviewQueueItem)
        .where(
            ReviewQueueItem.kind == BOARD_VERIFY_KIND,
            ReviewQueueItem.status == "open",
            ReviewQueueItem.payload.contains(
                {"vendor": board.vendor, "identifier": board.identifier}
            ),
        )
        .order_by(ReviewQueueItem.id)
    )
    return list(session.scalars(stmt))


def record_board_not_found(session: Session, board: AtsBoard, *, now: datetime) -> str:
    """Record one 404 poll of ``board`` and return its resulting ``status``.

    * ``not_found_count`` goes up by one; ``first_not_found_at`` is set on the first
      404 of a streak and kept until :func:`record_board_ok` resets it.
    * When ``not_found_count >= 2`` and ``now - first_not_found_at >= 12h`` the board
      becomes ``dead`` and one ``review_queue`` item (``kind='board_verify'``, payload
      ``{board_id, vendor, identifier, reason: 'not_found_twice', first_not_found_at,
      last_not_found_at}``) is opened, unless an open ``board_verify`` item for the same
      vendor + identifier already exists. Two 404s inside 12 hours are not enough: the
      streak waits for a later 404 that is.
    * A ``paused`` board keeps its status (a human owns it); its count still advances.
    * ``last_polled_at`` and ``last_verified_at`` are never touched: a 404 is not an
      observation of the board's postings, and ownership verification is discovery's.

    Flushes; never commits.
    """
    _require_aware(now, "now")
    _ensure_persistent(session, board)
    board.not_found_count = (board.not_found_count or 0) + 1
    if board.first_not_found_at is None:
        board.first_not_found_at = now
    first = board.first_not_found_at
    dies = board.not_found_count >= NOT_FOUND_DEAD_AFTER and now - first >= NOT_FOUND_DEAD_GAP
    if dies and board.status != "paused":
        board.status = "dead"
        if not _open_board_verify(session, board):
            session.add(
                ReviewQueueItem(
                    kind=BOARD_VERIFY_KIND,
                    payload={
                        "board_id": board.id,
                        "vendor": board.vendor,
                        "identifier": board.identifier,
                        "reason": NOT_FOUND_REASON,
                        "first_not_found_at": iso_utc(first),
                        "last_not_found_at": iso_utc(now),
                    },
                    created_at=now,
                )
            )
    session.flush()
    return board.status


def record_board_ok(session: Session, board: AtsBoard, *, now: datetime) -> None:
    """Record one poll of ``board`` that answered 200; call it after :func:`sync_board_postings`.

    Resets ``not_found_count`` / ``first_not_found_at`` to ``0`` / ``NULL``, sets
    ``last_polled_at = now`` and turns a ``dead`` board back to ``active``. A board
    that had reached the death threshold also gets the ``not_found_twice`` item
    :func:`record_board_not_found` opened for it resolved, since the board answers
    again. ``last_verified_at`` is never touched (discovery owns it).

    Flushes; never commits.
    """
    _require_aware(now, "now")
    _ensure_persistent(session, board)
    reached_threshold = (
        board.status == "dead" or (board.not_found_count or 0) >= NOT_FOUND_DEAD_AFTER
    )
    board.not_found_count = 0
    board.first_not_found_at = None
    board.last_polled_at = now
    if board.status == "dead":
        board.status = "active"
    if reached_threshold:
        for item in _open_board_verify(session, board):
            if item.payload.get("reason") == NOT_FOUND_REASON:
                item.status = "resolved"
                item.resolved_at = now
    session.flush()


# --- collector registration ---------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """What ``CollectorSpec.run_fixture`` returns after replaying one fixture."""

    fetched: int
    inserted: int
    skipped: int
    events: list[Event] = field(default_factory=list)


class RunFixture(Protocol):
    """Signature of ``CollectorSpec.run_fixture`` (see the module docstring)."""

    def __call__(
        self, session: Session, case_path: Path, /, *, run_id: str, now: datetime
    ) -> RunResult: ...


@dataclass(frozen=True)
class CollectorSpec:
    """The registration object every collector module exposes as ``COLLECTOR``."""

    source: str
    kind: CollectorKind
    response_model: type[pydantic.BaseModel]
    fixture_dir: Path
    drift_missing_field: str
    run_fixture: RunFixture

    def __post_init__(self) -> None:
        if not self.source:
            msg = "CollectorSpec.source must be a non-empty string"
            raise ValueError(msg)
        if self.kind not in COLLECTOR_KINDS:
            msg = f"CollectorSpec.kind must be one of {COLLECTOR_KINDS}, got {self.kind!r}"
            raise ValueError(msg)
        if not self.drift_missing_field:
            msg = "CollectorSpec.drift_missing_field must name the field drift.json omits"
            raise ValueError(msg)


def _collectors_only() -> frozenset[str] | None:
    """The module names ``TELL_COLLECTORS_ONLY`` selects, or ``None`` when it is unset/blank."""
    raw = os.environ.get(COLLECTORS_ONLY_ENV, "")
    names = frozenset(part.strip() for part in raw.split(",") if part.strip())
    return names or None


def discover_collectors() -> list[CollectorSpec]:
    """Import every module under ``tell.collectors`` and return their ``COLLECTOR`` specs.

    Modules without a ``COLLECTOR`` (this one, private helpers) are skipped. Two
    modules claiming the same ``source`` is an error. Sorted by ``source``.

    Local-development aid: when ``TELL_COLLECTORS_ONLY`` is set to a comma-separated
    list of module names (``greenhouse,lever``), only those modules are imported, so
    an agent working on one collector in a shared checkout can run the generic
    contract and idempotency tests without importing a sibling's half-written module.
    Unset or blank means everything, which is what CI runs; the variable is never read
    anywhere else.
    """
    only = _collectors_only()
    package = importlib.import_module("tell.collectors")
    specs: dict[str, CollectorSpec] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name == "base" or info.name.startswith("_"):
            continue
        if only is not None and info.name not in only:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        spec = getattr(module, "COLLECTOR", None)
        if not isinstance(spec, CollectorSpec):
            continue
        if spec.source in specs:
            msg = f"two collector modules register source {spec.source!r}"
            raise ValueError(msg)
        specs[spec.source] = spec
    return [specs[name] for name in sorted(specs)]
