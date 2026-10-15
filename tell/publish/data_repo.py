"""The ``tell-data`` system of record: JSONL events and trimmed board snapshots (spec section 9).

Layout under the ``tell-data`` checkout (``out_dir``)::

    events/2026/10/06.jsonl                                   # append-only, one event per line
    postings/snapshots/2026-10-06/{vendor}-{identifier}.json  # 90-day retention

Events
======

``events/YYYY/MM/DD.jsonl`` holds every ``events`` row *observed* on that UTC day, one
JSON object per line with every column (:data:`EVENT_COLUMNS`, in DDL order; datetimes
ISO 8601 UTC, ``magnitude`` as a JSON number). The files are append-only:

* a run adds only the ``event_id`` values missing from the file, in
  ``(observed_at, event_id)`` order, and never rewrites, reorders or removes a line;
* ``raw_ref`` is the line's own address, ``events/YYYY/MM/DD.jsonl#L<n>``; it is written
  into the line and copied back to ``events.raw_ref`` when that column is still empty
  (spec section 5: "tell-data path of the JSONL line");
* every write is flushed and ``fsync``-ed before the function returns.

The same writer is the 180-day archive the prune job (Phase 9) moves old events into:
:func:`archive_events` writes events older than a cutoff to their observed-day files and
returns the ids that are now safely on disk, so prune deletes only those.

Snapshots
=========

``postings/snapshots/YYYY-MM-DD/{vendor}-{identifier}.json`` is the trimmed current state
of one polled board: its open postings with ``external_id``, ``title``, ``role_family``,
``location``, ``posted_at`` and ``url`` (never descriptions). :func:`prune_snapshots`
removes day directories older than 90 days.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Table, bindparam, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from tell.collectors.base import iso_utc, utcnow
from tell.models import AtsBoard, Company, EventRow, Posting

__all__ = [
    "EVENT_COLUMNS",
    "SNAPSHOT_RETENTION_DAYS",
    "JsonlWrite",
    "archive_events",
    "event_record",
    "events_path",
    "prune_snapshots",
    "read_event_ids",
    "snapshot_path",
    "write_events_jsonl",
    "write_snapshots",
]

EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "company_id",
    "source",
    "type",
    "occurred_at",
    "observed_at",
    "magnitude",
    "url",
    "payload",
    "raw_ref",
    "run_id",
)
"""Every ``events`` column, in DDL order: the keys of each JSONL line."""

SNAPSHOT_RETENTION_DAYS = 90
"""Snapshot day directories older than this are removed (spec section 9)."""

_DAY = timedelta(days=1)
_EVENTS = cast("Table", EventRow.__table__)  # declarative; mapped to the one events table
_SNAPSHOT_CHUNK = 500
"""Board ids per ``IN (...)`` list when loading open postings for snapshots."""
_SNAPSHOT_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class JsonlWrite:
    """Outcome of one append to an events file."""

    path: Path
    written: list[str]
    """Event ids appended by this call, in file order."""
    present: list[str]
    """Event ids of the call's input that are now in the file (written or already there)."""


# --- paths --------------------------------------------------------------------------------


def events_path(out_dir: Path, day: date) -> Path:
    """``out_dir/events/YYYY/MM/DD.jsonl`` for a UTC day."""
    return out_dir / "events" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}.jsonl"


def snapshot_path(out_dir: Path, day: date, vendor: str, identifier: str) -> Path:
    """``out_dir/postings/snapshots/YYYY-MM-DD/{vendor}-{identifier}.json``.

    Characters outside ``[A-Za-z0-9._-]`` in the identifier (Lever's ``eu:`` prefix)
    become ``_`` so the name is a safe file name on every platform.
    """
    safe = _UNSAFE.sub("_", f"{vendor}-{identifier}")
    return out_dir / "postings" / "snapshots" / day.isoformat() / f"{safe}.json"


def _relative(out_dir: Path, path: Path) -> str:
    return path.relative_to(out_dir).as_posix()


# --- records ------------------------------------------------------------------------------


def _number(value: Decimal | float | int) -> int | float:
    dec = value if isinstance(value, Decimal) else Decimal(repr(value))
    return int(dec) if dec == dec.to_integral_value() else float(dec)


