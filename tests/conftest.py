"""Shared fixtures.

The ``test_database_url`` fixture creates a throwaway database named
``tell_test_<8 hex>`` on the local server for the whole pytest session, brings it
to ``alembic upgrade head`` and drops it at teardown, so several pytest processes
(or agents) can run concurrently against one server without colliding.

Every test that touches the database (anything requesting ``engine`` directly or
through ``db_session``) gets the model tables truncated afterwards, so counting
tests are isolated from each other regardless of commits.

Tests are hermetic with respect to configuration: the autouse ``_hermetic_settings``
fixture strips every ``tell`` variable from the environment (``DATABASE_URL`` and
``TELL_TEST_ADMIN_URL`` excepted, since they locate the database server), stops
``Settings`` from reading the checkout's ``.env`` and clears the ``get_settings``
cache before and after each test. The database URL itself is resolved once, at
session start, from the environment or the checkout's ``.env``.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

import tests._ci_env  # noqa: F401  # must run before typer reads the environment
from tell.config import Settings, get_settings
from tell.db import SessionLocal, make_engine
from tell.models import Base

MAINTENANCE_DB = "postgres"
REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
DOTENV = REPO_ROOT / ".env"

KEPT_ENV_VARS = frozenset({"DATABASE_URL", "TELL_TEST_ADMIN_URL"})
"""Environment variables the hermetic fixture leaves alone: they locate the test server."""

STRIPPED_ENV_VARS = frozenset({"SEC_USER_AGENT", "SLACK_WEBHOOK_URL", "GITHUB_TOKEN"})
"""Non-``TELL_`` settings variables the hermetic fixture removes (plus every ``TELL_*``)."""


def _is_stripped(name: str) -> bool:
    if name in KEPT_ENV_VARS:
        return False
    return name in STRIPPED_ENV_VARS or name.startswith("TELL_")


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient configuration leaks into a test; no test leaks a cached ``Settings``.

    Removes every ``TELL_*`` / ``SEC_USER_AGENT`` / ``SLACK_WEBHOOK_URL`` /
    ``GITHUB_TOKEN`` variable (keeping ``DATABASE_URL`` and ``TELL_TEST_ADMIN_URL``),
    points ``Settings.model_config["env_file"]`` at nothing so the checkout's ``.env``
    is never read, and clears the ``get_settings`` cache on both sides of the test. A
    test that wants a ``.env`` passes ``Settings(_env_file=path)`` explicitly.
    """
    for name in [n for n in os.environ if _is_stripped(n)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def admin_url() -> str:
    """URL of the maintenance database used to CREATE/DROP the test database.

    ``TELL_TEST_ADMIN_URL`` wins when set; otherwise ``DATABASE_URL`` (from the
    environment or the checkout's ``.env``, read explicitly so the hermetic fixture
    does not hide it) with its database name swapped for ``postgres``.
    """
    explicit = os.environ.get("TELL_TEST_ADMIN_URL")
    if explicit:
        return explicit
    # Resolve only the database URL, never a full Settings: a malformed ambient
    # TELL_* value must not break database setup before the hermetic fixture runs.
    base = os.environ.get("DATABASE_URL")
    if not base and DOTENV.is_file():
        base = dotenv_values(DOTENV).get("DATABASE_URL") or None
    if not base:
        base = str(Settings.model_fields["database_url"].default)
    return make_url(base).set(database=MAINTENANCE_DB).render_as_string(hide_password=False)


def alembic_config(url: str) -> Config:
    """An alembic ``Config`` for ``url`` that leaves the test process's logging alone."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["url"] = url
    cfg.attributes["skip_logging_config"] = True
    return cfg


def apply_migrations(url: str) -> None:
    """Bring a freshly created database to the current schema (``alembic upgrade head``)."""
    command.upgrade(alembic_config(url), "head")


def truncate_all(engine: Engine) -> None:
    """``TRUNCATE`` every model table, resetting sequences; FKs are handled by CASCADE."""
    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Create ``tell_test_<hex>`` for this session; yield its URL; drop it afterwards."""
    db_name = f"tell_test_{secrets.token_hex(4)}"
    admin = make_url(admin_url())
    admin_engine = make_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT", poolclass=None
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = admin.set(database=db_name).render_as_string(hide_password=False)
    try:
        apply_migrations(url)
        yield url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    """Engine bound to the session's temporary database."""
    eng = make_engine(test_database_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(autouse=True)
def _isolate_database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Truncate the model tables after any test that used the database.

    Resolves ``engine`` up front (it is session-scoped, so this is free) only when
    the test's fixture closure includes it; tests without database fixtures never
    trigger database creation.
    """
    eng: Engine | None = None
    if "engine" in request.fixturenames:
        eng = request.getfixturevalue("engine")
    yield
    if eng is not None:
        truncate_all(eng)


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session on the temporary database; uncommitted work is rolled back at the end."""
    session = SessionLocal(bind=engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
