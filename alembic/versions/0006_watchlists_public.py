"""Add ``watchlists.slug`` and ``watchlists.is_public``.

SPEC section 9 publishes ``api/v0/watchlists/{slug}.json`` "only for watchlists marked
public", but section 5's ``watchlists`` has neither a slug nor a public flag. ``slug``
is nullable and unique (a list gets one when it is published); ``is_public`` defaults to
false so every existing list stays private. See
``docs/decisions/0022-public-watchlists.md``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("watchlists", sa.Column("slug", sa.Text(), nullable=True))
    op.add_column(
        "watchlists",
        sa.Column("is_public", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_unique_constraint("watchlists_slug_key", "watchlists", ["slug"])


def downgrade() -> None:
    op.drop_constraint("watchlists_slug_key", "watchlists", type_="unique")
    op.drop_column("watchlists", "is_public")
    op.drop_column("watchlists", "slug")
