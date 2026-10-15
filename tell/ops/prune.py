"""The size-budget prune (spec section 5, "Size budget on Neon's 1 GB"; PLAN Phase 9).

One pass, in this order, inside the caller's transaction:

1. **Archive.** :func:`tell.publish.data_repo.archive_events` writes every event with
   ``observed_at`` older than 180 days to its ``tell-data`` file
   ``events/YYYY/MM/DD.jsonl`` (append-only, ``fsync``-ed) and returns the ids now on
   disk. If any file cannot be read or written it raises, and nothing is deleted.
2. **Delete archived events.** Only ids the archive returned, and only while still
   older than the cutoff, are deleted. Events still referenced by a ``signals`` row are
   **kept**: ``signals.event_id`` is a foreign key, and signals (with the ``scores``
   whose ``contributions`` cite them, and any ``feedback`` on them) are history that the
   size budget does not cover. Such an event is in the archive *and* in the database;
   it is counted as ``events_kept``.
3. **Delete old observations.** ``posting_observations`` rows observed more than 90
   days ago are deleted (velocity reads 7- and 28-day windows; 90 days is the spec's
   retention).

Never commits: if the caller's commit fails after step 1, the archive lines stay on
disk and the next run's append is a no-op for them, so the pass can simply be rerun.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.models import EventRow, PostingObservation, Signal
from tell.publish.data_repo import archive_events

__all__ = [
    "DELETE_BATCH",
    "EVENT_RETENTION",
    "OBSERVATION_RETENTION",
    "PruneResult",
    "prune",
]

OBSERVATION_RETENTION = timedelta(days=90)
"""``posting_observations`` older than this are deleted."""

EVENT_RETENTION = timedelta(days=180)
"""Events observed longer ago than this move to the ``tell-data`` JSONL archive."""

DELETE_BATCH = 1000
"""Event ids per ``DELETE`` statement."""


@dataclass(frozen=True)
class PruneResult:
    """What one prune pass did."""

    observation_cutoff: str
    event_cutoff: str
    observations_deleted: int
    events_archived: int
    """Events older than the cutoff now present in the archive files."""
    events_deleted: int
    events_kept: int
    """Archived events left in the database because a ``signals`` row references them."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_cutoff": self.observation_cutoff,
            "event_cutoff": self.event_cutoff,
            "observations_deleted": self.observations_deleted,
            "events_archived": self.events_archived,
            "events_deleted": self.events_deleted,
            "events_kept": self.events_kept,
        }


def _rowcount(session: Session, stmt: Any) -> int:
    # Session.execute() is typed as a generic Result; a DELETE always yields a CursorResult.
    return int(cast("CursorResult[Any]", session.execute(stmt)).rowcount or 0)


def _delete_archived(session: Session, ids: list[str], cutoff: datetime) -> int:
    deleted = 0
    for start in range(0, len(ids), DELETE_BATCH):
        chunk = ids[start : start + DELETE_BATCH]
        referenced = select(Signal.event_id).where(Signal.event_id.in_(chunk))
        # archive_events just loaded every old event into the session; the deleted rows
        # are never read again, so skip ORM synchronization (no identity-map pass, no
        # RETURNING of up to DELETE_BATCH ids).
        stmt = (
            delete(EventRow)
            .where(
                EventRow.event_id.in_(chunk),
                EventRow.observed_at < cutoff,
                EventRow.event_id.not_in(referenced),
            )
            .execution_options(synchronize_session=False)
        )
        deleted += _rowcount(session, stmt)
    return deleted


def prune(
    session: Session,
    out_dir: Path,
    *,
    now: datetime,
    observation_retention: timedelta = OBSERVATION_RETENTION,
    event_retention: timedelta = EVENT_RETENTION,
) -> PruneResult:
    """Archive then delete old events, delete old observations (see the module docstring).

    ``out_dir`` is the ``tell-data`` checkout. Flushes; never commits.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be timezone-aware, got naive {now!r}"
        raise ValueError(msg)
    event_cutoff = now - event_retention
    observation_cutoff = now - observation_retention
    archived = list(dict.fromkeys(archive_events(session, out_dir, event_cutoff)))
    events_deleted = _delete_archived(session, archived, event_cutoff)
    observations_deleted = _rowcount(
        session,
        delete(PostingObservation)
        .where(PostingObservation.observed_at < observation_cutoff)
        .execution_options(synchronize_session=False),
    )
    session.flush()
    return PruneResult(
        observation_cutoff=iso_utc(observation_cutoff),
        event_cutoff=iso_utc(event_cutoff),
        observations_deleted=observations_deleted,
        events_archived=len(archived),
        events_deleted=events_deleted,
        events_kept=len(archived) - events_deleted,
    )
