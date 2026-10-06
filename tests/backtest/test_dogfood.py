"""13.5 dogfood: hours to surface, outcome proxies, and private-only output."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tell.backtest import dogfood
from tell.backtest.common import METHODS_DIR, REPO_ROOT, render_report
from tell.models import EventRow, Feedback, Signal, Watchlist, WatchlistItem
from tell.signals.profiles import get_profile
from tests.backtest.helpers import HOUR, NOW, company, event, job_opened


def _at(text: str) -> datetime:
    return datetime.fromisoformat(f"{text}+00:00")


def _signal(session: Session, row: EventRow, signal_type: str) -> Signal:
    assert row.company_id is not None
    signal = Signal(
        company_id=row.company_id,
        event_id=row.event_id,
        signal_type=signal_type,
        role_family="engineering",
        weight=Decimal(1),
        magnitude=Decimal(1),
        half_life_days=30,
        starts_at=row.occurred_at,
    )
    session.add(signal)
    session.flush()
    return signal


def build_world(session: Session) -> Watchlist:
    target = company(session, "target.example")
    elsewhere = company(session, "elsewhere.example")
    watchlist = Watchlist(owner="owner-handle", name="karan-targets", icp_profile="job-seeker")
    session.add(watchlist)
    session.flush()
    session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=target.id))
    session.flush()

    e1 = job_opened(
        session,
        target,
        _at("2026-10-01T00:00:00"),
        "e1",
        family="engineering",
        observed=_at("2026-10-01T06:00:00"),
    )
    e2 = job_opened(
        session,
        target,
        _at("2026-10-02T00:00:00"),
        "e2",
        family="ml_ai",
        observed=_at("2026-10-02T10:00:00"),
    )
    job_opened(
        session,
        target,
        _at("2026-09-30T00:00:00"),
        "e3",
        family="data",
        observed=_at("2026-09-30T02:00:00"),
    )
    # Not surfaced: wrong family, first-poll baseline, intern, other company, too old.
    job_opened(
        session,
        target,
        _at("2026-10-01T00:00:00"),
        "s1",
        family="sales",
        observed=_at("2026-10-01T01:00:00"),
    )
    job_opened(
        session,
        target,
        _at("2026-01-01T00:00:00"),
        "b1",
        family="engineering",
        first_poll=True,
        observed=_at("2026-10-01T01:00:00"),
    )
    job_opened(
        session, target, _at("2026-10-01T00:00:00"), "i1", family="engineering", is_intern=True
    )
    job_opened(session, elsewhere, _at("2026-10-01T00:00:00"), "x1", family="engineering")
    job_opened(session, target, _at("2026-05-01T00:00:00"), "old", family="engineering")

    # worked on a velocity signal listing e1; booked on e2's own event.
    velocity = event(
        session,
        "hiring_velocity",
        _at("2026-10-02T18:00:00"),
        owner=target,
        payload={"role_family": "engineering", "counted_postings": [{"external_id": "e1"}, "junk"]},
        source="tell",
    )
    session.add_all(
        [
            Feedback(
                watchlist_id=watchlist.id,
                signal_id=_signal(session, velocity, "hiring_velocity").id,
                verdict="worked",
                created_at=NOW,
            ),
            Feedback(
                watchlist_id=watchlist.id,
                signal_id=_signal(session, e2, "hiring_velocity").id,
                verdict="booked",
                created_at=NOW,
            ),
        ]
    )
    session.flush()
    assert e1.payload["posting_external_id"] == "e1"
    return watchlist


def test_check_private_out(tmp_path: Path) -> None:
    assert dogfood.check_private_out(tmp_path) == tmp_path.resolve()
    for inside in (REPO_ROOT, METHODS_DIR, REPO_ROOT / "tmp" / "x"):
        with pytest.raises(dogfood.PrivateOutputError, match="never written inside"):
            dogfood.check_private_out(inside)
    assert dogfood.DEFAULT_OUT.parts[-2:] == ("tell-private", "reports")


@pytest.mark.db
def test_numbers_match_hand_calculation(db_session: Session) -> None:
    build_world(db_session)
    watchlist = dogfood.find_watchlist(db_session)
    assert watchlist is not None
    rows = dogfood.surfaced_postings(db_session, watchlist, get_profile("job-seeker"), NOW)
    assert sorted((r.external_id, r.hours, r.applied, r.interview) for r in rows) == [
        ("e1", 6.0, True, False),
        ("e2", 10.0, True, True),
        ("e3", 2.0, False, False),
    ]
    report = dogfood.build_report(db_session, NOW)
    text = render_report(report)
    assert report.private
    assert not report.quotable
    assert (
        "Median hours to surface (surfaced_at - posted_at): 6.0 h (N=3; too small to quote)" in text
    )
    assert "Applications generated (verdict worked, booked or won): 2" in text
    assert "Interviews (verdict booked or won): 1" in text
    assert (
        "| target.example | Software Engineer | ml_ai | 2026-10-02 00:00 | 2026-10-02 10:00 | 10.0 | yes | yes |"
        in text
    )
    assert "private: true" in text
    assert "[" not in text


@pytest.mark.db
def test_missing_watchlist(db_session: Session) -> None:
    report = dogfood.build_report(db_session, NOW)
    assert report.n == 0
    assert "Watchlist karan-targets not found" in render_report(report)


def test_posted_at_fallbacks() -> None:
    occurred = NOW - HOUR
    assert dogfood._posted_at({"posted_at": "not a date"}, occurred) == occurred
    assert dogfood._posted_at({}, occurred) == occurred
