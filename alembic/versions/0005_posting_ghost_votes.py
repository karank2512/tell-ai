"""Add the ``posting_ghost_votes`` table.

SPEC section 7 (stale requisitions): job-seeker thumbs ("this looks like a ghost
job") feed a ``ghost_votes`` count that stays inert until there are 500 votes.
Section 5 has nowhere to keep them. One anonymous row per vote: the posting, when,
and through which surface. See ``docs/decisions/0017-posting-ghost-votes.md``.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05 22:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "posting_ghost_votes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("voted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["posting_id"], ["postings.id"], name="posting_ghost_votes_posting_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="posting_ghost_votes_pkey"),
    )
    op.create_index(
        "posting_ghost_votes_posting_id_idx", "posting_ghost_votes", ["posting_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("posting_ghost_votes_posting_id_idx", table_name="posting_ghost_votes")
    op.drop_table("posting_ghost_votes")
