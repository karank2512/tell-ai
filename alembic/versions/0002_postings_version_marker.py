"""Add ``postings.version_marker``: the provider change marker last seen for the posting.

``job_updated`` (SPEC section 5, idempotency rule 2) needs the marker from the previous
poll. Deriving it from ``events`` breaks once events older than 180 days are pruned,
so the posting row keeps it. See ``docs/decisions/0013-postings-version-marker.md``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05 16:40:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("postings", sa.Column("version_marker", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("postings", "version_marker")
