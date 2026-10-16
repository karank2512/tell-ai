"""Record the HubSpot push on an ``enrich`` verdict (ADR 0040).

When a watchlist's ``tell-private`` record holds a HubSpot token, an ``enrich`` verdict
pushes the company, the receipt note and a task to HubSpot as the nudge goes out. The
ledger row remembers what that did (``crm_action``: ``created`` / ``updated`` /
``unchanged`` / ``failed``, the HubSpot company id, when), so a resend after a failed
Slack POST never pushes twice and a failed push is retried on the next attempt.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-17 02:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("enrichment_verdicts", sa.Column("crm_action", sa.Text(), nullable=True))
    op.add_column("enrichment_verdicts", sa.Column("crm_company_id", sa.Text(), nullable=True))
    op.add_column(
        "enrichment_verdicts",
        sa.Column("crm_pushed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enrichment_verdicts", "crm_pushed_at")
    op.drop_column("enrichment_verdicts", "crm_company_id")
    op.drop_column("enrichment_verdicts", "crm_action")
