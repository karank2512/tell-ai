"""Initial schema: the thirteen tables of docs/SPEC.md section 5.

Hand-ordered to follow the DDL in the spec; constraint and index names are
PostgreSQL's defaults, matching ``tell.models.NAMING_CONVENTION`` so that
``alembic.autogenerate.compare_metadata`` against ``tell.models.Base.metadata``
yields no diff after ``upgrade head`` (``tests/test_schema.py``).

Revision ID: 0001
Revises:
Create Date: 2026-10-05 08:10:42+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamptz() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # companies: one row per resolved company, keyed by domain
    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=True),
        sa.Column("cik", sa.Text(), nullable=True),
        sa.Column("hq_city", sa.Text(), nullable=True),
        sa.Column("hq_state", sa.Text(), nullable=True),
        sa.Column("hq_country", sa.Text(), nullable=True),
        sa.Column("github_org", sa.Text(), nullable=True),
        sa.Column("created_at", _timestamptz(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _timestamptz(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="companies_pkey"),
        sa.UniqueConstraint("domain", name="companies_domain_key"),
        sa.UniqueConstraint("cik", name="companies_cik_key"),
    )

    op.create_table(
        "company_aliases",
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name="company_aliases_company_id_fkey"
        ),
        sa.PrimaryKeyConstraint("company_id", "alias", name="company_aliases_pkey"),
    )

    op.create_table(
        "ats_boards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("vendor", sa.Text(), nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("discovered_from", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_verified_at", _timestamptz(), nullable=True),
        sa.Column("last_polled_at", _timestamptz(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="ats_boards_company_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="ats_boards_pkey"),
        sa.UniqueConstraint("vendor", "identifier", name="ats_boards_vendor_identifier_key"),
    )

    # postings: current state of every posting ever seen
    op.create_table(
        "postings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("board_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.Text(), nullable=False),
        sa.Column("role_family", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=True),
        sa.Column("posted_at", _timestamptz(), nullable=True),
        sa.Column("first_seen_at", _timestamptz(), nullable=False),
        sa.Column("last_seen_at", _timestamptz(), nullable=False),
        sa.Column("closed_at", _timestamptz(), nullable=True),
        sa.Column("repost_of", sa.BigInteger(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["board_id"], ["ats_boards.id"], name="postings_board_id_fkey"),
        sa.ForeignKeyConstraint(["repost_of"], ["postings.id"], name="postings_repost_of_fkey"),
        sa.PrimaryKeyConstraint("id", name="postings_pkey"),
        sa.UniqueConstraint("board_id", "external_id", name="postings_board_id_external_id_key"),
    )
    op.create_index(
        "postings_board_id_fingerprint_idx", "postings", ["board_id", "fingerprint"], unique=False
    )
    op.create_index(
        "postings_role_family_posted_at_idx",
        "postings",
        ["role_family", "posted_at"],
        unique=False,
    )

    # posting_observations: one row per poll per posting; the audit trail for velocity
    op.create_table(
        "posting_observations",
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", _timestamptz(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["posting_id"], ["postings.id"], name="posting_observations_posting_id_fkey"
        ),
        sa.PrimaryKeyConstraint("posting_id", "observed_at", name="posting_observations_pkey"),
    )

    # events: the one shape every source becomes
    op.create_table(
        "events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("occurred_at", _timestamptz(), nullable=False),
        sa.Column("observed_at", _timestamptz(), nullable=False),
        sa.Column("magnitude", sa.Numeric(), server_default=sa.text("1"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="events_company_id_fkey"),
        sa.PrimaryKeyConstraint("event_id", name="events_pkey"),
    )
    op.create_index(
        "events_company_id_occurred_at_idx",
        "events",
        ["company_id", sa.literal_column("occurred_at DESC")],
        unique=False,
    )
    op.create_index(
        "events_type_occurred_at_idx",
        "events",
        ["type", sa.literal_column("occurred_at DESC")],
        unique=False,
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("role_family", sa.Text(), nullable=True),
        sa.Column("weight", sa.Numeric(), nullable=False),
        sa.Column("magnitude", sa.Numeric(), nullable=False),
        sa.Column("half_life_days", sa.Integer(), nullable=False),
        sa.Column("starts_at", _timestamptz(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="signals_company_id_fkey"),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"], name="signals_event_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="signals_pkey"),
        sa.UniqueConstraint(
            "event_id",
            "signal_type",
            "role_family",
            name="signals_event_id_signal_type_role_family_key",
        ),
    )

    op.create_table(
        "scores",
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("icp_profile", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("score", sa.Numeric(), nullable=False),
        sa.Column("contributions", _jsonb(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="scores_company_id_fkey"),
        sa.PrimaryKeyConstraint("company_id", "icp_profile", "as_of", name="scores_pkey"),
    )

    op.create_table(
        "watchlists",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("icp_profile", sa.Text(), server_default=sa.text("'default'"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="watchlists_pkey"),
    )
    op.create_table(
        "watchlist_items",
        sa.Column("watchlist_id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_id"], ["watchlists.id"], name="watchlist_items_watchlist_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name="watchlist_items_company_id_fkey"
        ),
        sa.PrimaryKeyConstraint("watchlist_id", "company_id", name="watchlist_items_pkey"),
    )

    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("watchlist_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", _timestamptz(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_id"], ["watchlists.id"], name="feedback_watchlist_id_fkey"
        ),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], name="feedback_signal_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="feedback_pkey"),
    )

    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", _timestamptz(), nullable=False),
        sa.Column("finished_at", _timestamptz(), nullable=True),
        sa.Column("fetched", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("inserted", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("skipped", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("errors", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("notes", _jsonb(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name="runs_pkey"),
    )

    op.create_table(
        "review_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", _timestamptz(), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", _timestamptz(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="review_queue_pkey"),
    )


def downgrade() -> None:
    op.drop_table("review_queue")
    op.drop_table("runs")
    op.drop_table("feedback")
    op.drop_table("watchlist_items")
    op.drop_table("watchlists")
    op.drop_table("scores")
    op.drop_table("signals")
    op.drop_index("events_type_occurred_at_idx", table_name="events")
    op.drop_index("events_company_id_occurred_at_idx", table_name="events")
    op.drop_table("events")
    op.drop_table("posting_observations")
    op.drop_index("postings_role_family_posted_at_idx", table_name="postings")
    op.drop_index("postings_board_id_fingerprint_idx", table_name="postings")
    op.drop_table("postings")
    op.drop_table("ats_boards")
    op.drop_table("company_aliases")
    op.drop_table("companies")
