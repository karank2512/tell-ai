"""Database side of :mod:`tell.ops.source_eligibility`: decisions for stored rows.

``PERMITS`` is read at call time (as :func:`tell.signals.scoring.eligible_for_scoring`
does), so every output applies the same permit list.

:mod:`~tell.ops.source_eligibility` stays pure; this module loads what it needs (the
source event behind each signal) and applies it. Reads only; never writes or commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tell.models import EventRow, Signal
from tell.ops import source_eligibility
from tell.ops.source_eligibility import (
    HIRING_VELOCITY,
    INDEPENDENT_SOURCES,
    NEWS_SOURCE,
    SCORE_CROSSED_THRESHOLD,
    TELL_SOURCE,
    Decision,
    classify_event,
    classify_stamped_crossing,
    stamped_signal_ids,
)

__all__ = [
    "candidate_predicate",
    "crossing_predicate",
    "deletable_predicate",
    "event_decision",
    "is_crossing",
    "permitted_news_predicate",
    "signal_decisions",
    "signal_ids_of_crossings",
]

_CHUNK = 1000


def is_crossing(source: str, type_: str) -> bool:
    return source == TELL_SOURCE and type_ == SCORE_CROSSED_THRESHOLD


def signal_decisions(session: Session, ids: Iterable[int]) -> dict[int, Decision]:
    """Each known signal id's decision, from the stored event it was derived from.

    An id with no ``signals`` row (or no event) is absent, which every caller treats as
    unknown and therefore not clean.
    """
    wanted = sorted(set(ids))
    out: dict[int, Decision] = {}
    for start in range(0, len(wanted), _CHUNK):
        stmt = (
            select(Signal.id, EventRow.source, EventRow.type, EventRow.url, EventRow.payload)
            .join(EventRow, EventRow.event_id == Signal.event_id)
            .where(Signal.id.in_(wanted[start : start + _CHUNK]))
        )
        for signal_id, source, type_, url, payload in session.execute(stmt):
            out[int(signal_id)] = classify_event(
                source, type_, url, payload, permits=source_eligibility.PERMITS
            )
    return out


def signal_ids_of_crossings(payloads: Iterable[Any]) -> set[int]:
    ids: set[int] = set()
    for payload in payloads:
        ids.update(stamped_signal_ids(payload) or ())
    return ids


def event_decision(
    source: str,
    type_: str,
    url: object,
    payload: object,
    decisions: Mapping[int, Decision],
) -> Decision:
    """:func:`classify_event`, with a crossing cleared only through its stamp.

    A payload that is not a JSON object gets ``payload_not_object`` for every type,
    crossings included (before, a non-object crossing read ``lineage_incomplete``: also
    excluded, but for a misleading reason).
    """
    if is_crossing(source, type_) and isinstance(payload, Mapping):
        return classify_stamped_crossing(payload, decisions)
    return classify_event(source, type_, url, payload, permits=source_eligibility.PERMITS)


def deletable_predicate() -> ColumnElement[bool]:
    """SQL guard on every prune ``DELETE``: rows ``classify_event`` always calls eligible.

    Mirrors :func:`classify_event` conservatively: an independent source, or
    ``tell``/``hiring_velocity``. ``news`` rows (even a permitted company feed) and
    ``score_crossed_threshold`` rows are never deleted by SQL, whatever ids are passed:
    their eligibility depends on payload provenance or lineage the SQL cannot re-check.
    So the SQL set is a subset of the Python-eligible set, equal on every other row. A
    payload that is not a JSON object is never deletable, as :func:`classify_event`
    excludes it (``payload_not_object``).
    """
    return and_(
        func.jsonb_typeof(EventRow.payload) == "object",
        or_(
            EventRow.source.in_(sorted(INDEPENDENT_SOURCES)),
            and_(EventRow.source == TELL_SOURCE, EventRow.type == HIRING_VELOCITY),
        ),
    )


def permitted_news_predicate() -> ColumnElement[bool]:
    """SQL pre-filter: the only ``news`` rows that *can* be eligible.

    A news row is eligible only as a company feed whose ``payload.query`` is the exact
    URL of a recorded permit (:func:`~tell.ops.source_eligibility.classify_event`), so
    this is a necessary condition, never a sufficient one: callers still classify every
    row it returns. ``PERMITS`` is read at call time; with no permit it matches nothing,
    so Google News and unpermitted feeds are never even read.
    """
    feeds = sorted({permit.feed_url for permit in source_eligibility.PERMITS})
    return and_(
        EventRow.source == NEWS_SOURCE,
        func.jsonb_typeof(EventRow.payload) == "object",
        EventRow.payload["query"].astext.in_(feeds),
    )


def crossing_predicate() -> ColumnElement[bool]:
    """SQL: ``score_crossed_threshold`` rows (cleared only through their stamp)."""
    return and_(EventRow.source == TELL_SOURCE, EventRow.type == SCORE_CROSSED_THRESHOLD)


def candidate_predicate() -> ColumnElement[bool]:
    """SQL pre-filter: every row :func:`event_decision` could call eligible, and fewer others.

    The union of :func:`deletable_predicate` (always eligible), :func:`permitted_news_predicate`
    and :func:`crossing_predicate` (both still classified in Python). Rows outside it are
    certainly excluded: Google News, unpermitted or unknown news, unrecognized sources,
    other ``tell`` types and non-object payloads of the independent sources.
    """
    return or_(deletable_predicate(), permitted_news_predicate(), crossing_predicate())
