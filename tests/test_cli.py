"""The ``tell`` CLI (PLAN Phase 9): every top-level command against the temporary database.

The database-backed tests point :func:`tell.db.session_scope` at the test database by
replacing ``tell.db.get_engine``, so the mounted sub-apps (resolve, score, publish,
discover) see it too. HTTP is mocked with respx; clocks are frozen where a rule depends
on them (dead-man, digest hour).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from typer.testing import CliRunner, Result

import tell.db
from tell import __version__
from tell import cli as tell_cli
from tell.backtest.cli import REPORTS
from tell.cli import MIN_POLL_GAP, app
from tell.collectors import base as collectors_base
from tell.collectors import edgar, github_org, greenhouse, news
from tell.config import get_settings
from tell.db import SessionLocal
from tell.models import AtsBoard, Company, EventRow, PostingObservation, Run, Watchlist
from tell.ops.deadman import SCHEDULED_SOURCES
from tell.publish.data_repo import events_path
from tests.ops.helpers import (
    add_board,
    add_company,
    add_event,
    add_posting,
    add_run,
    add_signal,
)
from tests.publish import scenario as sc

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GREENHOUSE_NORMAL = FIXTURES / "greenhouse" / "normal.json"
YC_SAMPLE = FIXTURES / "yc" / "sample.json"
BOARD = "cli-board"
WEBHOOK = "https://hooks.slack.invalid/services/T1/B1/Z1"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _invoke(args: list[str], code: int = 0) -> Result:
    result = runner.invoke(app, args)
    assert result.exit_code == code, (result.output, result.exception)
    return result


def _json(result: Result) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return record


def _runs(engine: Engine, source: str) -> list[Run]:
    with SessionLocal(bind=engine) as session:
        return list(
            session.scalars(select(Run).where(Run.source == source).order_by(Run.started_at))
        )


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Every ``session_scope()`` (CLI and mounted sub-apps) opens on the test database."""
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    return engine


# --- no database --------------------------------------------------------------------------


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"tell {__version__}"


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "version" in result.output


def test_console_script_module_runs() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "tell.cli", "version"], capture_output=True, text=True, check=True
    )
    assert proc.stdout.strip() == f"tell {__version__}"


COMMANDS: list[list[str]] = [
    [],
    ["ingest"],
    ["resolve"],
    ["resolve", "run"],
    ["resolve", "stats"],
    ["score"],
    ["score", "all"],
    ["score", "build-signals"],
    ["score", "feedback-report"],
    ["publish"],
    ["publish", "site"],
    ["notify"],
    ["weekly"],
    ["runlog"],
    ["deadman"],
    ["prune"],
    ["seed-yc"],
    ["discover"],
    ["discover", "batch"],
    ["discover", "reverify-guesses"],
    ["backtest"],
    ["backtest", "list"],
    ["api"],
    ["api", "create-key"],
    ["api", "revoke-key"],
    ["api", "usage"],
    ["crm"],
    ["crm", "hubspot"],
    ["crm", "hubspot", "push"],
    ["version"],
]


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: " ".join(p) or "tell")
def test_help_on_every_command(path: list[str]) -> None:
    result = _invoke([*path, "--help"])
    assert "Usage:" in result.output


def test_top_level_commands_match_the_spec() -> None:
    names = set(_invoke(["--help"]).output.split())
    expected = {
        "ingest",
        "discover",
        "resolve",
        "score",
        "publish",
        "notify",
        "weekly",
        "runlog",
        "deadman",
        "prune",
        "seed-yc",
        "backtest",
        "api",
        "version",
    }
    assert expected <= names


def test_backtest_list() -> None:
    result = _invoke(["backtest", "list"])
    assert result.stdout.split() == list(REPORTS)
    assert len(REPORTS) == 5


