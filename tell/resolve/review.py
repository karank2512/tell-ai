"""Review-queue workflow for ``entity_match`` items (spec section 6, "Review queue").

The default workflow is a CSV round trip: :func:`export_review_csv` writes the open
items with their top candidates and blank ``verdict`` columns, a human fills
``verdict`` (``link`` | ``create`` | ``reject``) plus ``company_id_or_domain`` /
``new_company_name`` where needed, and :func:`import_review_csv` applies them. The
Streamlit page (:mod:`tell.resolve.review_app`) calls the same :func:`apply_verdict`,
so every rule lives here and is unit-tested without a display.

* ``link``: the event gets ``company_id`` with the same enrichment an auto-link gets
  (:func:`tell.resolve.match.link_event`: ``cik`` / ``legal_name`` when empty, the
  issuer name and every previous name as aliases, so a later filing under a former name
  resolves through R2); the item's name also becomes a ``manual`` alias of that company
  so the pair never comes back; item ``resolved``.
* ``create``: a new ``companies`` row (``domain`` required, ``name`` from
  ``new_company_name`` or the event name), then as ``link``.
* ``reject``: item ``rejected`` with its verdict recorded; the resolver skips the same
  (name, candidates) pair afterwards (:func:`tell.resolve.match.resolve_events`).
"""

from __future__ import annotations

import csv
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc, utcnow
from tell.discovery import registrable_domain
from tell.models import Company, EventRow, ReviewQueueItem
from tell.resolve.match import REVIEW_KIND, add_aliases, link_event

__all__ = [
    "CSV_CANDIDATES",
    "CSV_COLUMNS",
    "VERDICTS",
    "ImportStats",
    "ReviewError",
    "apply_verdict",
    "check_password",
    "export_review_csv",
    "import_review_csv",
    "open_items",
]

Verdict = Literal["link", "create", "reject"]
VERDICTS: tuple[str, ...] = ("link", "create", "reject")

CSV_CANDIDATES = 3
"""Candidates written per CSV row (``candidate_1_*`` .. ``candidate_3_*``)."""

CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "event_id",
    "name",
    "state",
    "city",
    *(
        f"candidate_{n}_{part}"
        for n in range(1, CSV_CANDIDATES + 1)
        for part in ("id", "alias", "ratio")
    ),
    "verdict",
    "company_id_or_domain",
    "new_company_name",
)


class ReviewError(ValueError):
    """A verdict that cannot be applied (unknown item, missing domain, bad verdict)."""


def open_items(session: Session, kind: str = REVIEW_KIND) -> list[ReviewQueueItem]:
    """Open review items of ``kind``, oldest first."""
    stmt = (
        select(ReviewQueueItem)
        .where(ReviewQueueItem.kind == kind, ReviewQueueItem.status == "open")
        .order_by(ReviewQueueItem.id)
    )
    return list(session.scalars(stmt))


def _row(item: ReviewQueueItem) -> dict[str, Any]:
    payload = item.payload
    row: dict[str, Any] = {
        "id": item.id,
        "kind": item.kind,
        "event_id": payload.get("event_id", ""),
        "name": payload.get("name", ""),
        "state": payload.get("state") or "",
        "city": payload.get("city") or "",
        "verdict": "",
        "company_id_or_domain": "",
        "new_company_name": "",
    }
    candidates = payload.get("candidates") or []
    for n in range(1, CSV_CANDIDATES + 1):
        candidate = candidates[n - 1] if n - 1 < len(candidates) else {}
        row[f"candidate_{n}_id"] = candidate.get("company_id", "")
        row[f"candidate_{n}_alias"] = candidate.get("alias", "")
        row[f"candidate_{n}_ratio"] = candidate.get("ratio", "")
    return row


def export_review_csv(session: Session, path: Path | str, kind: str = REVIEW_KIND) -> int:
    """Write the open items of ``kind`` to ``path``; return the number of rows written."""
    items = open_items(session, kind)
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for item in items:
            writer.writerow(_row(item))
    return len(items)


# --- verdicts ---------------------------------------------------------------------------


def _event(session: Session, item: ReviewQueueItem) -> EventRow | None:
    event_id = item.payload.get("event_id")
    return session.get(EventRow, event_id) if isinstance(event_id, str) else None


def _company_from_ref(session: Session, ref: str) -> Company | None:
    """``ref`` is a ``companies.id`` or a domain (``acme.com`` / ``https://www.acme.com``)."""
    text = ref.strip()
    if text.isdigit():
        return session.get(Company, int(text))
    domain = registrable_domain(text)
    if domain is None:
        return None
    return session.scalars(select(Company).where(Company.domain == domain)).first()


