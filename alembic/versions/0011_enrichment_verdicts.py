"""Add the ``enrichment_verdicts`` ledger (enrichment gate v0, ADR 0038).

One row per (crossing event, watchlist): the gate's verdict (``enrich`` / ``skip`` /
``hold``), the reasons it gave and the signal families it saw, decided when the nudge
for that crossing was built. The ledger is what makes rule 4 (no credit on the same
company within 90 days) and rule 5 (the watchlist's monthly credit budget) possible, and
is the denominator for the acted-on rate once feedback arrives. Nothing here names a
person; tell never enriches and never stores an enrichment result.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 23:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enrichment_verdicts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("watchlist_id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("families", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.event_id"], name="enrichment_verdicts_event_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["watchlist_id"], ["watchlists.id"], name="enrichment_verdicts_watchlist_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name="enrichment_verdicts_company_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="enrichment_verdicts_pkey"),
        sa.UniqueConstraint(
            "event_id", "watchlist_id", name="enrichment_verdicts_event_id_watchlist_id_key"
        ),
    )
    op.create_index(
        "enrichment_verdicts_watchlist_id_company_id_decided_at_idx",
        "enrichment_verdicts",
        ["watchlist_id", "company_id", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "enrichment_verdicts_watchlist_id_company_id_decided_at_idx", table_name="enrichment_verdicts"
    )
    op.drop_table("enrichment_verdicts")