def test_score_without_options_prints_help() -> None:
    result = _invoke(["score"])
    assert "--all-profiles" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["ingest", "--source", "workday"],
        ["ingest", "--source", "greenhouse", "--limit", "0"],
        ["ingest", "--source", "edgar", "--board", "x"],
        ["ingest", "--source", "github", "--domain", "acme.example"],
        ["ingest", "--source", "edgar", "--limit", "3"],
        ["runlog"],
        ["runlog", "--week", "--source", "lever"],
        ["score", "--all-profiles", "--as-of", "yesterday"],
    ],
)
def test_usage_errors_exit_2(args: list[str]) -> None:
    _invoke(args, code=2)


# --- ingest -------------------------------------------------------------------------------


@pytest.fixture
def gh_board(bound: Engine) -> Engine:
    with SessionLocal(bind=bound) as session:
        company = add_company(session, "cli-fixture.example")
        add_board(session, company, BOARD)
        add_board(session, company, "cli-paused", status="paused")
        session.commit()
    return bound


def _greenhouse_route(mock: respx.MockRouter, identifier: str = BOARD) -> respx.Route:
    return mock.get(host="boards-api.greenhouse.io", path=f"/v1/boards/{identifier}/jobs")


def test_ingest_greenhouse_board(gh_board: Engine) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = _greenhouse_route(mock)
        route.respond(200, content=GREENHOUSE_NORMAL.read_bytes())
        first = _json(_invoke(["ingest", "--source", "greenhouse", "--board", BOARD]))
        assert route.call_count == 1
        assert route.calls[0].request.url.params["content"] == "true"

        assert first["source"] == "greenhouse"
        assert first["status"] == "ok"
        assert first["fetched"] == 5
        assert first["inserted"] > 0
        assert first["errors"] == 0
        assert set(first) >= {"run_id", "started_at", "finished_at", "skipped"}

        # Polled moments ago: not due again without --force.
        second = _json(_invoke(["ingest", "--source", "greenhouse"]))
        assert route.call_count == 1
        assert (second["fetched"], second["status"]) == (0, "ok")

        forced = _json(_invoke(["ingest", "--source", "greenhouse", "--force", "--limit", "5"]))
        assert route.call_count == 2
        assert forced["fetched"] == 5
        assert forced["inserted"] == 0  # idempotent rerun

    runs = _runs(gh_board, "greenhouse")
    assert [r.status for r in runs] == ["ok", "ok", "ok"]
    assert runs[1].notes == {
        "boards_due": 0,
        "boards_polled": 0,
        "boards_not_due": 1,
        "force": False,
    }
    with SessionLocal(bind=gh_board) as session:
        board = session.scalars(select(AtsBoard).where(AtsBoard.identifier == BOARD)).one()
        assert board.last_polled_at is not None
        events = session.scalar(select(func.count()).select_from(EventRow)) or 0
        assert events == first["inserted"]


def test_ingest_by_domain_and_due_gap(gh_board: Engine) -> None:
    stale = datetime.now(UTC) - MIN_POLL_GAP - HOUR
    with SessionLocal(bind=gh_board) as session:
        board = session.scalars(select(AtsBoard).where(AtsBoard.identifier == BOARD)).one()
        board.last_polled_at = stale
        session.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        route = _greenhouse_route(mock)
        route.respond(200, content=GREENHOUSE_NORMAL.read_bytes())
        line = _json(
            _invoke(["ingest", "--source", "greenhouse", "--domain", "WWW.CLI-Fixture.example"])
        )
        assert route.call_count == 1
        assert line["fetched"] == 5
        nobody = _json(_invoke(["ingest", "--source", "greenhouse", "--domain", "other.example"]))
        assert nobody["fetched"] == 0
        assert route.call_count == 1


def test_ingest_counts_invalid_jobs_as_run_errors(gh_board: Engine) -> None:
    doc = json.loads(GREENHOUSE_NORMAL.read_text(encoding="utf-8"))
    del doc["jobs"][0]["updated_at"]
    with respx.mock(assert_all_mocked=True) as mock:
        _greenhouse_route(mock).respond(200, json=doc)
        line = _json(_invoke(["ingest", "--source", "greenhouse", "--board", BOARD]))
    assert (line["status"], line["fetched"], line["errors"]) == ("ok", 5, 1)
    run = _runs(gh_board, "greenhouse")[0]
    assert run.errors == 1
    assert run.notes is not None
    assert run.notes["invalid_jobs"] == {BOARD: 1}
    assert run.notes["boards_polled"] == 1
    assert "failed_boards" not in run.notes


