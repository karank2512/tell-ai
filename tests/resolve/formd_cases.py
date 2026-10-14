"""The 41 Form D links production made on 2026-10-14, as test data (ADR 0034).

``formd_links_20261014.json`` holds, per link, the issuer fields of the event payload
(name, city/state, CIK, previous names, jurisdiction, entity type, year of incorporation,
industry, filing date), the rule that linked it, the linked YC company's directory fields
(name, domain, HQ, batch, status) and the hand-verified verdict (``SAME`` / ``DIFFERENT``
/ ``UNCLEAR``). Company and issuer names only; no person appears in it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from tell.models import Company, EventRow
from tell.resolve.match import backfill_aliases

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
CASES_PATH = Path(__file__).with_name("formd_links_20261014.json")
CASES: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _company(session: Session, domain: str, name: str, **extra: Any) -> Company:
    company = Company(domain=domain, name=name, **extra)
    session.add(company)
    session.flush()
    return company


def seed_cases(session: Session) -> dict[str, Company]:
    companies: dict[str, Company] = {}
    for case in CASES:
        info = case["company"]
        if info["domain"] in companies:
            continue
        companies[info["domain"]] = _company(
            session,
            info["domain"],
            info["name"],
            hq_city=info["hq_city"],
            hq_state=info["hq_state"],
            yc_status=info["yc_status"],
            yc_batch=info["yc_batch"],
        )
    backfill_aliases(session)
    return companies


def add_event(session: Session, case: dict[str, Any], company_id: int | None = None) -> EventRow:
    payload = dict(case["payload"])
    row = EventRow(
        event_id=case["event_id"],
        company_id=company_id,
        source="edgar",
        type=case["type"],
        occurred_at=datetime.fromisoformat(payload["date_filed"]).replace(tzinfo=UTC),
        observed_at=NOW,
        magnitude=Decimal(1),
        url=f"https://example.test/{case['event_id']}",
        payload=payload,
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row
