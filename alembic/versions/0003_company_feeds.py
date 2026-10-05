"""Add ``company_feeds``: RSS/Atom feeds discovered on a company's own site.

SPEC section 3.7 has the news collector discover each company's blog feed
(``<link rel="alternate">``, then ``/feed``, ``/rss``, ``/blog/rss.xml``,
``/blog/feed``) and cache polls for 6 hours; section 5 has no table to keep the
discovered URL or the conditional-request validators. See
``docs/decisions/0014-company-feeds-table.md``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamptz() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "company_feeds",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column("discovered_from", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_polled_at", _timestamptz(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("created_at", _timestamptz(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name="company_feeds_company_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="company_feeds_pkey"),
        sa.UniqueConstraint("feed_url", name="company_feeds_feed_url_key"),
    )


def downgrade() -> None:
    op.drop_table("company_feeds")
