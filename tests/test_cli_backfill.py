"""``tell backfill formd`` and the news cohort flags of ``tell ingest`` (SPEC 13.1 wiring).

Database-backed like ``tests/test_cli.py``: :func:`tell.db.get_engine` is pointed at the
temporary database. HTTP goes through ``respx``; nothing reaches sec.gov or Google News.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
from tell.cli import app
from tell.collectors import edgar_backfill, news
from tell.config import get_settings
from tell.db import SessionLocal
from tell.models import EventRow, Run
from tests.collectors.test_edgar_backfill import REAL_AGENT, build_zip, zip_bytes

runner = CliRunner()

NOW = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
Q1_URL = edgar_backfill.dataset_url(2026, 1)


def _invoke(args: list[str], code: int = 0) -> Result:
    result = runner.invoke(app, args)
    assert result.exit_code == code, (result.output, result.exception)
    return result


def _json(result: Result) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return record


def _events(engine: Engine) -> int:
    with SessionLocal(bind=engine) as session:
        return session.execute(select(func.count()).select_from(EventRow)).scalar_one()


def _runs(engine: Engine, source: str) -> list[Run]:
    with SessionLocal(bind=engine) as session:
        return list(
            session.scalars(select(Run).where(Run.source == source).order_by(Run.started_at))
        )


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setattr(tell.db, "get_engine", lambda: engine)
    return engine


# --- tell backfill formd ---------------------------------------------------------------------------


def test_file_load_is_offline_and_idempotent(bound: Engine, tmp_path: Path) -> None:
    archive = build_zip(tmp_path / "2026q1_d.zip")
    with respx.mock(assert_all_mocked=True) as mock, freeze_time(NOW):
        first = _json(_invoke(["backfill", "formd", "--file", str(archive)]))
        again = _json(_invoke(["backfill", "formd", "--file", str(archive)]))
        assert mock.calls.call_count == 0
    assert (first["source"], first["status"]) == ("edgar_backfill", "ok")
    assert (first["fetched"], first["inserted"], first["skipped"]) == (2, 2, 0)
    assert first["notes"]["datasets"] == ["2026q1_d.zip"]
    assert first["notes"]["since_days"] == 120
    assert (again["fetched"], again["inserted"], again["skipped"]) == (2, 0, 2)
    assert _events(bound) == 2
    assert [r.status for r in _runs(bound, "edgar_backfill")] == ["ok", "ok"]


def test_since_days_filters_the_write(bound: Engine, tmp_path: Path) -> None:
    archive = build_zip(tmp_path / "2026q1_d.zip")
    with freeze_time(NOW):
        line = _json(_invoke(["backfill", "formd", "--file", str(archive), "--since-days", "60"]))
    assert (line["inserted"], line["skipped"]) == (1, 1)
    assert line["notes"]["outside_window"] == 1
    assert line["notes"]["cutoff"] == "2026-01-31"


def test_placeholder_contact_refuses_before_any_request(bound: Engine, tmp_path: Path) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        result = _invoke(
            ["backfill", "formd", "--quarter", "2026Q1", "--cache-dir", str(tmp_path)], code=2
        )
        assert mock.calls.call_count == 0
    assert "SEC_USER_AGENT is still the placeholder" in result.stderr
    assert result.stdout == ""
    assert _runs(bound, "edgar_backfill") == []
    assert list(tmp_path.iterdir()) == []


def test_quarter_downloads_once_then_reads_the_cache(
    bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", REAL_AGENT)
    monkeypatch.setenv("TELL_RATE_EDGAR_RPS", "1000")
    get_settings.cache_clear()
    args = ["backfill", "formd", "--quarter", "2026q1", "--cache-dir", str(tmp_path)]
    with respx.mock(assert_all_called=True) as mock, freeze_time(NOW):
        route = mock.get(Q1_URL).mock(return_value=httpx.Response(200, content=zip_bytes()))
        first = _json(_invoke(args))
        again = _json(_invoke(args))
        assert route.call_count == 1
        assert route.calls.last.request.headers["User-Agent"] == REAL_AGENT
    assert (tmp_path / "2026q1_d.zip").is_file()
    assert (first["inserted"], again["inserted"]) == (2, 0)
    assert first["notes"]["datasets"] == ["2026q1_d.zip"]


def test_a_missing_data_set_is_a_failed_run(
    bound: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", REAL_AGENT)
    get_settings.cache_clear()
    with respx.mock(assert_all_called=True) as mock:
        for url in edgar_backfill.dataset_urls(2026, 3):
            mock.get(url).mock(return_value=httpx.Response(404))
        result = _invoke(
            ["backfill", "formd", "--quarter", "2026Q3", "--cache-dir", str(tmp_path)], code=1
        )
    assert _json(result)["status"] == "failed"
    (run,) = _runs(bound, "edgar_backfill")
    assert run.notes is not None
    assert run.notes["error"].startswith("DatasetNotPublishedError:")


def test_a_bad_file_is_a_failed_run(bound: Engine, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _invoke(["backfill", "formd", "--file", str(empty)], code=1)
    assert _json(result)["status"] == "failed"
    assert _events(bound) == 0


@pytest.mark.parametrize(
    "args",
    [
        ["backfill", "formd"],
        ["backfill", "formd", "--quarter", "2026Q9"],
        ["backfill", "formd", "--file", "/nonexistent/2026q1_d.zip"],
        ["backfill", "formd", "--quarter", "2026Q1", "--since-days", "0"],
    ],
)
def test_backfill_usage_errors_exit_2(args: list[str]) -> None:
    _invoke(args, code=2)


def test_backfill_help() -> None:
    assert "formd" in _invoke(["backfill", "--help"]).output
    help_text = _invoke(["backfill", "formd", "--help"]).output
    for option in ("--quarter", "--file", "--since-days", "--cache-dir"):
        assert option in help_text


# --- tell ingest --source news --cohort formd --when-days N ---------------------------------------


def _spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    real = news.ingest

    def spy(session: Any, **kwargs: Any) -> Run:
        seen.update(kwargs)
        return real(session, **kwargs)

    monkeypatch.setattr(news, "ingest", spy)
    return seen


def test_cohort_defaults_to_a_120_day_window(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch)
    with respx.mock(assert_all_mocked=True):
        line = _json(_invoke(["ingest", "--source", "news", "--cohort", "formd"]))
    assert (seen["cohort"], seen["when_days"], seen["company_ids"]) == ("formd", 120, None)
    assert seen["cohort_days"] == 90
    assert (line["source"], line["status"], line["fetched"]) == ("news", "ok", 0)
    (run,) = _runs(bound, "news")
    assert run.notes is not None
    notes = run.notes
    assert (notes["cohort"], notes["cohort_size"], notes["cohort_days"]) == ("formd", 0, 90)
    assert notes["when_days"] == 120


def test_when_days_is_passed_through(bound: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy(monkeypatch)
    with respx.mock(assert_all_mocked=True):
        _invoke(
            [
                "ingest",
                "--source",
                "news",
                "--cohort",
                "formd",
                "--when-days",
                "300",
                "--cohort-days",
                "250",
            ]
        )
    assert (seen["cohort"], seen["when_days"], seen["cohort_days"]) == ("formd", 300, 250)
    with respx.mock(assert_all_mocked=True):
        _invoke(["ingest", "--source", "news", "--when-days", "30", "--limit", "5"])
    assert (seen["cohort"], seen["when_days"]) == (None, 30)


def test_scheduled_news_keeps_the_seven_day_window(
    bound: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch)
    with respx.mock(assert_all_mocked=True):
        _invoke(["ingest", "--source", "news"])
    assert (seen["cohort"], seen["when_days"]) == (None, 7)


@pytest.mark.parametrize(
    "args",
    [
        ["ingest", "--source", "edgar", "--when-days", "120"],
        ["ingest", "--source", "greenhouse", "--cohort", "formd"],
        ["ingest", "--source", "news", "--cohort", "formd", "--domain", "acme.example"],
        ["ingest", "--source", "news", "--cohort", "everyone"],
        ["ingest", "--source", "news", "--when-days", "0"],
        ["ingest", "--source", "news", "--when-days", "400"],
        ["ingest", "--source", "news", "--cohort-days", "30"],
        ["ingest", "--source", "news", "--cohort", "formd", "--cohort-days", "0"],
    ],
)
def test_news_cohort_usage_errors_exit_2(args: list[str]) -> None:
    _invoke(args, code=2)