def test_ingest_board_failure_exits_1(gh_board: Engine) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        _greenhouse_route(mock).respond(400)
        result = _invoke(["ingest", "--source", "greenhouse", "--board", BOARD], code=1)
    line = _json(result)
    assert line["status"] == "failed"
    assert line["errors"] == 1
    run = _runs(gh_board, "greenhouse")[0]
    assert run.notes is not None
    assert list(run.notes["failed_boards"]) == [BOARD]
    assert "HTTPStatusError" in run.notes["failed_boards"][BOARD]


def test_ingest_unknown_or_paused_board_is_a_usage_error(gh_board: Engine) -> None:
    _invoke(["ingest", "--source", "greenhouse", "--board", "nope"], code=2)
    _invoke(["ingest", "--source", "greenhouse", "--board", "cli-paused"], code=2)
    assert _runs(gh_board, "greenhouse") == []


def test_ingest_vendor_without_boards_is_an_ok_empty_run(bound: Engine) -> None:
    line = _json(_invoke(["ingest", "--source", "lever"]))
    assert (line["source"], line["status"], line["fetched"]) == ("lever", "ok", 0)


def test_ingest_edgar(bound: Engine) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(host="www.sec.gov").respond(404)
        line = _json(_invoke(["ingest", "--source", "edgar"]))
        calls = route.call_count
    assert (line["source"], line["status"], line["fetched"]) == ("edgar", "ok", 0)
    run = _runs(bound, "edgar")[0]
    assert run.notes is not None
    # First run: today and the 3 days before it; the quarter listing 404s, so no index.
    assert len(run.notes["dates"]) == edgar.FIRST_RUN_DAYS_BACK + 1
    days = [datetime.fromisoformat(d).date() for d in run.notes["dates"]]
    assert calls == len({edgar.quarter_listing_url(d) for d in days})


def test_ingest_edgar_failure_is_recorded(bound: Engine) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(host="www.sec.gov").respond(403)
        result = _invoke(["ingest", "--source", "edgar"], code=1)
    assert _json(result)["status"] == "failed"
    assert "HTTPStatusError" in result.output
    run = _runs(bound, "edgar")[0]
    assert (run.status, run.errors) == ("failed", 1)
    assert run.notes is not None
    assert "HTTPStatusError" in run.notes["error"]


def test_ingest_news_and_github_with_nothing_to_poll(bound: Engine) -> None:
    with respx.mock(assert_all_mocked=True):
        news_line = _json(_invoke(["ingest", "--source", "news", "--limit", "5"]))
        github_line = _json(_invoke(["ingest", "--source", "github"]))
    assert (news_line["source"], news_line["status"], news_line["fetched"]) == ("news", "ok", 0)
    assert (github_line["source"], github_line["status"]) == ("github", "ok")


