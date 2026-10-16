"""Feedback form reader (ADR 0039): the Google Form's response sheet into ``feedback`` rows.

The nudge, the account page and the CSV link to a prefilled Google Form
(``TELL_FEEDBACK_FORM_URL``) because the v1 API is not deployed; until it is, this reader
is how a click becomes a label. ``tell intake feedback-sync`` (nightly, in ``tell-private``
next to ``tell intake sync``) fetches the sheet published as CSV
(``TELL_FEEDBACK_CSV_URL``, a secret) and writes one ``feedback`` row per response.

Columns (``docs/intake/feedback-form.md``; matched after lowercasing and turning
punctuation into spaces, like the intake sheet): ``Timestamp`` (Google's), ``Signal id``,
``Verdict``, and optionally ``Domain``, ``Watchlist`` and ``Note``. A row is rejected,
with its sheet row number and a reason and nothing else, when the signal id is not a
stored signal, the verdict is not one of :data:`tell.models.FEEDBACK_VERDICTS`, the
domain names another company than the signal's, or no watchlist holds the signal's
company (the given slug's, else the lowest-id one, the same rule as ``POST /v1/feedback``).
Each applied response is remembered by ``feedback.submission_id`` (SHA-256 of
``timestamp|signal_id|verdict|watchlist``), so re-reading the sheet applies nothing.

Nothing personal is read: the form has no name or email question and must not collect
addresses (a ``Email Address`` column, if present, is ignored). The run is recorded as
``runs.source = feedback_sync``, a non-pipeline source.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, start_run
from tell.intake.reader import IntakeError, RowError, _normalise_header
from tell.models import FEEDBACK_VERDICTS, Company, Feedback, Signal, Watchlist, WatchlistItem

__all__ = [
    "COLUMN_ALIASES",
    "FEEDBACK_SOURCE",
    "MAX_NOTE_LENGTH",
    "REQUIRED_FIELDS",
    "FeedbackResponse",
    "FeedbackSyncResult",
    "parse_responses",
    "submission_id",
    "sync_feedback",
]

FEEDBACK_SOURCE: Final = "feedback_sync"
"""``runs.source`` of one reader run."""

MAX_NOTE_LENGTH: Final = 1000
"""Longer notes are cut, like ``POST /v1/feedback``'s limit."""

COLUMN_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "timestamp": frozenset({"timestamp"}),
    "signal_id": frozenset({"signal id", "signal", "signal_id", "signalid"}),
    "domain": frozenset({"domain", "company domain"}),
    "verdict": frozenset({"verdict", "rating", "was this useful"}),
    "watchlist": frozenset({"watchlist", "watchlist slug", "list"}),
    "note": frozenset({"note", "notes", "comment", "comments"}),
}
"""Accepted header spellings per field, after :func:`_normalise_header`."""

REQUIRED_FIELDS: Final = ("timestamp", "signal_id", "verdict")


