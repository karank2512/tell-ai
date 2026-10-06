"""Add the ``api_keys`` and ``api_usage`` tables.

SPEC section 10 (FastAPI service, v1): bearer keys are ``tell_`` plus 32 random bytes
base64url, stored as SHA-256 in ``api_keys`` with ``owner``, ``created_at``,
``last_used_at`` and ``daily_quota``; metering upserts one ``api_usage (key_id, day,
calls)`` row per key and UTC day before the handler runs. Section 5 lists neither
table. ``key_prefix`` (first 12 characters, for display) and ``revoked_at`` are the
additions recorded in ``docs/decisions/0024-api-keys-tables.md``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("daily_quota", sa.Integer(), server_default=sa.text("1000"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="api_keys_pkey"),
        sa.UniqueConstraint("key_hash", name="api_keys_key_hash_key"),
    )
    op.create_table(
        "api_usage",
        sa.Column("key_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.id"], name="api_usage_key_id_fkey"),
        sa.PrimaryKeyConstraint("key_id", "day", name="api_usage_pkey"),
    )


def downgrade() -> None:
    op.drop_table("api_usage")
    op.drop_table("api_keys")
