"""Run-health per source: last success, 7-day success rate, 24h sums, freshness p95, staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import FIRST_POLL_KEY, Event, finish_run, start_run, write_events
from tell.models import AtsBoard, Company, CompanyFeed, Posting, PostingObservation
from tell.publish.health import (
    INTERNAL_JOBS,
    compute_health,
    freshness_p95_h,
    health_sources,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


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


def _watched(session: Session, source: str, observed: datetime) -> dict[str, Any]:
    """The payload of an event from a place tell already watched (ADR 0043).

    ATS sources get a board polled 30 hours before ``observed``; ``news`` gets a company
    feed discovered three days before. ``tell`` events need nothing (never counted).
    """
    if source == "tell":
        return {}
    company = session.scalars(select(Company).where(Company.domain == "acme.example")).first()
    if company is None:
        company = Company(domain="acme.example", name="Acme")
        session.add(company)
        session.flush()
    if source == "news":
        url = "https://acme.example/feed.xml"
        if session.scalars(select(CompanyFeed).where(CompanyFeed.feed_url == url)).first() is None:
            session.add(
                CompanyFeed(company_id=company.id, feed_url=url, created_at=observed - 3 * DAY)
            )
            session.flush()
        return {"query": url}
    board = AtsBoard(company_id=company.id, vendor=source, identifier="acme", confidence=Decimal(1))
    session.add(board)
    session.flush()
    posting = Posting(
        board_id=board.id,
        external_id="older",
        fingerprint="fp-older",
        title="Account Executive",
        title_norm="account executive",
        role_family="sales",
        first_seen_at=observed - 30 * HOUR,
        last_seen_at=observed,
        url="https://jobs.acme.example/older",
    )
    session.add(posting)
    session.flush()
    session.add(
        PostingObservation(posting_id=posting.id, observed_at=observed - 30 * HOUR, run_id="r")
    )
    session.flush()
    return {"vendor": source, "board_identifier": "acme"}


def _events(
    session: Session, source: str, lags_h: list[float], *, first_poll: bool = False
) -> None:
    events = []
    observed = NOW - HOUR * 2
    watched = _watched(session, source, observed) if not first_poll else {}
    for n, lag in enumerate(lags_h):
        payload = {FIRST_POLL_KEY: True} if first_poll else dict(watched)
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
