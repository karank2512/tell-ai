"""``tell resolve`` sub-app against the test database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.db import SessionLocal
from tell.models import Company, EventRow
from tell.resolve import cli

runner = CliRunner()
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Point the CLI's ``session_scope`` at the temporary test database."""

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
    return engine


def _seed(engine: Engine) -> None:
    with SessionLocal(bind=engine) as session:
        session.add(Company(domain="acme.com", name="Acme", hq_state="CA"))
        session.add(Company(domain="acmerobotics.com", name="Acme Robotics", hq_state="NY"))
        session.flush()
        for event_id, name in (("ev-1", "Acme, Inc."), ("ev-2", "Acme Robotics Group")):
            session.add(
                EventRow(
                    event_id=event_id,
                    source="edgar",
                    type="form_d_filed",
                    occurred_at=NOW,
                    observed_at=NOW,
                    magnitude=Decimal(1),
                    url=f"https://example.test/{event_id}",
                    payload={"issuer_name": name, "issuer_state": "WA", "previous_names": []},
                    run_id="r",
                )
            )
        session.commit()


def _json(output: str) -> dict[str, object]:
    data: dict[str, object] = json.loads(output.strip().splitlines()[-1])
    return data


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "backfill-aliases", "review-export", "review-import", "stats"):
        assert command in result.output


def test_full_cli_round_trip(bound: Engine, tmp_path: Path) -> None:
    _seed(bound)
    result = runner.invoke(cli.app, ["backfill-aliases"])
    assert result.exit_code == 0, result.output
    assert _json(result.output) == {"aliases_added": 3}

    result = runner.invoke(cli.app, ["run", "--limit", "10"])
    assert result.exit_code == 0, result.output
    stats = _json(result.output)
    assert (stats["scanned"], stats["auto_linked"], stats["review"]) == (2, 1, 1)
    assert stats["by_rule"] == {"R2": 1, "R5": 1}

    csv_path = tmp_path / "review.csv"
    result = runner.invoke(cli.app, ["review-export", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert _json(result.output) == {"path": str(csv_path), "rows": 1}

    text = csv_path.read_text(encoding="utf-8")
    header, row = text.strip().splitlines()
    columns = header.split(",")
    values = row.split(",")
    values[columns.index("verdict")] = "reject"
    csv_path.write_text(header + "\n" + ",".join(values) + "\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["review-import", str(csv_path)])
    assert result.exit_code == 0, result.output
    imported = _json(result.output)
    assert (imported["rows"], imported["rejected"], imported["errors"]) == (1, 1, [])

    result = runner.invoke(cli.app, ["stats"])
    assert result.exit_code == 0, result.output
    assert _json(result.output) == {
        "events_linked": 1,
        "events_unlinked": 1,
        "by_rule": {"R2": 1, "R5": 1},
        "review": {"rejected": 1},
    }

    # --retry re-runs the unlinked event; the rejected pair stays out of the queue
    result = runner.invoke(cli.app, ["run", "--retry"])
    assert result.exit_code == 0, result.output
    assert _json(result.output)["rejected_skipped"] == 1


def test_stats_on_empty_database(bound: Engine) -> None:
    result = runner.invoke(cli.app, ["stats"])
    assert result.exit_code == 0, result.output
    assert _json(result.output) == {
        "events_linked": 0,
        "events_unlinked": 0,
        "by_rule": {},
        "review": {},
    }


def test_review_import_requires_existing_file(bound: Engine, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["review-import", str(tmp_path / "missing.csv")])
    assert result.exit_code != 0
