"""Feedback loop v1: verdict labels and the weekly precision report."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from tell.models import (
    FEEDBACK_VERDICTS,
    Company,
    EventRow,
    Feedback,
    Signal,
    Watchlist,
)
from tell.signals.feedback import (
    MIN_LABELS_FOR_REWEIGHTING,
    NEGATIVE,
    POSITIVE,
    VERDICT_LABELS,
    PrecisionReport,
    PrecisionRow,
    format_markdown,
    label_for,
    precision_report,
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_every_verdict_has_the_spec_label() -> None:
    assert set(VERDICT_LABELS) == set(FEEDBACK_VERDICTS)
    assert {v for v, label in VERDICT_LABELS.items() if label == POSITIVE} == {
        "useful",
        "worked",
        "booked",
        "won",
    }
    assert {v for v, label in VERDICT_LABELS.items() if label == NEGATIVE} == {
        "not_useful",
        "already_knew",
    }
    assert label_for("won") == POSITIVE
    with pytest.raises(KeyError):
        label_for("meh")


def _signal(session: Session, company: Company, signal_type: str, family: str | None) -> Signal:
    event = EventRow(
        event_id=f"ev-{company.id}-{signal_type}-{family}",
        company_id=company.id,
        source="synthetic",
        type=signal_type,
        occurred_at=T0,
        observed_at=T0,
        magnitude=Decimal(1),
        url=f"https://example.test/{signal_type}",
        payload={},
        run_id="r",
    )
    session.add(event)
    session.flush()
    signal = Signal(
        company_id=company.id,
        event_id=event.event_id,
        signal_type=signal_type,
        role_family=family,
        weight=Decimal(1),
        magnitude=Decimal(1),
        half_life_days=30,
        starts_at=T0,
    )
    session.add(signal)
    session.flush()
    return signal


def _watchlist(session: Session, profile: str) -> Watchlist:
    watchlist = Watchlist(owner="team-a", name=f"list-{profile}", icp_profile=profile)
    session.add(watchlist)
    session.flush()
    return watchlist


def _feedback(session: Session, watchlist: Watchlist, signal: Signal, *verdicts: str) -> None:
    session.add_all(
        Feedback(watchlist_id=watchlist.id, signal_id=signal.id, verdict=v) for v in verdicts
    )
    session.flush()


def test_precision_report_groups_by_type_and_family_per_profile(db_session: Session) -> None:
    company = Company(domain="acme.example", name="Acme")
    db_session.add(company)
    db_session.flush()
    funding = _signal(db_session, company, "funding", None)
    sales = _signal(db_session, company, "hiring_velocity", "sales")
    engineering = _signal(db_session, company, "hiring_velocity", "engineering")
    mine = _watchlist(db_session, "default")
    theirs = _watchlist(db_session, "partner-x")
    _feedback(db_session, mine, funding, "useful", "won", "booked", "not_useful")
    _feedback(db_session, mine, sales, "worked", "already_knew")
    _feedback(db_session, mine, engineering, "not_useful", "not_useful")
    _feedback(db_session, theirs, funding, "not_useful", "not_useful", "not_useful")

    report = precision_report(db_session, "default")
    assert report.profile == "default"
    assert [(r.signal_type, r.role_family, r.positives, r.negatives) for r in report.rows] == [
        ("funding", None, 3, 1),
        ("hiring_velocity", "engineering", 0, 2),
        ("hiring_velocity", "sales", 1, 1),
    ]
    assert report.rows[0].precision == 0.75
    assert report.rows[1].precision == 0.0
    assert report.labels == 8
    assert report.positives == 4
    assert report.precision == 0.5
    assert report.note is not None
    assert "weights stay hand-tuned" in report.note
    assert "8 labels" in report.note
    assert str(MIN_LABELS_FOR_REWEIGHTING) in report.note

    other = precision_report(db_session, "partner-x")
    assert [(r.signal_type, r.positives, r.negatives) for r in other.rows] == [("funding", 0, 3)]
    assert precision_report(db_session, "nobody").rows == []


def test_note_disappears_at_three_hundred_labels(db_session: Session) -> None:
    company = Company(domain="acme.example", name="Acme")
    db_session.add(company)
    db_session.flush()
    signal = _signal(db_session, company, "funding", None)
    watchlist = _watchlist(db_session, "default")
    _feedback(db_session, watchlist, signal, *(["useful"] * 150 + ["not_useful"] * 149))
    assert precision_report(db_session, "default").note is not None
    _feedback(db_session, watchlist, signal, "already_knew")
    report = precision_report(db_session, "default")
    assert report.labels == MIN_LABELS_FOR_REWEIGHTING
    assert report.note is None
    assert report.rows[0].precision == 0.5


def test_markdown_table() -> None:
    report = PrecisionReport(
        profile="default",
        rows=[
            PrecisionRow("funding", None, 3, 1),
            PrecisionRow("hiring_velocity", "sales", 1, 2),
        ],
        note="weights stay hand-tuned: 7 labels",
    )
    text = format_markdown(report)
    lines = text.splitlines()
    assert lines[0] == "## Precision by signal type: profile `default`"
    assert lines[2] == "| signal_type | role_family | positives | negatives | precision |"
    assert "| funding | - | 3 | 1 | 75.0% |" in lines
    assert "| hiring_velocity | sales | 1 | 2 | 33.3% |" in lines
    assert "| **all** | | 4 | 3 | 57.1% |" in lines
    assert "Labels: 7." in lines
    assert lines[-1] == "Note: weights stay hand-tuned: 7 labels."
    assert text.endswith("\n")


def test_markdown_table_with_no_labels() -> None:
    empty = PrecisionReport(profile="p", rows=[], note=None)
    text = format_markdown(empty)
    assert "| - | - | 0 | 0 | n/a |" in text
    assert "| **all** | | 0 | 0 | n/a |" in text
    assert "Note:" not in text
    assert PrecisionRow("funding", None, 0, 0).precision is None
