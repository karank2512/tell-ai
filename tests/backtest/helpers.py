"""Row builders for the backtest tests (synthetic companies, boards and events; no PII)."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from tell.models import AtsBoard, Company, EventRow, Posting, Run

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


def company(session: Session, domain: str, *, cik: str | None = None) -> Company:
    row = Company(domain=domain, name=domain.split(".", maxsplit=1)[0].title(), cik=cik)
    session.add(row)
    session.flush()
    return row


def board(
    session: Session,
    owner: Company | None,
    identifier: str,
    *,
    vendor: str = "greenhouse",
    status: str = "active",
) -> AtsBoard:
    row = AtsBoard(
        company_id=owner.id if owner is not None else None,
        vendor=vendor,
        identifier=identifier,
        confidence=Decimal("0.95"),
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def posting(
    session: Session,
    on: AtsBoard,
    external_id: str,
    *,
    first_seen: datetime,
    posted: datetime | None = None,
    closed: datetime | None = None,
    family: str = "sales",
    fingerprint: str | None = None,
    repost_of: Posting | None = None,
) -> Posting:
    row = Posting(
        board_id=on.id,
        external_id=external_id,
        fingerprint=fingerprint or f"fp-{external_id}",
        title="Account Executive",
        title_norm="account executive",
        role_family=family,
        posted_at=posted,
        first_seen_at=first_seen,
        last_seen_at=closed or first_seen,
        closed_at=closed,
        repost_of=repost_of.id if repost_of is not None else None,
        url=f"https://boards.greenhouse.io/{on.identifier}/jobs/{external_id}",
    )
    session.add(row)
    session.flush()
    return row


def event(
    session: Session,
    type_: str,
    occurred: datetime,
    *,
    owner: Company | None,
    observed: datetime | None = None,
    payload: dict[str, Any] | None = None,
    source: str = "edgar",
) -> EventRow:
    row = EventRow(
        event_id=secrets.token_hex(16),
        company_id=owner.id if owner is not None else None,
        source=source,
        type=type_,
        occurred_at=occurred,
        observed_at=observed or occurred,
        magnitude=Decimal(1),
        url=f"https://example.invalid/{type_}/{secrets.token_hex(4)}",
        payload=payload or {},
        run_id="backtest-test",
    )
    session.add(row)
    session.flush()
    return row


def formd(
    session: Session,
    owner: Company | None,
    filed: date,
    *,
    observed: datetime | None = None,
    rule: str | None = "R2",
    amendment: bool = False,
    backfill: bool = False,
    cik: str = "0000000001",
    accession: str | None = None,
) -> EventRow:
    payload: dict[str, Any] = {
        "is_amendment": amendment,
        "cik": cik,
        "accession_number": accession or f"0000000001-26-{secrets.randbelow(999999):06d}",
    }
    if rule is not None:
        payload["match"] = {"rule": rule, "confidence": 0.95}
    if backfill:
        payload["backfill"] = True
    return event(
        session,
        "form_d_filed",
        midnight(filed),
        owner=owner,
        observed=observed or midnight(filed) + 8 * HOUR,
        payload=payload,
    )


def news(session: Session, owner: Company, at: datetime, type_: str = "news_funding") -> EventRow:
    return event(
        session, type_, at, owner=owner, payload={"title": "raises a round"}, source="news"
    )


def job_opened(
    session: Session,
    owner: Company,
    at: datetime,
    fingerprint: str,
    *,
    family: str = "sales",
    first_poll: bool = False,
    observed: datetime | None = None,
    external_id: str | None = None,
    **extra: Any,
) -> EventRow:
    payload: dict[str, Any] = {
        "vendor": "greenhouse",
        "board_identifier": f"board-{owner.id}",
        "posting_external_id": external_id or fingerprint,
        "title": "Software Engineer",
        "role_family": family,
        "posted_at": at.isoformat(),
        "fingerprint": fingerprint,
        "is_intern": False,
        "repost_of_external_id": None,
        **extra,
    }
    if first_poll:
        payload["first_poll"] = True
    return event(
        session,
        "job_opened",
        at,
        owner=owner,
        observed=observed,
        payload=payload,
        source="greenhouse",
    )


def run(
    session: Session,
    source: str,
    started: datetime,
    status: str = "ok",
    *,
    notes: dict[str, Any] | None = None,
) -> Run:
    row = Run(
        run_id=f"{source}-{started:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}",
        source=source,
        started_at=started,
        finished_at=None if status == "running" else started + timedelta(minutes=3),
        status=status,
        notes=notes,
    )
    session.add(row)
    session.flush()
    return row
