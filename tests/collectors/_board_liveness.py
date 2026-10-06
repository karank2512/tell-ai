"""Shared ADR 0015 board-liveness scenarios, run by each ATS collector's own test module.

Each collector test supplies a ``poll(now, outcome)`` callable that mocks its provider
(``outcome`` is ``"present"``: a 200 listing the board's postings, ``"absent"``: a 200
with an empty board, or ``"404"``) and runs the collector's real ``ingest_board``. The
scenarios below then check the one rule every ATS collector follows:

* a 404 between two real polls never counts as a poll without the posting, so
  ``200 (present) -> 404 -> 200 (absent)`` emits no ``job_closed``;
* two 404s under 12 hours apart leave the board ``active`` (count 2);
* two 404s at least 12 hours apart mark it ``dead`` and open exactly one
  ``board_verify`` item, and further 404s open no second one;
* a later 200 revives it, resets the streak and resolves that item;
* collectors never write ``last_verified_at``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import JOB_CLOSED, RunResult
from tell.models import AtsBoard, EventRow, Posting, ReviewQueueItem

Outcome = Literal["present", "absent", "404"]
Poll = Callable[[datetime, Outcome], RunResult]

H = timedelta(hours=1)


def _verify_items(session: Session, board: AtsBoard) -> list[ReviewQueueItem]:
    stmt = (
        select(ReviewQueueItem)
        .where(
            ReviewQueueItem.kind == "board_verify",
            ReviewQueueItem.payload.contains(
                {"vendor": board.vendor, "identifier": board.identifier}
            ),
        )
        .order_by(ReviewQueueItem.id)
    )
    return list(session.scalars(stmt))


def _closed_rows(session: Session) -> list[EventRow]:
    return list(session.scalars(select(EventRow).where(EventRow.type == JOB_CLOSED)))


def a_404_between_polls_never_closes(
    session: Session, board: AtsBoard, poll: Poll, t0: datetime
) -> None:
    verified = board.last_verified_at
    first = poll(t0, "present")
    assert first.fetched > 0
    assert board.last_polled_at == t0

    missing = poll(t0 + 12 * H, "404")
    assert (missing.fetched, missing.inserted, missing.events) == (0, 0, [])
    assert board.last_polled_at == t0  # a 404 is not a poll
    assert board.not_found_count == 1

    absent = poll(t0 + 24 * H, "absent")
    assert [e for e in absent.events if e.type == JOB_CLOSED] == []
    assert _closed_rows(session) == []
    postings = list(session.scalars(select(Posting).where(Posting.board_id == board.id)))
    assert postings
    assert all(p.closed_at is None for p in postings)
    assert board.last_polled_at == t0 + 24 * H
    assert (board.status, board.not_found_count, board.first_not_found_at) == ("active", 0, None)
    assert board.last_verified_at == verified


def two_404s_under_12h_stay_active(
    session: Session, board: AtsBoard, poll: Poll, t0: datetime
) -> None:
    verified, polled = board.last_verified_at, board.last_polled_at
    poll(t0, "404")
    status = poll(t0 + 11 * H, "404")
    assert status.events == []
    assert board.status == "active"
    assert board.not_found_count == 2
    assert board.first_not_found_at == t0
    assert _verify_items(session, board) == []
    assert (board.last_verified_at, board.last_polled_at) == (verified, polled)


def two_404s_12h_apart_kill_once(
    session: Session, board: AtsBoard, poll: Poll, t0: datetime
) -> None:
    verified, polled = board.last_verified_at, board.last_polled_at
    poll(t0, "404")
    poll(t0 + 12 * H, "404")
    assert board.status == "dead"
    assert board.not_found_count == 2
    (item,) = _verify_items(session, board)
    assert item.status == "open"
    assert item.payload == {
        "board_id": board.id,
        "vendor": board.vendor,
        "identifier": board.identifier,
        "reason": "not_found_twice",
        "first_not_found_at": t0.isoformat(),
        "last_not_found_at": (t0 + 12 * H).isoformat(),
    }

    poll(t0 + 18 * H, "404")
    assert board.status == "dead"
    assert board.not_found_count == 3
    assert len(_verify_items(session, board)) == 1
    assert (board.last_verified_at, board.last_polled_at) == (verified, polled)


def a_later_200_revives_and_resets(
    session: Session, board: AtsBoard, poll: Poll, t0: datetime
) -> None:
    verified = board.last_verified_at
    poll(t0, "404")
    poll(t0 + 12 * H, "404")
    assert board.status == "dead"

    revived = poll(t0 + 18 * H, "present")
    assert revived.fetched > 0
    assert board.status == "active"
    assert (board.not_found_count, board.first_not_found_at) == (0, None)
    assert board.last_polled_at == t0 + 18 * H
    assert board.last_verified_at == verified
    (item,) = _verify_items(session, board)
    assert (item.status, item.resolved_at) == ("resolved", t0 + 18 * H)

    # The streak starts over: one fresh 404 is only the first.
    poll(t0 + 24 * H, "404")
    assert (board.status, board.not_found_count, board.first_not_found_at) == (
        "active",
        1,
        t0 + 24 * H,
    )
