"""Add the ``nudges`` table (per-event Slack nudges, ADR 0036).

A nudge is one Slack message about one ``score_crossed_threshold`` event sent to one
watchlist's own webhook, as soon as the ingest run that scored it finishes (the digest
stays a once-a-day summary). The table is what makes the nudge idempotent across the up
to four ingest runs a day: a row with ``status = 'ok'`` means the message went out, a
``failed`` row is retried on the next run while the event is still inside the 24-hour
window. One row per (event, watchlist): a retry updates the row rather than adding one.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nudges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("watchlist_id", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"], name="nudges_event_id_fkey"),
        sa.ForeignKeyConstraint(
            ["watchlist_id"], ["watchlists.id"], name="nudges_watchlist_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="nudges_pkey"),
        sa.UniqueConstraint("event_id", "watchlist_id", name="nudges_event_id_watchlist_id_key"),
    )


def downgrade() -> None:
    op.drop_table("nudges")
