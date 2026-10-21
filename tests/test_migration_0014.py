"""Migration 0014 (ADR 0044): ``companies.public_opt_out`` / ``companies.public_opted_out_at``.

Runs against a database of its own (created and dropped here), brought to revision 0013 with
one company in it, then upgraded to 0014: the existing company has not opted out (``false``,
``NULL``), a new row defaults the same way, and the downgrade removes both columns again.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

from tell.db import make_engine
from tests.conftest import admin_url, alembic_config

pytestmark = pytest.mark.db

COLUMNS = {"public_opt_out", "public_opted_out_at"}


@pytest.fixture
def db_0013() -> Iterator[tuple[str, Engine]]:
    name = f"tell_test_mig_{secrets.token_hex(4)}"
    admin = make_url(admin_url())
    admin_engine = make_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT", poolclass=None
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin.set(database=name).render_as_string(hide_password=False)
    engine = make_engine(url)
    try:
        command.upgrade(alembic_config(url), "0013")
        yield url, engine
    finally:
        engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin_engine.dispose()


def _columns(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'companies'"
            )
        )
        return {str(row[0]) for row in rows}


def test_upgrade_backfills_no_opt_out_and_downgrade_drops_the_columns(
    db_0013: tuple[str, Engine],
) -> None:
    url, engine = db_0013
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO companies (domain, name) VALUES ('old.example', 'Old')"))
    assert not COLUMNS & _columns(engine)

    command.upgrade(alembic_config(url), "0014")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO companies (domain, name) VALUES ('new.example', 'New')"))
        rows = conn.execute(
            text("SELECT domain, public_opt_out, public_opted_out_at FROM companies ORDER BY id")
        ).all()
    assert [tuple(row) for row in rows] == [
        ("old.example", False, None),
        ("new.example", False, None),
    ]

    command.downgrade(alembic_config(url), "0013")
    assert not COLUMNS & _columns(engine)
