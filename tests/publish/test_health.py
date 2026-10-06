"""Run-health per source: last success, 7-day success rate, 24h sums, freshness p95, staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy.orm import Session

from tell.collectors.base import FIRST_POLL_KEY, Event, finish_run, start_run, write_events
from tell.publish.health import (
    INTERNAL_JOBS,
    compute_health,
    freshness_p95_h,
    health_sources,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _run(
    session: Session,
    source: str,
    at: datetime,
    status: Literal["ok", "failed"] | None,
    fetched: int = 10,
    inserted: int = 2,
) -> None:
    run_id = start_run(session, source, now=at)
    if status is not None:
        finish_run(
            session,
            run_id,
            status=status,
            fetched=fetched,
            inserted=inserted,
            skipped=0,
            now=at + timedelta(minutes=5),
        )


def _events(
    session: Session, source: str, lags_h: list[float], *, first_poll: bool = False
) -> None:
    events = []
    for n, lag in enumerate(lags_h):
        observed = NOW - HOUR * 2
        payload = {FIRST_POLL_KEY: True} if first_poll else {}
        events.append(
            Event(
                source=source,
                type="job_opened",
                external_id=f"{source}-{first_poll}-{n}",
                version="1",
                occurred_at=observed - timedelta(hours=lag),
                observed_at=observed,
                url=f"https://jobs.example/{n}",
                payload=payload,
                run_id="r",
            )
        )
    write_events(session, events)


def test_sources_are_collectors_then_internal_jobs() -> None:
    sources = health_sources()
    names = [name for name, _ in sources]
    assert names[-3:] == list(INTERNAL_JOBS)
    assert {"greenhouse", "lever", "ashby", "smartrecruiters", "edgar", "news"} <= set(names)
    assert {kind for name, kind in sources if name in INTERNAL_JOBS} == {"job"}


def test_health_per_source(db_session: Session) -> None:
    _run(db_session, "greenhouse", NOW - HOUR * 3, "ok", fetched=100, inserted=7)
    _run(db_session, "greenhouse", NOW - HOUR * 9, "failed", fetched=4, inserted=0)
    _run(db_session, "greenhouse", NOW - timedelta(days=2), "ok", fetched=50, inserted=5)
    _run(db_session, "greenhouse", NOW - timedelta(days=9), "failed")  # outside 7 days
    _run(db_session, "greenhouse", NOW - HOUR, None)  # still running: not counted
    _run(db_session, "lever", NOW - timedelta(days=3), "ok")
    _run(db_session, "score", NOW - HOUR, "ok")
    _events(db_session, "greenhouse", [float(n) for n in range(1, 21)])
    _events(db_session, "greenhouse", [5000.0], first_poll=True)  # a baseline, never counted
    sources = [("greenhouse", "collector"), ("lever", "collector"), ("ashby", "collector")]
    sources += [("score", "job"), ("publish", "job")]
    health = compute_health(db_session, NOW, sources)
    rows = {row.source: row for row in health.sources}

    gh = rows["greenhouse"]
    assert gh.last_ok_at == "2026-10-06T09:05:00+00:00"
    assert gh.success_rate_7d == round(2 / 3, 4)
    assert (gh.fetched_24h, gh.inserted_24h) == (104, 7)
    assert gh.freshness_p95_h == 19.1  # percentile_cont: 19 + 0.05 over lags 1..20 h
    assert (gh.stale, gh.status) == (False, "ok")

    lever = rows["lever"]
    assert (lever.stale, lever.status) == (True, "stale")
    assert lever.fetched_24h == 0
    assert lever.freshness_p95_h is None

    assert (rows["ashby"].stale, rows["ashby"].status) == (False, "never_run")
    assert rows["ashby"].last_ok_at is None
    assert rows["ashby"].success_rate_7d is None
    assert (rows["score"].status, rows["score"].freshness_p95_h) == ("ok", None)
    assert rows["publish"].status == "never_run"

    assert health.stale_sources == ["lever"]
    assert health.any_stale is True
    assert health.freshness_p95_h == 19.1
    as_dict = health.to_dict()
    assert as_dict["stale_sources"] == ["lever"]
    assert as_dict["sources"][0]["source"] == "greenhouse"


def test_freshness_excludes_tell_events_and_old_ones(db_session: Session) -> None:
    _events(db_session, "tell", [400.0])
    assert freshness_p95_h(db_session, NOW) is None
    _events(db_session, "news", [2.0])
    assert freshness_p95_h(db_session, NOW) == 2.0
    assert freshness_p95_h(db_session, NOW + timedelta(days=2)) is None
    assert freshness_p95_h(db_session, NOW, ["greenhouse"]) is None


def test_naive_now_is_rejected(db_session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_health(db_session, datetime(2026, 10, 6, 12))
