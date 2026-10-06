"""``tell backtest``: every command against the temporary database, writing to tmp dirs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.backtest import cli
from tell.backtest.common import INDEX_END, INDEX_START, METHODS_DIR
from tell.db import SessionLocal
from tests.backtest.helpers import DAY, NOW, board, company, formd, news, posting, run
from tests.backtest.test_dogfood import build_world

pytestmark = pytest.mark.db

runner = CliRunner()


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Point the sub-app at the temporary database, with a small world committed."""

    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    with SessionLocal(bind=engine) as session:
        run(session, "greenhouse", NOW - 20 * DAY)
        owner = company(session, "alpha.example")
        gh = board(session, owner, "alpha")
        posting(session, gh, "p1", first_seen=NOW - 5 * DAY)
        formd(session, owner, NOW.date() - 10 * DAY)
        news(session, owner, NOW - 8 * DAY)
        build_world(session)
        session.commit()
    with freeze_time(NOW):
        yield engine


def _lines(args: list[str]) -> list[dict[str, Any]]:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, (result.output, result.exception)
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_each_public_command(bound: Engine, tmp_path: Path) -> None:
    for name in ("speed-vs-press", "noise", "ops"):
        (line,) = _lines([name, "--out", str(tmp_path), "--refresh"])
        assert line["report"] == name
        assert line["path"] == str(tmp_path / f"{name}-2026-10-06.md")
        assert line["latest"] == str(tmp_path / f"{name}-latest.md")
        assert line["quotable"] is False
    (line,) = _lines(
        [
            "velocity-lead",
            "--out",
            str(tmp_path),
            "--as-of",
            "2026-10-01",
            "--wayback-cache",
            str(tmp_path / "wb"),
        ]
    )
    assert line["path"] == str(tmp_path / "velocity-lead-2026-10-01.md")
    text = (tmp_path / "velocity-lead-latest.md").read_text(encoding="utf-8")
    assert "Not yet runnable: 15 days of collection < 60." in text  # evaluated 2026-10-01 23:59:59


def test_refresh_writes_every_public_report_and_the_index(bound: Engine, tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(f"{INDEX_START}\n{INDEX_END}\n", encoding="utf-8")
    lines = _lines(["refresh", "--out", str(tmp_path), "--wayback-cache", str(tmp_path / "wb")])
    assert [line["report"] for line in lines] == list(cli.PUBLIC_REPORTS)
    assert "dogfood" not in {line["report"] for line in lines}
    index = (tmp_path / "README.md").read_text(encoding="utf-8")
    for name in cli.PUBLIC_REPORTS:
        assert f'<a href="{name}-latest.md">' in index
    assert not list(tmp_path.glob("dogfood*"))
    for path in tmp_path.glob("*.md"):
        assert "[" not in path.read_text(encoding="utf-8"), path


def test_group_refresh_flag(bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, Path]] = []
    monkeypatch.setattr(cli, "run_public", lambda now, out: calls.append((now, out)))
    _lines(["--refresh"])
    assert calls == [(NOW, METHODS_DIR)]
    result = runner.invoke(cli.app, [])
    assert "Usage" in result.output
    assert not calls[1:]


def test_bad_as_of(bound: Engine, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["noise", "--out", str(tmp_path), "--as-of", "yesterday"])
    assert result.exit_code == 2
    assert "--as-of must be an ISO date" in result.output


def test_dogfood_writes_to_the_private_dir_only(bound: Engine, tmp_path: Path) -> None:
    before = sorted(p.name for p in METHODS_DIR.iterdir())
    private = tmp_path / "tell-private" / "reports"
    (line,) = _lines(["dogfood", "--out", str(private)])
    assert line["path"] == str(private.resolve() / "dogfood-2026-10-06.md")
    assert line["latest"] is None
    assert sorted(p.name for p in private.iterdir()) == ["dogfood-2026-10-06.md"]
    text = (private / "dogfood-2026-10-06.md").read_text(encoding="utf-8")
    assert "Median hours to surface" in text
    assert sorted(p.name for p in METHODS_DIR.iterdir()) == before

    refused = runner.invoke(cli.app, ["dogfood", "--out", str(METHODS_DIR)])
    assert refused.exit_code == 2
    assert "never written inside the public checkout" in " ".join(
        refused.output.replace("│", " ").split()
    )
    assert sorted(p.name for p in METHODS_DIR.iterdir()) == before


def test_list() -> None:
    result = runner.invoke(cli.app, ["list"])
    assert result.stdout.split() == list(cli.REPORTS)
