"""``tell discover`` sub-app: domain, batch and seed-yc commands against the test database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.db import SessionLocal
from tell.discovery import cli
from tell.discovery.ats import CANDIDATE_PATHS
from tell.discovery.yc import YC_DIRECTORY_URL
from tell.models import AtsBoard, Company

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "yc" / "sample.json"
runner = CliRunner()


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


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


def _count(engine: Engine, model: type[Company] | type[AtsBoard]) -> int:
    with SessionLocal(bind=engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _site(router: respx.MockRouter, base: str, careers: bytes) -> None:
    router.get(f"{base}/robots.txt").respond(404)
    for path in CANDIDATE_PATHS:
        router.get(f"{base}{path}").respond(404)
    router.get(f"{base}/careers").respond(200, content=careers)


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("domain", "batch", "seed-yc"):
        assert command in result.output


def test_seed_yc_from_file(bound: Engine) -> None:
    result = runner.invoke(cli.app, ["seed-yc", "--file", str(SAMPLE)])
    assert result.exit_code == 0, result.output
    stats = json.loads(result.output)
    assert (stats["inserted"], stats["skipped_no_website"]) == (20, 1)
    assert _count(bound, Company) == 20


def test_seed_yc_fetch(bound: Engine, router: respx.MockRouter) -> None:
    router.get(YC_DIRECTORY_URL).respond(200, content=SAMPLE.read_bytes())
    result = runner.invoke(cli.app, ["seed-yc", "--fetch"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["inserted"] == 20


@pytest.mark.parametrize("args", [[], ["--file", str(SAMPLE), "--fetch"]])
def test_seed_yc_needs_exactly_one_source(args: list[str]) -> None:
    result = runner.invoke(cli.app, ["seed-yc", *args])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_domain_command(bound: Engine, router: respx.MockRouter) -> None:
    _site(router, "https://acme.com", (FIXTURES / "discovery" / "workday.html").read_bytes())
    result = runner.invoke(cli.app, ["domain", "--domain", "acme.com", "--name", "Acme"])
    assert result.exit_code == 0, result.output
    line = json.loads(result.output)
    assert (line["outcome"], line["vendor"]) == ("unsupported", "workday")
    assert line["discovered_from"] == "https://acme.com/careers"
    assert _count(bound, AtsBoard) == 1


def test_domain_command_rejects_bad_domain(bound: Engine) -> None:
    result = runner.invoke(cli.app, ["domain", "--domain", "localhost"])
    assert result.exit_code != 0
    assert "registrable" in result.output


def test_batch_command(bound: Engine, router: respx.MockRouter) -> None:
    with SessionLocal(bind=bound) as session:
        session.add(Company(domain="acme.com", name="Acme"))
        session.commit()
    _site(router, "https://acme.com", (FIXTURES / "discovery" / "oracle.html").read_bytes())
    result = runner.invoke(cli.app, ["batch", "--limit", "5", "--rps", "1000"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["new_domains"] == 1
    assert summary["outcomes"]["unsupported"] == 1
    assert summary["errors"] == 0


def test_batch_command_transport_errors_do_not_crash(
    bound: Engine, router: respx.MockRouter
) -> None:
    with SessionLocal(bind=bound) as session:
        session.add(Company(domain="acme.com", name="Acme"))
        session.commit()
    router.route(host="acme.com").mock(side_effect=httpx.ConnectError("down"))
    router.route().respond(404)
    result = runner.invoke(cli.app, ["batch", "--rps", "1000"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["outcomes"]["no_board"] == 1
