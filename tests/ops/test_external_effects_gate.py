"""The external-effect gate (``tell/ops/effects.py``): refusal first, zero side effects.

Synthetic and local only. Every refused command is run under a tripwire that fails the
test on any database session or connection, settings or token read, HubSpot client,
``httpx`` send, score/prune work, DNS lookup or non-Unix socket connect; the tripwire is
shown to be live by control runs with the gate open. The gate module is imported inside
each test so that, on a tree without it, every test fails on behaviour, not collection.
"""

from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine
from typer.testing import CliRunner, Result

import tell.db
from tell import cli as tell_cli
from tell.config import get_settings
from tell.crm import cli as crm_cli
from tell.crm import hubspot
from tell.db import SessionLocal
from tell.models import Company, Score
from tell.ops import prune, source_eligibility
from tell.publish import cli as publish_cli

GATE_EXIT = 3
GATE_TEXT = "external effects are not enabled"
TOKEN = "pat-na1-placeholder-token-value"  # a placeholder, not a credential
DOMAIN = "gate.example"
HUBSPOT_BASE = "https://hubspot.invalid"

runner = CliRunner()


def _effects() -> Any:
    return importlib.import_module("tell.ops.effects")


# --- constants: everything off, nothing configurable -----------------------------------


def test_production_constants_are_all_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert source_eligibility.WIRED is False
    assert source_eligibility.ELIGIBILITY_CUTOVER is None
    assert source_eligibility.PERMITS == ()
    effects = _effects()
    assert effects.EXTERNAL_EFFECTS_ENABLED is False
    assert effects.EFFECTS_BLOCKED_EXIT_CODE == GATE_EXIT
    for name in (
        "EXTERNAL_EFFECTS_ENABLED",
        "TELL_EXTERNAL_EFFECTS",
        "TELL_EXTERNAL_EFFECTS_ENABLED",
        "TELL_EFFECTS",
    ):
        monkeypatch.setenv(name, "true")
    get_settings.cache_clear()
    assert effects.effects_blocked()
    with pytest.raises(effects.ExternalEffectsBlockedError, match=GATE_TEXT):
        effects.ensure_effects_enabled("anything")
    get_settings.cache_clear()


def test_only_exactly_true_opens_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    effects = _effects()
    for value in (1, "true", "True", object(), None):
        monkeypatch.setattr(effects, "EXTERNAL_EFFECTS_ENABLED", value)
        assert effects.effects_blocked(), value
    monkeypatch.setattr(effects, "EXTERNAL_EFFECTS_ENABLED", True)
    assert not effects.effects_blocked()


# --- tripwire ---------------------------------------------------------------------------


class _Tripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.network: list[str] = []

    def hook(self, label: str) -> Any:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(label)
            msg = f"tripwire: {label} reached"
            raise AssertionError(msg)

        return record


