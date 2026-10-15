"""The CSV with receipts (spec section 9): the "send me 50 domains" deliverable.

One row per domain, columns exactly :data:`CSV_COLUMNS` in that order:

=============================  ==================================================================
column                         content
=============================  ==================================================================
``domain``, ``name``           the resolved company
``score``                      raw score, one decimal
``heat``                       0-100 percentile of the score within the exported list
``top_signal_N``               ``{type}[ ({role_family})]: {summary}`` of the N-th contribution
``top_signal_N_date``          its ``occurred_at`` (ISO date)
``top_signal_N_url``           its receipt
``new_roles_7d_sales``         counted new ``sales`` postings in the 7 days to ``as_of`` (``new7``)
``new_roles_7d_eng``           the same for ``engineering``
``new_roles_7d_other``         the same summed over every other family
``funding_last_180d_usd``      sum of new Form D offering amounts in the 180 days to ``as_of``
                               (integer USD; empty when none or undisclosed)
``funding_url``                the most recent of those filings
``open_postings``              open postings on the company's boards
``long_open_roles``            open postings older than 90 days
``ats``                        ``vendor:identifier`` of the active board, or ``no_board``
``coverage_note``              ``greenhouse:acme`` | ``no_board`` | ``unsupported:workday`` |
                               ``dead:lever:acme`` so gaps are explicit
``account_page``               the public account page URL
``rate_this_row``              the prefilled feedback form link (``TELL_FEEDBACK_FORM_URL``), or empty
=============================  ==================================================================
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.models import Company, EventRow
from tell.publish.json_api import company_facts_many, feedback_link, round_half_up
from tell.signals.profiles import Profile
from tell.signals.scoring import FORM_D_FILED, heat, score_companies, score_instant
from tell.signals.taxonomy import FAMILIES
from tell.signals.velocity import velocity

__all__ = ["CSV_COLUMNS", "FUNDING_WINDOW_DAYS", "csv_rows", "export_csv"]

CSV_COLUMNS: tuple[str, ...] = (
    "domain",
    "name",
    "score",
    "heat",
    "top_signal_1",
    "top_signal_1_date",
    "top_signal_1_url",
    "top_signal_2",
    "top_signal_2_date",
    "top_signal_2_url",
    "new_roles_7d_sales",
    "new_roles_7d_eng",
    "new_roles_7d_other",
    "funding_last_180d_usd",
    "funding_url",
    "open_postings",
    "long_open_roles",
    "ats",
    "coverage_note",
    "account_page",
    "rate_this_row",
)
"""Spec section 9's column list, in order."""

FUNDING_WINDOW_DAYS = 180
_ENG = "engineering"
_SALES = "sales"


def _signal_label(item: dict[str, Any]) -> str:
    kind = str(item["signal_type"])
    family = item.get("role_family")
    head = f"{kind} ({family})" if family else kind
    return f"{head}: {item['summary']}"


def _funding(session: Session, company_id: int, as_of: date) -> tuple[int | None, str]:
    end = score_instant(as_of)
    stmt = (
        select(EventRow.payload, EventRow.url, EventRow.occurred_at)
        .where(
            EventRow.company_id == company_id,
            EventRow.type == FORM_D_FILED,
            EventRow.occurred_at > end - timedelta(days=FUNDING_WINDOW_DAYS),
            EventRow.occurred_at <= end,
        )
        .order_by(EventRow.occurred_at.desc(), EventRow.event_id)
    )
    total: int | None = None
    url = ""
    for payload, event_url, _ in session.execute(stmt):
        if (payload or {}).get("is_amendment") is True:
            continue
        url = url or event_url
        amount = (payload or {}).get("total_offering_amount")
        if isinstance(amount, int | float) and not isinstance(amount, bool) and amount > 0:
            total = (total or 0) + round(amount)
    return total, url


def csv_rows(
    session: Session, company_ids: Sequence[int], profile: Profile, as_of: date
) -> list[dict[str, str]]:
    """The CSV rows for ``company_ids`` (highest score first, then domain)."""
    ids = sorted(set(company_ids))
    if not ids:
        return []
    settings = get_settings()
    base = settings.tell_base_url.rstrip("/")
    t = score_instant(as_of)
    results = {r.company_id: r for r in score_companies(session, profile, as_of, ids, write=False)}
    heats = heat({cid: r.score for cid, r in results.items()})
    companies = {c.id: c for c in session.scalars(select(Company).where(Company.id.in_(ids)))}
    facts_by_id = company_facts_many(session, list(companies.values()), t)
    rows: list[dict[str, str]] = []
    for cid in ids:
        company = companies.get(cid)
        if company is None:
            continue
        result = results[cid]
        facts = facts_by_id[cid]
        new7 = {family: velocity(session, cid, family, t).new7 for family in FAMILIES}
        funding_total, funding_url = _funding(session, cid, as_of)
        tops = result.contributions[:2]
        row: dict[str, str] = {
            "domain": company.domain,
            "name": company.name,
            "score": f"{round_half_up(result.score):.1f}",
            "heat": str(heats.get(cid, 0)),
        }
        for n in (1, 2):
            item = tops[n - 1] if len(tops) >= n else None
            row[f"top_signal_{n}"] = _signal_label(item) if item else ""
            row[f"top_signal_{n}_date"] = str(item["occurred_at"])[:10] if item else ""
            row[f"top_signal_{n}_url"] = str(item["url"]) if item else ""
        row["new_roles_7d_sales"] = str(new7.get(_SALES, 0))
        row["new_roles_7d_eng"] = str(new7.get(_ENG, 0))
        row["new_roles_7d_other"] = str(sum(v for k, v in new7.items() if k not in (_SALES, _ENG)))
        row["funding_last_180d_usd"] = "" if funding_total is None else str(funding_total)
        row["funding_url"] = funding_url
        row["open_postings"] = str(facts.open_postings)
        row["long_open_roles"] = str(len(facts.long_open))
        row["ats"] = facts.ats
        row["coverage_note"] = facts.coverage_note
        row["account_page"] = f"{base}/companies/{company.domain}.html"
        top_id = int(tops[0]["signal_id"]) if tops else None
        row["rate_this_row"] = (
            feedback_link(signal_id=top_id, domain=company.domain, verdict="")
            if settings.tell_feedback_form_url
            else ""
        )
        rows.append(row)
    rows.sort(key=lambda r: (-float(r["score"]), r["domain"]))
    return rows


def export_csv(
    session: Session, path: Path, company_ids: Sequence[int], profile: Profile, as_of: date
) -> int:
    """Write the CSV for ``company_ids`` to ``path``; return the number of data rows."""
    rows = csv_rows(session, company_ids, profile, as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
