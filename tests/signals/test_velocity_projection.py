"""The velocity loaders read only the ``postings`` columns they consume.

:meth:`_Series.load` and :func:`_postings_by_company` used to select whole
:class:`Posting` rows (title, normalised title, fingerprint, content hash, marker,
location, ...) to read three or six columns of each. These tests pin the projected
shape (the exact column list each query sends to Postgres), prove that consuming the
rows emits no further SQL (no ORM instance is left to lazy-load a deferred column),
and check the results against the same computation over full rows.

What they do not cover: the size of the read itself (no byte counts are measured
here), and query plans, which the projection does not change.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tell.collectors.base import STALE_AFTER, stale_weight
from tell.models import AtsBoard, Company, Posting
from tell.signals import velocity as vel

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)

OPEN_COLUMNS = {"posted_at", "first_seen_at", "closed_at"}
"""What :func:`_open_weight` (through ``stale_weight``) reads of a posting."""

ROLE_COLUMNS = OPEN_COLUMNS | {"title", "url", "external_id"}
"""What :func:`_long_open` reads on top, for the account page's long-open roles."""


# --- helpers ------------------------------------------------------------------------------------


@contextmanager
def _statements(session: Session) -> Iterator[list[str]]:
    """Every statement the engine sends while active."""
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _posting_selects(statements: list[str]) -> list[str]:
    """The select lists of the statements that read ``postings``."""
    out = []
    for sql in statements:
        head, sep, tail = sql.partition("\nFROM ")
        if sep and tail.startswith("postings"):
            out.append(head.removeprefix("SELECT "))
    return out


def _columns(select_list: str, table: str) -> set[str]:
    return set(re.findall(rf"\b{table}\.(\w+)", select_list))


def _company(session: Session, n: int, *, board: bool = True) -> Company:
    company = Company(domain=f"c{n}.test", name=f"C{n}")
    session.add(company)
    session.flush()
    if board:
        session.add(
            AtsBoard(
                vendor="greenhouse",
                identifier=f"c{n}",
                confidence=Decimal("0.95"),
                company_id=company.id,
            )
        )
        session.flush()
    return company


def _posting(
    session: Session,
    company: Company,
    external_id: str,
    *,
    posted_at: datetime | None,
    first_seen_at: datetime,
    closed_at: datetime | None = None,
    title: str = "Account Executive",
) -> None:
    board = session.scalars(select(AtsBoard).where(AtsBoard.company_id == company.id)).one()
    session.add(
        Posting(
            board_id=board.id,
            external_id=external_id,
            fingerprint=f"fp-{external_id}",
            title=title,
            title_norm=title.lower(),
            role_family="sales",
            department="Sales",
            location="New York, NY",
            remote=False,
            posted_at=posted_at,
            first_seen_at=first_seen_at,
            last_seen_at=T0,
            closed_at=closed_at,
            url=f"https://boards.greenhouse.io/{board.identifier}/jobs/{external_id}",
            content_hash="h" * 64,
            version_marker="v1",
        )
    )
    session.flush()


@pytest.fixture
def companies(db_session: Session) -> tuple[Company, Company, Company]:
    """Two companies with boards covering every weight and closure case, one without."""
    acme, beta, bare = (
        _company(db_session, 1),
        _company(db_session, 2),
        _company(db_session, 3, board=False),
    )
    seen = T0 - 120 * DAY
    _posting(db_session, acme, "fresh", posted_at=T0 - 10 * DAY, first_seen_at=seen)
    _posting(
        db_session,
        acme,
        "stale",
        posted_at=T0 - 100 * DAY,
        first_seen_at=seen,
        title="Platform Engineer",
    )
    _posting(db_session, acme, "gone", posted_at=T0 - 200 * DAY, first_seen_at=T0 - 200 * DAY)
    # No provider date: the first sighting dates it (stale by first_seen_at).
    _posting(
        db_session,
        acme,
        "undated",
        posted_at=None,
        first_seen_at=T0 - 95 * DAY,
        title="Data Engineer",
    )
    # Closed after T0: still open at T0. Closed before T0: open two days earlier only.
    _posting(
        db_session,
        acme,
        "closing",
        posted_at=T0 - 100 * DAY,
        first_seen_at=seen,
        closed_at=T0 + DAY,
    )
    _posting(
        db_session, acme, "closed", posted_at=T0 - 100 * DAY, first_seen_at=seen, closed_at=T0 - DAY
    )
    # Seen after T0: not yet open at T0.
    _posting(db_session, acme, "future", posted_at=None, first_seen_at=T0 + DAY)
    _posting(db_session, beta, "only", posted_at=T0 - 91 * DAY, first_seen_at=seen)
    return acme, beta, bare


# --- the reference: the same arithmetic over whole Posting rows -----------------------------


def _full_rows(session: Session, company_id: int) -> list[Posting]:
    stmt = (
        select(Posting)
        .join(AtsBoard, AtsBoard.id == Posting.board_id)
        .where(AtsBoard.company_id == company_id)
        .order_by(Posting.id)
    )
    return list(session.scalars(stmt))


def _reference_open(session: Session, company_id: int, t: datetime) -> float:
    return sum(
        stale_weight(p, t)
        for p in _full_rows(session, company_id)
        if p.first_seen_at <= t and (p.closed_at is None or p.closed_at > t)
    )


