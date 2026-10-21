"""Add ``companies.public_opt_out`` and ``companies.public_opted_out_at`` (ADR 0044).

A company can ask to be left off tell's public site: it opens a GitHub issue titled
``Opt out: <domain>`` and the operator runs ``tell publish opt-out add DOMAIN``. SPEC
section 5's ``companies`` has nowhere to keep that, so this migration adds two columns:

* ``public_opt_out`` (boolean, not null, default ``false``): every row before this
  migration has not opted out, so the default backfills them.
* ``public_opted_out_at`` (timestamptz, null): when the opt-out was recorded; ``NULL``
  while the company has not opted out.

The site generator leaves an opted-out company off every public page and JSON file; the
private outputs (Slack nudges and digests, the HubSpot push, the CSV) do not read either
column.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "public_opt_out", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "companies",
        sa.Column("public_opted_out_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "public_opted_out_at")
    op.drop_column("companies", "public_opt_out")
