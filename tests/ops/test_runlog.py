"""``tell.ops.runlog``: latest run line and the weekly operations line."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from tell.ops import runlog
from tell.ops.deadman import SCHEDULED_SOURCES
from tests.ops.helpers import add_board, add_company, add_posting, add_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _every_six_hours(session: Session, start: datetime, end: datetime) -> None:
    t = start
    while t <= end:
        for source in SCHEDULED_SOURCES:
            add_run(session, source, t)
        t += 6 * HOUR


def test_run_line_has_spec_keys_and_status(db_session: Session) -> None:
    run = add_run(db_session, "greenhouse", NOW - HOUR, fetched=12, inserted=3)
    line = runlog.run_line(run)
    assert list(line)[: len(runlog.RUN_LINE_KEYS)] == list(runlog.RUN_LINE_KEYS)
    assert line["status"] == "ok"
    assert line["fetched"] == 12
    assert line["inserted"] == 3
    assert line["started_at"] == "2026-10-06T19:30:00+00:00"
    running = runlog.run_line(add_run(db_session, "lever", NOW, "running"))
    assert running["finished_at"] is None


def test_latest_run(db_session: Session) -> None:
    assert runlog.latest_run(db_session, "greenhouse") is None
    add_run(db_session, "greenhouse", NOW - 7 * HOUR)
    newest = add_run(db_session, "greenhouse", NOW - HOUR, "failed")
    add_run(db_session, "lever", NOW)
    found = runlog.latest_run(db_session, "greenhouse")
    assert found is not None
    assert found.run_id == newest.run_id


def test_success_rates(db_session: Session) -> None:
    add_run(db_session, "greenhouse", NOW - DAY)
    add_run(db_session, "greenhouse", NOW - 2 * DAY, "failed")
    add_run(db_session, "edgar", NOW - DAY)
    add_run(db_session, "edgar", NOW - HOUR, "running")  # not finished: not counted
    add_run(db_session, "edgar", NOW - 9 * DAY, "failed")  # outside the period
    add_run(db_session, "intake", NOW - DAY, "failed")  # tell-private's reader: not a pipeline run
    assert "intake" in runlog.NON_PIPELINE_SOURCES
    overall, by_source = runlog.success_rates(db_session, NOW - 7 * DAY, NOW)
    assert by_source == {"edgar": 1.0, "greenhouse": 0.5}
    assert overall == round(2 / 3, 4)
    assert runlog.success_rates(db_session, NOW, NOW) == (None, {})


def test_weeks_unattended_without_gap_counts_from_first_run(db_session: Session) -> None:
    assert runlog.weeks_unattended(db_session, NOW) == (None, None)
    start = NOW - 21 * DAY
    _every_six_hours(db_session, start, NOW - HOUR)
    weeks, since = runlog.weeks_unattended(db_session, NOW)
    assert since == start
    assert weeks == 3.0


def test_weeks_unattended_restarts_after_a_silent_gap(db_session: Session) -> None:
    _every_six_hours(db_session, NOW - 21 * DAY, NOW - 15 * DAY)
    # edgar silent for two days, then back: the recovery run anchors the period.
    recovery = NOW - 13 * DAY
    for source in SCHEDULED_SOURCES:
        if source != "edgar":
            add_run(db_session, source, NOW - 14 * DAY)
    _every_six_hours(db_session, recovery, NOW - HOUR)
    weeks, since = runlog.weeks_unattended(db_session, NOW)
    assert since == recovery + timedelta(minutes=3)
    assert weeks == round((NOW - since).total_seconds() / (7 * 86400), 1)


def test_weeks_unattended_is_zero_while_a_source_is_silent(db_session: Session) -> None:
    _every_six_hours(db_session, NOW - 14 * DAY, NOW - HOUR)
    add_run(db_session, "news", NOW - 2 * HOUR, "failed")
    assert runlog.weeks_unattended(db_session, NOW)[0] == 2.0
    weeks, since = runlog.weeks_unattended(db_session, NOW + 2 * DAY)
    assert weeks == 0.0
    assert since == NOW + 2 * DAY


def test_ops_line(db_session: Session) -> None:
    _every_six_hours(db_session, NOW - 14 * DAY, NOW - HOUR)
    add_run(db_session, "greenhouse", NOW - 2 * HOUR, "failed")
    with_board = add_company(db_session, "boarded.example")
    add_company(db_session, "filer.example", cik="0000000001")
    add_company(db_session, "unseen.example")
    paused = add_company(db_session, "paused.example")
    board = add_board(db_session, with_board, "boarded")
    add_board(db_session, paused, "paused", status="paused")
    add_posting(db_session, board, "1", NOW - DAY)
    add_posting(db_session, board, "2", NOW - DAY, closed=True)

    line = runlog.ops_line(db_session, NOW)
    assert line.companies_monitored == 2
    assert line.postings_tracked == 1
    assert line.period_days == 7
    assert line.weeks_unattended == 2.0
    assert line.success_rate_by_source["edgar"] == 1.0
    assert line.success_rate_by_source["greenhouse"] < 1.0
    assert line.success_rate is not None
    assert 0.99 < line.success_rate < 1.0
    assert line.freshness_p95_h is None
    record = line.to_dict()
    assert record["summary"].startswith("2 weeks unattended · success rate 99.")
    assert "2 companies monitored · 1 postings tracked" in record["summary"]


def test_ops_line_empty_and_naive(db_session: Session) -> None:
    line = runlog.ops_line(db_session, NOW)
    assert line.summary.startswith("n/a weeks unattended · success rate n/a (7d)")
    assert "freshness p95 n/a" in line.summary
    with pytest.raises(ValueError, match="timezone-aware"):
        runlog.ops_line(db_session, NOW.replace(tzinfo=None))
