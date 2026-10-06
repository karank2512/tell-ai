"""Row builders shared by the ``tell.ops`` and CLI tests (synthetic data only, no PII)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from tell.models import AtsBoard, Company, EventRow, Posting, Run, Signal


def add_run(
    session: Session,
    source: str,
    started: datetime,
    status: str = "ok",
    *,
    minutes: int = 3,
    fetched: int = 1,
    inserted: int = 1,
) -> Run:
    """One finished (or ``running``) ``runs`` row."""
    run = Run(
        run_id=f"{source}-{started:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}",
        source=source,
        started_at=started,
        finished_at=None if status == "running" else started + timedelta(minutes=minutes),
        fetched=fetched,
        inserted=inserted,
        skipped=0,
        errors=0 if status != "failed" else 1,
        status=status,
    )
    session.add(run)
    session.flush()
    return run


def add_company(session: Session, domain: str, *, cik: str | None = None) -> Company:
    company = Company(domain=domain, name=domain.split(".", maxsplit=1)[0].title(), cik=cik)
    session.add(company)
    session.flush()
    return company


def add_board(
    session: Session,
    company: Company | None,
    identifier: str,
    *,
    vendor: str = "greenhouse",
    status: str = "active",
    last_polled_at: datetime | None = None,
) -> AtsBoard:
    board = AtsBoard(
        company_id=company.id if company is not None else None,
        vendor=vendor,
        identifier=identifier,
        confidence=Decimal("0.95"),
        status=status,
        last_polled_at=last_polled_at,
    )
    session.add(board)
    session.flush()
    return board


def add_posting(
    session: Session, board: AtsBoard, external_id: str, seen: datetime, *, closed: bool = False
) -> Posting:
    posting = Posting(
        board_id=board.id,
        external_id=external_id,
        fingerprint=f"fp-{external_id}",
        title="Account Executive",
        title_norm="account executive",
        role_family="sales",
        first_seen_at=seen,
        last_seen_at=seen,
        closed_at=seen if closed else None,
        url=f"https://boards.greenhouse.io/{board.identifier}/jobs/{external_id}",
    )
    session.add(posting)
    session.flush()
    return posting


def add_event(
    session: Session, event_id: str, observed: datetime, *, company: Company | None = None
) -> EventRow:
    row = EventRow(
        event_id=event_id,
        company_id=company.id if company is not None else None,
        source="edgar",
        type="form_d_filed",
        occurred_at=observed - timedelta(hours=6),
        observed_at=observed,
        magnitude=Decimal(1),
        url=f"https://www.sec.gov/Archives/edgar/data/{event_id}/",
        payload={"form_type": "D"},
        run_id="edgar-test",
    )
    session.add(row)
    session.flush()
    return row


def add_signal(session: Session, event: EventRow, company: Company) -> Signal:
    signal = Signal(
        company_id=company.id,
        event_id=event.event_id,
        signal_type="funding",
        role_family=None,
        weight=Decimal("3.0"),
        magnitude=Decimal(1),
        half_life_days=45,
        starts_at=event.occurred_at,
    )
    session.add(signal)
    session.flush()
    return signal
