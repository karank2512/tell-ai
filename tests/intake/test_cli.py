"""``tell intake sync``: the mounted sub-app against the temporary database.

Placeholder contacts use ``example.invalid`` addresses in this file only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

from tell.cli import app
from tell.db import SessionLocal
from tell.intake import cli as intake_cli
from tell.intake import load_private_watchlists
from tell.intake.reader import REPO_ROOT, is_intake_owner
from tell.models import Feedback, Run, Signal, Watchlist, WatchlistItem
from tests.publish import scenario as sc

runner = CliRunner()

EMAIL = "user1@example.invalid"
WEBHOOK = "https://hooks.slack.com/services/TPLACE/BPLACE/placeholder"
CSV_URL = "https://docs.google.invalid/spreadsheets/d/e/placeholder/pub?output=csv"
SHEET = (
    'Timestamp,Name,Email,Company,Role,"Domains (up to 50, one per line)",'
    "Who do you sell to?,Slack webhook URL (optional),I'm a job seeker\n"
    f'10/5/2026 9:15:03,Placeholder One,{EMAIL},Acme Rockets,Head of Sales,"acme.com\nbeta.io",'
    f"default,{WEBHOOK},\n"
    "10/5/2026 9:20:00,Placeholder Two,no-at-sign,Other Co,Founder,gamma.io,default,,\n"
)
SECRETS = (EMAIL, "Placeholder One", "Acme Rockets", "acme-rockets", WEBHOOK, "Head of Sales")


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Point the sub-app's ``session_scope`` at the temporary test database."""

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

    monkeypatch.setattr(intake_cli, "session_scope", scope)
    return engine


def _invoke(args: list[str], code: int = 0) -> Result:
    result = runner.invoke(app, ["intake", *args])
    assert result.exit_code == code, (result.output, result.exception)
    return result


def _json(result: Result) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return record


def _assert_no_secrets(output: str) -> None:
    for secret in SECRETS:
        assert secret not in output
    assert "@" not in output


