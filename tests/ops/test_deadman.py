"""``tell.ops.deadman``: the 24-hour silence rule over ``runs``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tell.ops import deadman
from tests.ops.helpers import add_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)


def test_all_recent_ok_is_green(db_session: Session) -> None:
    for source in deadman.SCHEDULED_SOURCES:
        add_run(db_session, source, NOW - 3 * HOUR)
    report = deadman.check(db_session, NOW)
    assert report.ok
    assert report.silent == []
    assert [s.status for s in report.sources] == ["ok"] * len(deadman.SCHEDULED_SOURCES)
    assert "green" in report.markdown()
    assert report.to_dict()["ok"] is True


def test_stale_failed_only_and_never_ran_are_silent(db_session: Session) -> None:
    add_run(db_session, "greenhouse", NOW - 3 * HOUR)
    add_run(db_session, "lever", NOW - 30 * HOUR)  # last ok older than 24 h
    add_run(db_session, "lever", NOW - 2 * HOUR, "failed")
    add_run(db_session, "edgar", NOW - 2 * HOUR, "failed")  # never succeeded
    add_run(db_session, "news", NOW - 1 * HOUR, "running")  # a running row is not a success
    report = deadman.check(db_session, NOW, ["greenhouse", "lever", "edgar", "news", "ashby"])
    by_source = {s.source: s for s in report.sources}
    assert by_source["greenhouse"].status == "ok"
    assert by_source["lever"].status == "stale"
    assert by_source["lever"].last_ok_at == "2026-10-05T14:33:00+00:00"
    assert by_source["edgar"].status == "stale"
    assert by_source["edgar"].last_ok_at is None
    assert by_source["news"].status == "stale"
    assert by_source["ashby"].status == "never_ran"
    assert report.silent == ["lever", "edgar", "news", "ashby"]
    assert not report.ok
    text = report.markdown()
    assert "red" in text
    assert "`ashby`: never_ran (last ok: never)" in text


def test_runs_after_now_are_ignored(db_session: Session) -> None:
    add_run(db_session, "greenhouse", NOW + HOUR)
    report = deadman.check(db_session, NOW, ["greenhouse"])
    assert report.sources[0].status == "never_ran"


def test_exactly_24_hours_is_still_ok(db_session: Session) -> None:
    add_run(db_session, "greenhouse", NOW - 24 * HOUR, minutes=0)
    assert deadman.check(db_session, NOW, ["greenhouse"]).ok


def test_rejects_naive_now_and_no_sources(db_session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        deadman.check(db_session, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="at least one"):
        deadman.check(db_session, NOW, [])


def test_write_step_summary_appends(db_session: Session, tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    report = deadman.check(db_session, NOW, ["github"])
    deadman.write_step_summary(report, summary)
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n### dead-man (2026-10-06T20:30:00+00:00)")
    assert "No successful run in 24 h: github." in text