def test_ingest_news_domain(bound: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _invoke(["ingest", "--source", "news", "--domain", "missing.example"], code=2)
    with SessionLocal(bind=bound) as session:
        company_id = add_company(session, "newsy.example").id
        session.commit()
    seen: dict[str, Any] = {}
    real = news.ingest

    def spy(session: Any, **kwargs: Any) -> Run:
        seen.update(kwargs)
        return real(session, **{**kwargs, "company_ids": []})

    monkeypatch.setattr(news, "ingest", spy)
    _invoke(["ingest", "--source", "news", "--domain", "newsy.example"])
    assert seen["company_ids"] == [company_id]
    assert seen["max_companies"] == news.MAX_COMPANIES


@pytest.mark.parametrize("module", [news, github_org], ids=["news", "github"])
def test_ingest_collector_crash_records_a_failed_run(
    bound: Engine, monkeypatch: pytest.MonkeyPatch, module: Any
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Run:
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(module, "ingest", boom)
    result = _invoke(["ingest", "--source", module.SOURCE], code=1)
    assert _json(result)["status"] == "failed"
    run = _runs(bound, module.SOURCE)[0]
    assert run.status == "failed"
    assert run.notes == {"error": "ConnectError: unreachable"}


def _news_company(engine: Engine) -> None:
    with SessionLocal(bind=engine) as session:
        add_company(session, "blockedco.example")
        session.commit()


def _mock_news_sites(mock: respx.MockRouter) -> None:
    mock.get(url__regex=r"https://blockedco\.example/.*").mock(return_value=httpx.Response(404))


@pytest.mark.parametrize("status", [429, 503])
def test_ingest_news_block_fails_the_run_but_exits_0(
    bound: Engine, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A Google News block alone no longer turns the ingest job red (2026-10-14)."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _news_company(bound)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(status)
        )
        _mock_news_sites(mock)
        line = _json(_invoke(["ingest", "--source", "news", "--domain", "blockedco.example"]))
    assert line["status"] == "failed"  # the run row still says failed ...
    run = _runs(bound, "news")[-1]
    assert run.status == "failed"
    assert run.notes is not None
    assert run.notes["blocked"] is True
    assert run.notes["blocked_by"]["status"] == status
    # ... so the dead-man still alarms: news has no ok run in the last 24 hours.
    report = _json(_invoke(["deadman", "--source", "news"], code=1))
    assert report["silent"] == ["news"]
    assert report["sources"][0]["status"] == "stale"


def test_ingest_news_block_after_an_old_ok_run_still_alarms_the_deadman(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _news_company(bound)
    with SessionLocal(bind=bound) as session:
        add_run(session, "news", datetime.now(UTC) - 30 * HOUR)
        session.commit()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(503)
        )
        _mock_news_sites(mock)
        _invoke(["ingest", "--source", "news", "--domain", "blockedco.example"])
    assert _json(_invoke(["deadman", "--source", "news"], code=1))["silent"] == ["news"]


def test_ingest_news_contract_failure_still_exits_1(bound: Engine) -> None:
    _news_company(bound)
    broken = b"<rss><channel><item><title>BlockedCo raises</title></item></channel></rss>"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(200, content=broken)
        )
        _mock_news_sites(mock)
        line = _json(
            _invoke(["ingest", "--source", "news", "--domain", "blockedco.example"], code=1)
        )
    assert line["status"] == "failed"
    run = _runs(bound, "news")[-1]
    assert run.notes is not None
    assert run.notes["blocked"] is False
    assert "drift" in run.notes


def test_ingest_github_passes_the_token(bound: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-value")
    get_settings.cache_clear()
    seen: dict[str, Any] = {}

    def fake(session: Any, **kwargs: Any) -> Run:
        seen.update(kwargs)
        return add_run(session, "github", datetime.now(UTC) - HOUR)

    monkeypatch.setattr(github_org, "ingest", fake)
    _invoke(["ingest", "--source", "github"])
    assert seen["token"] == "test-token-value"


# --- resolve / score / publish / notify / weekly / discover --------------------------------


def test_resolve_without_command_backfills_and_runs(bound: Engine) -> None:
    now = datetime.now(UTC)
    with SessionLocal(bind=bound) as session:
        add_company(session, "resolvable.example")
        sc.replay(session, edgar.COLLECTOR, "normal.json", now - DAY)
        session.commit()
    line = _json(_invoke(["resolve"]))
    assert (line["source"], line["status"]) == ("resolve", "ok")
    assert line["fetched"] > 0
    assert line["notes"]["aliases_backfilled"] >= 1
    assert line["fetched"] == line["notes"]["scanned"]
    stats = _json(_invoke(["resolve", "stats"]))
    assert stats["events_unlinked"] + stats["events_linked"] == line["fetched"]
    assert [r.status for r in _runs(bound, "resolve")] == ["ok"]


@pytest.fixture
def _manual_cleared() -> Any:
    """Leave no process-wide ``--manual`` override behind for later tests."""
    yield
    collectors_base.set_manual_run(None)


@pytest.mark.usefixtures("_manual_cleared")
def test_manual_flag_marks_every_run_of_the_command(bound: Engine) -> None:
    """SPEC 13.4: ``tell --manual <cmd>`` writes runs.notes.manual = true (the anchor)."""
    line = _json(_invoke(["--manual", "resolve"]))
    assert line["notes"]["manual"] is True
    (run,) = _runs(bound, "resolve")
    assert run.notes is not None
    assert run.notes["manual"] is True
    assert run.status == "ok"
    # The override does not outlive the invocation: the next plain command is unmarked.
    _invoke(["resolve"])
    assert [(r.notes or {}).get("manual") for r in _runs(bound, "resolve")] == [True, None]


@pytest.mark.usefixtures("_manual_cleared")
def test_manual_env_marks_runs_and_survives_a_failure(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TELL_RUN_MANUAL=true`` (ingest.yml on workflow_dispatch) marks the run, failed or not."""
    monkeypatch.setenv("TELL_RUN_MANUAL", "true")
    get_settings.cache_clear()
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(host="www.sec.gov").respond(403)
        _invoke(["ingest", "--source", "edgar"], code=1)
    (run,) = _runs(bound, "edgar")
    assert run.status == "failed"
    assert run.notes is not None
    assert run.notes["manual"] is True
    assert "HTTPStatusError" in run.notes["error"]
    line = _json(_invoke(["--manual", "runlog", "--week"]))
    assert "weeks_unattended" in line  # a read-only command records no run


def test_default_runs_carry_no_manual_marker(bound: Engine) -> None:
    """A scheduled run (no flag, no variable) never resets 'weeks unattended'."""
    _invoke(["resolve"])
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(host="www.sec.gov").respond(404)
        _invoke(["ingest", "--source", "edgar"])
    for source in ("resolve", "edgar"):
        (run,) = _runs(bound, source)
        assert "manual" not in (run.notes or {}), source


def test_resolve_failure_is_recorded(bound: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> int:
        msg = "alias table locked"
        raise RuntimeError(msg)

    monkeypatch.setattr(tell_cli, "backfill_aliases", boom)
    result = _invoke(["resolve"], code=1)
    line = _json(result)
    assert line["status"] == "failed"
    assert line["notes"] == {"error": "RuntimeError: alias table locked"}


@pytest.fixture
def scenario(bound: Engine) -> Engine:
    with freeze_time(sc.NOW), SessionLocal(bind=bound) as session:
        sc.build(session)
        session.commit()
    return bound


def test_score_all_profiles(scenario: Engine) -> None:
    with freeze_time(sc.NOW):
        line = _json(_invoke(["score", "--all-profiles", "--as-of", sc.AS_OF.isoformat()]))
    assert (line["source"], line["status"]) == ("score", "ok")
    notes = line["notes"]
    assert notes["as_of"] == sc.AS_OF.isoformat()
    assert "default" in notes["profiles"]
    assert notes["profiles"]["default"]["companies"] > 0
    assert line["fetched"] == sum(p["companies"] for p in notes["profiles"].values())
    assert [r.status for r in _runs(scenario, "score")] == ["ok"]


def test_publish_out_writes_data_site_health_weekly(scenario: Engine, tmp_path: Path) -> None:
    with freeze_time(sc.NOW):
        record = _json(_invoke(["publish", "--out", str(tmp_path)]))
    assert record["companies"] > 0
    assert record["weekly"] == "2026-W40"
    site = tmp_path / "site"
    for rel in (
        "index.html",
        "health.html",
        "weekly/2026-W40.html",
        "api/v0/health.json",
        "api/v0/weekly/latest.json",
    ):
        assert (site / rel).is_file(), rel
    assert any((tmp_path / "events").rglob("*.jsonl"))


def test_weekly_alias(bound: Engine, tmp_path: Path) -> None:
    with freeze_time(sc.NOW):
        record = _json(_invoke(["weekly", "--week", "current", "--out", str(tmp_path)]))
    assert record["week"] == "2026-W40"
    assert (tmp_path / "site" / "weekly" / "2026-W40.html").is_file()


def test_notify_slack_at_the_digest_hour(bound: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    with SessionLocal(bind=bound) as session:
        session.add(Watchlist(owner="team-ops", name="Ops list"))
        session.commit()
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    get_settings.cache_clear()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(WEBHOOK).respond(200, text="ok")
        with freeze_time("2026-10-06T12:17:00Z"):  # 05:17 in Los Angeles: too early
            early = _json(_invoke(["notify", "--slack"]))
        assert early["skipped"] == "not the digest run"
        assert route.call_count == 0
        with freeze_time("2026-10-06T14:17:00Z"):  # 07:17 in Los Angeles: the digest run
            sent = _json(_invoke(["notify", "--slack"]))
        assert sent["sent"] == 1
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert body["text"].startswith("tell · Ops list · 2026-10-06")
        with freeze_time("2026-10-06T20:17:00Z"):  # already sent today
            again = _json(_invoke(["notify", "--slack"]))
        assert again["skipped"] == "not the digest run"
        assert route.call_count == 1


def test_seed_yc_alias_and_discover_mount(bound: Engine) -> None:
    stats = _json(_invoke(["seed-yc", "--file", str(YC_SAMPLE)]))
    assert stats
    with SessionLocal(bind=bound) as session:
        assert (session.scalar(select(func.count()).select_from(Company)) or 0) > 0
    _invoke(["discover", "seed-yc"], code=2)  # neither --file nor --fetch


# --- run health ---------------------------------------------------------------------------

FROZEN = datetime(2026, 10, 6, 20, 40, tzinfo=UTC)


def _healthy(engine: Engine, *, skip: str | None = None) -> None:
    with SessionLocal(bind=engine) as session:
        for source in SCHEDULED_SOURCES:
            if source != skip:
                add_run(session, source, FROZEN - 2 * HOUR)
        session.commit()


def test_deadman_green(bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(bound)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with freeze_time(FROZEN):
        report = _json(_invoke(["deadman"]))
    assert report["ok"] is True
    assert report["silent"] == []
    assert "dead-man: green" in summary.read_text(encoding="utf-8")


def test_deadman_red(bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(bound, skip="news")
    with SessionLocal(bind=bound) as session:
        add_run(session, "lever", FROZEN - HOUR, "failed")
        add_run(session, "greenhouse", FROZEN - 40 * HOUR)
        session.commit()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with freeze_time(FROZEN + 23 * HOUR):  # the 2h-old runs are now 25h old
        result = _invoke(["deadman"], code=1)
    report = _json(result)
    assert report["silent"] == list(SCHEDULED_SOURCES)
    statuses = {s["source"]: s["status"] for s in report["sources"]}
    assert statuses["news"] == "never_ran"
    assert statuses["lever"] == "stale"
    assert "dead-man: silent sources: greenhouse" in result.output
    text = summary.read_text(encoding="utf-8")
    assert "dead-man: red" in text
    assert "`news`: never_ran" in text


def test_deadman_named_sources_without_summary(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _healthy(bound)
    with freeze_time(FROZEN):
        ok = _json(_invoke(["deadman", "--source", "edgar", "--source", "news"]))
        assert [s["source"] for s in ok["sources"]] == ["edgar", "news"]
        red = _json(_invoke(["deadman", "--source", "github"], code=1))
    assert red["silent"] == ["github"]


def test_runlog_source(bound: Engine) -> None:
    _invoke(["runlog", "--source", "greenhouse"], code=1)
    with SessionLocal(bind=bound) as session:
        add_run(session, "greenhouse", FROZEN - 7 * HOUR)
        latest = add_run(session, "greenhouse", FROZEN - HOUR, fetched=42).run_id
        session.commit()
    line = _json(_invoke(["runlog", "--source", "greenhouse"]))
    assert line["run_id"] == latest
    assert line["fetched"] == 42
    assert line["status"] == "ok"


def test_runlog_week(bound: Engine) -> None:
    with SessionLocal(bind=bound) as session:
        t = FROZEN - 14 * DAY
        while t < FROZEN:
            for source in SCHEDULED_SOURCES:
                add_run(session, source, t)
            t += 6 * HOUR
        add_run(session, "news", FROZEN - 3 * HOUR, "failed")
        company = add_company(session, "monitored.example")
        add_company(session, "filer.example", cik="0000000002")
        board = add_board(session, company, "monitored")
        add_posting(session, board, "1", FROZEN - DAY)
        session.commit()
    with freeze_time(FROZEN):
        line = _json(_invoke(["runlog", "--week"]))
    assert line["weeks_unattended"] == 2.0
    assert line["companies_monitored"] == 2
    assert line["postings_tracked"] == 1
    assert line["period_days"] == 7
    assert line["success_rate_by_source"]["edgar"] == 1.0
    assert line["success_rate_by_source"]["news"] < 1.0
    assert 0.9 < line["success_rate"] < 1.0
    assert "freshness_p95_h" in line
    assert line["summary"].startswith("2 weeks unattended")


# --- prune ----------------------------------------------------------------------------------


def _prunable(engine: Engine) -> datetime:
    now = datetime.now(UTC)
    with SessionLocal(bind=engine) as session:
        company = add_company(session, "prune-cli.example")
        add_event(session, "cli-old", now - 200 * DAY, company=company)
        cited = add_event(session, "cli-old-cited", now - 200 * DAY, company=company)
        add_signal(session, cited, company)
        add_event(session, "cli-recent", now - DAY, company=company)
        board = add_board(session, company, "prune-cli")
        posting = add_posting(session, board, "1", now - 100 * DAY)
        session.add_all(
            PostingObservation(posting_id=posting.id, observed_at=now - age * DAY, run_id="r")
            for age in (100, 5)
        )
        session.commit()
    return now


def test_prune_archives_then_deletes(bound: Engine, tmp_path: Path) -> None:
    now = _prunable(bound)
    line = _json(_invoke(["prune", "--out", str(tmp_path)]))
    assert (line["source"], line["status"]) == ("prune", "ok")
    notes = line["notes"]
    assert notes["events_archived"] == 2
    assert notes["events_deleted"] == 1
    assert notes["events_kept"] == 1
    assert notes["observations_deleted"] == 1
    archive = events_path(tmp_path, (now - 200 * DAY).date()).read_text(encoding="utf-8")
    assert "cli-old" in archive
    assert "cli-old-cited" in archive
    with SessionLocal(bind=bound) as session:
        ids = sorted(session.scalars(select(EventRow.event_id)))
        assert ids == ["cli-old-cited", "cli-recent"]
        assert session.scalar(select(func.count()).select_from(PostingObservation)) == 1


def test_prune_archive_failure_deletes_nothing(bound: Engine, tmp_path: Path) -> None:
    now = _prunable(bound)
    target = events_path(tmp_path, (now - 200 * DAY).date())
    target.parent.mkdir(parents=True)
    target.write_text("not json\n", encoding="utf-8")
    result = _invoke(["prune", "--out", str(tmp_path)], code=1)
    line = _json(result)
    assert line["status"] == "failed"
    assert "not JSON" in line["notes"]["error"]
    with SessionLocal(bind=bound) as session:
        assert (session.scalar(select(func.count()).select_from(EventRow)) or 0) == 3
        assert session.scalar(select(func.count()).select_from(PostingObservation)) == 2
    assert [r.status for r in _runs(bound, "prune")] == ["failed"]


def test_greenhouse_module_is_the_greenhouse_ingest() -> None:
    assert tell_cli.ATS_INGEST["greenhouse"] is greenhouse.ingest_board
