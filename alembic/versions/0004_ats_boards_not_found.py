"""Add ``ats_boards.not_found_count`` and ``ats_boards.first_not_found_at``.

SPEC section 4 step 5 re-verifies a board "whenever a board returns 404 twice in a
row" and section 5 rule 3 closes a posting only after two consecutive polls without
it. Section 5's ``ats_boards`` has no column for a 404 streak, so the four ATS
collectors each inferred it from ``last_polled_at`` / ``last_verified_at`` or from a
review item. These two columns hold the streak for all of them. See
``docs/decisions/0015-ats-404-and-reverification.md``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ats_boards",
        sa.Column("not_found_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "ats_boards",
        sa.Column("first_not_found_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ats_boards", "first_not_found_at")
    op.drop_column("ats_boards", "not_found_count")
