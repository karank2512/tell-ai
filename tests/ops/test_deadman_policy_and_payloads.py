"""Dead-man follows the news fetch policy; non-object payloads fail closed (synthetic only).

Third local slice. Every test here runs with outbound sockets refused (loopback only, for
the throwaway Postgres).

Dead-man: with no company-feed permit (the shipped policy) news cannot run, so it is
reported as ``skipped_by_policy``: listed in the JSON and the step summary, never silent,
never failing. No ``runs`` row is written for it. With a permit, news is checked as before.

Payloads: an event whose payload is not a JSON object (a list, a string, null, a number)
is excluded by :func:`classify_event` with ``payload_not_object`` and never raises; the
signal build, scoring, the Slack count and the prune delete guard all complete over it.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import tell.db
from tell import cli as tell_cli
from tell.db import SessionLocal
from tell.models import EventRow, Run, Signal
from tell.ops import deadman, source_eligibility
from tell.ops.lineage import deletable_predicate
from tell.ops.source_eligibility import INDEPENDENT_SOURCES, Provenance, Reason, classify_event
from tell.publish.slack import new_signals_count
from tell.signals.profiles import load_profiles
from tell.signals.scoring import build_signals, score_companies, score_instant
from tests.ops.helpers import add_company, add_run
from tests.source_feed import TEST_PERMIT

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
HOUR = timedelta(hours=1)
OTHERS = tuple(s for s in deadman.SCHEDULED_SOURCES if s != "news")


@pytest.fixture(autouse=True)
def _no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every socket connection except loopback (the throwaway Postgres)."""
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
    assert deadman.SCHEDULED_SOURCES == (
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "edgar",
        "news",
    )


# --- dead-man -----------------------------------------------------------------------------


def _statuses(report: deadman.DeadmanReport) -> dict[str, str]:
    return {s.source: s.status for s in report.sources}


@pytest.mark.db
def test_no_permit_five_sources_ok_is_green_and_news_is_skipped_by_policy(
    db_session: Session,
) -> None:
    for source in OTHERS:
        add_run(db_session, source, NOW - 3 * HOUR)
    report = deadman.check(db_session, NOW)
    assert report.ok
    assert report.silent == []
    statuses = _statuses(report)
    assert statuses == {**dict.fromkeys(OTHERS, "ok"), "news": "skipped_by_policy"}
    assert [s.source for s in report.sources] == list(deadman.SCHEDULED_SOURCES)
    text = report.markdown()
    assert "green" in text
    assert "`news`: skipped_by_policy" in text
    # No run row was written for news.
    assert db_session.scalar(select(func.count()).where(Run.source == "news")) == 0


@pytest.mark.db
def test_no_permit_one_stale_ats_source_is_red_with_only_that_source_silent(
    db_session: Session,
) -> None:
    for source in OTHERS:
        add_run(db_session, source, NOW - (30 if source == "ashby" else 3) * HOUR)
    report = deadman.check(db_session, NOW)
    assert not report.ok
    assert report.silent == ["ashby"]
    assert _statuses(report)["ashby"] == "stale"
    assert _statuses(report)["news"] == "skipped_by_policy"
    text = report.markdown()
    assert "red" in text
    assert "No successful run in 24 h: ashby." in text
    assert "`news`: skipped_by_policy" in text


@pytest.mark.db
def test_with_a_permit_news_is_checked_again(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_eligibility, "PERMITS", (TEST_PERMIT,))
    for source in OTHERS:
        add_run(db_session, source, NOW - 3 * HOUR)
    never = deadman.check(db_session, NOW)
    assert _statuses(never)["news"] == "never_ran"
    assert never.silent == ["news"]
    add_run(db_session, "news", NOW - 40 * HOUR)
    stale = deadman.check(db_session, NOW)
    assert _statuses(stale)["news"] == "stale"
    assert stale.silent == ["news"]


@pytest.mark.db
def test_explicit_news_only_without_a_permit_is_skipped_by_policy(db_session: Session) -> None:
    report = deadman.check(db_session, NOW, ("news",))
    assert report.ok
    assert _statuses(report) == {"news": "skipped_by_policy"}
    assert "skipped_by_policy" in report.markdown()


@pytest.mark.db
def test_json_shape_is_unchanged_apart_from_the_new_status(db_session: Session) -> None:
    for source in OTHERS:
        add_run(db_session, source, NOW - 3 * HOUR)
    obj = deadman.check(db_session, NOW).to_dict()
    assert list(obj) == ["checked_at", "ok", "silent", "sources"]
    for entry in obj["sources"]:
        assert list(entry) == ["source", "last_ok_at", "status"]
    assert {e["status"] for e in obj["sources"]} == {"ok", "skipped_by_policy"}


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    return engine


