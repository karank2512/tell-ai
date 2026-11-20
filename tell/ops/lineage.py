"""Database side of :mod:`tell.ops.source_eligibility`: decisions for stored rows.

:mod:`~tell.ops.source_eligibility` stays pure; this module loads what it needs (the
source event behind each signal) and applies it. Reads only; never writes or commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tell.models import EventRow, Signal
from tell.ops.source_eligibility import (
    HIRING_VELOCITY,
    INDEPENDENT_SOURCES,
    SCORE_CROSSED_THRESHOLD,
    TELL_SOURCE,
    Decision,
    classify_event,
    classify_stamped_crossing,
    stamped_signal_ids,
)

__all__ = [
    "deletable_predicate",
    "event_decision",
    "is_crossing",
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
            out[int(signal_id)] = classify_event(source, type_, url, payload)
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
    payload: Mapping[str, Any] | None,
    decisions: Mapping[int, Decision],
) -> Decision:
    """:func:`classify_event`, with a crossing cleared only through its stamp."""
    if is_crossing(source, type_):
        return classify_stamped_crossing(payload, decisions)
    return classify_event(source, type_, url, payload)


def deletable_predicate() -> ColumnElement[bool]:
    """SQL guard on every prune ``DELETE``: rows ``classify_event`` always calls eligible.

    Mirrors :func:`classify_event` conservatively: an independent source, or
    ``tell``/``hiring_velocity``. ``news`` rows (even a permitted company feed) and
    ``score_crossed_threshold`` rows are never deleted by SQL, whatever ids are passed:
    their eligibility depends on payload provenance or lineage the SQL cannot re-check.
    So the SQL set is a subset of the Python-eligible set, equal on every other row.
    """
    return or_(
        EventRow.source.in_(sorted(INDEPENDENT_SOURCES)),
        and_(EventRow.source == TELL_SOURCE, EventRow.type == HIRING_VELOCITY),
    )
