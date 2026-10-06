"""Weekly edition: ISO weeks, the two top-25 lists, the method line and coverage numbers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tell.collectors.base import Event, write_events
from tell.models import AtsBoard, Company, Posting, PostingObservation
from tell.publish.weekly import (
    NO_METHOD_CHANGE,
    IsoWeek,
    build_weekly,
    coverage,
    method_change_line,
    resolve_week,
    top_funding,
    top_hiring,
)

W40 = IsoWeek(2026, 40)


def test_iso_weeks() -> None:
    assert W40.label == "2026-W40"
    assert W40.monday == date(2026, 9, 28)
    assert W40.start == datetime(2026, 9, 28, tzinfo=UTC)
    assert W40.end == datetime(2026, 10, 5, tzinfo=UTC)
    assert IsoWeek.parse("2026-W40") == W40
    assert IsoWeek.containing(date(2026, 10, 4)) == W40
    assert IsoWeek(2026, 1).previous() == IsoWeek(2025, 52)
    assert IsoWeek(2021, 1).previous() == IsoWeek(2020, 53)
    # "current" is the most recent complete week: on Monday 06:23 UTC that is last week.
    assert resolve_week("current", datetime(2026, 10, 5, 6, 23, tzinfo=UTC)) == W40
    assert resolve_week("current", datetime(2026, 10, 11, 23, 59, tzinfo=UTC)) == W40
    assert resolve_week("2026-W12", datetime(2026, 10, 5, tzinfo=UTC)) == IsoWeek(2026, 12)
    for bad in ("2026-40", "W40", "2026-W54"):
        with pytest.raises(ValueError):  # noqa: PT011 - the message differs per input
            resolve_week(bad, datetime(2026, 10, 5, tzinfo=UTC))


def test_method_change_line(tmp_path: Path) -> None:
    log = tmp_path / "CHANGELOG.md"
    log.write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n"
        "- Publishing: the static site.\n"
        "- Scoring rule: `funding` magnitude is **capped** at 8.\n"
        "- Velocity: older line.\n"
    )
    assert method_change_line(log) == "Scoring rule: funding magnitude is capped at 8."
    log.write_text("- Nothing relevant.\n* Still nothing (resolutions are plural).\n")
    assert method_change_line(log) == NO_METHOD_CHANGE
    assert method_change_line(tmp_path / "missing.md") == NO_METHOD_CHANGE
    log.write_text("- Resolution " + "x" * 400 + "\n")
    line = method_change_line(log)
    assert len(line) == 280
    assert line.endswith("...")
    assert method_change_line().strip()  # the repository's own changelog


def test_method_change_line_prefers_labelled_bullets_and_sentence_cuts(tmp_path: Path) -> None:
    log = tmp_path / "CHANGELOG.md"
    release = "Phase 7 scoring: " + ", ".join(
        f"`module_{i}` with every scoring rule" for i in range(40)
    )
    log.write_text(f"- {release}\n- Velocity: reposts within 30 days are not new roles.\n")
    assert method_change_line(log) == "Velocity: reposts within 30 days are not new roles."
    sentences = " ".join(f"Scoring moved sentence {i}." for i in range(40))
    log.write_text(f"- {sentences}\n")
    line = method_change_line(log)
    assert len(line) <= 280
    assert line.endswith(".")
    assert not line.endswith("...")
    assert line.startswith("Scoring moved sentence 0.")


def _company(session: Session, n: int, *, cik: str | None = None) -> Company:
    company = Company(domain=f"w{n}.example", name=f"Weekly {n}", cik=cik)
    session.add(company)
    session.flush()
    return company


def _event(
    company: Company,
    kind: str,
    at: datetime,
    payload: dict[str, object],
    *,
    source: str = "tell",
    n: int = 0,
    magnitude: float = 1,
) -> Event:
    return Event(
        source=source,
        type=kind,
        external_id=f"{company.id}:{kind}:{at.isoformat()}:{n}",
        version="1",
        occurred_at=at,
        observed_at=at,
        url=f"https://receipts.example/{company.id}/{kind}/{n}",
        payload=payload,
        run_id="r",
        magnitude=magnitude,
        company_id=company.id,
    )


@pytest.mark.db
def test_top_lists_rank_filter_and_cap(db_session: Session) -> None:
    inside = datetime(2026, 9, 30, 12, tzinfo=UTC)
    outside = datetime(2026, 10, 6, 12, tzinfo=UTC)
    events: list[Event] = []
    companies = [_company(db_session, n, cik=f"{n:010d}") for n in range(30)]
    for n, company in enumerate(companies):
        events.append(
            _event(
                company,
                "hiring_velocity",
                inside,
                {"role_family": "sales", "new7": n % 10, "base": 0.5, "counted_postings": []},
                magnitude=n % 10,
            )
        )
        events.append(
            _event(
                company,
                "form_d_filed",
                inside,
                {
                    "is_amendment": False,
                    "total_offering_amount": n * 1_000_000.0,
                    "total_amount_sold": None,
                    "date_filed": "2026-09-30",
                },
                source="edgar",
            )
        )
    top = companies[0]
    events.append(
        _event(
            top,
            "hiring_velocity",
            inside + timedelta(days=1),
            {
                "role_family": "sales",
                "new7": 9,
                "base": 1.0,
                "counted_postings": [
                    {
                        "title": "AE",
                        "posted_at": "2026-09-29T10:00:00+00:00",
                        "url": "https://jobs.example/ae",
                    },
                    {"title": "no url"},
                ],
            },
            magnitude=9,
            n=1,
        )
    )
    events.append(
        _event(top, "hiring_velocity", inside, {"role_family": "engineering"}, magnitude=10, n=2)
    )
    events.append(
        _event(top, "hiring_velocity", outside, {"role_family": "sales"}, magnitude=10, n=3)
    )
    events.append(
        _event(
            top,
            "form_d_filed",
            inside,
            {"is_amendment": True, "total_offering_amount": 9e12},
            source="edgar",
            n=4,
        )
    )
    unmonitored = _company(db_session, 99)
    events.append(
        _event(
            unmonitored,
            "form_d_filed",
            inside,
            {"is_amendment": False, "total_offering_amount": "5e12"},
            source="edgar",
        )
    )
    write_events(db_session, events)
    db_session.flush()

    hiring = top_hiring(db_session, W40)
    assert len(hiring) == 25
    assert (hiring[0].domain, hiring[0].velocity) == ("w0.example", 9)
    assert [r.velocity for r in hiring[1:4]] == [9, 9, 9]
    assert [r.domain for r in hiring[1:4]] == ["w19.example", "w29.example", "w9.example"]
    receipts = hiring[0].receipts
    assert len(receipts) == 2
    assert receipts[1]["postings"] == [
        {"title": "AE", "posted_at": "2026-09-29", "url": "https://jobs.example/ae"}
    ]
    assert receipts[1]["summary"] == "9 new sales roles in 7 days vs 1.0 baseline"

    funding = top_funding(db_session, W40)
    assert len(funding) == 25
    assert funding[0].domain == "w29.example"
    assert funding[0].amount_usd == 29_000_000
    assert "w99.example" not in [r.domain for r in funding]  # no board, no CIK
    assert "w0.example" not in [r.domain for r in funding[:-1]]
    assert top_funding(db_session, IsoWeek(2026, 39)) == []


@pytest.mark.db
def test_coverage_numbers(db_session: Session) -> None:
    with_board = _company(db_session, 1)
    _company(db_session, 2, cik="0000000002")
    _company(db_session, 3)
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    boards = []
    for n, status in enumerate(("active", "active", "unsupported")):
        board = AtsBoard(
            company_id=with_board.id,
            vendor="greenhouse",
            identifier=f"b{n}",
            confidence=Decimal("0.9"),
            status=status,
            last_polled_at=now if n == 1 else None,
        )
        db_session.add(board)
        boards.append(board)
    db_session.flush()
    posting = Posting(
        board_id=boards[0].id,
        external_id="1",
        fingerprint="f",
        title="AE",
        title_norm="ae",
        role_family="sales",
        first_seen_at=now,
        last_seen_at=now,
        url="https://jobs.example/1",
    )
    db_session.add(posting)
    db_session.flush()
    db_session.add(PostingObservation(posting_id=posting.id, observed_at=now, run_id="r"))
    write_events(
        db_session,
        [
            _event(with_board, "form_8k_filed", now, {}, source="edgar"),
            _event(with_board, "form_d_filed", now - timedelta(days=10), {}, source="edgar"),
        ],
    )
    db_session.flush()
    cov = coverage(db_session, W40)
    assert (cov.companies_monitored, cov.boards_polled, cov.filings_read) == (2, 2, 1)
    data = build_weekly(db_session, W40, changelog=Path("/nonexistent"))
    assert data.method_change == NO_METHOD_CHANGE
    as_dict = data.to_dict()
    assert as_dict["week"] == "2026-W40"
    assert as_dict["coverage"] == {"companies_monitored": 2, "boards_polled": 2, "filings_read": 1}
