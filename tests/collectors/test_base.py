"""The collector base, exercised with a synthetic in-test collector.

Covers the Event shape and idempotent write path, the D/A payload merge, the run
lifecycle (row + one JSON log line), the posting state machine (open / update /
close / reopen) and the registration contract (CollectorSpec + discovery).
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from tell.collectors import base
from tell.collectors.base import (
    CollectorSpec,
    Event,
    ObservedPosting,
    RunResult,
    WriteStats,
    content_hash,
    discover_collectors,
    finish_run,
    from_epoch_ms,
    iso_utc,
    new_run_id,
    parse_iso,
    sha256_hex,
    start_run,
    sync_board_postings,
    update_event_payload,
    utcnow,
    write_events,
)
from tell.models import AtsBoard, Company, EventRow, Posting, PostingObservation, Run
from tell.signals import taxonomy

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
DAY = timedelta(days=1)

# --- helpers ---------------------------------------------------------------------------------


def obs(
    external_id: str = "j1",
    *,
    title: str = "Account Executive",
    version: str = "v1",
    posted_at: datetime | None = T0 - 2 * DAY,
    department: str | None = "Sales",
    location: str | None = "New York, US",
    remote: bool | None = False,
    description: str | None = "Sell the thing.",
    extra: dict[str, Any] | None = None,
) -> ObservedPosting:
    return ObservedPosting(
        external_id=external_id,
        title=title,
        url=f"https://jobs.example.test/acme/{external_id}",
        version=version,
        department=department,
        location=location,
        remote=remote,
        posted_at=posted_at,
        description_plain=description,
        extra=extra or {},
    )


def make_event(
    external_id: str = "e1",
    *,
    version: str = "v1",
    type_: str = "form_d_filed",
    payload: dict[str, Any] | None = None,
    run_id: str = "run-x",
) -> Event:
    return Event(
        source="synthetic",
        type=type_,
        external_id=external_id,
        version=version,
        occurred_at=T0 - DAY,
        observed_at=T0,
        url=f"https://filings.example.test/{external_id}",
        payload=payload if payload is not None else {"amount": 1000000},
        run_id=run_id,
    )


def poll(
    session: Session,
    board: AtsBoard,
    observed: list[ObservedPosting],
    *,
    now: datetime,
    run_id: str = "run-1",
) -> tuple[list[Event], WriteStats]:
    events = sync_board_postings(session, board, observed, run_id=run_id, now=now)
    return events, write_events(session, events)


def count(session: Session, model: type[Any]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def types_of(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def stored_types(session: Session) -> list[str]:
    return list(session.scalars(select(EventRow.type).order_by(EventRow.observed_at)).all())


def posting(session: Session, board: AtsBoard, external_id: str = "j1") -> Posting:
    stmt = select(Posting).where(Posting.board_id == board.id, Posting.external_id == external_id)
    return session.scalars(stmt).one()


@pytest.fixture
def board(db_session: Session) -> AtsBoard:
    b = AtsBoard(vendor="synthetic", identifier="acme", confidence=Decimal("0.95"))
    db_session.add(b)
    db_session.flush()
    return b


# --- helpers under test ------------------------------------------------------------------------


def test_time_helpers() -> None:
    assert utcnow().tzinfo is UTC
    assert parse_iso("2026-10-05T12:00:00Z") == T0
    assert parse_iso("2026-10-05T14:00:00+02:00") == T0
    assert parse_iso("2026-10-05T12:00:00") == T0  # naive means UTC
    assert parse_iso("2026-10-05T12:00:00Z").tzinfo is UTC
    assert from_epoch_ms(int(T0.timestamp() * 1000)) == T0
    assert iso_utc(T0) == "2026-10-05T12:00:00+00:00"
    assert iso_utc(T0.astimezone(timezone(timedelta(hours=-7)))) == iso_utc(T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        iso_utc(datetime(2026, 1, 1))


def test_hash_helpers() -> None:
    assert sha256_hex("a|b|c") == sha256_hex("a|b|c")
    assert len(sha256_hex("x")) == 64
    assert content_hash(None) is None
    assert content_hash("   ") is None
    assert content_hash("Sell  the\nthing.") == content_hash("Sell the thing.")
    assert content_hash("a") != content_hash("b")


def test_taxonomy_stubs_behave_as_documented() -> None:
    assert taxonomy.normalize_title("  Senior   Account\tExecutive ") == "senior account executive"
    assert isinstance(taxonomy.role_family("Account Executive", "Sales"), str)
    fp = taxonomy.fingerprint("account executive", "Sales", "NYC", False)
    assert re.fullmatch(r"[0-9a-f]{40}", fp)
    assert fp == taxonomy.fingerprint("account executive", "Sales", "NYC", False)
    assert fp != taxonomy.fingerprint("account executive", "Sales", "NYC", True)
    assert fp != taxonomy.fingerprint("account executive", "Sales", "NYC", None)
    assert fp != taxonomy.fingerprint("account executive", None, "NYC", False)
    assert fp != taxonomy.fingerprint("account executive", "Sales", None, False)


# --- Event ----------------------------------------------------------------------------------------


def test_event_id_formula_and_row() -> None:
    event = make_event("acc-1", version="0001")
    assert event.event_id == sha256_hex("synthetic|acc-1|0001")
    row = event.to_row()
    assert row["event_id"] == event.event_id
    assert row["magnitude"] == 1
    assert row["company_id"] is None
    assert set(row) == {
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
    }


def test_event_validation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Event(
            source="s",
            type="t",
            external_id="x",
            version="v",
            occurred_at=datetime(2026, 1, 1),
            observed_at=T0,
            url="https://example.test",
            payload={},
            run_id="r",
        )
    with pytest.raises(TypeError, match="JSON-serialisable"):
        make_event(payload={"when": T0})
    with pytest.raises(ValueError, match="personal data"):
        make_event(payload={"contact_email": "x"})
    with pytest.raises(ValueError, match="personal data"):
        make_event(payload={"Phone": "x"})
    with pytest.raises(ValueError, match="non-empty"):
        make_event(version="")


def test_pii_keys_are_rejected_at_any_depth() -> None:
    # Nested mapping, list of mappings, and a mapping inside a list inside a mapping.
    with pytest.raises(ValueError, match=r"payload\.issuer\.email"):
        make_event(payload={"issuer": {"name": "Acme", "email": "x"}})
    with pytest.raises(ValueError, match=r"payload\.people\[1\]\.contact_phone"):
        make_event(payload={"people": [{"role": "ceo"}, {"contact_phone": "x"}]})
    with pytest.raises(ValueError, match=r"payload\.a\.b\[0\]\.first_name"):
        make_event(payload={"a": {"b": [{"first_name": "x"}]}})
    # Values that merely contain the words are fine; only key names are checked.
    event = make_event(payload={"note": "email us", "tags": ["phone", "first_name"]})
    assert event.payload["tags"] == ["phone", "first_name"]


# --- write_events / update_event_payload ------------------------------------------------------------


def test_write_events_returns_exact_counts(db_session: Session) -> None:
    assert write_events(db_session, []) == WriteStats(inserted=0, skipped=0)
    events = [make_event("a"), make_event("b"), make_event("a")]  # one in-batch duplicate
    stats = write_events(db_session, events)
    assert stats == WriteStats(inserted=2, skipped=1)
    assert stats.total == 3
    assert count(db_session, EventRow) == 2

    again = write_events(db_session, events)
    assert again == WriteStats(inserted=0, skipped=3)
    assert count(db_session, EventRow) == 2

    mixed = write_events(db_session, [make_event("a"), make_event("c")])
    assert mixed == WriteStats(inserted=1, skipped=1)
    assert count(db_session, EventRow) == 3


def test_write_events_batches(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "WRITE_BATCH", 2)
    events = [make_event(f"e{i}") for i in range(5)]
    assert write_events(db_session, events) == WriteStats(inserted=5, skipped=0)
    assert write_events(db_session, events) == WriteStats(inserted=0, skipped=5)


def test_update_event_payload_merges_and_never_inserts(db_session: Session) -> None:
    original = make_event("0001234567-26-000001", payload={"amount": 1000000, "form": "D"})
    write_events(db_session, [original])
    assert count(db_session, EventRow) == 1

    assert update_event_payload(db_session, original.event_id, {"amount": 2500000, "amended": True})
    row = db_session.get(EventRow, original.event_id)
    assert row is not None
    assert row.payload == {"amount": 2500000, "form": "D", "amended": True}
    assert count(db_session, EventRow) == 1

    assert not update_event_payload(db_session, sha256_hex("missing"), {"amount": 1})
    assert count(db_session, EventRow) == 1

    with pytest.raises(ValueError, match="personal data"):
        update_event_payload(db_session, original.event_id, {"email": "x"})
    with pytest.raises(ValueError, match=r"patch\.issuer\.email"):
        update_event_payload(db_session, original.event_id, {"issuer": {"email": "x"}})
    with pytest.raises(TypeError, match="JSON-serialisable"):
        update_event_payload(db_session, original.event_id, {"when": T0})


# --- runs -------------------------------------------------------------------------------------------------


def test_run_lifecycle_writes_row_and_one_json_log_line(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tell.runs")
    run_id = start_run(db_session, "synthetic", now=T0)
    assert re.fullmatch(r"synthetic-20261005T120000-[0-9a-f]{6}", run_id)

    run = db_session.get(Run, run_id)
    assert run is not None
    assert (run.status, run.fetched, run.finished_at) == ("running", 0, None)
    assert caplog.records == []

    finish_run(
        db_session,
        run_id,
        status="ok",
        fetched=10,
        inserted=7,
        skipped=3,
        errors=0,
        notes={"boards": 2},
        now=T0 + H,
    )
    assert (run.status, run.fetched, run.inserted, run.skipped, run.errors) == ("ok", 10, 7, 3, 0)
    assert run.finished_at == T0 + H
    assert run.notes == {"boards": 2}

    lines = [r for r in caplog.records if r.name == "tell.runs"]
    assert len(lines) == 1
    logged = json.loads(lines[0].getMessage())
    assert logged == {
        "run_id": run_id,
        "source": "synthetic",
        "started_at": "2026-10-05T12:00:00+00:00",
        "finished_at": "2026-10-05T13:00:00+00:00",
        "fetched": 10,
        "inserted": 7,
        "skipped": 3,
        "errors": 0,
    }


def test_run_lifecycle_validation(db_session: Session) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        start_run(db_session, "")
    with pytest.raises(ValueError, match="timezone-aware"):
        start_run(db_session, "synthetic", now=datetime(2026, 1, 1))
    with pytest.raises(LookupError, match="unknown run_id"):
        finish_run(db_session, "nope", status="ok", fetched=0, inserted=0, skipped=0)
    run_id = start_run(db_session, "synthetic")
    with pytest.raises(ValueError, match="status"):
        finish_run(
            db_session,
            run_id,
            status="running",  # type: ignore[arg-type]  # deliberately invalid
            fetched=0,
            inserted=0,
            skipped=0,
        )
    with pytest.raises(TypeError, match="JSON-serialisable"):
        finish_run(
            db_session, run_id, status="failed", fetched=0, inserted=0, skipped=0, notes={"t": T0}
        )
    run = finish_run(
        db_session, run_id, status="failed", fetched=1, inserted=0, skipped=0, errors=1
    )
    assert run.status == "failed"
    assert run.finished_at is not None
    assert new_run_id("x").startswith("x-")


# --- ObservedPosting ---------------------------------------------------------------------------------


def test_observed_posting_hashes_and_discards_description() -> None:
    o = obs(description="Sell the thing.")
    assert o.description_plain is None
    assert o.content_hash == content_hash("Sell the thing.")
    assert "description_plain" not in repr(o)
    assert obs(description=None).content_hash is None
    with pytest.raises(ValueError, match="personal data"):
        obs(extra={"recruiter_email": "x"})
    with pytest.raises(ValueError, match=r"extra\.hiring_manager\.email"):
        obs(extra={"hiring_manager": {"email": "x"}})
    with pytest.raises(ValueError, match="non-empty"):
        obs(version="")
    with pytest.raises(ValueError, match="timezone-aware"):
        obs(posted_at=datetime(2026, 1, 1))


def test_observed_posting_extra_cannot_smuggle_description_text() -> None:
    # Description-like keys, at the top level and nested, in the vendors' spellings.
    for extra in (
        {"description": "x"},
        {"descriptionPlain": "x"},
        {"jobAd": {"sections": {"jobDescription": {"text": "x"}}}},
        {"content": "x"},
        {"lists": [{"text": "Requirements", "body": "x"}]},
    ):
        with pytest.raises(ValueError, match="posting text"):
            obs(extra=extra)
    # Long strings anywhere inside extra are refused; short provider fields pass.
    long_text = "x" * (base.EXTRA_MAX_CHARS + 1)
    with pytest.raises(ValueError, match=r"extra\.team' is 501 characters"):
        obs(extra={"team": long_text})
    with pytest.raises(ValueError, match=r"extra\.tags\[1\]' is 501 characters"):
        obs(extra={"tags": ["ok", long_text]})
    ok = obs(extra={"team": "x" * base.EXTRA_MAX_CHARS, "compensation": {"currency": "USD"}})
    assert ok.extra["compensation"] == {"currency": "USD"}


# --- posting state machine -------------------------------------------------------------------------------


def test_open_then_rerun_inserts_zero_rows(db_session: Session, board: AtsBoard) -> None:
    events, stats = poll(db_session, board, [obs("j1"), obs("j2")], now=T0)
    assert types_of(events) == ["job_opened", "job_opened"]
    assert stats == WriteStats(inserted=2, skipped=0)
    assert count(db_session, Posting) == 2
    assert count(db_session, PostingObservation) == 2
    assert board.last_polled_at == T0

    p = posting(db_session, board, "j1")
    assert (p.first_seen_at, p.last_seen_at, p.closed_at) == (T0, T0, None)
    assert p.posted_at == T0 - 2 * DAY
    assert p.title_norm == "account executive"
    assert p.role_family == taxonomy.role_family("Account Executive", "Sales")
    assert p.fingerprint == taxonomy.fingerprint(
        "account executive", "Sales", "New York, US", False
    )
    assert p.content_hash == content_hash("Sell the thing.")
    assert p.url == "https://jobs.example.test/acme/j1"

    opened = events[0]
    assert opened.source == "synthetic"
    assert opened.external_id == "synthetic:acme:j1"
    assert opened.version == iso_utc(T0 - 2 * DAY)
    assert opened.occurred_at == T0 - 2 * DAY
    assert opened.observed_at == T0
    assert opened.url == p.url
    assert opened.payload["version_marker"] == "v1"
    assert opened.payload["posting_external_id"] == "j1"

    # Same poll replayed (same clock): nothing new anywhere.
    events, stats = poll(db_session, board, [obs("j1"), obs("j2")], now=T0)
    assert events == []
    assert stats == WriteStats(inserted=0, skipped=0)
    assert count(db_session, PostingObservation) == 2

    # Next poll, unchanged markers: observations grow, events do not.
    events, stats = poll(db_session, board, [obs("j1"), obs("j2")], now=T0 + 6 * H)
    assert events == []
    assert count(db_session, EventRow) == 2
    assert count(db_session, Posting) == 2
    assert count(db_session, PostingObservation) == 4
    assert posting(db_session, board, "j1").last_seen_at == T0 + 6 * H


def test_job_opened_without_posted_at_is_keyed_on_first_seen(
    db_session: Session, board: AtsBoard
) -> None:
    events, _ = poll(db_session, board, [obs("j1", posted_at=None)], now=T0)
    assert events[0].version == iso_utc(T0)
    assert events[0].occurred_at == T0
    # A later poll that now carries posted_at fills the column but never re-keys job_opened.
    events, stats = poll(db_session, board, [obs("j1", posted_at=T0 - DAY)], now=T0 + 6 * H)
    assert events == []
    assert stats.inserted == 0
    assert posting(db_session, board).posted_at == T0 - DAY
    events, _ = poll(db_session, board, [obs("j1", posted_at=T0 - 3 * DAY)], now=T0 + 12 * H)
    assert events == []
    assert posting(db_session, board).posted_at == T0 - DAY  # first value seen is kept


def test_version_change_emits_exactly_one_job_updated(db_session: Session, board: AtsBoard) -> None:
    poll(db_session, board, [obs("j1", version="v1")], now=T0)
    events, _ = poll(db_session, board, [obs("j1", version="v1")], now=T0 + 6 * H)
    assert events == []

    events, stats = poll(
        db_session,
        board,
        [obs("j1", version="v2", title="Senior Account Executive")],
        now=T0 + 12 * H,
    )
    assert types_of(events) == ["job_updated"]
    assert stats == WriteStats(inserted=1, skipped=0)
    updated = events[0]
    assert updated.external_id == "synthetic:acme:j1"
    assert updated.version == "v2"
    assert updated.occurred_at == T0 + 12 * H
    assert updated.payload["title"] == "Senior Account Executive"
    assert posting(db_session, board).title == "Senior Account Executive"

    # Same new marker again: nothing; never a second job_opened.
    events, stats = poll(db_session, board, [obs("j1", version="v2")], now=T0 + 18 * H)
    assert events == []
    assert stats.inserted == 0
    assert stored_types(db_session) == ["job_opened", "job_updated"]

    # Another change: a second job_updated, still one job_opened.
    events, stats = poll(db_session, board, [obs("j1", version="v3")], now=T0 + 24 * H)
    assert types_of(events) == ["job_updated"]
    assert stats.inserted == 1
    assert stored_types(db_session).count("job_opened") == 1


def test_duplicate_external_ids_in_one_poll_are_collapsed(
    db_session: Session, board: AtsBoard
) -> None:
    events, stats = poll(
        db_session, board, [obs("j1", version="v1"), obs("j1", version="v2")], now=T0
    )
    assert types_of(events) == ["job_opened"]
    assert stats.inserted == 1
    assert count(db_session, Posting) == 1
    assert count(db_session, PostingObservation) == 1


def test_absent_once_is_not_closed(db_session: Session, board: AtsBoard) -> None:
    poll(db_session, board, [obs("j1"), obs("j2")], now=T0)
    events, _ = poll(db_session, board, [obs("j2")], now=T0 + DAY)
    assert events == []
    assert posting(db_session, board, "j1").closed_at is None
    assert posting(db_session, board, "j1").last_seen_at == T0


def test_absent_twice_less_than_12h_apart_is_not_closed(
    db_session: Session, board: AtsBoard
) -> None:
    poll(db_session, board, [obs("j1")], now=T0)
    poll(db_session, board, [], now=T0 + 6 * H)
    events, _ = poll(db_session, board, [], now=T0 + 6 * H + 11 * H + timedelta(minutes=59))
    assert events == []
    assert posting(db_session, board).closed_at is None


def test_absent_twice_at_least_12h_apart_closes_exactly_once(
    db_session: Session, board: AtsBoard
) -> None:
    poll(db_session, board, [obs("j1"), obs("j2")], now=T0)
    events, _ = poll(db_session, board, [obs("j2")], now=T0 + 6 * H)  # first absence
    assert events == []

    events, stats = poll(db_session, board, [obs("j2")], now=T0 + 18 * H)  # second, 12h later
    assert types_of(events) == ["job_closed"]
    assert stats == WriteStats(inserted=1, skipped=0)
    closed = events[0]
    p = posting(db_session, board, "j1")
    assert p.closed_at == T0 + 18 * H
    assert closed.external_id == "synthetic:acme:j1"
    assert closed.version == iso_utc(T0 + 18 * H)
    assert closed.occurred_at == T0 + 18 * H
    assert closed.url == p.url
    assert closed.payload["closed_at"] == iso_utc(T0 + 18 * H)
    assert closed.payload["last_seen_at"] == iso_utc(T0)
    assert posting(db_session, board, "j2").closed_at is None

    # Still absent later: no second close, no second event.
    events, stats = poll(db_session, board, [obs("j2")], now=T0 + 2 * DAY)
    assert events == []
    assert stats.inserted == 0
    assert stored_types(db_session).count("job_closed") == 1

    # Replaying the closing poll with the same clock is also a no-op.
    events, stats = poll(db_session, board, [obs("j2")], now=T0 + 2 * DAY)
    assert events == []


def test_no_closes_on_first_poll_and_exactly_12h_counts(
    db_session: Session, board: AtsBoard
) -> None:
    # A board that has never been polled cannot close anything.
    assert board.last_polled_at is None
    events, _ = poll(db_session, board, [], now=T0)
    assert events == []

    poll(db_session, board, [obs("j1")], now=T0 + H)
    poll(db_session, board, [], now=T0 + 2 * H)
    events, _ = poll(db_session, board, [], now=T0 + 14 * H)  # exactly 12h after the previous poll
    assert types_of(events) == ["job_closed"]


def test_reappearance_after_close_clears_closed_at(db_session: Session, board: AtsBoard) -> None:
    poll(db_session, board, [obs("j1")], now=T0)
    poll(db_session, board, [], now=T0 + 6 * H)
    events, _ = poll(db_session, board, [], now=T0 + 18 * H)
    assert types_of(events) == ["job_closed"]
    assert posting(db_session, board).closed_at is not None

    events, stats = poll(db_session, board, [obs("j1")], now=T0 + 2 * DAY)
    assert events == []  # no second job_opened; Phase 6 adds job_reposted on top of this
    assert stats.inserted == 0
    p = posting(db_session, board)
    assert p.closed_at is None
    assert p.last_seen_at == T0 + 2 * DAY
    assert count(db_session, Posting) == 1
    assert stored_types(db_session) == ["job_opened", "job_closed"]


def test_sync_requires_aware_clock_and_persists_unsaved_board(db_session: Session) -> None:
    board = AtsBoard(vendor="synthetic", identifier="fresh", confidence=Decimal("0.60"))
    with pytest.raises(ValueError, match="timezone-aware"):
        sync_board_postings(db_session, board, [], run_id="r", now=datetime(2026, 1, 1))
    events = sync_board_postings(db_session, board, [obs("j1")], run_id="r", now=T0)
    assert board.id is not None
    assert types_of(events) == ["job_opened"]
    # The board is named, never numbered: the database-internal id stays out of payloads.
    assert "board_id" not in events[0].payload
    assert (events[0].payload["vendor"], events[0].payload["board_identifier"]) == (
        "synthetic",
        "fresh",
    )


def test_version_marker_is_stored_and_refreshed_on_every_poll(
    db_session: Session, board: AtsBoard
) -> None:
    poll(db_session, board, [obs("j1", version="v1")], now=T0)
    assert posting(db_session, board).version_marker == "v1"
    poll(db_session, board, [obs("j1", version="v2")], now=T0 + 6 * H)
    assert posting(db_session, board).version_marker == "v2"
    # Reverting to an earlier marker is a change like any other and is recorded as such.
    events, _ = poll(db_session, board, [obs("j1", version="v1")], now=T0 + 12 * H)
    assert types_of(events) == ["job_updated"]
    assert posting(db_session, board).version_marker == "v1"


def test_pruned_events_never_cause_a_spurious_job_updated(
    db_session: Session, board: AtsBoard
) -> None:
    """ADR 0013: the marker lives on the posting, so deleting events changes nothing."""
    poll(db_session, board, [obs("j1", version="v1"), obs("j2", version="v7")], now=T0)
    poll(db_session, board, [obs("j1", version="v2"), obs("j2", version="v7")], now=T0 + 6 * H)
    assert stored_types(db_session) == ["job_opened", "job_opened", "job_updated"]

    # Phase 9's prune: every event of this board is gone (archived to JSONL).
    db_session.execute(delete(EventRow))
    db_session.flush()
    assert count(db_session, EventRow) == 0

    # Same markers again: nothing is reported, and in particular no job_updated.
    events, stats = poll(
        db_session, board, [obs("j1", version="v2"), obs("j2", version="v7")], now=T0 + 12 * H
    )
    assert events == []
    assert stats == WriteStats(inserted=0, skipped=0)
    assert count(db_session, EventRow) == 0

    # A real change after the prune still fires exactly once.
    events, _ = poll(
        db_session, board, [obs("j1", version="v3"), obs("j2", version="v7")], now=T0 + 18 * H
    )
    assert types_of(events) == ["job_updated"]
    assert events[0].payload["posting_external_id"] == "j1"


def test_row_without_marker_records_it_silently(db_session: Session, board: AtsBoard) -> None:
    """A posting written before migration 0002 has no previous marker to compare against."""
    poll(db_session, board, [obs("j1", version="v1")], now=T0)
    p = posting(db_session, board)
    p.version_marker = None
    db_session.flush()
    events, _ = poll(db_session, board, [obs("j1", version="v2")], now=T0 + 6 * H)
    assert events == []
    assert posting(db_session, board).version_marker == "v2"
    events, _ = poll(db_session, board, [obs("j1", version="v3")], now=T0 + 12 * H)
    assert types_of(events) == ["job_updated"]


def test_events_carry_board_company(db_session: Session) -> None:
    company = Company(domain="acme.test", name="Acme")
    db_session.add(company)
    db_session.flush()
    board = AtsBoard(
        vendor="synthetic", identifier="acme", confidence=Decimal("0.95"), company_id=company.id
    )
    db_session.add(board)
    db_session.flush()
    events, _ = poll(db_session, board, [obs("j1", extra={"team": "GTM"})], now=T0)
    assert events[0].company_id == company.id
    assert events[0].payload["extra"] == {"team": "GTM"}


# --- registration contract, exercised with a synthetic collector ----------------------------------


class SyntheticJob(pydantic.BaseModel):
    id: str
    title: str
    updated_at: str
    absolute_url: str
    location: str | None = None


class SyntheticBoardResponse(pydantic.BaseModel):
    jobs: list[SyntheticJob]


def synthetic_run_fixture(
    session: Session, case_path: Path, *, run_id: str, now: datetime
) -> RunResult:
    parsed = SyntheticBoardResponse.model_validate_json(case_path.read_text(encoding="utf-8"))
    board = session.scalars(
        select(AtsBoard).where(AtsBoard.vendor == "synthetic", AtsBoard.identifier == "acme")
    ).one_or_none()
    if board is None:
        board = AtsBoard(vendor="synthetic", identifier="acme", confidence=Decimal("0.95"))
        session.add(board)
        session.flush()
    observed = [
        ObservedPosting(
            external_id=job.id,
            title=job.title,
            url=job.absolute_url,
            version=job.updated_at,
            location=job.location,
            posted_at=parse_iso(job.updated_at),
        )
        for job in parsed.jobs
    ]
    events = sync_board_postings(session, board, observed, run_id=run_id, now=now)
    stats = write_events(session, events)
    return RunResult(
        fetched=len(parsed.jobs), inserted=stats.inserted, skipped=stats.skipped, events=events
    )


def make_spec(fixture_dir: Path) -> CollectorSpec:
    return CollectorSpec(
        source="synthetic",
        kind="ats",
        response_model=SyntheticBoardResponse,
        fixture_dir=fixture_dir,
        drift_missing_field="updated_at",
        run_fixture=synthetic_run_fixture,
    )


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    jobs = [
        {
            "id": "1",
            "title": "Account Executive",
            "updated_at": "2026-10-01T00:00:00Z",
            "absolute_url": "https://jobs.example.test/acme/1",
            "location": "NYC",
        },
        {
            "id": "2",
            "title": "Software Engineer",
            "updated_at": "2026-10-02T00:00:00Z",
            "absolute_url": "https://jobs.example.test/acme/2",
        },
    ]
    (tmp_path / "normal.json").write_text(json.dumps({"jobs": jobs}))
    (tmp_path / "empty_board.json").write_text(json.dumps({"jobs": []}))
    updated = json.loads(json.dumps(jobs))
    updated[0]["updated_at"] = "2026-10-04T00:00:00Z"
    (tmp_path / "updated.json").write_text(json.dumps({"jobs": updated}))
    drift = [{k: v for k, v in job.items() if k != "updated_at"} for job in jobs]
    (tmp_path / "drift.json").write_text(json.dumps({"jobs": drift}))
    return tmp_path


def test_synthetic_collector_satisfies_the_contract(fixture_dir: Path, db_session: Session) -> None:
    spec = make_spec(fixture_dir)
    for name in ("normal.json", "empty_board.json"):
        spec.response_model.model_validate_json((fixture_dir / name).read_text())
    with pytest.raises(pydantic.ValidationError) as excinfo:
        spec.response_model.model_validate_json((fixture_dir / "drift.json").read_text())
    assert spec.drift_missing_field in str(excinfo.value)

    first = spec.run_fixture(db_session, fixture_dir / "normal.json", run_id="r1", now=T0)
    assert (first.fetched, first.inserted, first.skipped) == (2, 2, 0)
    assert types_of(first.events) == ["job_opened", "job_opened"]

    second = spec.run_fixture(db_session, fixture_dir / "normal.json", run_id="r2", now=T0 + 6 * H)
    assert (second.fetched, second.inserted, second.skipped) == (2, 0, 0)
    assert second.events == []

    third = spec.run_fixture(db_session, fixture_dir / "updated.json", run_id="r3", now=T0 + 12 * H)
    assert (third.inserted, types_of(third.events)) == (1, ["job_updated"])
    assert stored_types(db_session).count("job_opened") == 2

    empty = spec.run_fixture(
        db_session, fixture_dir / "empty_board.json", run_id="r4", now=T0 + DAY
    )
    assert (empty.fetched, empty.inserted) == (0, 0)
    assert empty.events == []  # first absence: nothing closes yet


def test_collector_spec_validation(fixture_dir: Path) -> None:
    spec = make_spec(fixture_dir)
    with pytest.raises(ValueError, match="kind"):
        CollectorSpec(
            source="x",
            kind="rss",  # type: ignore[arg-type]  # deliberately invalid
            response_model=SyntheticBoardResponse,
            fixture_dir=fixture_dir,
            drift_missing_field="f",
            run_fixture=synthetic_run_fixture,
        )
    with pytest.raises(ValueError, match="source"):
        CollectorSpec(
            source="",
            kind="ats",
            response_model=SyntheticBoardResponse,
            fixture_dir=fixture_dir,
            drift_missing_field="f",
            run_fixture=synthetic_run_fixture,
        )
    with pytest.raises(ValueError, match="drift_missing_field"):
        CollectorSpec(
            source="x",
            kind="ats",
            response_model=SyntheticBoardResponse,
            fixture_dir=fixture_dir,
            drift_missing_field="",
            run_fixture=synthetic_run_fixture,
        )
    assert spec.kind == "ats"


def test_discover_collectors_returns_registered_specs_sorted() -> None:
    specs = discover_collectors()
    assert all(isinstance(s, CollectorSpec) for s in specs)
    assert [s.source for s in specs] == sorted(s.source for s in specs)
    assert len({s.source for s in specs}) == len(specs)


def _fake_modules(monkeypatch: pytest.MonkeyPatch, modules: dict[str, Any]) -> Callable[[str], Any]:
    real_import = importlib.import_module

    def fake_import(name: str) -> Any:
        if name == "tell.collectors":
            return real_import(name)
        return modules[name.rsplit(".", 1)[1]]

    monkeypatch.setattr(
        pkgutil,
        "iter_modules",
        lambda _path: [pkgutil.ModuleInfo(None, name, False) for name in modules],  # type: ignore[arg-type]  # fake finder
    )
    monkeypatch.setattr(importlib, "import_module", fake_import)
    return fake_import


def test_discover_collectors_skips_modules_without_collector(
    monkeypatch: pytest.MonkeyPatch, fixture_dir: Path
) -> None:
    spec = make_spec(fixture_dir)
    _fake_modules(
        monkeypatch,
        {
            "zeta": SimpleNamespace(COLLECTOR=spec),
            "base": SimpleNamespace(COLLECTOR="never imported"),
            "_private": SimpleNamespace(COLLECTOR=spec),
            "helper": SimpleNamespace(),
            "wrong_type": SimpleNamespace(COLLECTOR="not a spec"),
        },
    )
    assert discover_collectors() == [spec]


def test_discover_collectors_rejects_duplicate_sources(
    monkeypatch: pytest.MonkeyPatch, fixture_dir: Path
) -> None:
    spec = make_spec(fixture_dir)
    _fake_modules(
        monkeypatch, {"a": SimpleNamespace(COLLECTOR=spec), "b": SimpleNamespace(COLLECTOR=spec)}
    )
    with pytest.raises(ValueError, match="two collector modules"):
        discover_collectors()


def _spec_named(fixture_dir: Path, source: str) -> CollectorSpec:
    return CollectorSpec(
        source=source,
        kind="ats",
        response_model=SyntheticBoardResponse,
        fixture_dir=fixture_dir,
        drift_missing_field="updated_at",
        run_fixture=synthetic_run_fixture,
    )


def test_discover_collectors_honours_tell_collectors_only(
    monkeypatch: pytest.MonkeyPatch, fixture_dir: Path
) -> None:
    imported: list[str] = []
    modules = {
        "alpha": SimpleNamespace(COLLECTOR=_spec_named(fixture_dir, "alpha")),
        "beta": SimpleNamespace(COLLECTOR=_spec_named(fixture_dir, "beta")),
        "gamma": SimpleNamespace(COLLECTOR=_spec_named(fixture_dir, "gamma")),
    }
    fake_import = _fake_modules(monkeypatch, modules)

    def recording_import(name: str) -> Any:
        imported.append(name.rsplit(".", 1)[1])
        return fake_import(name)

    monkeypatch.setattr(importlib, "import_module", recording_import)

    # Unset (the conftest fixture strips TELL_*) and blank both mean "everything".
    assert [s.source for s in discover_collectors()] == ["alpha", "beta", "gamma"]
    monkeypatch.setenv(base.COLLECTORS_ONLY_ENV, " , ")
    assert [s.source for s in discover_collectors()] == ["alpha", "beta", "gamma"]

    imported.clear()
    monkeypatch.setenv(base.COLLECTORS_ONLY_ENV, "gamma, alpha")
    assert [s.source for s in discover_collectors()] == ["alpha", "gamma"]
    assert "beta" not in imported  # the excluded module is never imported at all

    monkeypatch.setenv(base.COLLECTORS_ONLY_ENV, "nope")
    assert discover_collectors() == []
