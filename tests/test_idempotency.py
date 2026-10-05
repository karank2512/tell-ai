"""Idempotency tests, generic over every registered collector (spec section 5 rule 5).

Replaying a fixture twice must leave ``events``, ``postings`` and ``signals`` counts
unchanged with ``inserted == 0`` on the second run. What ``skipped`` reports on that
rerun depends on the kind (see the contract in ``tell/collectors/base.py``): a non-ATS
collector re-derives the same events and ``write_events`` skips every one of them,
while an ATS collector's state machine returns no events for an unchanged board. For
ATS collectors with an ``updated.json`` fixture, replaying ``normal`` then ``updated``
yields exactly one ``job_updated`` and no new ``job_opened``. Adding a source never
edits this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import CollectorSpec, discover_collectors, start_run
from tell.models import EventRow, Posting, Signal

pytestmark = pytest.mark.db

SPECS: list[CollectorSpec] = discover_collectors()
NO_COLLECTORS = (
    "no collectors registered yet (tell/collectors/*.py with COLLECTOR); Phase 2 adds them"
)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)


def _spec_id(spec: CollectorSpec | None) -> str:
    return spec.source if spec is not None else "none"


def _normal_fixture(spec: CollectorSpec) -> Path:
    files = sorted(spec.fixture_dir.glob("normal*.json"))
    assert files, f"{spec.source}: no normal*.json under {spec.fixture_dir}"
    return files[0]


def _counts(session: Session) -> dict[str, int]:
    return {
        "events": session.execute(select(func.count()).select_from(EventRow)).scalar_one(),
        "postings": session.execute(select(func.count()).select_from(Posting)).scalar_one(),
        "signals": session.execute(select(func.count()).select_from(Signal)).scalar_one(),
    }


def _count_type(session: Session, type_: str) -> int:
    stmt = select(func.count()).select_from(EventRow).where(EventRow.type == type_)
    return session.execute(stmt).scalar_one()


@pytest.mark.parametrize("spec", SPECS or [None], ids=_spec_id)
def test_second_run_inserts_nothing(spec: CollectorSpec | None, db_session: Session) -> None:
    if spec is None:
        pytest.skip(NO_COLLECTORS)
    normal = _normal_fixture(spec)

    first_run = start_run(db_session, spec.source, now=NOW)
    first = spec.run_fixture(db_session, normal, run_id=first_run, now=NOW)
    db_session.flush()
    before = _counts(db_session)
    assert first.inserted == before["events"], "first run must account for every event written"

    second_run = start_run(db_session, spec.source, now=LATER)
    second = spec.run_fixture(db_session, normal, run_id=second_run, now=LATER)
    db_session.flush()

    assert second.inserted == 0
    assert second.fetched == first.fetched
    if spec.kind == "ats":
        # Unchanged board: the state machine emits nothing, so there is nothing to skip.
        assert second.events == []
        assert second.skipped == 0
    else:
        # Same fixture, same event ids: every event is re-derived and skipped on conflict.
        assert second.skipped == first.inserted + first.skipped
    assert _counts(db_session) == before


@pytest.mark.parametrize("spec", SPECS or [None], ids=_spec_id)
def test_updated_fixture_yields_exactly_one_job_updated(
    spec: CollectorSpec | None, db_session: Session
) -> None:
    if spec is None:
        pytest.skip(NO_COLLECTORS)
    if spec.kind != "ats":
        pytest.skip(f"{spec.source}: job_updated applies to ATS collectors only")
    updated = spec.fixture_dir / "updated.json"
    if not updated.is_file():
        pytest.skip(f"{spec.source}: no updated.json fixture")

    run_a = start_run(db_session, spec.source, now=NOW)
    spec.run_fixture(db_session, _normal_fixture(spec), run_id=run_a, now=NOW)
    db_session.flush()
    opened_before = _count_type(db_session, "job_opened")
    assert _count_type(db_session, "job_updated") == 0

    run_b = start_run(db_session, spec.source, now=LATER)
    result = spec.run_fixture(db_session, updated, run_id=run_b, now=LATER)
    db_session.flush()

    assert result.inserted == 1
    assert _count_type(db_session, "job_updated") == 1
    assert _count_type(db_session, "job_opened") == opened_before

    # Replaying the updated fixture again changes nothing.
    run_c = start_run(db_session, spec.source, now=LATER + timedelta(hours=6))
    again = spec.run_fixture(db_session, updated, run_id=run_c, now=LATER + timedelta(hours=6))
    assert again.inserted == 0
    assert _count_type(db_session, "job_updated") == 1
