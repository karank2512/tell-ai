"""s5 review fixes: score subcommands and Slack behind the effect gate; policy-status UI.

Synthetic and local only; outbound sockets refused (loopback only, for the throwaway
Postgres).

* F1: ``tell score run``, ``tell score all`` and ``tell score --all-profiles all`` refuse
  (exit 3) before any profile load, session or connection while the external-effect gate
  is closed; ``--help`` still works; with the gate open they score (controls).
* F2: the Slack library refuses while the gate is closed (``post_blocks``,
  ``send_digests``, ``send_nudges``) before any client, request or database read.
* F3: no test module applies ``external_effects`` module-wide.
* F4: the documented ``policy_breach`` window: a news run that started exactly 24 hours
  ago is no breach; one second later it is.
* F6 and F7: the publishing doc lists both policy statuses; the health page says a
  breached source *ran*, and the stylesheet has rows for both statuses.
"""

from __future__ import annotations

import importlib
import json
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import tell.db
from tell import cli as tell_cli
from tell.ops import deadman, effects, source_eligibility
from tell.signals import cli as signals_cli
from tests.ops.helpers import add_run

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
WEBHOOK = "https://hooks.slack.invalid/services/T0/B0/S5"


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
    assert effects.EXTERNAL_EFFECTS_ENABLED is False


# --- F1: score subcommands ----------------------------------------------------------------


class _Tripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def hook(self, label: str) -> Any:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(label)
            msg = f"tripwire: {label} reached while the effect gate is closed"
            raise AssertionError(msg)

        return record


@pytest.fixture
def score_tripwire(monkeypatch: pytest.MonkeyPatch) -> _Tripwire:
    wire = _Tripwire()
    monkeypatch.setattr(signals_cli, "session_scope", wire.hook("signals.session_scope"))
    monkeypatch.setattr(signals_cli, "get_profile", wire.hook("signals.get_profile"))
    monkeypatch.setattr(signals_cli, "score_profile", wire.hook("score_profile"))
    monkeypatch.setattr(signals_cli, "score_all", wire.hook("score_all"))
    monkeypatch.setattr(tell_cli, "session_scope", wire.hook("tell.cli.session_scope"))
    monkeypatch.setattr(Engine, "connect", wire.hook("Engine.connect"))
    monkeypatch.setattr(httpx.Client, "send", wire.hook("httpx.Client.send"))
    return wire


SCORE_ARGVS = [
    ["score", "run"],
    ["score", "run", "--profile", "default", "--as-of", "2026-10-06"],
    ["score", "all"],
    ["score", "all", "--as-of", "2026-10-06"],
    ["score", "--all-profiles", "all"],
    ["score", "--all-profiles", "run"],
    ["score", "--all-profiles"],
]


@pytest.mark.parametrize("argv", SCORE_ARGVS, ids=" ".join)
def test_score_paths_refuse_before_any_session_while_the_gate_is_closed(
    score_tripwire: _Tripwire, argv: list[str]
) -> None:
    result = CliRunner().invoke(tell_cli.app, argv)
    assert result.exit_code == effects.EFFECTS_BLOCKED_EXIT_CODE == 3, result.output
    assert "external effects are not enabled" in result.output
    assert score_tripwire.calls == []


@pytest.mark.parametrize(
    "argv",
    [["score", "--help"], ["score", "run", "--help"], ["score", "all", "--help"], ["score"]],
    ids=" ".join,
)
def test_score_help_still_works_while_the_gate_is_closed(
    score_tripwire: _Tripwire, argv: list[str]
) -> None:
    result = CliRunner().invoke(tell_cli.app, argv)
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    assert score_tripwire.calls == []


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    return engine


@pytest.mark.db
@pytest.mark.usefixtures("external_effects", "bound")
@pytest.mark.parametrize(
    ("argv", "key"),
    [
        (["score", "all", "--as-of", "2026-10-06"], "profiles"),
        (["score", "run", "--profile", "default", "--as-of", "2026-10-06"], "companies"),
        (["score", "--all-profiles", "all", "--as-of", "2026-10-06"], "profiles"),
    ],
    ids=["all", "run", "all-profiles all"],
)
def test_score_paths_run_with_the_gate_open(argv: list[str], key: str) -> None:
    result = CliRunner().invoke(tell_cli.app, argv)
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout.strip().splitlines()[-1])
    assert key in record
    assert record["as_of"] == "2026-10-06"


# --- F2: Slack library backstop -----------------------------------------------------------


class _NoSession:
    """Any attribute access means the function touched the database."""

    def __getattr__(self, name: str) -> Any:
        msg = f"database touched ({name}) while the effect gate is closed"
        raise AssertionError(msg)


def test_post_blocks_refuses_before_any_request_while_the_gate_is_closed() -> None:
    from tell.publish.slack import post_blocks  # noqa: PLC0415

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(WEBHOOK).respond(200)
        with pytest.raises(effects.ExternalEffectsBlockedError, match="Slack webhook"):
            post_blocks(WEBHOOK, "text", [], sleep=lambda _: None)
    assert route.call_count == 0


