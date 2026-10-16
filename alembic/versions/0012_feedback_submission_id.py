"""Add ``feedback.submission_id`` (feedback form reader, ADR 0039).

``tell intake feedback-sync`` reads the feedback Google Form's published response sheet
and writes one ``feedback`` row per response. The sheet is re-read nightly, so each row
remembers which response it came from: ``submission_id`` is the SHA-256 of
``timestamp|signal_id|verdict|watchlist`` and is unique. Rows written through
``POST /v1/feedback`` keep ``NULL``.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-17 00:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("submission_id", sa.Text(), nullable=True))
    op.create_unique_constraint("feedback_submission_id_key", "feedback", ["submission_id"])


def downgrade() -> None:
    op.drop_constraint("feedback_submission_id_key", "feedback", type_="unique")
    op.drop_column("feedback", "submission_id")
