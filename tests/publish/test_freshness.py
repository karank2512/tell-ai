"""Freshness p95 is tell's lag, not the age of what tell reads (ADR 0043, SPEC sections 12, 13.4).

The first dated operations report (2026-10-19) printed a freshness p95 of 23298.1 h (N=455):
SPEC 12's ``observed_at - occurred_at`` taken literally, over a window in which 66 of the 455
events were company-blog posts from 2015 on, read when tell discovered each blog's feed.
``tests/fixtures/freshness/window_2026-10-19.json`` is a hand-authored miniature of that
window (plus an old-dated posting, a backfilled Form D and a widened Google News query);
the tests below show the literal formula reproducing the bug on it and the new definition
measuring tell's lag, then pin each rule on its own.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tell.backtest.common import percentile
from tell.backtest.speed_vs_press import INDEX_LAG_HOURS
from tell.models import AtsBoard, Company, CompanyFeed, EventRow, Posting, PostingObservation, Run
from tell.publish.health import (
    BACKFILL,
    EDGAR_INDEX_LAG,
    FIRST_LOOK,
    FIRST_SEEN_ONLY,
    Freshness,
    compute_health,
    freshness,
    freshness_by_source,
    freshness_events,
    freshness_p95_h,
)
from tests.publish.freshness_window import literal_spec_lags, load_window

pytestmark = pytest.mark.db

ARCHIVE = "company feed archive read on discovery"
OLD_DATED = "old-dated posting on a later poll"
COHORT = "Google News item from the when:120d cohort run"
FORM_D = "Form D from the quarterly data set (tell backfill formd)"
LATE = "Google News item dated before the previous query (indexed late)"

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


# --- the 2026-10-19 window --------------------------------------------------------------------


def test_literal_spec_formula_measures_the_age_of_blog_archives(db_session: Session) -> None:
    """The cause, on the fixture: the pre-ADR-0043 query lands its p95 in years-old posts."""
    window = load_window(db_session)
    lags = literal_spec_lags(db_session, window.now)
    assert len(lags) == 29
    p95 = percentile([lag for _, lag in lags], 0.95)
    assert p95 is not None
    assert round(p95, 1) == 72584.6  # production: 23298.1 h; either way years, not hours
    rank = math.floor(0.95 * (len(lags) - 1))
    assert {window.cases[event_id] for event_id, _ in lags[rank:]} == {ARCHIVE}
    oldest = Counter(window.cases[event_id] for event_id, _ in lags[-10:])
    assert oldest == {ARCHIVE: 8, COHORT: 1, OLD_DATED: 1}


def test_window_freshness_is_tells_lag(db_session: Session) -> None:
    window = load_window(db_session)
    result = freshness(db_session, window.now)
    assert result == Freshness(p95_h=103.0, n=16, first_look=15, first_seen_only=1, backfill=2)
    assert (result.observed, result.excluded) == (34, 18)
    assert freshness_by_source(db_session, window.now) == {
        "ashby": Freshness(p95_h=8.0, n=1, first_look=4),
        "edgar": Freshness(p95_h=None, n=0, backfill=1),
        "greenhouse": Freshness(p95_h=10.8, n=3, first_seen_only=1),
        "news": Freshness(p95_h=105.4, n=12, first_look=11, backfill=1),
    }
    assert freshness_p95_h(db_session, window.now, ["greenhouse", "ashby"]) == 10.7


def test_window_cases(db_session: Session) -> None:
    """Every case of the fixture lands where ADR 0043 says, with the lag it says."""
    window = load_window(db_session)
    events = {e.event_id: e for e in freshness_events(db_session, window.now)}
    assert set(events) == set(window.cases)
    by_case: dict[str, list[Any]] = defaultdict(list)
    for event_id, case in window.cases.items():
        by_case[case].append(events[event_id])
    assert {case: {e.reason for e in found} for case, found in by_case.items()} == {
        OLD_DATED: {None},
        "new posting": {None},
        "job_updated: no source timestamp": {FIRST_SEEN_ONLY},
        "board first poll": {FIRST_LOOK},
        ARCHIVE: {FIRST_LOOK},
        "company feed item published after discovery": {None},
        "Google News, company polled before": {None},
        LATE: {None},
        "Google News, first query for the company": {FIRST_LOOK},
        COHORT: {BACKFILL},
        FORM_D: {BACKFILL},
    }
    # Greenhouse dates the posting by first_published (2026-09-02); the board's previous poll
    # (2026-10-18 15:45:04) did not list it, so tell's lag is at most the gap since: 11.2 h,
    # where the literal formula said 1,116.3 h.
    (old,) = by_case[OLD_DATED]
    assert round(old.lag_h, 2) == 11.19
    # Dated 2026-10-15 09:00 but absent from the query of 2026-10-18 02:33:04: 24.1 h, not 89.6 h.
    (late,) = by_case[LATE]
    assert round(late.lag_h, 2) == 24.07
    (posted,) = by_case["company feed item published after discovery"]
    assert round(posted.lag_h, 2) == 16.62
    assert sorted(round(e.lag_h, 1) for e in by_case["new posting"]) == [4.3, 6.8, 8.0]


def test_window_health_rows(db_session: Session) -> None:
    window = load_window(db_session)
    sources = [(name, "collector") for name in ("ashby", "edgar", "greenhouse", "news")]
    health = compute_health(db_session, window.now, sources)
    assert {row.source: row.freshness_p95_h for row in health.sources} == {
        "ashby": 8.0,
        "edgar": None,
        "greenhouse": 10.8,
        "news": 105.4,
    }
    assert health.freshness_p95_h == 103.0


# --- one rule at a time ---------------------------------------------------------------------------


def _company(session: Session, domain: str = "kilo.example") -> Company:
    row = Company(domain=domain, name=domain.split(".", maxsplit=1)[0].title())
    session.add(row)
    session.flush()
    return row


def _run(session: Session, source: str, at: datetime, notes: dict[str, Any] | None = None) -> Run:
    row = Run(
        run_id=f"{source}-{at:%Y%m%dT%H%M%S}-{len(source)}{at.microsecond}",
        source=source,
        started_at=at,
        finished_at=at + timedelta(minutes=3),
        status="ok",
        notes=notes,
    )
    session.add(row)
    session.flush()
    return row


def _event(
    session: Session,
    source: str,
    occurred: datetime,
    observed: datetime,
    *,
    company: Company | None = None,
    type_: str = "event",
    payload: dict[str, Any] | None = None,
    run_id: str = "run",
) -> EventRow:
    row = EventRow(
        event_id=f"{source}-{occurred.isoformat()}-{observed.isoformat()}-{type_}-{run_id}",
        company_id=company.id if company is not None else None,
        source=source,
        type=type_,
        occurred_at=occurred,
        observed_at=observed,
        magnitude=Decimal(1),
        url=f"https://example.invalid/{source}/{type_}",
        payload=payload or {},
        run_id=run_id,
    )
    session.add(row)
    session.flush()
    return row


def _reasons(session: Session, now: datetime = NOW) -> list[tuple[str | None, float | None]]:
    return sorted(
        (
            (e.reason, None if e.lag_h is None else round(e.lag_h, 2))
            for e in freshness_events(session, now)
        ),
        key=lambda item: (item[0] or "", item[1] or 0.0),
    )


def test_edgar_lag_runs_from_the_daily_index(db_session: Session) -> None:
    """A filing dated D is listed from D + 1 at about 02:02 UTC: that is the earliest moment."""
    assert timedelta(hours=INDEX_LAG_HOURS) == EDGAR_INDEX_LAG
    _run(db_session, "edgar", datetime(2026, 10, 14, 2, 35, tzinfo=UTC))
    filed = datetime(2026, 10, 19, tzinfo=UTC)
    _event(
        db_session, "edgar", filed, datetime(2026, 10, 20, 2, 35, tzinfo=UTC), type_="form_d_filed"
    )
    # Indexed 2026-10-13 02:00, before tell's first EDGAR run read anything: a first look.
    early = datetime(2026, 10, 12, tzinfo=UTC)
    _event(
        db_session,
        "edgar",
        early,
        datetime(2026, 10, 20, 2, 35, tzinfo=UTC),
        type_="form_d_amendment",
    )
    assert _reasons(db_session) == [(None, 0.58), (FIRST_LOOK, None)]
    assert freshness_p95_h(db_session, NOW, ["edgar"]) == 0.6  # literal formula: 26.6 h


def test_any_other_source_counts_from_its_first_run(db_session: Session) -> None:
    _event(db_session, "github", NOW - 3 * HOUR, NOW - HOUR, type_="github_release")
    assert _reasons(db_session) == [(FIRST_LOOK, None)]  # no github run on record
    _run(db_session, "github", NOW - 10 * DAY)
    _event(db_session, "github", NOW - 400 * DAY, NOW - HOUR, type_="github_repo_created")
    assert _reasons(db_session) == [(None, 2.0), (FIRST_LOOK, None)]


def test_company_feed_needs_its_discovery_on_record(db_session: Session) -> None:
    company = _company(db_session)
    url = "https://kilo.example/feed.xml"
    payload = {"query": url, "match": {"rule": "R1", "confidence": 0.98}}
    _event(db_session, "news", NOW - 5 * HOUR, NOW - HOUR, company=company, payload=payload)
    assert _reasons(db_session) == [(FIRST_LOOK, None)]  # no company_feeds row
    db_session.add(
        CompanyFeed(company_id=company.id, feed_url=url, status="active", created_at=NOW - 2 * DAY)
    )
    db_session.flush()
    assert _reasons(db_session) == [(None, 4.0)]


def test_google_news_uses_the_previous_query(db_session: Session) -> None:
    company = _company(db_session)
    query = "https://news.google.com/rss/search?q=%22Kilo%22+when%3A7d&hl=en-US&gl=US&ceid=US:en"
    run = _run(db_session, "news", NOW - HOUR)
    observed = run.started_at
    for lag in (2, 30):
        _event(
            db_session,
            "news",
            observed - lag * HOUR,
            observed,
            company=company,
            type_=f"news_{lag}",
            payload={"query": query},
            run_id=run.run_id,
        )
    assert _reasons(db_session) == [(FIRST_LOOK, None), (FIRST_LOOK, None)]  # first query
    _run(db_session, "news", observed - 10 * HOUR, {"polled": {str(company.id): "x"}})
    _run(db_session, "news", observed - 3 * DAY, {"polled": {str(company.id + 1): "x"}})
    _run(db_session, "news", observed - 4 * DAY, {"polled": ["not", "an", "object"]})
    assert _reasons(db_session) == [(None, 2.0), (None, 10.0)]  # 30 h old, unseen 10 h ago


def test_widened_google_news_window_is_a_backfill(db_session: Session) -> None:
    company = _company(db_session)
    query = "https://news.google.com/rss/search?q=%22Kilo%22+when%3A120d&hl=en-US&gl=US&ceid=US:en"
    _run(db_session, "news", NOW - 9 * DAY, {"polled": {str(company.id): "x"}})
    for days, run_id in ((120, "news-cohort"), ("120", "news-odd"), (None, "news-plain")):
        notes: dict[str, Any] = {"polled": {str(company.id): "x"}}
        if days is not None:
            notes["when_days"] = days
        db_session.add(
            Run(run_id=run_id, source="news", started_at=NOW - 2 * HOUR, status="ok", notes=notes)
        )
        _event(
            db_session,
            "news",
            NOW - 50 * DAY,
            NOW - 2 * HOUR,
            company=company,
            type_=run_id,
            payload={"query": query},
            run_id=run_id,
        )
    # Only a numeric when_days above 7 marks a cohort run; the others are ordinary queries
    # whose previous look (9 days ago) bounds the lag.
    assert _reasons(db_session) == [(None, 214.0), (None, 214.0), (BACKFILL, None)]


def test_board_without_an_earlier_poll_is_a_first_look(db_session: Session) -> None:
    """A repost on a board's first poll carries no ``first_poll`` flag; the observations decide."""
    company = _company(db_session)
    board = AtsBoard(
        company_id=company.id, vendor="lever", identifier="kilo", confidence=Decimal("0.9")
    )
    db_session.add(board)
    db_session.flush()
    posting = Posting(
        board_id=board.id,
        external_id="p-1",
        fingerprint="fp",
        title="Engineer",
        title_norm="engineer",
        role_family="engineering",
        first_seen_at=NOW - 2 * HOUR,
        last_seen_at=NOW - 2 * HOUR,
        url="https://jobs.lever.co/kilo/p-1",
    )
    db_session.add(posting)
    db_session.flush()
    db_session.add(
        PostingObservation(posting_id=posting.id, observed_at=NOW - 2 * HOUR, run_id="r")
    )
    payload = {"vendor": "lever", "board_identifier": "kilo", "posting_external_id": "p-1"}
    _event(
        db_session,
        "lever",
        NOW - 30 * DAY,
        NOW - 2 * HOUR,
        company=company,
        type_="job_reposted",
        payload=payload,
    )
    _event(db_session, "lever", NOW - 3 * HOUR, NOW - HOUR, type_="job_opened", payload={})
    assert _reasons(db_session) == [(FIRST_LOOK, None), (FIRST_LOOK, None)]
    db_session.add(
        PostingObservation(posting_id=posting.id, observed_at=NOW - 7 * HOUR, run_id="r")
    )
    db_session.flush()
    assert _reasons(db_session) == [(None, 5.0), (FIRST_LOOK, None)]


def test_window_scope_and_negative_lags(db_session: Session) -> None:
    _run(db_session, "github", NOW - 10 * DAY)
    _event(db_session, "tell", NOW - 400 * HOUR, NOW - HOUR, type_="hiring_velocity")
    _event(db_session, "github", NOW + HOUR, NOW - HOUR, type_="clock_skew")  # counts as zero
    _event(db_session, "github", NOW - 30 * HOUR, NOW - 25 * HOUR, type_="before_window")
    _event(db_session, "github", NOW - 2 * HOUR, NOW - 2 * HOUR, type_="dated_by_tell")
    assert freshness(db_session, NOW) == Freshness(p95_h=0.0, n=1, first_seen_only=1)
    assert freshness(db_session, NOW, ["tell"]).observed == 1
    assert freshness(db_session, NOW, ["lever"]) == Freshness(p95_h=None, n=0)
    assert freshness(db_session, NOW + 2 * DAY) == Freshness(p95_h=None, n=0)
