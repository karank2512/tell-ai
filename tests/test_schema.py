"""Schema parity: models == migration == the DDL in docs/SPEC.md section 5."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine

from tell import models
from tell.models import Base
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

HEAD_REVISION = "0001"

# --- the spec, hardcoded -----------------------------------------------------------------

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "companies": [
        "id",
        "domain",
        "name",
        "legal_name",
        "cik",
        "hq_city",
        "hq_state",
        "hq_country",
        "github_org",
        "created_at",
        "updated_at",
    ],
    "company_aliases": ["company_id", "alias", "source"],
    "ats_boards": [
        "id",
        "company_id",
        "vendor",
        "identifier",
        "discovered_from",
        "confidence",
        "status",
        "last_verified_at",
        "last_polled_at",
    ],
    "postings": [
        "id",
        "board_id",
        "external_id",
        "fingerprint",
        "title",
        "title_norm",
        "role_family",
        "department",
        "location",
        "remote",
        "posted_at",
        "first_seen_at",
        "last_seen_at",
        "closed_at",
        "repost_of",
        "url",
        "content_hash",
    ],
    "posting_observations": ["posting_id", "observed_at", "run_id"],
    "events": [
        "event_id",
        "company_id",
        "source",
        "type",
        "occurred_at",
        "observed_at",
        "magnitude",
        "url",
        "payload",
        "raw_ref",
        "run_id",
    ],
    "signals": [
        "id",
        "company_id",
        "event_id",
        "signal_type",
        "role_family",
        "weight",
        "magnitude",
        "half_life_days",
        "starts_at",
    ],
    "scores": ["company_id", "icp_profile", "as_of", "score", "contributions"],
    "watchlists": ["id", "owner", "name", "icp_profile"],
    "watchlist_items": ["watchlist_id", "company_id"],
    "feedback": ["id", "watchlist_id", "signal_id", "verdict", "note", "created_at"],
    "runs": [
        "run_id",
        "source",
        "started_at",
        "finished_at",
        "fetched",
        "inserted",
        "skipped",
        "errors",
        "status",
        "notes",
    ],
    "review_queue": ["id", "kind", "payload", "status", "created_at", "resolved_at"],
}

EXPECTED_PRIMARY_KEYS: dict[str, list[str]] = {
    "companies": ["id"],
    "company_aliases": ["company_id", "alias"],
    "ats_boards": ["id"],
    "postings": ["id"],
    "posting_observations": ["posting_id", "observed_at"],
    "events": ["event_id"],
    "signals": ["id"],
    "scores": ["company_id", "icp_profile", "as_of"],
    "watchlists": ["id"],
    "watchlist_items": ["watchlist_id", "company_id"],
    "feedback": ["id"],
    "runs": ["run_id"],
    "review_queue": ["id"],
}

EXPECTED_UNIQUES: dict[str, set[tuple[str, ...]]] = {
    "companies": {("domain",), ("cik",)},
    "ats_boards": {("vendor", "identifier")},
    "postings": {("board_id", "external_id")},
    "signals": {("event_id", "signal_type", "role_family")},
}

# (constrained columns, referred table, referred columns)
EXPECTED_FOREIGN_KEYS: dict[str, set[tuple[tuple[str, ...], str, tuple[str, ...]]]] = {
    "company_aliases": {(("company_id",), "companies", ("id",))},
    "ats_boards": {(("company_id",), "companies", ("id",))},
    "postings": {
        (("board_id",), "ats_boards", ("id",)),
        (("repost_of",), "postings", ("id",)),
    },
    "posting_observations": {(("posting_id",), "postings", ("id",))},
    "events": {(("company_id",), "companies", ("id",))},
    "signals": {
        (("company_id",), "companies", ("id",)),
        (("event_id",), "events", ("event_id",)),
    },
    "scores": {(("company_id",), "companies", ("id",))},
    "watchlist_items": {
        (("watchlist_id",), "watchlists", ("id",)),
        (("company_id",), "companies", ("id",)),
    },
    "feedback": {
        (("watchlist_id",), "watchlists", ("id",)),
        (("signal_id",), "signals", ("id",)),
    },
}

# name -> (columns, {column: sort})
EXPECTED_INDEXES: dict[str, dict[str, tuple[list[str], dict[str, str]]]] = {
    "postings": {
        "postings_board_id_fingerprint_idx": (["board_id", "fingerprint"], {}),
        "postings_role_family_posted_at_idx": (["role_family", "posted_at"], {}),
    },
    "events": {
        "events_company_id_occurred_at_idx": (
            ["company_id", "occurred_at"],
            {"occurred_at": "desc"},
        ),
        "events_type_occurred_at_idx": (["type", "occurred_at"], {"occurred_at": "desc"}),
    },
}

EXPECTED_NULLABLE: dict[str, set[str]] = {
    "companies": {"legal_name", "cik", "hq_city", "hq_state", "hq_country", "github_org"},
    "company_aliases": set(),
    "ats_boards": {"company_id", "discovered_from", "last_verified_at", "last_polled_at"},
    "postings": {
        "department",
        "location",
        "remote",
        "posted_at",
        "closed_at",
        "repost_of",
        "content_hash",
    },
    "posting_observations": set(),
    "events": {"company_id", "raw_ref"},
    "signals": {"role_family"},
    "scores": set(),
    "watchlists": set(),
    "watchlist_items": set(),
    "feedback": {"note"},
    "runs": {"finished_at", "fetched", "inserted", "skipped", "errors", "notes"},
    "review_queue": {"resolved_at"},
}

# Columns with a server default, with the default's leading token after cast stripping.
EXPECTED_SERVER_DEFAULTS: dict[tuple[str, str], str] = {
    ("companies", "created_at"): "now()",
    ("companies", "updated_at"): "now()",
    ("ats_boards", "status"): "'active'",
    ("events", "magnitude"): "1",
    ("watchlists", "icp_profile"): "'default'",
    ("feedback", "created_at"): "now()",
    ("runs", "fetched"): "0",
    ("runs", "inserted"): "0",
    ("runs", "skipped"): "0",
    ("runs", "errors"): "0",
    ("review_queue", "status"): "'open'",
    ("review_queue", "created_at"): "now()",
}

EXPECTED_TYPES: dict[tuple[str, str], str] = {
    ("companies", "id"): "BIGINT",
    ("companies", "domain"): "TEXT",
    ("companies", "created_at"): "TIMESTAMP WITH TIME ZONE",
    ("ats_boards", "confidence"): "NUMERIC(3, 2)",
    ("postings", "remote"): "BOOLEAN",
    ("events", "magnitude"): "NUMERIC",
    ("events", "payload"): "JSONB",
    ("signals", "half_life_days"): "INTEGER",
    ("scores", "as_of"): "DATE",
    ("runs", "notes"): "JSONB",
}

SERIAL_PRIMARY_KEYS = {
    "companies",
    "ats_boards",
    "postings",
    "signals",
    "watchlists",
    "feedback",
    "review_queue",
}


def _include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    # tests/test_db.py creates scratch tables named p0_* for the Phase 0 helpers; they are
    # not part of the schema under test and may coexist in the same temporary database.
    _ = parent_names
    return not (type_ == "table" and name is not None and name.startswith("p0_"))


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(
        conn,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_name": _include_name,
        },
    )
    return list(compare_metadata(ctx, Base.metadata))


def _schema_tables(conn: Connection) -> set[str]:
    names = set(inspect(conn).get_table_names())
    return {n for n in names if not n.startswith("p0_")}


# --- (a) parity --------------------------------------------------------------------------


def test_models_match_applied_migration(engine: Engine) -> None:
    with engine.connect() as conn:
        assert _diff(conn) == []


# --- (b) upgrade head twice --------------------------------------------------------------


def test_upgrade_head_twice_is_a_noop(engine: Engine, test_database_url: str) -> None:
    with engine.connect() as conn:
        before_version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        before_tables = _schema_tables(conn)
    assert before_version == HEAD_REVISION

    command.upgrade(alembic_config(test_database_url), "head")

    with engine.connect() as conn:
        after_version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert after_version == before_version
        assert _schema_tables(conn) == before_tables
        assert _diff(conn) == []


# --- (c) explicit expectations from the spec -----------------------------------------------


def test_every_table_and_column_from_the_spec(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        assert _schema_tables(conn) == set(EXPECTED_COLUMNS) | {"alembic_version"}
        for table, expected in EXPECTED_COLUMNS.items():
            actual = [c["name"] for c in insp.get_columns(table)]
            assert actual == expected, table


def test_models_declare_exactly_the_spec_tables_and_columns() -> None:
    assert set(Base.metadata.tables) == set(EXPECTED_COLUMNS)
    for table, expected in EXPECTED_COLUMNS.items():
        assert [c.name for c in Base.metadata.tables[table].columns] == expected, table
    assert [m.__tablename__ for m in models.ALL_MODELS] == list(EXPECTED_COLUMNS)


def test_primary_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        for table, expected in EXPECTED_PRIMARY_KEYS.items():
            pk = insp.get_pk_constraint(table)
            assert pk["constrained_columns"] == expected, table
            assert pk["name"] == f"{table}_pkey"


def test_serial_primary_keys_have_sequences(engine: Engine) -> None:
    with engine.connect() as conn:
        for table in SERIAL_PRIMARY_KEYS:
            default = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'id'"
                ),
                {"t": table},
            ).scalar_one()
            assert default is not None, table
            assert default.startswith("nextval("), table


def test_unique_constraints(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        for table in EXPECTED_COLUMNS:
            actual = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}
            assert actual == EXPECTED_UNIQUES.get(table, set()), table


def test_foreign_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        for table in EXPECTED_COLUMNS:
            actual = {
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in insp.get_foreign_keys(table)
            }
            assert actual == EXPECTED_FOREIGN_KEYS.get(table, set()), table


def test_indexes(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        for table in EXPECTED_COLUMNS:
            indexes = [ix for ix in insp.get_indexes(table) if not ix.get("duplicates_constraint")]
            actual = {
                ix["name"]: (
                    list(ix["column_names"]),
                    {k: v[0] for k, v in ix.get("column_sorting", {}).items()},
                )
                for ix in indexes
            }
            assert actual == EXPECTED_INDEXES.get(table, {}), table
            assert all(not ix["unique"] for ix in indexes), table


def test_nullability(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        for table, expected in EXPECTED_NULLABLE.items():
            actual = {c["name"] for c in insp.get_columns(table) if c["nullable"]}
            assert actual == expected, table


def test_server_defaults(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        seen: dict[tuple[str, str], str] = {}
        for table in EXPECTED_COLUMNS:
            for col in insp.get_columns(table):
                default = col.get("default")
                if default is None or (col["name"] == "id" and "nextval(" in default):
                    continue
                seen[(table, col["name"])] = default.split("::")[0]
        assert seen == EXPECTED_SERVER_DEFAULTS


def test_column_types(engine: Engine) -> None:
    with engine.connect() as conn:
        insp = inspect(conn)
        for (table, column), expected in EXPECTED_TYPES.items():
            col = next(c for c in insp.get_columns(table) if c["name"] == column)
            rendered = col["type"].compile(dialect=postgresql.dialect())
            assert rendered == expected, (table, column)


# --- constants and repr ---------------------------------------------------------------------


def test_allowed_value_constants() -> None:
    assert models.ATS_BOARD_STATUSES == ("active", "no_board", "dead", "paused", "unsupported")
    assert set(models.ATS_VENDORS) >= {"greenhouse", "lever", "ashby", "smartrecruiters"}
    assert "unsupported" in models.ATS_BOARD_STATUSES
    assert models.RUN_STATUSES == ("running", "ok", "failed")
    assert models.REVIEW_QUEUE_STATUSES == ("open", "resolved", "rejected")
    assert models.REVIEW_QUEUE_KINDS == ("entity_match", "board_verify", "news_classify")
    assert set(models.FEEDBACK_VERDICTS) == {
        "useful",
        "not_useful",
        "already_knew",
        "worked",
        "booked",
        "won",
    }
    assert models.ALIAS_SOURCES == ("ats_board", "edgar", "news", "manual")
    assert len(models.SIGNAL_TYPES) == 6


def test_repr_shows_primary_key() -> None:
    assert repr(models.Company(id=7, domain="acme.com", name="Acme")) == "<Company id=7>"
    alias = models.CompanyAlias(company_id=7, alias="acme", source="manual")
    assert repr(alias) == "<CompanyAlias company_id=7 alias='acme'>"
    board = models.AtsBoard(vendor="greenhouse", identifier="acme", confidence=Decimal("0.95"))
    assert repr(board) == "<AtsBoard id=None>"
    run = models.Run(run_id="r1", source="x", started_at=datetime.now(UTC), status="running")
    assert repr(run) == "<Run run_id='r1'>"
