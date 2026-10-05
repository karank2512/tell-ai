"""Shared fixtures.

The ``test_database_url`` fixture creates a throwaway database named
``tell_test_<8 hex>`` on the local server for the whole pytest session, brings it
to ``alembic upgrade head`` and drops it at teardown, so several pytest processes
(or agents) can run concurrently against one server without colliding.

Every test that touches the database (anything requesting ``engine`` directly or
through ``db_session``) gets the model tables truncated afterwards, so counting
tests are isolated from each other regardless of commits.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from tell.config import Settings
from tell.db import SessionLocal, make_engine
from tell.models import Base

MAINTENANCE_DB = "postgres"
REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def admin_url() -> str:
    """URL of the maintenance database used to CREATE/DROP the test database.

    ``TELL_TEST_ADMIN_URL`` wins when set; otherwise ``DATABASE_URL`` (from the
    environment or ``.env``) with its database name swapped for ``postgres``.
    """
    explicit = os.environ.get("TELL_TEST_ADMIN_URL")
    if explicit:
        return explicit
    base = Settings().database_url
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