@pytest.fixture
def tripwire(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tripwire]:
    wire = _Tripwire()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(real: Any) -> Any:
        def connect(sock: socket.socket, address: Any) -> Any:
            if sock.family == socket.AF_UNIX:
                return real(sock, address)
            wire.network.append(f"connect {address!r}")
            msg = f"socket guard: outbound connect to {address!r}"
            raise OSError(msg)

        return connect

    def resolve(host: Any, *_args: Any, **_kwargs: Any) -> Any:
        wire.network.append(f"getaddrinfo {host!r}")
        msg = f"socket guard: DNS lookup of {host!r}"
        raise OSError(msg)

    monkeypatch.setattr(socket.socket, "connect", guard(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(real_connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    for module, name in (
        (tell_cli, "session_scope"),
        (tell_cli, "emit_velocity_events"),
        (tell_cli, "build_signals"),
        (tell_cli, "score_all"),
        (tell_cli, "start_run"),
        (publish_cli, "session_scope"),
        (publish_cli, "get_settings"),
        (publish_cli, "load_private_webhooks"),
        (crm_cli, "session_scope"),
        (crm_cli, "get_settings"),
        (crm_cli, "load_receipt"),
        (crm_cli, "push_company"),
        (hubspot, "make_client"),
        (prune, "prune"),
    ):
        monkeypatch.setattr(module, name, wire.hook(f"{module.__name__}.{name}"))
    monkeypatch.setattr(hubspot.HubSpotClient, "__init__", wire.hook("HubSpotClient.__init__"))
    monkeypatch.setattr(Engine, "connect", wire.hook("Engine.connect"))
    monkeypatch.setattr(httpx.Client, "send", wire.hook("httpx.Client.send"))
    monkeypatch.setattr(httpx.AsyncClient, "send", wire.hook("httpx.AsyncClient.send"))
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", TOKEN)
    monkeypatch.setenv("HUBSPOT_API_BASE", HUBSPOT_BASE)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/services/T0/B0/GATE")
    yield wire
    get_settings.cache_clear()


def _refused(argv: list[str]) -> Result:
    result = runner.invoke(tell_cli.app, argv)
    assert result.exit_code == GATE_EXIT, (argv, result.output, result.exception)
    assert GATE_TEXT in result.output, (argv, result.output)
    assert "error: `tell " in result.output
    assert "refused before any database, file or network access" in result.output
    assert TOKEN not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), argv
    return result


def _publish_commands(out: Path) -> list[list[str]]:
    o = str(out)
    return [
        ["publish", "--out", o],
        ["publish", "data", "--out", o],
        ["publish", "site", "--out", o],
        ["publish", "csv", "--watchlist", "x", "--out", str(out / "x.csv")],
        ["publish", "weekly", "--out", o],
        ["publish", "health", "--out", o],
        ["publish", "notify", "--slack"],
        ["notify", "--slack"],
        ["notify", "--slack", "--force"],
        ["weekly", "--out", o],
        ["prune", "--out", o],
    ]


CRM_COMMANDS: list[list[str]] = [
    ["crm", "hubspot", "check"],
    ["crm", "hubspot", "setup"],
    ["crm", "hubspot", "push", DOMAIN],
    ["crm", "hubspot", "push", DOMAIN, "--task", "--owner-id", "7", "--force"],
    ["crm", "hubspot", "push", DOMAIN, "--profile", "nope"],  # refused before validation
]


# --- F2: the scheduled pipeline's output steps ----------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["score", "--all-profiles"],
        ["score", "--all-profiles", "--as-of", "2026-11-20"],
        ["score", "--all-profiles", "--as-of", "not-a-date"],  # refused before parsing
        ["--manual", "score", "--all-profiles"],
    ],
)
def test_score_all_profiles_is_refused_before_any_side_effect(
    tripwire: _Tripwire, argv: list[str]
) -> None:
    _refused(argv)
    assert tripwire.calls == []
    assert tripwire.network == []


def test_publish_prune_notify_are_refused_by_the_gate_even_once_wired(
    tripwire: _Tripwire, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WIRED flipped on (a future reviewed change): the effect gate still refuses, first.
    monkeypatch.setattr(source_eligibility, "WIRED", True)
    out = tmp_path / "tell-data"
    out.mkdir()
    for argv in _publish_commands(out):
        _refused(argv)
    assert tripwire.calls == []
    assert tripwire.network == []
    assert list(out.iterdir()) == []


def test_wired_refusal_still_comes_first_with_exit_2(tripwire: _Tripwire, tmp_path: Path) -> None:
    for argv in _publish_commands(tmp_path):
        result = runner.invoke(tell_cli.app, argv)
        assert result.exit_code == publish_cli.BLOCKED_EXIT_CODE == 2, argv
        assert "source eligibility is not wired" in result.output
    assert tripwire.calls == []
    assert tripwire.network == []


# --- F4: tell crm hubspot -----------------------------------------------------------------


@pytest.mark.parametrize("argv", CRM_COMMANDS)
def test_crm_hubspot_commands_are_refused_before_settings_db_or_http(
    tripwire: _Tripwire, argv: list[str]
) -> None:
    _refused(argv)
    assert tripwire.calls == []
    assert tripwire.network == []


def test_hubspot_client_cannot_exist_while_the_gate_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects = _effects()
    made: list[str] = []
    monkeypatch.setattr(hubspot, "make_client", lambda *a, **k: made.append("make_client"))
    sends: list[str] = []
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: sends.append("send"))
    with pytest.raises(effects.ExternalEffectsBlockedError, match="the HubSpot client"):
        hubspot.HubSpotClient(TOKEN, base_url=HUBSPOT_BASE, sleep=lambda _s: None)
    transport = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(effects.ExternalEffectsBlockedError):
        hubspot.HubSpotClient(TOKEN, base_url=HUBSPOT_BASE, client=transport)
    transport.close()
    assert made == []
    assert sends == []


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    monkeypatch.setenv("HUBSPOT_API_BASE", HUBSPOT_BASE)
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield engine
    get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.usefixtures("receipt_cutover")
