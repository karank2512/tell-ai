"""Per-event Slack nudges (ADR 0036): blocks, the v0 gate, routing, idempotency, the CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.collectors.base import finish_run, start_run
from tell.config import get_settings
from tell.db import SessionLocal
from tell.intake.reader import PrivateWatchlist, write_private_watchlists
from tell.models import Nudge, Run
from tell.publish import cli
from tell.publish.nudge import (
    NUDGE_SOURCE,
    build_nudge_blocks,
    load_private_webhooks,
    pending_nudges,
    send_nudges,
    worth_enriching,
)
from tell.publish.slack import DIGEST_SOURCE, SEND_INTERVAL_S, CrossedAccount, send_digests
from tests.publish import scenario as sc

GLOBAL = "https://hooks.slack.invalid/services/T0/B0/GLOBAL"
PRIVATE = "https://hooks.slack.invalid/services/T0/B0/PRIVATE"
BASE = "https://pages.example/tell-data"
FORM = "https://forms.example/f?s={signal_id}&d={domain}&v={verdict}"
SLUG = "private-list"
TZ = "America/Los_Angeles"


def _account(*families: str, threshold: float | None = 6.0) -> CrossedAccount:
    contributions = [
        {
            "signal_id": 100 + i,
            "signal_type": family,
            "contribution": 2.0,
            "summary": f"{family} summary <{i}> & more",
            "url": f"https://receipts.example/{i}",
        }
        for i, family in enumerate(families)
    ]
    return CrossedAccount(
        name="Acme & Co",
        domain="acme.example",
        score=7.5,
        contributions=contributions,
        event_id="evt",
        company_id=1,
        threshold=threshold,
    )


# --- blocks and the v0 gate --------------------------------------------------------------------


def test_worth_enriching_counts_distinct_families_with_receipts() -> None:
    assert worth_enriching(_account("funding", "hiring_velocity").contributions) == (
        True,
        ["funding", "hiring_velocity"],
    )
    assert worth_enriching(_account("funding", "funding").contributions) == (False, ["funding"])
    assert worth_enriching([]) == (False, [])
    without_url = [{"signal_type": "launch"}, {"signal_type": "funding", "url": "https://x"}]
    assert worth_enriching(without_url) == (False, ["funding"])


def test_nudge_blocks_shape_escaping_and_gate_yes() -> None:
    blocks = build_nudge_blocks(
        _account("funding", "hiring_velocity", "launch"), pages_base_url=BASE + "/"
    )
    assert [b["type"] for b in blocks] == ["section", "context"]
    lines = blocks[0]["text"]["text"].splitlines()
    assert lines[0] == "*Acme &amp; Co* (acme.example) · score 7.5 · crossed 6"
    assert lines[1] == "• <https://receipts.example/0|funding summary &lt;0&gt; &amp; more>"
    assert lines[-2] == (
        "Why now: funding summary &lt;0&gt; &amp; more and hiring_velocity summary &lt;1&gt; &amp; more."
    )
    assert lines[-1] == "Worth enriching: yes · funding + hiring_velocity"
    context = blocks[1]["elements"]
    assert context == [
        {"type": "mrkdwn", "text": f"<{BASE}/companies/acme.example.html|Account page>"}
    ]
    json.dumps(blocks)


def test_nudge_blocks_gate_not_yet_and_no_threshold() -> None:
    blocks = build_nudge_blocks(_account("funding", threshold=None), pages_base_url=BASE)
    lines = blocks[0]["text"]["text"].splitlines()
    assert lines[0] == "*Acme &amp; Co* (acme.example) · score 7.5"
    assert lines[-1] == "Worth enriching: not yet · one signal family"


def test_nudge_blocks_feedback_links_only_with_a_form() -> None:
    account = _account("funding", "launch")
    blocks = build_nudge_blocks(account, pages_base_url=BASE, feedback_form_url=FORM)
    rate = blocks[1]["elements"][1]["text"]
    assert rate == (
        "Rate it: <https://forms.example/f?s=100&d=acme.example&v=useful|useful>"
        " · <https://forms.example/f?s=100&d=acme.example&v=not_useful|not useful>"
    )
    assert len(build_nudge_blocks(account, pages_base_url=BASE)[1]["elements"]) == 1
    # No contribution: the links still work, with an empty signal id.
    lone = CrossedAccount(name="Q", domain="q.example", score=6.0, contributions=[])
    blocks = build_nudge_blocks(lone, pages_base_url=BASE, feedback_form_url=FORM)
    assert "f?s=&d=q.example&v=useful" in blocks[1]["elements"][1]["text"]


def test_nudge_section_stays_under_the_slack_limit() -> None:
    many = [
        {"signal_type": "launch", "summary": "x" * 200, "url": f"https://news.example/{i}"}
        for i in range(40)
    ]
    account = CrossedAccount(name="Big", domain="big.example", score=9.0, contributions=many)
    text = build_nudge_blocks(account, pages_base_url=BASE)[0]["text"]["text"]
    assert len(text) <= 3000
    assert "• …" in text
    assert text.endswith("Worth enriching: not yet · one signal family")


# --- private webhooks ----------------------------------------------------------------------------


def _record(slug: str, webhook: str | None) -> PrivateWatchlist:
    return PrivateWatchlist(
        slug=slug, company="Co", icp_profile="default", owner="x", slack_webhook_url=webhook
    )


def test_load_private_webhooks(tmp_path: Path) -> None:
    assert load_private_webhooks(tmp_path / "missing") == {}
    write_private_watchlists(
        tmp_path, {SLUG: _record(SLUG, PRIVATE), "other-default": _record("other-default", None)}
    )
    assert load_private_webhooks(tmp_path) == {SLUG: PRIVATE}
    (tmp_path / "watchlists" / "broken.yaml").write_text("slug: [unclosed", encoding="utf-8")
    assert load_private_webhooks(tmp_path) == {}  # one bad file: no webhooks at all
    (tmp_path / "watchlists" / "broken.yaml").write_text("slug: other\n", encoding="utf-8")
    assert load_private_webhooks(tmp_path) == {}


# --- sending -------------------------------------------------------------------------------------


@pytest.fixture
def frozen() -> Iterator[None]:
    with freeze_time(sc.NOW):
        yield


def _scenario_with_private_slug(session: Session) -> sc.Scenario:
    scenario = sc.build(session)
    scenario.private.slug = SLUG
    session.flush()
    return scenario


def _nudge_runs(session: Session) -> list[Run]:
    return list(session.scalars(select(Run).where(Run.source == NUDGE_SOURCE)))


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_send_nudges_one_message_per_account_to_the_private_webhook_only(
    db_session: Session,
) -> None:
    scenario = _scenario_with_private_slug(db_session)
    assert [a.domain for a in pending_nudges(db_session, scenario.private, sc.NOW)] == [
        sc.ROBOTICS,
        sc.BLUEFIN,
    ]
    sleeps: list[float] = []
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        route = mock.post(PRIVATE).respond(200, text="ok")
        outcome = send_nudges(
            db_session, now=sc.NOW, private_webhooks={SLUG: PRIVATE}, sleep=sleeps.append
        )
    assert (outcome.candidates, outcome.sent, outcome.errors) == (2, 2, 0)
    assert sleeps == [SEND_INTERVAL_S]
    bodies: list[dict[str, Any]] = [json.loads(c.request.content) for c in route.calls]
    assert [b["text"].split(" (")[0] for b in bodies] == [
        "tell · Example Robotics",
        "tell · Bluefin Data Systems",
    ]
    assert bodies[0]["blocks"][0]["text"]["text"].startswith("*Example Robotics* (")
    assert " · crossed 6" in bodies[0]["blocks"][0]["text"]["text"].splitlines()[0]
    assert all("Worth enriching:" in b["blocks"][0]["text"]["text"] for b in bodies)
    (run,) = _nudge_runs(db_session)
    assert (run.status, run.fetched, run.inserted, run.errors) == ("ok", 2, 2, 0)
    assert run.notes == {"watchlists": 1}
    rows = list(db_session.scalars(select(Nudge).order_by(Nudge.id)))
    assert [(r.watchlist_id, r.status, r.http_status) for r in rows] == [
        (scenario.private.id, "ok", 200),
        (scenario.private.id, "ok", 200),
    ]
    # Idempotent: the next run (same day) has nothing to send and records nothing.
    with respx.mock(assert_all_mocked=True):
        again = send_nudges(
            db_session,
            now=sc.NOW + timedelta(hours=6),
            private_webhooks={SLUG: PRIVATE},
            sleep=sleeps.append,
        )
    assert again.skipped == "nothing to nudge"
    assert len(_nudge_runs(db_session)) == 1
    # The public list has no private webhook, so it is never nudged (the global webhook
    # only ever receives the digest).
    assert send_nudges(db_session, now=sc.NOW, private_webhooks={}).skipped == (
        "no private webhooks"
    )
    # A slug that names no watchlist is simply nothing to do.
    assert send_nudges(db_session, now=sc.NOW, private_webhooks={"ghost": PRIVATE}).skipped == (
        "nothing to nudge"
    )


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_failed_nudge_is_recorded_and_retried_until_the_window_closes(
    db_session: Session,
) -> None:
    scenario = _scenario_with_private_slug(db_session)
    with respx.mock as mock:
        mock.post(PRIVATE).respond(404, text="no_service")
        first = send_nudges(
            db_session, now=sc.NOW, private_webhooks={SLUG: PRIVATE}, sleep=lambda _: None
        )
    assert (first.candidates, first.sent, first.errors) == (2, 0, 2)
    (run,) = _nudge_runs(db_session)
    assert run.status == "failed"
    rows = list(db_session.scalars(select(Nudge)))
    assert {(r.status, r.http_status) for r in rows} == {("failed", 404)}
    with respx.mock as mock:
        mock.post(PRIVATE).mock(side_effect=httpx.ConnectError("down"))
        second = send_nudges(
            db_session,
            now=sc.NOW + timedelta(hours=6),
            private_webhooks={SLUG: PRIVATE},
            sleep=lambda _: None,
        )
    assert (second.sent, second.errors) == (0, 2)
    rows = list(db_session.scalars(select(Nudge)))
    assert len(rows) == 2  # updated in place, never duplicated
    assert {(r.status, r.http_status) for r in rows} == {("failed", None)}
    with respx.mock as mock:
        route = mock.post(PRIVATE).respond(200)
        third = send_nudges(
            db_session,
            now=sc.NOW + timedelta(hours=12),
            private_webhooks={SLUG: PRIVATE},
            sleep=lambda _: None,
        )
    assert (third.sent, third.errors, route.call_count) == (2, 0, 2)
    assert {r.status for r in db_session.scalars(select(Nudge))} == {"ok"}
    # Past 24 hours a crossing is never nudged, even when it was never sent.
    db_session.query(Nudge).delete()
    assert pending_nudges(db_session, scenario.private, sc.NOW + timedelta(days=2)) == []
    late = send_nudges(db_session, now=sc.NOW + timedelta(days=2), private_webhooks={SLUG: PRIVATE})
    assert late.skipped == "nothing to nudge"


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_digest_routes_to_the_private_webhook_else_the_global_one(db_session: Session) -> None:
    scenario = _scenario_with_private_slug(db_session)
    now = datetime(2026, 10, 6, 14, 17, tzinfo=UTC)  # 07:17 in Los Angeles: the digest run
    with respx.mock(assert_all_called=True) as mock:
        public_route = mock.post(GLOBAL).respond(200)
        private_route = mock.post(PRIVATE).respond(200)
        outcome = send_digests(
            db_session,
            now=now,
            webhook_url=GLOBAL,
            private_webhooks={SLUG: PRIVATE},
            sleep=lambda _: None,
            tz=TZ,
            hour=7,
        )
    assert (outcome.sent, outcome.errors, outcome.without_webhook) == (2, 0, 0)
    assert json.loads(public_route.calls[0].request.content)["text"].startswith("tell · Pilot list")
    assert json.loads(private_route.calls[0].request.content)["text"].startswith(
        "tell · Private list"
    )
    # Without the global webhook only the private list is sent; the other is counted.
    run_id = start_run(db_session, DIGEST_SOURCE, now=now - timedelta(days=1))
    finish_run(db_session, run_id, status="ok", fetched=1, inserted=1, skipped=0, now=now)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        private_route = mock.post(PRIVATE).respond(200)
        outcome = send_digests(
            db_session,
            now=now,
            webhook_url=None,
            private_webhooks={SLUG: PRIVATE},
            sleep=lambda _: None,
            force=True,
            tz=TZ,
            hour=7,
        )
    assert (outcome.sent, outcome.errors, outcome.without_webhook) == (1, 0, 1)
    assert private_route.call_count == 1
    assert scenario.public.slug == "pilot"
    assert send_digests(db_session, now=now, webhook_url=None).skipped == "no webhook"


# --- CLI -------------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    monkeypatch.setattr("tell.publish.slack.time.sleep", lambda _: None)
    monkeypatch.setattr("tell.publish.nudge.time.sleep", lambda _: None)
    with freeze_time(sc.NOW):
        with SessionLocal(bind=engine) as session:
            _scenario_with_private_slug(session)
            session.commit()
        yield engine


@pytest.mark.db
def test_notify_cli_reports_nudges_and_digest(
    bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_private_watchlists(tmp_path, {SLUG: _record(SLUG, PRIVATE)})
    monkeypatch.setenv("SLACK_WEBHOOK_URL", GLOBAL)
    monkeypatch.setenv("TELL_FEEDBACK_FORM_URL", FORM)
    get_settings.cache_clear()
    args = ["notify", "--slack", "--private-dir", str(tmp_path)]
    with freeze_time("2026-10-06T12:17:00Z"), respx.mock(assert_all_mocked=True) as mock:
        private_route = mock.post(PRIVATE).respond(200)
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        record = json.loads(result.output)
        # 05:17 in Los Angeles: nudges go out now, the digest waits for the digest hour.
        assert record["skipped"] == "not the digest run"
        assert record["nudges"]["candidates"] == 2
        assert record["nudges"]["sent"] == 2
        assert record["nudges"]["run_id"]
        assert private_route.call_count == 2
        body = json.loads(private_route.calls[0].request.content)
        assert "Rate it:" in body["blocks"][1]["elements"][1]["text"]
        # Second call: nothing left to nudge.
        record = json.loads(runner.invoke(cli.app, args).output)
        assert record["nudges"]["skipped"] == "nothing to nudge"
        assert private_route.call_count == 2
    # Everything attempted failed: exit 1. Here the digest (forced) fails on both webhooks
    # while there is nothing to nudge.
    with respx.mock as mock:
        mock.post(GLOBAL).respond(500)
        mock.post(PRIVATE).respond(500)
        result = runner.invoke(cli.app, [*args, "--force"])
    assert result.exit_code == 1
    record = json.loads(result.output)
    assert (record["sent"], record["errors"]) == (0, 2)
    assert record["nudges"]["skipped"] == "nothing to nudge"
    # A missing private dir means no private webhooks; the digest still goes global.
    with respx.mock as mock:
        mock.post(GLOBAL).respond(200)
        result = runner.invoke(
            cli.app, ["notify", "--slack", "--force", "--private-dir", str(tmp_path / "none")]
        )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["nudges"]["skipped"] == "no private webhooks"
    assert record["sent"] == 2