@freeze_time("2026-10-06 07:00:00")
def test_sync_from_file_twice(bound: Engine, tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    private_dir = tmp_path / "tell-private"
    args = ["sync", "--csv-file", str(sheet), "--private-dir", str(private_dir)]

    first = _invoke(args)
    _assert_no_secrets(first.output)
    record = _json(first)
    assert record["applied"] == 1
    assert record["errors"] == 1
    assert record["rejected_rows"] == [{"row": 3, "reason": "invalid email"}]
    assert record["watchlists_created"] == 1
    assert record["items_added"] == 2
    assert record["private_files_written"] == 1

    with SessionLocal(bind=bound) as session:
        watchlist = session.scalar(select(Watchlist))
        assert watchlist is not None
        assert watchlist.slug == "acme-rockets-default"
        assert is_intake_owner(watchlist.owner)
        runs = list(session.scalars(select(Run).where(Run.source == "intake")))
        assert [r.status for r in runs] == ["ok"]

    private = load_private_watchlists(private_dir)["acme-rockets-default"]
    assert private.slack_webhook_url == WEBHOOK
    assert private.contacts[0].email == EMAIL
    assert private.owner == watchlist.owner

    second = _json(_invoke(args))
    assert (second["applied"], second["skipped"], second["private_files_written"]) == (0, 1, 0)


@respx.mock
def test_sync_from_url_reads_the_env_var(
    bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(CSV_URL).mock(return_value=httpx.Response(200, text=SHEET))
    monkeypatch.setenv("TELL_INTAKE_CSV_URL", CSV_URL)
    result = _invoke(["sync", "--private-dir", str(tmp_path / "p")])
    _assert_no_secrets(result.output)
    assert "placeholder" not in result.output
    assert _json(result)["applied"] == 1


@respx.mock
def test_fetch_failure_exits_1_without_the_url(bound: Engine, tmp_path: Path) -> None:
    respx.get(CSV_URL).mock(return_value=httpx.Response(403))
    result = _invoke(["sync", "--csv-url", CSV_URL, "--private-dir", str(tmp_path)], code=1)
    assert _json(result) == {"error": "intake CSV fetch failed: HTTP 403", "source": "intake"}
    assert "placeholder" not in result.output


def test_non_utf8_sheet_exit_1_names_the_sheet(bound: Engine, tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_bytes(SHEET.encode("utf-16"))
    result = _invoke(
        ["sync", "--csv-file", str(sheet), "--private-dir", str(tmp_path / "p")], code=1
    )
    assert _json(result) == {"error": "intake CSV is not UTF-8", "source": "intake"}


def test_drifted_columns_exit_1(bound: Engine, tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text("Timestamp,Email\n", encoding="utf-8")
    result = _invoke(
        ["sync", "--csv-file", str(sheet), "--private-dir", str(tmp_path / "p")], code=1
    )
    assert "missing required columns" in _json(result)["error"]


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("slug: [unclosed\n", "ParserError"),  # not YAML: the message would quote the file
        (
            f"slug: other\ncompany: Acme\nicp_profile: default\nowner: x\nnote: {EMAIL}\n",
            "PrivateRecordError",
        ),
    ],
)
def test_bad_private_file_exits_1_naming_only_the_error_type(
    bound: Engine, tmp_path: Path, body: str, kind: str
) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    private_dir = tmp_path / "tell-private"
    (private_dir / "watchlists").mkdir(parents=True)
    (private_dir / "watchlists" / "acme-rockets-default.yaml").write_text(body, encoding="utf-8")
    result = _invoke(["sync", "--csv-file", str(sheet), "--private-dir", str(private_dir)], code=1)
    assert _json(result) == {
        "error": f"invalid private watchlist file ({kind})",
        "source": "intake",
    }
    _assert_no_secrets(result.output)
    assert "unclosed" not in result.output
    with SessionLocal(bind=bound) as session:
        assert session.scalar(select(Watchlist)) is None  # nothing was applied


def test_unwritable_private_dir_exits_1_after_the_commit(
    bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    private_dir = tmp_path / "tell-private"
    private_dir.mkdir()

    def refuse(*_args: Any, **_kwargs: Any) -> list[Path]:
        msg = f"{private_dir / 'watchlists' / 'acme-rockets-default.yaml'}: read-only"
        raise PermissionError(msg)

    monkeypatch.setattr(intake_cli, "write_private_watchlists", refuse)
    result = _invoke(["sync", "--csv-file", str(sheet), "--private-dir", str(private_dir)], code=1)
    assert _json(result) == {
        "error": "could not write private watchlist files (PermissionError)",
        "source": "intake",
    }
    _assert_no_secrets(result.output)
    with SessionLocal(bind=bound) as session:
        assert session.scalar(select(Watchlist)) is not None  # the database side was committed


def test_private_dir_inside_repo_exits_1(tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    result = _invoke(
        ["sync", "--csv-file", str(sheet), "--private-dir", str(REPO_ROOT / "private")], code=1
    )
    assert "outside" in _json(result)["error"]


@pytest.mark.parametrize("extra", [[], ["--csv-url", CSV_URL, "--csv-file", "SHEET"]])
def test_exactly_one_source_is_required(tmp_path: Path, extra: list[str]) -> None:
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    args = [str(sheet) if a == "SHEET" else a for a in extra]
    result = _invoke(["sync", *args, "--private-dir", str(tmp_path)], code=2)
    assert "exactly one" in result.output


def test_intake_is_mounted() -> None:
    assert "sync" in _invoke(["--help"]).output
    assert "--csv-url" in _invoke(["sync", "--help"]).output


# --- feedback-sync (ADR 0039) --------------------------------------------------------------------

FEEDBACK_CSV_URL = "https://docs.google.invalid/spreadsheets/d/e/feedback/pub?output=csv"


def _seed_signal(engine: Engine) -> int:
    with freeze_time(sc.NOW), SessionLocal(bind=engine) as session:
        scenario = sc.build(session)
        signal = session.scalars(
            select(Signal)
            .join(WatchlistItem, WatchlistItem.company_id == Signal.company_id)
            .where(WatchlistItem.watchlist_id == scenario.private.id)
            .order_by(Signal.id)
        ).first()
        assert signal is not None
        session.commit()
        return signal.id


def _feedback_sheet(signal_id: int) -> str:
    return (
        "Timestamp,Signal id,Domain,Verdict,Watchlist,Note\n"
        f"10/6/2026 9:15:03,{signal_id},,useful,,Looked real\n"
        "10/6/2026 9:16:00,999999,,useful,,\n"
    )


@freeze_time("2026-10-06 12:00:00")
def test_feedback_sync_from_file_twice(bound: Engine, tmp_path: Path) -> None:
    signal_id = _seed_signal(bound)
    sheet = tmp_path / "feedback.csv"
    sheet.write_text(_feedback_sheet(signal_id), encoding="utf-8")
    first = _json(_invoke(["feedback-sync", "--csv-file", str(sheet)]))
    assert (first["source"], first["rows"], first["applied"], first["skipped"]) == (
        "feedback_sync",
        2,
        1,
        0,
    )
    assert first["rejected_rows"] == [{"row": 3, "reason": "unknown signal"}]
    assert "Looked real" not in json.dumps(first)
    with SessionLocal(bind=bound) as session:
        (row,) = session.scalars(select(Feedback))
        assert (row.signal_id, row.verdict, row.note) == (signal_id, "useful", "Looked real")
        assert [r.status for r in session.scalars(select(Run).where(Run.source == "feedback_sync"))]
    second = _json(_invoke(["feedback-sync", "--csv-file", str(sheet)]))
    assert (second["applied"], second["skipped"]) == (0, 1)


@respx.mock
def test_feedback_sync_from_url_reads_the_env_var(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_id = _seed_signal(bound)
    respx.get(FEEDBACK_CSV_URL).mock(
        return_value=httpx.Response(200, text=_feedback_sheet(signal_id))
    )
    monkeypatch.setenv("TELL_FEEDBACK_CSV_URL", FEEDBACK_CSV_URL)
    result = _invoke(["feedback-sync"])
    assert "feedback/pub" not in result.output
    assert _json(result)["applied"] == 1


@respx.mock
def test_feedback_sync_fetch_failure_and_drift_exit_1(bound: Engine, tmp_path: Path) -> None:
    respx.get(FEEDBACK_CSV_URL).mock(return_value=httpx.Response(403))
    failed = _invoke(["feedback-sync", "--csv-url", FEEDBACK_CSV_URL], code=1)
    assert json.loads(failed.output) == {
        "source": "feedback_sync",
        "error": "intake CSV fetch failed: HTTP 403",
    }
    assert FEEDBACK_CSV_URL not in failed.output
    sheet = tmp_path / "drift.csv"
    sheet.write_text("Timestamp,Note\nt,x\n", encoding="utf-8")
    drifted = _invoke(["feedback-sync", "--csv-file", str(sheet)], code=1)
    assert "missing required columns" in json.loads(drifted.output)["error"]
    with SessionLocal(bind=bound) as session:
        statuses = [
            r.status for r in session.scalars(select(Run).where(Run.source == "feedback_sync"))
        ]
    assert statuses == ["failed"]


@pytest.mark.parametrize("extra", [[], ["--csv-url", FEEDBACK_CSV_URL, "--csv-file", "x.csv"]])
def test_feedback_sync_needs_exactly_one_source(tmp_path: Path, extra: list[str]) -> None:
    if "x.csv" in extra:
        (tmp_path / "x.csv").write_text("", encoding="utf-8")
        extra[-1] = str(tmp_path / "x.csv")
    result = runner.invoke(app, ["intake", "feedback-sync", *extra])
    assert result.exit_code == 2
    assert "exactly one of --csv-url or --csv-file" in result.output