def test_send_digests_and_nudges_refuse_before_reading_while_the_gate_is_closed() -> None:
    from tell.publish.nudge import send_nudges  # noqa: PLC0415
    from tell.publish.slack import send_digests  # noqa: PLC0415

    session: Any = _NoSession()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(WEBHOOK).respond(200)
        with pytest.raises(effects.ExternalEffectsBlockedError, match="Slack digest"):
            send_digests(session, now=NOW, webhook_url=WEBHOOK, force=True)
        with pytest.raises(effects.ExternalEffectsBlockedError, match="Slack nudges"):
            send_nudges(session, now=NOW, private_webhooks={"s": WEBHOOK})
    assert route.call_count == 0


@pytest.mark.usefixtures("external_effects")
def test_post_blocks_sends_with_the_gate_open() -> None:
    from tell.publish.slack import post_blocks  # noqa: PLC0415

    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(WEBHOOK).respond(200)
        assert post_blocks(WEBHOOK, "text", [], sleep=lambda _: None) == 200
    assert route.call_count == 1


# --- F3: no module-wide external_effects --------------------------------------------------


def test_no_test_module_opens_the_effect_gate_module_wide() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in sorted((REPO / "tests").rglob("test_*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("pytestmark") and "external_effects" in line
    ]
    assert offenders == []


def test_effect_gate_refusal_tests_run_with_the_gate_closed() -> None:
    module = importlib.import_module("tests.ops.test_external_effects_gate")
    marks = getattr(module, "pytestmark", [])
    marks = marks if isinstance(marks, list) else [marks]
    assert not any("external_effects" in (getattr(m, "args", ()) or ()) for m in marks)


# --- F4: the documented policy_breach window ----------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    ("started_ago", "status"),
    [
        (timedelta(hours=24), "skipped_by_policy"),
        (timedelta(hours=24) - timedelta(seconds=1), "policy_breach"),
        (timedelta(hours=25), "skipped_by_policy"),
        (timedelta(hours=1), "policy_breach"),
    ],
    ids=["exactly-24h", "24h-minus-1s", "25h", "1h"],
)
def test_policy_breach_window_boundary(
    db_session: Session, started_ago: timedelta, status: str
) -> None:
    add_run(db_session, "news", NOW - started_ago, minutes=0)
    assert deadman.policy_statuses(db_session, NOW, ["news"]) == {"news": status}


def test_breach_window_is_documented() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    runbook = (REPO / "docs" / "deploy" / "github-actions.md").read_text(encoding="utf-8")
    for text in (changelog, runbook):
        assert "up to 24 hours" in text
        assert "policy_breach" in text


# --- F6 / F7: docs and health page --------------------------------------------------------


def test_publishing_doc_lists_both_policy_statuses() -> None:
    text = (REPO / "docs" / "methods" / "publishing.md").read_text(encoding="utf-8")
    assert "`skipped_by_policy`" in text
    assert "`policy_breach`" in text


def _render_health(sources: list[dict[str, Any]], stale: list[str]) -> str:
    from tell.publish.site import environment, page_context  # noqa: PLC0415

    h = {
        "generated_at": "2026-10-20T20:30:00+00:00",
        "stale_sources": stale,
        "freshness_p95_h": None,
        "sources": sources,
    }
    return (
        environment()
        .get_template("health.html")
        .render(**page_context(date(2026, 10, 20), None, "2026-W42", ""), h=h)
    )


def _row(source: str, status: str, *, stale: bool) -> dict[str, Any]:
    return {
        "source": source,
        "kind": "collector",
        "last_ok_at": None,
        "success_rate_7d": None,
        "fetched_24h": 0,
        "inserted_24h": 0,
        "freshness_p95_h": None,
        "stale": stale,
        "status": status,
    }


def test_health_banner_says_a_breached_source_ran() -> None:
    page = _render_health([_row("news", "policy_breach", stale=True)], ["news"])
    assert "Policy breach: news ran in the last 24 hours" in page
    assert "no successful run" not in page  # a breach is not described as silence
    both = _render_health(
        [_row("news", "policy_breach", stale=True), _row("lever", "stale", stale=True)],
        ["lever", "news"],
    )
    assert "Policy breach: news ran" in both
    assert "Stale: lever (no successful run in 24 hours)." in both
    skipped = _render_health([_row("news", "skipped_by_policy", stale=False)], [])
    assert 'class="skipped_by_policy"' in skipped
    assert "skipped by policy" in skipped


def test_stylesheet_has_rows_for_both_policy_statuses() -> None:
    css = (REPO / "tell" / "publish" / "templates" / "style.css").read_text(encoding="utf-8")
    assert "tr.policy_breach td { color: var(--bad);" in css
    assert "tr.skipped_by_policy td { color: var(--muted);" in css