def event_record(row: EventRow, *, raw_ref: str | None = None) -> dict[str, Any]:
    """One JSONL line's object: every column, datetimes ISO UTC, ``magnitude`` numeric."""
    return {
        "event_id": row.event_id,
        "company_id": row.company_id,
        "source": row.source,
        "type": row.type,
        "occurred_at": iso_utc(row.occurred_at),
        "observed_at": iso_utc(row.observed_at),
        "magnitude": _number(row.magnitude),
        "url": row.url,
        "payload": row.payload,
        "raw_ref": row.raw_ref if row.raw_ref else raw_ref,
        "run_id": row.run_id,
    }


def _dumps(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def read_event_ids(path: Path) -> list[str]:
    """The ``event_id`` of every line of an events file, in file order (``[]`` when absent).

    Raises ``ValueError`` on a line that is not a JSON object with an ``event_id``, or a
    file whose last line is unterminated: an archive is never silently repaired.
    """
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        msg = f"{path}: last line is unterminated; refusing to append"
        raise ValueError(msg)
    ids: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"{path}:{number}: not JSON ({exc.msg})"
            raise ValueError(msg) from exc
        if not isinstance(obj, dict) or not isinstance(obj.get("event_id"), str):
            msg = f"{path}:{number}: line has no event_id"
            raise ValueError(msg)
        ids.append(obj["event_id"])
    return ids


def _append(
    out_dir: Path, day: date, rows: Sequence[EventRow]
) -> tuple[JsonlWrite, list[tuple[EventRow, str]]]:
    """Append the rows missing from the day's file; return the outcome and raw_ref updates."""
    path = events_path(out_dir, day)
    existing = read_event_ids(path)
    known = set(existing)
    rel = _relative(out_dir, path)
    fresh = sorted(
        (row for row in rows if row.event_id not in known),
        key=lambda r: (r.observed_at, r.event_id),
    )
    unique: list[EventRow] = []
    for row in fresh:
        if row.event_id in known:
            continue
        known.add(row.event_id)
        unique.append(row)
    refs: list[tuple[EventRow, str]] = []
    if unique:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for offset, row in enumerate(unique, start=len(existing) + 1):
            ref = f"{rel}#L{offset}"
            lines.append(_dumps(event_record(row, raw_ref=ref)) + "\n")
            if not row.raw_ref:
                refs.append((row, ref))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
    present = [row.event_id for row in rows if row.event_id in known]
    return JsonlWrite(path=path, written=[r.event_id for r in unique], present=present), refs


_STAMP_RAW_REF = (
    update(_EVENTS)
    .where(_EVENTS.c.event_id == bindparam("b_event_id"), _EVENTS.c.raw_ref.is_(None))
    .values(raw_ref=bindparam("b_raw_ref"))
)
"""Fill one empty ``events.raw_ref``; run as a single executemany (Core, no ORM sync)."""


def _stamp_raw_refs(session: Session, refs: Sequence[tuple[EventRow, str]]) -> None:
    """Copy each new line's address into ``events.raw_ref`` where it is still empty.

    One Core executemany on the ``events`` table: no per-row statement and no ORM
    ``synchronize_session`` pass over the identity map (which holds every event the
    caller loaded; with ~22k events the per-statement ORM evaluation cost 56 ms, i.e.
    16 minutes for one day of Form D events). The loaded objects are brought in line
    with :func:`~sqlalchemy.orm.attributes.set_committed_value`, so they read the new
    value and the next flush issues no second UPDATE.
    """
    if not refs:
        return
    session.connection().execute(
        _STAMP_RAW_REF,
        [{"b_event_id": row.event_id, "b_raw_ref": ref} for row, ref in refs],
    )
    for row, ref in refs:
        set_committed_value(row, "raw_ref", ref)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + _DAY


def write_events_jsonl(session: Session, out_dir: Path, day: date) -> JsonlWrite:
    """Append every event observed on UTC ``day`` that the day's file lacks.

    Idempotent: a rerun appends nothing; a later run the same day appends only the
    events observed since. Fills ``events.raw_ref`` where it was empty. Flushes the
    session; never commits.
    """
    start, end = _day_bounds(day)
    rows = list(
        session.scalars(
            select(EventRow)
            .where(EventRow.observed_at >= start, EventRow.observed_at < end)
            .order_by(EventRow.observed_at, EventRow.event_id)
        )
    )
    result, refs = _append(out_dir, day, rows)
    _stamp_raw_refs(session, refs)
    session.flush()
    return result


def _by_observed_day(rows: Iterable[EventRow]) -> Iterator[tuple[date, list[EventRow]]]:
    current: date | None = None
    bucket: list[EventRow] = []
    for row in rows:
        day = row.observed_at.astimezone(UTC).date()
        if current is not None and day != current:
            yield current, bucket
            bucket = []
        current = day
        bucket.append(row)
    if current is not None:
        yield current, bucket


def archive_events(session: Session, out_dir: Path, older_than: datetime) -> list[str]:
    """Write every event with ``observed_at < older_than`` to its observed-day file.

    Returns the ids of those events that are now on disk (appended by this call or
    already archived by an earlier nightly publish), ``fsync``-ed, so the prune job can
    delete exactly these rows after this function returns. Raises (and returns nothing)
    when any file cannot be read or written. Flushes; never commits.
    """
    if older_than.tzinfo is None or older_than.utcoffset() is None:
        msg = f"older_than must be timezone-aware, got naive {older_than!r}"
        raise ValueError(msg)
    stmt = (
        select(EventRow)
        .where(EventRow.observed_at < older_than)
        .order_by(EventRow.observed_at, EventRow.event_id)
        .execution_options(yield_per=2000)
    )
    archived: list[str] = []
    stamps: list[tuple[EventRow, str]] = []
    for day, rows in _by_observed_day(session.scalars(stmt)):
        result, refs = _append(out_dir, day, rows)
        stamps.extend(refs)
        archived.extend(result.present)
    # Stamped after the streaming cursor is exhausted, never interleaved with its fetches.
    _stamp_raw_refs(session, stamps)
    session.flush()
    return archived


# --- snapshots ------------------------------------------------------------------------------


def _iso_or_none(value: datetime | None) -> str | None:
    return iso_utc(value) if value is not None else None


def write_snapshots(session: Session, out_dir: Path, day: date) -> list[Path]:
    """Write one trimmed snapshot per active, polled board for ``day``; return the paths.

    A board qualifies when its status is ``active`` and it has been polled at least once.
    The file lists the board's open postings (``closed_at`` NULL) sorted by
    ``external_id``; rewriting the same day replaces the file with the current state.
    """
    boards = session.execute(
        select(AtsBoard, Company.domain)
        .outerjoin(Company, Company.id == AtsBoard.company_id)
        .where(AtsBoard.status == "active", AtsBoard.last_polled_at.is_not(None))
        .order_by(AtsBoard.vendor, AtsBoard.identifier)
    ).all()
    open_by_board: dict[int, list[Posting]] = {}
    board_ids = [board.id for board, _ in boards]
    for start in range(0, len(board_ids), _SNAPSHOT_CHUNK):  # not one query per board
        for posting in session.scalars(
            select(Posting)
            .where(
                Posting.board_id.in_(board_ids[start : start + _SNAPSHOT_CHUNK]),
                Posting.closed_at.is_(None),
            )
            .order_by(Posting.board_id, Posting.external_id)
        ):
            open_by_board.setdefault(posting.board_id, []).append(posting)
    written: list[Path] = []
    for board, domain in boards:
        postings = open_by_board.get(board.id, [])
        doc = {
            "vendor": board.vendor,
            "identifier": board.identifier,
            "domain": domain,
            "as_of": day.isoformat(),
            "last_polled_at": _iso_or_none(board.last_polled_at),
            "open_postings": [
                {
                    "external_id": p.external_id,
                    "title": p.title,
                    "role_family": p.role_family,
                    "location": p.location,
                    "posted_at": _iso_or_none(p.posted_at),
                    "url": p.url,
                }
                for p in postings
            ],
        }
        path = snapshot_path(out_dir, day, board.vendor, board.identifier)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        written.append(path)
    return written


def prune_snapshots(
    out_dir: Path, keep_days: int = SNAPSHOT_RETENTION_DAYS, *, today: date | None = None
) -> list[Path]:
    """Remove snapshot day directories older than ``keep_days`` before ``today``.

    Only directories named ``YYYY-MM-DD`` are touched; anything else is left alone.
    Returns the removed directories, oldest first.
    """
    if keep_days < 0:
        msg = "keep_days must be >= 0"
        raise ValueError(msg)
    root = out_dir / "postings" / "snapshots"
    if not root.is_dir():
        return []
    cutoff = (today or utcnow().date()) - timedelta(days=keep_days)
    removed: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not _SNAPSHOT_DIR.match(child.name):
            continue
        try:
            day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(child)
            removed.append(child)
    return removed
