"""13.3 noise: repost share, lifetimes and long-open boards on hand-built postings."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from tell.backtest import noise
from tell.backtest.common import render_report
from tests.backtest.helpers import DAY, NOW, board, company, posting

pytestmark = pytest.mark.db


def _postings(session: Session) -> None:
    owner = company(session, "alpha.example")
    other = company(session, "bravo.example")
    gh = board(session, owner, "alpha")
    lv = board(session, other, "bravo", vendor="lever")
    board(session, other, "empty-board")  # active, no postings
    dead = board(session, None, "gone", status="dead")

    p1 = posting(
        session, gh, "p1", first_seen=NOW - 10 * DAY, posted=NOW - 12 * DAY, closed=NOW - 2 * DAY
    )
    posting(session, gh, "p2", first_seen=NOW - 5 * DAY, repost_of=p1)
    posting(
        session,
        gh,
        "p3",
        first_seen=NOW - 20 * DAY,
        posted=NOW - 30 * DAY,
        closed=NOW - 20 * DAY,
        family="engineering",
    )
    # Outside the 90-day window, open, posted 200 days ago: the board's long-open role.
    posting(
        session, gh, "p4", first_seen=NOW - 100 * DAY, posted=NOW - 200 * DAY, family="engineering"
    )
    p5 = posting(
        session, lv, "p5", first_seen=NOW - 30 * DAY, posted=NOW - 40 * DAY, closed=NOW - 10 * DAY
    )
    posting(session, lv, "p6", first_seen=NOW - 3 * DAY, repost_of=p5)
    # Dead board: its old open posting does not count; closed in the future is ignored.
    posting(session, dead, "p7", first_seen=NOW - 300 * DAY, posted=NOW - 300 * DAY)
    posting(session, lv, "p8", first_seen=NOW - 95 * DAY, posted=NOW - 95 * DAY, closed=NOW + DAY)


def test_numbers_match_hand_calculation(db_session: Session) -> None:
    _postings(db_session)
    overall, by_vendor, by_family = noise.repost_shares(db_session, NOW)
    assert (overall.n, overall.part, overall.share) == (5, 2, 0.4)
    assert [(g.key, g.n, g.part) for g in by_vendor] == [("greenhouse", 3, 1), ("lever", 2, 1)]
    assert [(g.key, g.n, g.part) for g in by_family] == [("engineering", 1, 0), ("sales", 4, 2)]

    life_all, life_family = noise.lifetimes(db_session, NOW)
    # p1 10 days (sales), p3 10 days (engineering), p5 30 days (sales).
    assert (life_all.n, life_all.value) == (3, 10.0)
    assert [(g.key, g.n, g.value) for g in life_family] == [
        ("engineering", 1, 10.0),
        ("sales", 2, 20.0),
    ]

    boards = noise.long_open_boards(db_session, NOW)
    assert (boards.n, boards.part) == (3, 1)


def test_report(db_session: Session) -> None:
    _postings(db_session)
    report = noise.build_report(db_session, NOW)
    text = render_report(report)
    assert report.n == 5
    assert "quotable: false" in text
    assert (
        "Share of postings that are reposts (first seen in the last 90 days), percent: "
        "40.0% (N=5; too small to quote)"
    ) in text
    assert "| greenhouse | 3 | 1 | 33.3% (N=3; too small to quote) |" in text
    assert "| sales | 20.0 days (N=2; too small to quote) |" in text
    assert "33.3% (N=3; too small to quote)" in text
    assert f"at least {noise.MIN_RATED_ROWS} rated rows" in text
    assert noise.REPOST_SQL in text
    assert "[" not in text


def test_empty_database(db_session: Session) -> None:
    report = noise.build_report(db_session, NOW)
    assert report.n == 0
    assert "n/a (N=0; too small to quote)" in render_report(report)
