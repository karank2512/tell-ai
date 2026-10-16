"""``tell publish`` sub-app: every command against the temporary database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.config import get_settings
from tell.db import SessionLocal
from tell.models import Run
from tell.publish import cli
from tests.publish import scenario as sc

pytestmark = pytest.mark.db

runner = CliRunner()
WEBHOOK = "https://hooks.slack.invalid/services/T1/B1/Z1"


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Point the sub-app at the temporary database, with the scenario committed."""

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
    with freeze_time(sc.NOW):
        with SessionLocal(bind=engine) as session:
            sc.build(session)
            session.commit()
        yield engine


def _ok(args: list[str]) -> dict[str, Any]:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    record: dict[str, Any] = json.loads(result.output)
    return record


def test_publish_out_runs_data_then_site(bound: Engine, tmp_path: Path) -> None:
    record = _ok(["--out", str(tmp_path)])
    assert record["companies"] == 4
    assert record["weekly"] == "2026-W40"
    assert record["watchlists"] == ["pilot"]
    assert record["events_written"] > 0  # news observed 10-05, crossings 10-06
    assert (tmp_path / "site" / "index.html").is_file()
    with SessionLocal(bind=bound) as session:
        runs = session.scalars(select(Run).where(Run.source == cli.PUBLISH_SOURCE)).all()
        assert sorted((r.status, (r.notes or {}).get("step")) for r in runs) == [
            ("ok", "data"),
            ("ok", "site"),
        ]


def test_each_command(bound: Engine, tmp_path: Path) -> None:
    site = _ok(["site", "--out", str(tmp_path), "--as-of", sc.AS_OF.isoformat()])
    assert site["validated"] == 7
    data = _ok(["data", "--out", str(tmp_path), "--day", "2026-10-05"])
    assert data["events_written"] > 0
    assert list((tmp_path / "events" / "2026" / "10").glob("*.jsonl"))
    csv_path = tmp_path / "pilot.csv"
    record = _ok(["csv", "--watchlist", "Pilot list", "--out", str(csv_path)])
    assert record["rows"] == 3
    assert csv_path.read_text().startswith("domain,name,score,heat,")
    weekly = _ok(["weekly", "--week", "2026-W40", "--out", str(tmp_path)])
    assert weekly["week"] == "2026-W40"
    assert weekly["top_hiring"] == 1
    assert _ok(["weekly", "--out", str(tmp_path)])["week"] == "2026-W40"  # current edition
    health = _ok(["health", "--out", str(tmp_path)])
    assert health["stale_sources"] == ["edgar", "greenhouse"]
    assert (tmp_path / "site" / "health.html").is_file()


def test_bad_input_is_rejected(bound: Engine, tmp_path: Path) -> None:
    for args, message in (
        (["site", "--out", str(tmp_path), "--as-of", "yesterday"], "ISO date"),
        (["site", "--out", str(tmp_path), "--profile", "nope"], "no ICP profile"),
        (["csv", "--watchlist", "Missing", "--out", str(tmp_path / "x.csv")], "no watchlist"),
        (["weekly", "--week", "W40", "--out", str(tmp_path)], "ISO week"),
        (["notify"], "pass --slack"),
    ):
        result = runner.invoke(cli.app, args)
        assert result.exit_code != 0, args
        assert message in result.output, (args, result.output)
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_notify_slack(bound: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An explicit empty private dir: the default ``../tell-private`` may be a real checkout
    # with real webhooks on a developer's machine, and the test must not read it.
    notify = ["notify", "--slack", "--private-dir", str(tmp_path / "private")]
    assert _ok(notify)["skipped"] == "no webhook"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr("tell.publish.slack.time.sleep", lambda _: None)
    get_settings.cache_clear()
    with respx.mock as mock:
        mock.post(WEBHOOK).respond(200, text="ok")
        # 12:00 UTC is 05:00 in Los Angeles: before the digest hour, so only --force sends.
        assert _ok(notify)["skipped"] == "not the digest run"
        assert _ok([*notify, "--force"])["sent"] == 2
    with respx.mock as mock:
        mock.post(WEBHOOK).respond(500)
        result = runner.invoke(cli.app, [*notify, "--force"])
        assert result.exit_code == 1
        assert json.loads(result.output)["errors"] == 2
