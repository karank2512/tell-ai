"""Feedback form reader (ADR 0039): parsing, validation, placement, idempotency."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.intake.feedback import (
    FEEDBACK_SOURCE,
    MAX_NOTE_LENGTH,
    FeedbackResponse,
    parse_responses,
    submission_id,
    sync_feedback,
)
from tell.intake.reader import IntakeError, RowError
from tell.models import Company, Feedback, Run, Signal, Watchlist, WatchlistItem
from tests.publish import scenario as sc

HEADER = "Timestamp,Signal id,Domain,Verdict,Watchlist,Note\n"


def _sheet(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


# --- parsing -------------------------------------------------------------------------------------


def test_parse_maps_headers_loosely_and_validates_the_shape() -> None:
    text = (
        "Timestamp,signal_id,Company domain,Rating,Watchlist slug,Comments\n"
        "10/6/2026 9:15:03,42,Acme.Example,Useful,Pilot,  great  \n"
        "10/6/2026 9:16:00,42,,not-useful,,\n"
        "10/6/2026 9:17:00,abc,,useful,,\n"
        "10/6/2026 9:18:00,0,,useful,,\n"
        "10/6/2026 9:19:00,43,,meh,,\n"
        ",,,,,\n"
    )
    responses, errors = parse_responses(text)
    assert [(r.row, r.signal_id, r.verdict, r.domain, r.watchlist, r.note) for r in responses] == [
        (2, 42, "useful", "acme.example", "pilot", "great"),
        (3, 42, "not_useful", "", "", None),
    ]
    assert errors == [
        RowError(4, "missing or invalid signal id"),
        RowError(5, "missing or invalid signal id"),
        RowError(6, "unknown verdict"),
    ]
    assert responses[0].submission_id == submission_id("10/6/2026 9:15:03", "42", "useful", "pilot")
    assert responses[0].submission_id != responses[1].submission_id


def test_parse_cuts_long_notes_and_needs_the_required_columns() -> None:
    (one,), _ = parse_responses(_sheet(f"t,1,,won,,{'x' * (MAX_NOTE_LENGTH + 5)}"))
    assert one.note is not None
    assert len(one.note) == MAX_NOTE_LENGTH
    with pytest.raises(IntakeError, match="missing required columns: \\['verdict'\\]"):
        parse_responses("Timestamp,Signal id,Note\nt,1,x\n")
    with pytest.raises(IntakeError, match="empty"):
        parse_responses("")
    assert parse_responses("﻿" + HEADER) == ([], [])


def test_submission_id_is_stable_under_padding_and_case() -> None:
    assert submission_id(" t ", "7", "Useful", "Pilot") == submission_id(
        "t", "7", "useful", "pilot"
    )
    assert submission_id("t", "7", "useful", "") != submission_id("t", "7", "useful", "pilot")


# --- sync ----------------------------------------------------------------------------------------


def _signal_on(session: Session, watchlist: Watchlist) -> Signal:
    stmt = (
        select(Signal)
        .join(WatchlistItem, WatchlistItem.company_id == Signal.company_id)
        .where(WatchlistItem.watchlist_id == watchlist.id)
        .order_by(Signal.id)
    )
    signal = session.scalars(stmt).first()
    assert signal is not None
    return signal


@pytest.mark.db
def test_sync_places_validates_and_is_idempotent(db_session: Session) -> None:
    with freeze_time(sc.NOW):
        scenario = sc.build(db_session)
    public, private = scenario.public, scenario.private
    private.slug = "private-list"
    db_session.flush()
    on_both = _signal_on(db_session, private)  # robotics is on both lists
    robotics = db_session.get(Company, on_both.company_id)
    assert robotics is not None
    assert robotics.domain == sc.ROBOTICS
    only_public = db_session.scalars(
        select(Signal)
        .join(WatchlistItem, WatchlistItem.company_id == Signal.company_id)
        .where(
            WatchlistItem.watchlist_id == public.id,
            Signal.company_id != robotics.id,
        )
        .order_by(Signal.id)
    ).first()
    assert only_public is not None
    orphan = db_session.scalars(
        select(Signal).where(
            Signal.company_id.not_in(
                select(WatchlistItem.company_id).where(
                    WatchlistItem.watchlist_id.in_([public.id, private.id])
                )
            )
        )
    ).first()
    text = _sheet(
        f"t1,{on_both.id},{sc.ROBOTICS},useful,private-list,Nice",  # named list
        f"t2,{on_both.id},,not_useful,,",  # no list: lowest id (the public one)
        f"t3,{on_both.id},other.example,useful,,",  # wrong domain
        f"t4,{on_both.id},,useful,ghost,",  # unknown list
        f"t5,{only_public.id},,useful,private-list,",  # list does not hold the company
        f"t6,{orphan.id if orphan else 999999},,useful,,",  # no list holds it
        "t7,999999,,won,,",  # unknown signal
    )
    result = sync_feedback(db_session, text, now=sc.NOW)
    assert (result.rows, result.applied, result.skipped) == (7, 2, 0)
    assert [e.to_dict() for e in result.errors] == [
        {"row": 4, "reason": "domain does not match the signal"},
        {"row": 5, "reason": "unknown watchlist"},
        {"row": 6, "reason": "watchlist does not hold the signal's company"},
        {"row": 7, "reason": "no watchlist holds the signal's company"},
        {"row": 8, "reason": "unknown signal"},
    ]
    rows = list(db_session.scalars(select(Feedback).order_by(Feedback.id)))
    assert [(r.watchlist_id, r.signal_id, r.verdict, r.note) for r in rows] == [
        (private.id, on_both.id, "useful", "Nice"),
        (min(public.id, private.id), on_both.id, "not_useful", None),
    ]
    assert all(r.submission_id and r.created_at == sc.NOW for r in rows)
    (run,) = db_session.scalars(select(Run).where(Run.source == FEEDBACK_SOURCE))
    assert (run.status, run.fetched, run.inserted, run.skipped, run.errors) == ("ok", 7, 2, 0, 5)
    assert run.notes["rejected_rows"][0] == {"row": 4, "reason": "domain does not match the signal"}
    assert run.notes["applied"] == 2
    # The nightly re-read: nothing applied, nothing duplicated; a new row still lands.
    again = sync_feedback(
        db_session,
        text + f"t8,{on_both.id},,booked,private-list,\n",
        now=sc.NOW + timedelta(days=1),
    )
    assert (again.applied, again.skipped, len(again.errors)) == (1, 2, 5)
    assert db_session.scalar(select(Feedback.id).where(Feedback.verdict == "booked")) is not None
    assert len(list(db_session.scalars(select(Feedback)))) == 3


@pytest.mark.db
def test_sync_fails_the_run_on_drifted_columns(db_session: Session) -> None:
    with pytest.raises(IntakeError):
        sync_feedback(db_session, "Timestamp,Note\nt,x\n", now=sc.NOW)
    (run,) = db_session.scalars(select(Run).where(Run.source == FEEDBACK_SOURCE))
    assert (run.status, run.errors) == ("failed", 1)


def test_response_dataclass_is_frozen_and_keyword_only() -> None:
    response = FeedbackResponse(
        row=2, submission_id="x", signal_id=1, verdict="useful", domain="", watchlist="", note=None
    )
    with pytest.raises(AttributeError):
        response.verdict = "won"  # type: ignore[misc]  # the point of the test
