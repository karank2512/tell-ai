"""``tell-data`` system of record: JSONL events (append-only, archive) and board snapshots."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from tell.collectors.base import Event, write_events
from tell.models import AtsBoard, Company, EventRow, Posting
from tell.publish.data_repo import (
    EVENT_COLUMNS,
    archive_events,
    event_record,
    events_path,
    prune_snapshots,
    read_event_ids,
    snapshot_path,
    write_events_jsonl,
    write_snapshots,
)

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)
T = datetime(2026, 10, 6, 2, 30, tzinfo=UTC)


def _event(n: int, observed: datetime, *, magnitude: float = 1) -> Event:
    return Event(
        source="synthetic",
        type="news_launch",
        external_id=f"e{n}",
        version="1",
        occurred_at=observed - timedelta(hours=5),
        observed_at=observed,
        url=f"https://news.example/{n}",
        payload={"title": f"Headline {n}", "nested": {"k": [1, 2]}},
        run_id="synthetic-run",
        magnitude=magnitude,
    )


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_events_jsonl_has_every_column_and_iso_utc_datetimes(
    db_session: Session, tmp_path: Path
) -> None:
    write_events(db_session, [_event(1, T, magnitude=7.5), _event(2, T + timedelta(hours=1))])
    result = write_events_jsonl(db_session, tmp_path, DAY)
    assert result.path == tmp_path / "events" / "2026" / "10" / "06.jsonl"
    lines = _lines(result.path)
    assert [list(line) for line in lines] == [list(EVENT_COLUMNS)] * 2
    first = lines[0]
    assert first["occurred_at"] == "2026-10-05T21:30:00+00:00"
    assert first["observed_at"] == "2026-10-06T02:30:00+00:00"
    assert first["magnitude"] == 7.5
    assert lines[1]["magnitude"] == 1
    assert first["payload"] == {"title": "Headline 1", "nested": {"k": [1, 2]}}
    assert first["raw_ref"] == "events/2026/10/06.jsonl#L1"
    assert lines[1]["raw_ref"] == "events/2026/10/06.jsonl#L2"
    stored = db_session.get(EventRow, first["event_id"])
    assert stored is not None
    assert stored.raw_ref == "events/2026/10/06.jsonl#L1"


def test_events_jsonl_is_idempotent_and_append_only(db_session: Session, tmp_path: Path) -> None:
    write_events(db_session, [_event(1, T), _event(2, T + timedelta(hours=1))])
    first = write_events_jsonl(db_session, tmp_path, DAY)
    before = first.path.read_bytes()
    again = write_events_jsonl(db_session, tmp_path, DAY)
    assert again.written == []
    assert set(again.present) == set(first.written)
    assert first.path.read_bytes() == before

    # A later run the same day: an event observed earlier than the existing lines still
    # goes at the end; nothing is reordered or rewritten.
    write_events(db_session, [_event(3, T - timedelta(hours=1)), _event(4, T + timedelta(hours=9))])
    later = write_events_jsonl(db_session, tmp_path, DAY)
    assert len(later.written) == 2
    after = first.path.read_bytes()
    assert after.startswith(before)
    ids = read_event_ids(first.path)
    assert ids[:2] == first.written
    assert ids[2:] == later.written
    assert len(ids) == len(set(ids)) == 4
    assert [line["raw_ref"] for line in _lines(first.path)][2:] == [
        "events/2026/10/06.jsonl#L3",
        "events/2026/10/06.jsonl#L4",
    ]
    # Other days are untouched and empty days write no file.
    assert write_events_jsonl(db_session, tmp_path, DAY + timedelta(days=1)).written == []
    assert not events_path(tmp_path, DAY + timedelta(days=1)).exists()


def test_existing_raw_ref_is_kept(db_session: Session, tmp_path: Path) -> None:
    write_events(db_session, [_event(1, T)])
    row = db_session.scalars(select(EventRow)).one()
    row.raw_ref = "events/2026/10/06.jsonl#L9"
    db_session.flush()
    assert event_record(row, raw_ref="ignored")["raw_ref"] == "events/2026/10/06.jsonl#L9"
    write_events_jsonl(db_session, tmp_path, DAY)
    assert _lines(events_path(tmp_path, DAY))[0]["raw_ref"] == "events/2026/10/06.jsonl#L9"


def test_corrupt_files_are_never_repaired(tmp_path: Path) -> None:
    path = events_path(tmp_path, DAY)
    path.parent.mkdir(parents=True)
    path.write_text('{"event_id": "a"}')
    with pytest.raises(ValueError, match="unterminated"):
        read_event_ids(path)
    path.write_text("not json\n")
    with pytest.raises(ValueError, match="not JSON"):
        read_event_ids(path)
    path.write_text('{"id": 1}\n')
    with pytest.raises(ValueError, match="no event_id"):
        read_event_ids(path)
    assert read_event_ids(tmp_path / "missing.jsonl") == []


def test_archive_events_writes_old_events_to_their_day_files(
    db_session: Session, tmp_path: Path
) -> None:
    old_a = datetime(2026, 3, 1, 10, tzinfo=UTC)
    old_b = datetime(2026, 3, 2, 23, 59, tzinfo=UTC)
    write_events(db_session, [_event(1, old_a), _event(2, old_a), _event(3, old_b), _event(4, T)])
    # The nightly publish already archived day 2026-03-01 once.
    nightly = write_events_jsonl(db_session, tmp_path, old_a.date())
    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    archived = archive_events(db_session, tmp_path, cutoff)
    ids = {e.event_id for e in (_event(1, old_a), _event(2, old_a), _event(3, old_b))}
    assert set(archived) == ids
    assert read_event_ids(events_path(tmp_path, old_a.date())) == nightly.written
    assert len(read_event_ids(events_path(tmp_path, old_b.date()))) == 1
    assert not events_path(tmp_path, DAY).exists()
    assert set(archive_events(db_session, tmp_path, cutoff)) == ids  # rerun: same, no dupes
    assert len(read_event_ids(events_path(tmp_path, old_a.date()))) == 2
    with pytest.raises(ValueError, match="timezone-aware"):
        archive_events(db_session, tmp_path, datetime(2026, 4, 1))


class _Statements:
    """Every statement the engine sends while active: ``(sql, executemany)``."""

    def __init__(self, session: Session) -> None:
        self.engine = session.get_bind()
        self.seen: list[tuple[str, bool]] = []

    def _record(self, *args: Any) -> None:
        statement, executemany = args[2], args[5]
        self.seen.append((str(statement), bool(executemany)))

    def __enter__(self) -> _Statements:
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    def updates(self) -> list[tuple[str, bool]]:
        return [s for s in self.seen if s[0].lstrip().upper().startswith("UPDATE")]


def test_stamp_raw_refs_is_one_executemany_with_a_large_identity_map(
    db_session: Session, tmp_path: Path
) -> None:
    """Regression (2026-10-14): one ORM UPDATE per event, each evaluated against every
    loaded EventRow, made publish run past its 45-minute job limit."""
    n = 2000
    other_day = T - timedelta(days=3)
    write_events(db_session, [_event(i, T + timedelta(seconds=i)) for i in range(n)])
    write_events(db_session, [_event(n + i, other_day) for i in range(n)])
    loaded = list(db_session.scalars(select(EventRow)))  # 2n objects in the identity map
    assert len(loaded) == 2 * n
    started = time.perf_counter()
    with _Statements(db_session) as stmts:
        result = write_events_jsonl(db_session, tmp_path, DAY)
        db_session.flush()
    elapsed = time.perf_counter() - started
    assert len(result.written) == n
    updates = stmts.updates()
    assert len(updates) == 1, updates[:3]
    assert updates[0][1] is True  # executemany, not one round trip per event
    assert "raw_ref IS NULL" in updates[0][0]
    assert len(stmts.seen) <= 3  # the day's SELECT and the UPDATE; nothing per event
    assert elapsed < 10, f"{n} stamps took {elapsed:.1f}s"
    # The loaded objects carry the new value as committed state: no second UPDATE on flush.
    rows = {r.event_id: r for r in loaded}
    first = result.written[0]
    assert rows[first].raw_ref == "events/2026/10/06.jsonl#L1"
    assert not any(db_session.is_modified(r) for r in loaded)
    stored = {
        str(event_id): raw_ref
        for event_id, raw_ref in db_session.execute(
            select(EventRow.event_id, EventRow.raw_ref).where(EventRow.observed_at >= T)
        )
    }
    assert all(stored[e] is not None for e in result.written)
    with _Statements(db_session) as again:
        assert write_events_jsonl(db_session, tmp_path, DAY).written == []
        db_session.flush()
    assert again.updates() == []  # idempotent: nothing left to stamp


def test_stamp_raw_refs_never_overwrites_a_raw_ref_set_meanwhile(
    db_session: Session, tmp_path: Path
) -> None:
    write_events(db_session, [_event(1, T)])
    assert db_session.scalars(select(EventRow)).one().raw_ref is None  # loaded, empty
    # Another writer fills raw_ref behind this session's back (no ORM sync).
    db_session.execute(
        update(EventRow)
        .values(raw_ref="events/2026/10/06.jsonl#L7")
        .execution_options(synchronize_session=False)
    )
    write_events_jsonl(db_session, tmp_path, DAY)  # the stale object still reads None
    stored = db_session.execute(select(EventRow.raw_ref)).scalar_one()
    assert stored == "events/2026/10/06.jsonl#L7"  # WHERE raw_ref IS NULL kept it


# --- snapshots ---------------------------------------------------------------------------------


def _board(session: Session, vendor: str, identifier: str, *, polled: bool = True) -> AtsBoard:
    company = Company(domain=f"{identifier.replace(':', '-')}.example", name=identifier)
    session.add(company)
    session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=vendor,
        identifier=identifier,
        confidence=Decimal("0.95"),
        status="active",
        last_polled_at=T if polled else None,
    )
    session.add(board)
    session.flush()
    return board


def _posting(session: Session, board: AtsBoard, ext: str, *, closed: bool = False) -> None:
    session.add(
        Posting(
            board_id=board.id,
            external_id=ext,
            fingerprint=f"fp-{ext}",
            title=f"Role {ext}",
            title_norm=f"role {ext}",
            role_family="sales",
            location="Remote",
            posted_at=T - timedelta(days=3),
            first_seen_at=T - timedelta(days=3),
            last_seen_at=T,
            closed_at=T if closed else None,
            url=f"https://jobs.example/{ext}",
            content_hash="hash",
        )
    )
    session.flush()


def test_snapshots_hold_open_postings_without_descriptions(
    db_session: Session, tmp_path: Path
) -> None:
    gh = _board(db_session, "greenhouse", "acme")
    lever = _board(db_session, "lever", "eu:beta")
    _board(db_session, "ashby", "never-polled", polled=False)
    _posting(db_session, gh, "2")
    _posting(db_session, gh, "1")
    _posting(db_session, gh, "3", closed=True)
    paths = write_snapshots(db_session, tmp_path, DAY)
    assert paths == [
        snapshot_path(tmp_path, DAY, "greenhouse", "acme"),
        snapshot_path(tmp_path, DAY, "lever", "eu:beta"),
    ]
    assert paths[1].name == "lever-eu_beta.json"
    doc = json.loads(paths[0].read_text())
    assert doc["vendor"] == "greenhouse"
    assert doc["domain"] == "acme.example"
    assert [p["external_id"] for p in doc["open_postings"]] == ["1", "2"]
    assert set(doc["open_postings"][0]) == {
        "external_id",
        "title",
        "role_family",
        "location",
        "posted_at",
        "url",
    }
    assert json.loads(paths[1].read_text())["open_postings"] == []
    assert lever.identifier == "eu:beta"


def test_snapshot_retention_prunes_day_directories_past_90_days(tmp_path: Path) -> None:
    root = tmp_path / "postings" / "snapshots"
    for day in ("2026-07-07", "2026-07-08", "2026-10-06", "notes", "2026-13-45"):
        (root / day).mkdir(parents=True)
        (root / day / "x.json").write_text("{}")
    (root / "README.md").write_text("kept")
    removed = prune_snapshots(tmp_path, today=DAY)
    assert [p.name for p in removed] == ["2026-07-07"]
    assert sorted(p.name for p in root.iterdir()) == [
        "2026-07-08",
        "2026-10-06",
        "2026-13-45",
        "README.md",
        "notes",
    ]
    assert prune_snapshots(tmp_path, keep_days=0, today=DAY) == [root / "2026-07-08"]
    assert prune_snapshots(tmp_path / "nothing", today=DAY) == []
    with pytest.raises(ValueError, match="keep_days"):
        prune_snapshots(tmp_path, keep_days=-1)
