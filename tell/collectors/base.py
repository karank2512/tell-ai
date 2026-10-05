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
source is added.

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
* Posting descriptions are hashed into ``content_hash`` and discarded.
  ``ObservedPosting.extra`` rejects description-like keys and long strings so raw
  text cannot be smuggled into event payloads through it.
* Form D amendments (``D/A``) call :func:`update_event_payload` on the original
  ``form_d_filed`` event and never write a second funding event.
* One run = ``start_run`` ... ``finish_run``; ``finish_run`` emits exactly one JSON
  log line (spec section 1) on the ``tell.runs`` logger.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
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
from tell.models import AtsBoard, EventRow, Posting, PostingObservation, Run
from tell.signals.taxonomy import fingerprint as make_fingerprint
from tell.signals.taxonomy import normalize_title, role_family

__all__ = [
    "CLOSE_GAP",
    "COLLECTOR_KINDS",
    "EXTRA_MAX_CHARS",
    "EXTRA_TEXT_KEYS",
    "JOB_CLOSED",
    "JOB_OPENED",
    "JOB_UPDATED",
    "PII_KEYS",
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
    "sha256_hex",
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

JOB_OPENED = "job_opened"
JOB_UPDATED = "job_updated"
JOB_CLOSED = "job_closed"

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
    ``publishedAt``). ``description_plain`` is hashed into ``content_hash`` at
    construction and then discarded; it is never stored. ``extra`` is merged into
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
        object.__setattr__(self, "description_plain", None)


_Pair = tuple[Posting, ObservedPosting]


def _posting_key(board: AtsBoard, external_id: str) -> str:
    return f"{board.vendor}:{board.identifier}:{external_id}"


def _opened_version(posting: Posting) -> str:
    """``job_opened`` is keyed on the first ``posted_at`` seen, else the first sighting."""
    return iso_utc(posting.posted_at or posting.first_seen_at)


def _opened_candidates(source: str, key: str, posting: Posting) -> set[str]:
    """Event ids the posting's ``job_opened`` may carry.

    A posting that opened without ``posted_at`` was keyed on ``first_seen_at``; if
    the provider supplies ``posted_at`` later, the column is filled but the original
    key stands, so both ids are candidates.
    """
    ids = {_event_id(source, key, iso_utc(posting.first_seen_at))}
    if posting.posted_at is not None:
        ids.add(_event_id(source, key, iso_utc(posting.posted_at)))
    return ids


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
) -> Event:
    payload: dict[str, Any] = {
        "board_id": board.id,
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
    posting.last_seen_at = now
    posting.closed_at = None


def _ensure_persistent(session: Session, board: AtsBoard) -> None:
    if not sa_inspect(board).persistent:
        session.add(board)
        session.flush()


def _reconcile_postings(
    session: Session, board: AtsBoard, seen: Mapping[str, ObservedPosting], now: datetime
) -> tuple[dict[str, Posting], list[_Pair], list[_Pair]]:
    """Upsert ``postings`` for one poll; return (all postings, newly opened, revisited)."""
    existing: dict[str, Posting] = {
        posting.external_id: posting
        for posting in session.scalars(select(Posting).where(Posting.board_id == board.id))
    }
    opened: list[_Pair] = []
    revisited: list[_Pair] = []
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
            session.add(posting)
            existing[external_id] = posting
            opened.append((posting, obs))
        else:
            _apply_observation(posting, obs, now)
            revisited.append((posting, obs))
    session.flush()
    return existing, opened, revisited


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
        )
        for posting, obs in opened
    ]