def test_push_dry_run_stays_available_and_sends_nothing(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SessionLocal(bind=bound) as session:
        company = Company(domain=DOMAIN, name="Gate Co")
        session.add(company)
        session.flush()
        session.add(
            Score(
                company_id=company.id,
                icp_profile="default",
                as_of=date(2026, 11, 20),
                score=Decimal("12.0"),
                contributions=[
                    {"signal_type": "funding", "contribution": 12.0, "url": "https://r.example/1"}
                ],
            )
        )
        session.commit()
    made: list[str] = []
    monkeypatch.setattr(hubspot.HubSpotClient, "__init__", lambda *a, **k: made.append("client"))
    sends: list[str] = []
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: sends.append("send"))
    result = runner.invoke(tell_cli.app, ["crm", "hubspot", "push", DOMAIN, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert '"action": "dry_run"' in result.stdout
    assert TOKEN not in result.output
    assert made == []
    assert sends == []


# --- controls: the tripwire is live (the refusals above are not vacuous) ----------------


def test_control_open_gate_score_reaches_the_tripwire(
    tripwire: _Tripwire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_effects(), "EXTERNAL_EFFECTS_ENABLED", True)
    result = runner.invoke(tell_cli.app, ["score", "--all-profiles", "--as-of", "2026-11-20"])
    assert result.exit_code != GATE_EXIT
    assert tripwire.calls  # it went on to the session / run row


@pytest.mark.parametrize("argv", CRM_COMMANDS[:3])
def test_control_open_gate_crm_reaches_the_tripwire(
    tripwire: _Tripwire, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(_effects(), "EXTERNAL_EFFECTS_ENABLED", True)
    result = runner.invoke(tell_cli.app, argv)
    assert result.exit_code != GATE_EXIT
    assert tripwire.calls  # settings, database or the HubSpot client were reached


def test_control_open_gate_publish_reaches_the_tripwire(
    tripwire: _Tripwire, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_effects(), "EXTERNAL_EFFECTS_ENABLED", True)
    monkeypatch.setattr(source_eligibility, "WIRED", True)
    result = runner.invoke(tell_cli.app, ["publish", "data", "--out", str(tmp_path)])
    assert result.exit_code != GATE_EXIT
    assert tripwire.calls


def test_the_socket_guard_is_live(tripwire: _Tripwire) -> None:
    with pytest.raises(OSError, match="socket guard"):
        socket.getaddrinfo("example.invalid", 443)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="socket guard"):
            probe.connect(("192.0.2.1", 443))  # TEST-NET-1, never routed
    finally:
        probe.close()
    assert len(tripwire.network) == 2
    tripwire.network.clear()


def test_refusal_is_not_a_later_failure_no_runs_row_is_started(
    tripwire: _Tripwire, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the gate a scheduled run reached score's runs row and only stopped when
    # publish failed. Now start_run, the session and the scorer are never reached.
    started: list[datetime] = []
    monkeypatch.setattr(tell_cli, "start_run", lambda *_a, **k: started.append(k.get("now")))
    _refused(["score", "--all-profiles"])
    assert started == []
    assert tripwire.calls == []
