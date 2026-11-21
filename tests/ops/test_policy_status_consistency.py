"""One policy status across the dead-man, the health page and the runlog (synthetic only).

Fourth local slice. With no company-feed permit (the shipped policy) news cannot run:

* ``skipped_by_policy`` everywhere: the dead-man, ``health.json``/the health page and the
  weekly operations line. Not ``stale``, not silent, and it does not pin
  ``weeks_unattended`` to 0.0.
* ``policy_breach`` when a news run nonetheless *started* in the last 24 hours: the
  dead-man fails (silent), the health page marks it stale (red banner), and the ops line
  reports it and shows 0.0 weeks unattended. A breach is never hidden as a skip.
* With a permit, news is checked exactly as before.

Also: the dead-man headline when every requested source is skipped, and a crossing whose
payload is not a JSON object classified ``payload_not_object`` by ``event_decision``.
Outbound sockets are refused (loopback only, for the throwaway Postgres).
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tell.ops import deadman, runlog, source_eligibility
from tell.ops.lineage import event_decision
from tell.ops.source_eligibility import Reason
from tell.publish.health import compute_health
from tell.publish.json_api import build_health_json, validate_health_json
from tests.ops.helpers import add_run
from tests.source_feed import TEST_PERMIT

NOW = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
OTHERS = tuple(s for s in deadman.SCHEDULED_SOURCES if s != "news")
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if self.family == socket.AF_UNIX or host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(self, address)
        msg = f"outbound socket blocked in tests: {address!r}"
        raise OSError(msg)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def test_constants_stay_safe() -> None:
    assert source_eligibility.WIRED is False
    assert source_eligibility.ELIGIBILITY_CUTOVER is None
    assert source_eligibility.PERMITS == ()


def _steady(session: Session, sources: tuple[str, ...], days: int) -> None:
    """A successful run every 6 hours for ``days`` days up to 1 h before NOW."""
    for source in sources:
        t = NOW - days * DAY
        while t < NOW - HOUR:
            add_run(session, source, t)
            t += 6 * HOUR


def _news_history(session: Session) -> None:
    """News ran fine for two weeks, then stopped three weeks ago (before the exclusion)."""
    t = NOW - 35 * DAY
    while t < NOW - 21 * DAY:
        add_run(session, "news", t)
        t += 6 * HOUR


# --- runlog -------------------------------------------------------------------------------


@pytest.mark.db
def test_weeks_unattended_ignores_news_skipped_by_policy(db_session: Session) -> None:
    _steady(db_session, OTHERS, days=35)
    _news_history(db_session)
    weeks, since = runlog.weeks_unattended(db_session, NOW)
    assert weeks is not None
    assert weeks >= 4.9  # the five scheduled sources ran all along: not 0.0 forever
    assert since is not None
    line = runlog.ops_line(db_session, NOW)
    assert line.weeks_unattended == weeks
    assert line.skipped_by_policy == ["news"]
    assert line.policy_breaches == []
    assert "skipped by policy: news" in line.summary
    assert line.to_dict()["skipped_by_policy"] == ["news"]


@pytest.mark.db
def test_weeks_unattended_with_a_permit_counts_news_as_before(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_eligibility, "PERMITS", (TEST_PERMIT,))
    _steady(db_session, OTHERS, days=35)
    _news_history(db_session)
    weeks, since = runlog.weeks_unattended(db_session, NOW)
    assert (weeks, since) == (0.0, NOW)  # news has been silent for three weeks
    assert runlog.ops_line(db_session, NOW).skipped_by_policy == []


@pytest.mark.db
def test_a_news_run_while_excluded_is_a_breach_in_the_ops_line(db_session: Session) -> None:
    _steady(db_session, OTHERS, days=35)
    add_run(db_session, "news", NOW - 2 * HOUR)  # ran although policy excludes it
    weeks, _ = runlog.weeks_unattended(db_session, NOW)
    line = runlog.ops_line(db_session, NOW)
    assert line.policy_breaches == ["news"]
    assert line.skipped_by_policy == []
    assert weeks == 0.0
    assert line.weeks_unattended == 0.0
    assert "POLICY BREACH: news" in line.summary


# --- health -------------------------------------------------------------------------------


def _health_sources() -> list[tuple[str, str]]:
    return [*((s, "collector") for s in deadman.SCHEDULED_SOURCES), ("score", "job")]


@pytest.mark.db
def test_health_shows_news_skipped_by_policy_not_stale(db_session: Session) -> None:
    _steady(db_session, (*OTHERS, "score"), days=2)
    _news_history(db_session)
    report = compute_health(db_session, NOW, _health_sources())
    news = next(s for s in report.sources if s.source == "news")
    assert news.status == "skipped_by_policy"
    assert news.stale is False
    assert report.stale_sources == []
    assert not report.any_stale
    obj = build_health_json(report, sha=SHA)
    validate_health_json(obj)
    assert next(s for s in obj["sources"] if s["source"] == "news")["status"] == (
        "skipped_by_policy"
    )


@pytest.mark.db
def test_health_marks_a_news_run_while_excluded_as_a_red_breach(db_session: Session) -> None:
    _steady(db_session, (*OTHERS, "score"), days=2)
    add_run(db_session, "news", NOW - 2 * HOUR)
    report = compute_health(db_session, NOW, _health_sources())
    news = next(s for s in report.sources if s.source == "news")
    assert news.status == "policy_breach"
    assert news.stale is True
    assert report.stale_sources == ["news"]
    validate_health_json(build_health_json(report, sha=SHA))


@pytest.mark.db
def test_health_with_a_permit_shows_news_stale_as_before(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_eligibility, "PERMITS", (TEST_PERMIT,))
    _steady(db_session, (*OTHERS, "score"), days=2)
    _news_history(db_session)
    report = compute_health(db_session, NOW, _health_sources())
    news = next(s for s in report.sources if s.source == "news")
    assert (news.status, news.stale) == ("stale", True)


# --- dead-man: breach and headline --------------------------------------------------------


@pytest.mark.db
def test_deadman_reports_a_news_run_while_excluded_as_a_breach(db_session: Session) -> None:
    for source in OTHERS:
        add_run(db_session, source, NOW - 3 * HOUR)
    add_run(db_session, "news", NOW - 2 * HOUR)
    report = deadman.check(db_session, NOW)
    statuses = {s.source: s.status for s in report.sources}
    assert statuses["news"] == "policy_breach"
    assert report.silent == ["news"]
    assert not report.ok
    text = report.markdown()
    assert "red" in text
    assert "`news`: policy_breach" in text
    obj = report.to_dict()
    assert list(obj) == ["checked_at", "ok", "silent", "sources"]


@pytest.mark.db
def test_deadman_old_news_runs_are_not_a_breach(db_session: Session) -> None:
    for source in OTHERS:
        add_run(db_session, source, NOW - 3 * HOUR)
    _news_history(db_session)  # pre-exclusion runs, all older than 24 hours
    report = deadman.check(db_session, NOW)
    assert {s.source: s.status for s in report.sources}["news"] == "skipped_by_policy"
    assert report.ok


@pytest.mark.db
def test_deadman_headline_when_every_requested_source_is_skipped(db_session: Session) -> None:
    report = deadman.check(db_session, NOW, ("news",))
    assert report.ok
    head = report.markdown().splitlines()[2]
    assert "All 0" not in head
    assert "Nothing to check" in head
    assert "news" in head


# --- crossings with a non-object payload --------------------------------------------------


@pytest.mark.parametrize("payload", [None, [], ["x"], "text", 3], ids=repr)
def test_event_decision_gives_a_non_object_crossing_payload_not_object(payload: Any) -> None:
    decision = event_decision("tell", "score_crossed_threshold", "", payload, {})
    assert decision.reason is Reason.PAYLOAD_NOT_OBJECT
    assert not decision.eligible


def test_event_decision_still_reads_the_stamp_of_an_object_crossing() -> None:
    decision = event_decision("tell", "score_crossed_threshold", "", {"score": 1}, {})
    assert decision.reason is Reason.LINEAGE_INCOMPLETE  # unstamped, as before