def _updated_events(
    session: Session, board: AtsBoard, revisited: Sequence[_Pair], *, run_id: str, now: datetime
) -> list[Event]:
    """``job_updated`` for each revisited posting whose provider marker is new.

    A marker is known when an event already carries it (a previous ``job_updated``,
    or a ``job_opened`` whose version happens to equal it) or when the posting's
    ``job_opened`` payload recorded it at open time. One primary-key ``IN`` query.

    Because the spec's ``postings`` table has no version column, the known markers
    live only in ``events``. Two consequences the rest of the pipeline must respect:

    * The caller writes each poll's events before polling the same board again;
      otherwise the next poll cannot see the markers and re-emits them.
    * Phase 9's ``prune`` (events older than 180 days to the JSONL archive) must keep
      the latest ``job_opened`` / ``job_updated`` row of every posting that is still
      open, or every long-open posting emits one spurious ``job_updated`` on the
      first poll after the prune. Alternatively a later migration adds a
      ``postings.version_marker`` column and this function reads that instead.
    * A marker that reverts to a value recorded earlier (v1 -> v2 -> v1) is treated
      as known and emits nothing; the posting row itself is still overwritten.
    """
    if not revisited:
        return []
    source = board.vendor
    wanted: set[str] = set()
    for posting, obs in revisited:
        key = _posting_key(board, posting.external_id)
        wanted.add(_event_id(source, key, obs.version))
        wanted |= _opened_candidates(source, key, posting)
    known: dict[str, dict[str, Any]] = {
        row.event_id: row.payload
        for row in session.execute(
            select(EventRow.event_id, EventRow.payload).where(EventRow.event_id.in_(wanted))
        )
    }
    events: list[Event] = []
    for posting, obs in revisited:
        key = _posting_key(board, posting.external_id)
        if _event_id(source, key, obs.version) in known:
            continue  # this marker already produced an event
        opened_markers = {
            known[candidate].get("version_marker")
            for candidate in _opened_candidates(source, key, posting)
            if candidate in known
        }
        if obs.version in opened_markers:
            continue  # unchanged since the posting opened
        events.append(
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
            )
        )
    return events


def _closed_events(
    board: AtsBoard,
    existing: Mapping[str, Posting],
    seen: Mapping[str, ObservedPosting],
    *,
    previous_poll: datetime | None,
    run_id: str,
    now: datetime,
) -> list[Event]:
    """Close postings absent now and at the previous poll, when the polls are >= 12h apart."""
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
            )
        )
    return events


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
      ``last_seen_at = now`` always, ``closed_at = NULL`` when a posting reappears,
      ``posted_at`` kept from the first sighting that carried one.
    * Appends one ``posting_observations`` row per observed posting (idempotent on
      ``(posting_id, observed_at)``).
    * Emits ``job_opened`` once per posting (version = first ``posted_at`` seen, else
      ``first_seen_at``); ``job_updated`` when the provider marker changes for an
      existing posting (version = the new marker), never a second ``job_opened``;
      ``job_closed`` only when a posting is absent now, was absent at the previous
      poll too, and ``now - previous poll >= 12h`` (no closes on a first poll).
    * Reads ``board.last_polled_at`` as the previous poll time, then sets it to ``now``.

    The caller writes the returned events with :func:`write_events` in the same
    transaction; the ``job_updated`` check reads earlier events back from ``events``.
    """
    _require_aware(now, "now")
    _ensure_persistent(session, board)
    previous_poll = board.last_polled_at

    seen: dict[str, ObservedPosting] = {}
    for obs in observed:
        seen.setdefault(obs.external_id, obs)

    existing, opened, revisited = _reconcile_postings(session, board, seen, now)
    _record_observations(
        session, [existing[external_id] for external_id in seen], run_id=run_id, now=now
    )

    events = _opened_events(board, opened, run_id=run_id, now=now)
    events += _updated_events(session, board, revisited, run_id=run_id, now=now)
    events += _closed_events(
        board, existing, seen, previous_poll=previous_poll, run_id=run_id, now=now
    )

    board.last_polled_at = now
    session.flush()
    return events


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


def discover_collectors() -> list[CollectorSpec]:
    """Import every module under ``tell.collectors`` and return their ``COLLECTOR`` specs.

    Modules without a ``COLLECTOR`` (this one, private helpers) are skipped. Two
    modules claiming the same ``source`` is an error. Sorted by ``source``.
    """
    package = importlib.import_module("tell.collectors")
    specs: dict[str, CollectorSpec] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name == "base" or info.name.startswith("_"):
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
