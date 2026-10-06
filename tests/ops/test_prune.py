"""``tell.ops.prune``: archive first, delete only what the archive holds, keep signal events."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.models import EventRow, PostingObservation
from tell.ops import prune
from tell.publish.data_repo import events_path
from tests.ops.helpers import add_board, add_company, add_event, add_posting, add_signal

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
DAY = timedelta(days=1)


def _scenario(session: Session) -> None:
    company = add_company(session, "prunable.example")
    add_event(session, "old-plain", NOW - 200 * DAY, company=company)
    old_signal = add_event(session, "old-signal", NOW - 190 * DAY, company=company)
    add_signal(session, old_signal, company)
    add_event(session, "recent", NOW - 10 * DAY, company=company)
    board = add_board(session, company, "prunable")
    posting = add_posting(session, board, "1", NOW - 120 * DAY)
    for age in (120, 91, 89, 1):
        session.add(
            PostingObservation(posting_id=posting.id, observed_at=NOW - age * DAY, run_id="r")
        )
    session.flush()


def _event_ids(session: Session) -> list[str]:
    return sorted(session.scalars(select(EventRow.event_id)))


def test_prune_archives_then_deletes(db_session: Session, tmp_path: Path) -> None:
    _scenario(db_session)
    result = prune.prune(db_session, tmp_path, now=NOW)
    assert result.events_archived == 2
    assert result.events_deleted == 1
    assert result.events_kept == 1
    assert result.observations_deleted == 2
    assert result.event_cutoff == "2026-04-09T20:30:00+00:00"
    assert result.observation_cutoff == "2026-07-08T20:30:00+00:00"
    assert _event_ids(db_session) == ["old-signal", "recent"]
    remaining = db_session.scalar(select(func.count()).select_from(PostingObservation))
    assert remaining == 2

    plain = events_path(tmp_path, (NOW - 200 * DAY).date())
    lines = [json.loads(line) for line in plain.read_text(encoding="utf-8").splitlines()]
    assert [line["event_id"] for line in lines] == ["old-plain"]
    assert lines[0]["raw_ref"].endswith("#L1")
    kept = events_path(tmp_path, (NOW - 190 * DAY).date())
    assert "old-signal" in kept.read_text(encoding="utf-8")
    assert result.to_dict()["events_kept"] == 1

    again = prune.prune(db_session, tmp_path, now=NOW)
    assert (again.events_archived, again.events_deleted, again.events_kept) == (1, 0, 1)
    assert again.observations_deleted == 0
    assert plain.read_text(encoding="utf-8").count("\n") == 1


def test_archive_failure_deletes_nothing(db_session: Session, tmp_path: Path) -> None:
    _scenario(db_session)
    broken = events_path(tmp_path, (NOW - 200 * DAY).date())
    broken.parent.mkdir(parents=True)
    broken.write_text('{"event_id": "x"}', encoding="utf-8")  # unterminated last line
    with pytest.raises(ValueError, match="unterminated"):
        prune.prune(db_session, tmp_path, now=NOW)
    assert _event_ids(db_session) == ["old-plain", "old-signal", "recent"]
    assert db_session.scalar(select(func.count()).select_from(PostingObservation)) == 4


def test_batches_and_naive_now(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = add_company(db_session, "batched.example")
    for n in range(5):
        add_event(db_session, f"old-{n}", NOW - (181 + n) * DAY, company=company)
    monkeypatch.setattr(prune, "DELETE_BATCH", 2)
    result = prune.prune(db_session, tmp_path, now=NOW)
    assert result.events_deleted == 5
    assert _event_ids(db_session) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        prune.prune(db_session, tmp_path, now=NOW.replace(tzinfo=None))
