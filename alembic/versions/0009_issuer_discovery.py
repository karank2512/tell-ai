"""Add ``companies.source`` / ``companies.cik_source`` and the ``issuer_discovery`` table.

Issuer-first discovery (ADR 0035, proposed) creates companies from Form D issuers on site
evidence, and rule R0 links an EDGAR event by its CIK. Both need to know where a company
and its CIK came from, which SPEC section 5's ``companies`` does not record:

* ``companies.source`` (``yc`` / ``issuer_first`` / ``board_first`` / ``manual``), not
  null, default ``yc``: every row before this migration came from the YC directory seed
  (ADR 0009), so the default backfills them.
* ``companies.cik_source`` (``issuer_first_a`` / ``issuer_first_b`` / ``r1`` /
  ``guarded_r2`` / ``manual``), null when unknown. Only a CIK with a source feeds R0.
  Backfilled for existing CIKs where the events say how the CIK was earned: an ``edgar``
  event still linked to the company, carrying the same CIK, whose ``payload.match`` is a
  reviewer's link (``manual``), an R1 link (``r1``), or an R2 link of a Form D that passes
  the ADR 0034 guards today (``guarded_r2``: no ``guard_failed`` recorded; not a pooled
  fund; a Delaware corporation; the company's YC status is Active; the incorporation year
  fits the YC batch). Everything else stays ``NULL`` (untrusted until a trusted link or a
  human sets it).
* ``issuer_discovery``: one row per Form D issuer (CIK) that discovery tried, with its
  status, evidence tier, domain, evidence (``{path, span}`` only), and retry schedule.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The ADR 0034 guards, restated in SQL as of this migration (migrations do not import
# application code, which keeps changing). ``m`` is ``payload->'match'``.
BACKFILL_CIK_SOURCE = r"""
with linked as (
  select c.id as company_id,
         e.type,
         e.payload,
         e.payload->'match' as m,
         e.payload->'match'->>'rule' as rule,
         lower(coalesce(c.yc_status, '')) as yc_status,
         substring(coalesce(c.yc_batch, '') from '((19|20)[0-9]{2})')::int as batch_year
  from companies c
  join events e on e.company_id = c.id and e.source = 'edgar'
  where c.cik is not null and c.cik_source is null
    and e.payload->>'cik' ~ '^[0-9]{1,10}$'
    and lpad(e.payload->>'cik', 10, '0') = lpad(c.cik, 10, '0')
    and jsonb_typeof(e.payload->'match') = 'object'),
facts as (
  select l.*,
         case coalesce(jsonb_typeof(l.m->'guard_failed'), 'null')
           when 'null' then false
           when 'array' then jsonb_array_length(l.m->'guard_failed') > 0
           when 'boolean' then (l.m->>'guard_failed')::bool
           when 'string' then l.m->>'guard_failed' <> ''
           else true end as guard_failed,
         ' ' || regexp_replace(
                  lower(replace(replace(coalesce(l.payload->>'issuer_name', ''), '.', ''),
                                '&', ' ')),
                  '[^a-z0-9_ ]', '', 'g') || ' ' as name_tokens,
         case when jsonb_typeof(l.payload->'year_of_inc') = 'number'
              then (l.payload->>'year_of_inc')::int end as year_of_inc,
         (l.payload ? 'year_of_inc' and jsonb_typeof(l.payload->'year_of_inc') = 'null')
           as over_five_years,
         case when coalesce(l.payload->>'date_filed', '') ~ '^[0-9]{4}-'
              then substring(l.payload->>'date_filed' from 1 for 4)::int end as filed_year
  from linked l),
earned as (
  select company_id,
         case rule when 'manual' then 'manual' when 'R1' then 'r1' else 'guarded_r2' end
           as cik_source,
         case rule when 'manual' then 0 when 'R1' then 1 else 2 end as priority
  from facts
  where rule in ('manual', 'R1')
     or (rule = 'R2'
         and type in ('form_d_filed', 'form_d_amendment')
         and not guard_failed
         and lower(coalesce(payload->>'industry_group_type', '')) <> 'pooled investment fund'
         and name_tokens !~ ' (fund|funds|lp|spv|master|feeder|trust) '
         and position(' a series of ' in regexp_replace(name_tokens, '\s+', ' ', 'g')) = 0
         and lower(coalesce(payload->>'entity_type', '')) = 'corporation'
         and lower(coalesce(payload->>'jurisdiction_of_inc', '')) = 'delaware'
         and yc_status = 'active'
         and (batch_year is null or year_of_inc is null or year_of_inc <= batch_year)
         and not (batch_year is not null and over_five_years
                  and batch_year >= coalesce(filed_year - 2, 2024)))),
best as (
  select distinct on (company_id) company_id, cik_source
  from earned
  order by company_id, priority)
update companies c set cik_source = best.cik_source
from best where best.company_id = c.id
"""


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("source", sa.Text(), server_default=sa.text("'yc'"), nullable=False),
    )
    op.add_column("companies", sa.Column("cik_source", sa.Text(), nullable=True))
    op.execute(BACKFILL_CIK_SOURCE)
    op.create_table(
        "issuer_discovery",
        sa.Column("cik", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_tried_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cik", name="issuer_discovery_pkey"),
    )
    op.create_index(
        "issuer_discovery_status_next_retry_at_idx",
        "issuer_discovery",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("issuer_discovery_status_next_retry_at_idx", table_name="issuer_discovery")
    op.drop_table("issuer_discovery")
    op.drop_column("companies", "cik_source")
    op.drop_column("companies", "source")
