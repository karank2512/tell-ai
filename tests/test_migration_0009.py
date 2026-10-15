"""Migration 0009 (ADR 0035): ``companies.source`` / ``cik_source`` and their backfill.

Runs against a database of its own (created and dropped here), brought to revision 0008,
filled with companies and EDGAR events, then upgraded to 0009: every existing company gets
``source = 'yc'``, and a CIK gets a ``cik_source`` only where the events show a trusted link
earned it (a reviewer's link, R1, or an R2 Form D link that passes the ADR 0034 guards).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

from tell.db import make_engine
from tests.conftest import admin_url, alembic_config

pytestmark = pytest.mark.db


@pytest.fixture
def db_0008() -> Iterator[tuple[str, Engine]]:
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
        command.upgrade(alembic_config(url), "0008")
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


GOOD_FORMD: dict[str, Any] = {
    "issuer_name": "Acme Robotics, Inc.",
    "entity_type": "Corporation",
    "jurisdiction_of_inc": "DELAWARE",
    "year_of_inc": 2022,
    "date_filed": "2026-09-01",
    "industry_group_type": "Other Technology",
}

# key -> (yc_status, yc_batch, event type, payload overrides, match record or None,
#         event CIK differs from the company's, expected cik_source)
CASES: dict[str, tuple[Any, ...]] = {
    "guarded": ("Active", "Winter 2023", "form_d_filed", {}, {"rule": "R2"}, False, "guarded_r2"),
    "amendment": (
        "Active",
        "Winter 2023",
        "form_d_amendment",
        {},
        {"rule": "R2", "guard_failed": []},
        False,
        "guarded_r2",
    ),
    "acquired": ("Acquired", "Winter 2023", "form_d_filed", {}, {"rule": "R2"}, False, None),
    "no_status": (None, None, "form_d_filed", {}, {"rule": "R2"}, False, None),
    "guard_failed": (
        "Active",
        "Winter 2023",
        "form_d_filed",
        {},
        {"rule": "R2", "guard_failed": ["jurisdiction_not_delaware"]},
        False,
        None,
    ),
    "after_batch": (
        "Active",
        "Summer 2021",
        "form_d_filed",
        {"year_of_inc": 2024},
        {"rule": "R2"},
        False,
        None,
    ),
    "over_five_recent": (
        "Active",
        "Summer 2025",
        "form_d_filed",
        {"year_of_inc": None},
        {"rule": "R2"},
        False,
        None,
    ),
    "over_five_old": (
        "Active",
        "Summer 2019",
        "form_d_filed",
        {"year_of_inc": None},
        {"rule": "R2"},
        False,
        "guarded_r2",
    ),
    "llc": (
        "Active",
        "Winter 2023",
        "form_d_filed",
        {"entity_type": "Limited Liability Company"},
        {"rule": "R2"},
        False,
        None,
    ),
    "fund": (
        "Active",
        "Winter 2023",
        "form_d_filed",
        {"issuer_name": "Acme Opportunity Fund, L.P."},
        {"rule": "R2"},
        False,
        None,
    ),
    "series": (
        "Active",
        "Winter 2023",
        "form_d_filed",
        {"issuer_name": "Acme I, a series of Holdco Inc."},
        {"rule": "R2"},
        False,
        None,
    ),
    "fuzzy": ("Active", "Winter 2023", "form_d_filed", {}, {"rule": "R4"}, False, None),
    "collector": ("Active", "Winter 2023", "form_d_filed", {}, None, False, None),
    "manual": (
        "Inactive",
        "Winter 2023",
        "form_d_filed",
        {"entity_type": "Limited Partnership"},
        {"rule": "manual", "guard_failed": ["x"]},
        False,
        "manual",
    ),
    "r1": ("Active", "Winter 2023", "form_8k_filed", {}, {"rule": "R1"}, False, "r1"),
    "r2_other_form": ("Active", "Winter 2023", "form_8k_filed", {}, {"rule": "R2"}, False, None),
    "other_cik": ("Active", "Winter 2023", "form_d_filed", {}, {"rule": "R2"}, True, None),
}


def test_backfill_source_and_cik_source(db_0008: tuple[str, Engine]) -> None:
    url, engine = db_0008
    with engine.begin() as conn:
        for n, (key, case) in enumerate(CASES.items(), start=1):
            status, batch, event_type, overrides, match, other_cik, _ = case
            cik = f"{n:010d}"
            company_id = conn.execute(
                text(
                    "INSERT INTO companies (domain, name, cik, yc_status, yc_batch) "
                    "VALUES (:d, :n, :cik, :s, :b) RETURNING id"
                ),
                {"d": f"{key}.example", "n": key, "cik": cik, "s": status, "b": batch},
            ).scalar_one()
            payload = {**GOOD_FORMD, **overrides, "cik": f"{n + 500:010d}" if other_cik else cik}
            if match is not None:
                payload["match"] = match
            conn.execute(
                text(
                    "INSERT INTO events (event_id, company_id, source, type, occurred_at, "
                    "observed_at, url, payload, run_id) VALUES (:e, :c, 'edgar', :t, "
                    "'2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z', :u, "
                    "cast(:p as jsonb), 'r')"
                ),
                {
                    "e": f"event-{key}",
                    "c": company_id,
                    "t": event_type,
                    "u": f"https://example.test/{key}",
                    "p": json.dumps(payload),
                },
            )
        conn.execute(
            text("INSERT INTO companies (domain, name) VALUES ('nocik.example', 'No CIK')")
        )

    command.upgrade(alembic_config(url), "0009")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name, source, cik_source FROM companies")).all()
    found = {name: (source, cik_source) for name, source, cik_source in rows}
    assert {source for source, _ in found.values()} == {"yc"}
    expected = {key: case[-1] for key, case in CASES.items()} | {"No CIK": None}
    assert {name: cik_source for name, (_, cik_source) in found.items()} == expected

    command.downgrade(alembic_config(url), "0008")
    with engine.connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'companies'"
                )
            )
        }
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        }
    assert not {"source", "cik_source"} & columns
    assert "issuer_discovery" not in tables