def _seed(engine: Engine, *, stale: str | None = None) -> None:
    with SessionLocal(bind=engine) as session:
        for source in OTHERS:
            add_run(session, source, NOW - (30 if source == stale else 3) * HOUR)
        session.commit()


@pytest.mark.db
@pytest.mark.parametrize(("stale", "code"), [(None, 0), ("ashby", 1)])
def test_cli_deadman_exit_code_and_summary(
    bound: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale: str | None,
    code: int,
) -> None:
    _seed(bound, stale=stale)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with freeze_time(NOW):
        result = CliRunner().invoke(tell_cli.app, ["deadman"])
    assert result.exit_code == code, result.output
    report = json.loads(result.stdout.strip().splitlines()[0])
    assert report["silent"] == ([] if stale is None else [stale])
    text = summary.read_text(encoding="utf-8")
    assert "`news`: skipped_by_policy" in text
    assert ("dead-man: green" in text) is (code == 0)


# --- non-object payloads ------------------------------------------------------------------

NON_OBJECTS: list[Any] = [None, [], ["a", "list"], "a string", "", 7, 1.5, True]


@pytest.mark.parametrize("payload", NON_OBJECTS, ids=repr)
@pytest.mark.parametrize(
    ("source", "type_"),
    [
        *[(s, "form_d_filed") for s in sorted(INDEPENDENT_SOURCES)],
        ("tell", "hiring_velocity"),
        ("tell", "score_crossed_threshold"),
        ("news", "news_launch"),
        ("synthetic", "news_launch"),
    ],
)
def test_non_object_payload_is_excluded_with_a_reason(
    source: str, type_: str, payload: Any
) -> None:
    decision = classify_event(source, type_, "https://example.test/x", payload)
    assert decision.reason is Reason.PAYLOAD_NOT_OBJECT
    assert decision.provenance is Provenance.UNKNOWN
    assert not decision.eligible


def _event(
    session: Session, event_id: str, source: str, type_: str, company_id: int, payload: Any
) -> EventRow:
    row = EventRow(
        event_id=event_id,
        company_id=company_id,
        source=source,
        type=type_,
        occurred_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
        observed_at=datetime(2026, 10, 5, 13, tzinfo=UTC),
        magnitude=Decimal(1),
        url=f"https://example.test/{event_id}",
        payload=payload,
        run_id="synthetic",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def odd_payloads(db_session: Session) -> int:
    """One good Form D plus events whose JSONB payload is an array, a string and a number."""
    company = add_company(db_session, "odd.example")
    _event(
        db_session,
        "good-d",
        "edgar",
        "form_d_filed",
        company.id,
        {"total_offering_amount": 5_000_000},
    )
    odd = [
        _event(db_session, "list-d", "edgar", "form_d_filed", company.id, ["x", 1]),
        _event(db_session, "str-launch", "news", "news_launch", company.id, "headline"),
        _event(db_session, "num-velocity", "tell", "hiring_velocity", company.id, 3),
    ]
    # Legacy signals stored on the odd events before this change.
    for event in odd:
        db_session.add(
            Signal(
                company_id=company.id,
                event_id=event.event_id,
                signal_type="launch",
                role_family=None,
                weight=Decimal(2),
                magnitude=Decimal(1),
                half_life_days=30,
                starts_at=event.occurred_at,
            )
        )
    db_session.flush()
    kinds = dict(
        db_session.execute(select(EventRow.event_id, func.jsonb_typeof(EventRow.payload))).all()
    )
    assert kinds == {
        "good-d": "object",
        "list-d": "array",
        "str-launch": "string",
        "num-velocity": "number",
    }
    return company.id


@pytest.mark.db
def test_scoring_pass_over_non_object_payloads_completes(
    db_session: Session, odd_payloads: int
) -> None:
    as_of = date(2026, 10, 6)
    inserted = build_signals(db_session, now=score_instant(as_of))
    derived = set(db_session.scalars(select(Signal.event_id).where(Signal.event_id == "good-d")))
    assert inserted == 1
    assert derived == {"good-d"}
    profile = load_profiles()["default"]
    (result,) = score_companies(db_session, profile, as_of, [odd_payloads])
    good = db_session.scalars(select(Signal.id).where(Signal.event_id == "good-d")).one()
    assert {c["signal_id"] for c in result.contributions} == {good}
    assert new_signals_count(db_session, [odd_payloads], datetime(2026, 10, 5, 14, tzinfo=UTC)) == 1


@pytest.mark.db
def test_delete_guard_never_matches_a_non_object_payload(
    db_session: Session, odd_payloads: int
) -> None:
    del odd_payloads
    deletable = set(db_session.scalars(select(EventRow.event_id).where(deletable_predicate())))
    assert deletable == {"good-d"}
