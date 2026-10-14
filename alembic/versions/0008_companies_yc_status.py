"""Add ``companies.yc_status`` and ``companies.yc_batch``.

The Form D linking guards (ADR 0034) need what the YC directory says about a company: an
issuer is auto-linked only to an Active company, and its incorporation year has to fit
the company's batch. SPEC section 5's ``companies`` has neither column; the YC seeder
(``tell discover seed-yc``) writes both, as listed in the directory (``Active`` /
``Inactive`` / ``Acquired`` / ``Public``; ``Summer 2021``). Both stay ``NULL`` for
companies that did not come from the directory.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("yc_status", sa.Text(), nullable=True))
    op.add_column("companies", sa.Column("yc_batch", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "yc_batch")
    op.drop_column("companies", "yc_status")