def submission_id(timestamp: str, signal_id: str, verdict: str, watchlist: str) -> str:
    """Stable identity of one response: SHA-256 of ``timestamp|signal_id|verdict|watchlist``."""
    key = "|".join(
        (timestamp.strip(), signal_id.strip(), verdict.strip().lower(), watchlist.strip().lower())
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class FeedbackResponse:
    """One syntactically valid response (the database checks come in :func:`sync_feedback`)."""

    row: int
    submission_id: str
    signal_id: int
    verdict: str
    domain: str
    watchlist: str
    note: str | None


def _column_map(fieldnames: Sequence[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(fieldnames):
        normalised = _normalise_header(header)
        for name, aliases in COLUMN_ALIASES.items():
            if normalised in aliases and name not in mapping:
                mapping[name] = index
    missing = [name for name in REQUIRED_FIELDS if name not in mapping]
    if missing:
        msg = (
            f"feedback sheet is missing required columns: {missing} "
            "(see docs/intake/feedback-form.md)"
        )
        raise IntakeError(msg)
    return mapping


def _verdict(value: str) -> str | None:
    text = value.strip().lower().replace(" ", "_").replace("-", "_")
    return text if text in FEEDBACK_VERDICTS else None


def parse_responses(csv_text: str) -> tuple[list[FeedbackResponse], list[RowError]]:
    """Every response row of the published sheet, valid or rejected; blank rows are ignored.

    Raises :class:`IntakeError` when the header lacks a :data:`REQUIRED_FIELDS` column.
    """
    reader = csv.reader(io.StringIO(csv_text.lstrip("﻿")))
    header = next(reader, None)
    if header is None:
        msg = "feedback sheet is empty (no header row)"
        raise IntakeError(msg)
    columns = _column_map(header)
    responses: list[FeedbackResponse] = []
    errors: list[RowError] = []
    for offset, raw in enumerate(reader):
        row = offset + 2  # the header is sheet row 1
        if not any(cell.strip() for cell in raw):
            continue
        cells = {
            name: (raw[columns[name]] if name in columns and columns[name] < len(raw) else "")
            for name in COLUMN_ALIASES
        }
        signal_text = cells["signal_id"].strip()
        if not signal_text.isdigit() or int(signal_text) <= 0:
            errors.append(RowError(row, "missing or invalid signal id"))
            continue
        verdict = _verdict(cells["verdict"])
        if verdict is None:
            errors.append(RowError(row, "unknown verdict"))
            continue
        watchlist = cells["watchlist"].strip().lower()
        note = cells["note"].strip()[:MAX_NOTE_LENGTH] or None
        responses.append(
            FeedbackResponse(
                row=row,
                submission_id=submission_id(cells["timestamp"], signal_text, verdict, watchlist),
                signal_id=int(signal_text),
                verdict=verdict,
                domain=cells["domain"].strip().lower(),
                watchlist=watchlist,
                note=note,
            )
        )
    return responses, errors


@dataclass
class FeedbackSyncResult:
    """What one reader run did; counts and rejected row numbers only."""

    run_id: str
    rows: int = 0
    applied: int = 0
    skipped: int = 0
    errors: list[RowError] = field(default_factory=list)

    def log_record(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "source": FEEDBACK_SOURCE,
            "rows": self.rows,
            "applied": self.applied,
            "skipped": self.skipped,
            "errors": len(self.errors),
            "rejected_rows": [e.to_dict() for e in self.errors],
        }


def _watchlist_for(session: Session, response: FeedbackResponse, company_id: int) -> int | str:
    """The watchlist id a response lands on, or the rejection reason."""
    holds = (
        select(WatchlistItem.watchlist_id)
        .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
        .where(WatchlistItem.company_id == company_id)
    )
    if response.watchlist:
        owned = session.scalars(select(Watchlist).where(Watchlist.slug == response.watchlist))
        watchlist = owned.one_or_none()
        if watchlist is None:
            return "unknown watchlist"
        on_list = session.execute(holds.where(WatchlistItem.watchlist_id == watchlist.id)).first()
        if on_list is None:
            return "watchlist does not hold the signal's company"
        return watchlist.id
    first = session.execute(holds.order_by(WatchlistItem.watchlist_id).limit(1)).first()
    if first is None:
        return "no watchlist holds the signal's company"
    return int(first[0])


def sync_feedback(session: Session, csv_text: str, *, now: datetime) -> FeedbackSyncResult:
    """Write a ``feedback`` row for every new valid response as one ``feedback_sync`` run.

    Flushes, never commits. A missing required column fails the run and re-raises.
    """
    run_id = start_run(session, FEEDBACK_SOURCE, now=now)
    result = FeedbackSyncResult(run_id=run_id)
    try:
        responses, errors = parse_responses(csv_text)
    except IntakeError:
        finish_run(
            session, run_id, status="failed", fetched=0, inserted=0, skipped=0, errors=1, now=now
        )
        raise
    result.errors = errors
    result.rows = len(responses) + len(errors)
    ids = [r.submission_id for r in responses]
    done: set[str] = set()
    if ids:
        done = set(
            session.scalars(select(Feedback.submission_id).where(Feedback.submission_id.in_(ids)))
        )
    for response in responses:
        if response.submission_id in done:
            result.skipped += 1
            continue
        signal = session.get(Signal, response.signal_id)
        if signal is None:
            result.errors.append(RowError(response.row, "unknown signal"))
            continue
        if response.domain:
            company = session.get(Company, signal.company_id)
            if company is None or (company.domain or "").lower() != response.domain:
                result.errors.append(RowError(response.row, "domain does not match the signal"))
                continue
        placed = _watchlist_for(session, response, signal.company_id)
        if isinstance(placed, str):
            result.errors.append(RowError(response.row, placed))
            continue
        session.add(
            Feedback(
                watchlist_id=placed,
                signal_id=signal.id,
                verdict=response.verdict,
                note=response.note,
                submission_id=response.submission_id,
                created_at=now,
            )
        )
        done.add(response.submission_id)
        result.applied += 1
    session.flush()
    result.errors.sort(key=lambda e: e.row)
    notes = {k: v for k, v in result.log_record().items() if k not in ("run_id", "source")}
    finish_run(
        session,
        run_id,
        status="ok",
        fetched=result.rows,
        inserted=result.applied,
        skipped=result.skipped,
        errors=len(result.errors),
        notes=notes,
        now=now,
    )
    return result