def _link(session: Session, item: ReviewQueueItem, company: Company, now: datetime) -> None:
    name = str(item.payload.get("name") or "")
    event = _event(session, item)
    if event is not None:
        link_event(session, event, company.id)
        match = dict(event.payload.get("match") or {})
        match.update(
            {
                "rule": "manual",
                "confidence": 1.0,
                "review_id": item.id,
                "resolved_at": iso_utc(now),
            }
        )
        event.payload = {**event.payload, "match": match}
    if name:
        add_aliases(session, company.id, [name], source="manual")
    item.status = "resolved"
    item.resolved_at = now
    item.payload = {**item.payload, "verdict": "link", "company_id": company.id}


def apply_verdict(
    session: Session,
    item_id: int,
    verdict: str,
    *,
    company_id_or_domain: str | None = None,
    new_company_name: str | None = None,
    now: datetime | None = None,
) -> ReviewQueueItem:
    """Apply one verdict to an open ``entity_match`` item and return it.

    Raises :class:`ReviewError` when the item is unknown or not open, the verdict is not
    ``link`` / ``create`` / ``reject``, ``link`` names no existing company, or
    ``create`` has no domain.
    """
    when = now or utcnow()
    item = session.get(ReviewQueueItem, item_id)
    if item is None or item.kind != REVIEW_KIND:
        msg = f"no entity_match review item with id {item_id}"
        raise ReviewError(msg)
    if item.status != "open":
        msg = f"review item {item_id} is already {item.status}"
        raise ReviewError(msg)
    if verdict not in VERDICTS:
        msg = f"verdict must be one of {', '.join(VERDICTS)}, not {verdict!r}"
        raise ReviewError(msg)
    ref = (company_id_or_domain or "").strip()

    if verdict == "reject":
        item.status = "rejected"
        item.resolved_at = when
        item.payload = {**item.payload, "verdict": "reject"}
    elif verdict == "link":
        company = _company_from_ref(session, ref) if ref else None
        if company is None:
            msg = f"link needs an existing company id or domain, got {ref!r}"
            raise ReviewError(msg)
        _link(session, item, company, when)
    else:
        domain = registrable_domain(ref) if ref else None
        if domain is None:
            msg = f"create needs a domain in company_id_or_domain, got {ref!r}"
            raise ReviewError(msg)
        company = session.scalars(select(Company).where(Company.domain == domain)).first()
        if company is None:
            name = (new_company_name or "").strip() or str(item.payload.get("name") or domain)
            company = Company(domain=domain, name=name)
            session.add(company)
            session.flush()
        _link(session, item, company, when)
    session.flush()
    return item


@dataclass
class ImportStats:
    """Outcome of :func:`import_review_csv`."""

    rows: int = 0
    linked: int = 0
    created: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "linked": self.linked,
            "created": self.created,
            "rejected": self.rejected,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


def _apply_row(session: Session, row: Mapping[str, str], stats: ImportStats, now: datetime) -> None:
    verdict = (row.get("verdict") or "").strip().lower()
    if not verdict:
        stats.skipped += 1
        return
    raw_id = (row.get("id") or "").strip()
    if not raw_id.isdigit():
        stats.errors.append(f"row {stats.rows}: id {raw_id!r} is not a review item id")
        return
    try:
        apply_verdict(
            session,
            int(raw_id),
            verdict,
            company_id_or_domain=row.get("company_id_or_domain"),
            new_company_name=row.get("new_company_name"),
            now=now,
        )
    except ReviewError as exc:
        stats.errors.append(f"row {stats.rows}: {exc}")
        return
    if verdict == "link":
        stats.linked += 1
    elif verdict == "create":
        stats.created += 1
    else:
        stats.rejected += 1


def import_review_csv(
    session: Session, path: Path | str, *, now: datetime | None = None
) -> ImportStats:
    """Apply the verdicts in a CSV written by :func:`export_review_csv` and filled in by a human.

    Rows with a blank verdict are skipped; rows that fail are reported in ``errors`` and
    do not stop the import. The caller owns the transaction.
    """
    when = now or utcnow()
    stats = ImportStats()
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            stats.rows += 1
            _apply_row(session, row, stats, when)
    session.flush()
    return stats


# --- Streamlit gate -------------------------------------------------------------------------


def check_password(candidate: str, expected: SecretStr | str | None) -> bool:
    """Constant-time comparison against ``TELL_REVIEW_PASSWORD``; always ``False`` when unset."""
    if expected is None:
        return False
    secret = expected.get_secret_value() if isinstance(expected, SecretStr) else expected
    if not secret:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8"))