def _reference_long_open(
    session: Session, company_id: int, now: datetime
) -> list[vel.LongOpenRole]:
    roles = []
    for p in _full_rows(session, company_id):
        if p.closed_at is not None:
            continue
        age = now - (p.posted_at or p.first_seen_at)
        if age <= STALE_AFTER:
            continue
        roles.append(
            vel.LongOpenRole(
                title=p.title,
                url=p.url,
                external_id=p.external_id,
                posted_at=p.posted_at,
                days_open=age.days,
                weight=stale_weight(p, now),
            )
        )
    return sorted(roles, key=lambda r: (-r.days_open, r.title, r.external_id))


# --- tests ----------------------------------------------------------------------------------


def test_batch_loaders_match_full_row_results(
    db_session: Session, companies: tuple[Company, Company, Company]
) -> None:
    acme, beta, bare = companies
    ids = [acme.id, beta.id, bare.id]
    for t in (T0, T0 - 2 * DAY):
        assert vel.open_postings_many(db_session, ids, t) == {
            cid: _reference_open(db_session, cid, t) for cid in ids
        }
    # fresh 1.0 + stale 0.25 + undated 0.25 + closing 0.25; gone 0.0; closed and future out.
    assert vel.open_postings_many(db_session, ids, T0) == {
        acme.id: 1.75,
        beta.id: 0.25,
        bare.id: 0.0,
    }
    # Two days earlier "closed" was still open (0.25 more).
    assert vel.open_postings_many(db_session, ids, T0 - 2 * DAY)[acme.id] == 2.0

    roles = vel.long_open_roles_many(db_session, ids, T0)
    assert roles == {cid: _reference_long_open(db_session, cid, T0) for cid in ids}
    assert [r.external_id for r in roles[acme.id]] == ["gone", "stale", "undated"]
    assert roles[acme.id][2].posted_at is None
    assert all(r.posted_at.tzinfo is not None for r in roles[acme.id] if r.posted_at is not None)
    assert roles[bare.id] == []


def test_series_open_postings_match_full_row_results(
    db_session: Session, companies: tuple[Company, Company, Company]
) -> None:
    acme, _, _ = companies
    start = T0 - timedelta(hours=12)  # after "closed" closed (T0 - 1 day): that row is out
    series = vel._Series.load(db_session, acme.id, "sales", start, T0)
    # Same rows as the full query, same filter on closed_at, aware timestamps throughout.
    expected = sorted(
        (
            (p.posted_at, p.first_seen_at, p.closed_at)
            for p in _full_rows(db_session, acme.id)
            if p.closed_at is None or p.closed_at > start
        ),
        key=str,
    )
    got = sorted(((p.posted_at, p.first_seen_at, p.closed_at) for p in series.postings), key=str)
    assert got == expected
    assert len(got) == 6  # every posting of acme but "closed"
    assert all(p.first_seen_at.tzinfo is not None for p in series.postings)
    assert all(p.closed_at is None or p.closed_at.tzinfo is not None for p in series.postings)
    for t in (T0, start):
        assert series.open_postings(t) == _reference_open(db_session, acme.id, t)
    assert vel.velocity(db_session, acme.id, "sales", T0).open_postings == 1.75


def test_loaders_select_only_the_columns_they_consume(
    db_session: Session, companies: tuple[Company, Company, Company]
) -> None:
    acme, beta, bare = companies
    with _statements(db_session) as seen:
        vel.velocity(db_session, acme.id, "sales", T0)
    (select_list,) = _posting_selects(seen)
    assert _columns(select_list, "postings") == OPEN_COLUMNS
    assert _columns(select_list, "ats_boards") == set()

    for loader in (vel.open_postings_many, vel.long_open_roles_many):
        with _statements(db_session) as seen:
            loader(db_session, [acme.id, beta.id, bare.id], T0)
        (select_list,) = _posting_selects(seen)
        assert _columns(select_list, "postings") == ROLE_COLUMNS
        assert _columns(select_list, "ats_boards") == {"company_id"}


def test_consuming_projected_rows_emits_no_sql(
    db_session: Session, companies: tuple[Company, Company, Company]
) -> None:
    acme, beta, _ = companies
    ids, found = vel._postings_by_company(db_session, [acme.id, beta.id])
    series = vel._Series.load(db_session, acme.id, "sales", T0 - 2 * DAY, T0)
    assert ids == sorted([acme.id, beta.id])
    rows = [*found[acme.id], *found[beta.id], *series.postings]
    assert rows
    # Plain frozen dataclasses, not mapped instances: there is nothing to lazy-load.
    assert not any(isinstance(row, Posting) for row in rows)
    assert not any(hasattr(row, "title_norm") for row in rows)
    with _statements(db_session) as seen:
        vel._open_weight(found[acme.id], T0)
        vel._long_open(found[acme.id], T0)
        series.open_postings(T0)
        series.evaluate(T0)
    assert seen == []


def test_batch_loader_groups_across_chunks(
    db_session: Session,
    companies: tuple[Company, Company, Company],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acme, beta, bare = companies
    ids = [acme.id, beta.id, bare.id]
    expected = vel.open_postings_many(db_session, ids, T0)
    monkeypatch.setattr(vel, "_IN_CHUNK", 1)
    with _statements(db_session) as seen:
        assert vel.open_postings_many(db_session, ids, T0) == expected
    assert len(_posting_selects(seen)) == 3
