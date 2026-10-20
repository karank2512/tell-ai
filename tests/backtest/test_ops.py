"""13.4 ops: unattended weeks, success rate, freshness, coverage, resolution."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tell.backtest import ops
from tell.backtest.common import render_report
from tell.collectors import base as collectors_base
from tell.models import ReviewQueueItem
from tests.backtest.helpers import DAY, HOUR, NOW, board, company, event, midnight, posting, run
from tests.publish.freshness_window import load_window

pytestmark = pytest.mark.db

GOLD_DOC = """\
Measured 2026-10-05 against the gold set.

| Set | n | name cases | negatives (of which supersets) | precision | recall | by rule |
| --- | --- | --- | --- | --- | --- | --- |
| public sample (`gold_sample.csv`), names | 50 | 50 | 45 (30) | 1.0000 | 0.9800 | R2 57 |
| full private set (`companies.csv`), names | 300 | 300 | 270 (180) | 0.9900 | 0.9733 | R2 371 |
| public sample, boards (R1 check) | 50 | 33 | 0 | 1.0000 | 1.0000 | R1 33 |
"""


def _world(session: Session, *, manual: bool) -> None:
    run(
        session, "greenhouse", datetime.fromisoformat("2026-08-01T02:17:00+00:00")
    )  # outside period
    for k in range(3):
        run(session, "greenhouse", NOW - (k + 1) * DAY)
    run(session, "greenhouse", NOW - 4 * DAY, "failed")
    run(session, "edgar", NOW - 2 * DAY)
    run(session, "edgar", NOW - 3 * DAY, notes={"manual": manual})
    run(session, "news", NOW - HOUR, "running")
    # The nightly intake reader (tell-private) is not a pipeline run: never counted, never
    # an anchor, even when marked manual.
    run(session, "intake", NOW - DAY, "failed")
    run(session, "intake", NOW - 2 * DAY, "failed", notes={"manual": True})
    run(session, "intake", NOW - 100 * DAY)

    owner = company(session, "alpha.example")
    company(session, "bravo.example", cik="0000000009")
    company(session, "charlie.example")  # neither board nor CIK
    gh = board(session, owner, "alpha")
    posting(session, gh, "open-1", first_seen=NOW - 5 * DAY)
    posting(session, gh, "closed-1", first_seen=NOW - 5 * DAY, closed=NOW - DAY)

    # Freshness: two filings dated 2026-10-05 are in EDGAR's daily index from 2026-10-06 02:00
    # UTC (filing date + 26 h); recorded 1 h and 3 h later (p95 = 1 + 0.95 * 2 = 2.9 h).
    filed = midnight(date(2026, 10, 5))
    event(session, "form_d_filed", filed, owner=owner, observed=filed + 27 * HOUR)
    event(session, "form_d_filed", filed, owner=owner, observed=filed + 29 * HOUR)
    event(session, "form_d_filed", NOW - 20 * DAY, owner=owner, observed=NOW - 10 * DAY)
    event(session, "form_d_filed", NOW - 40 * DAY, owner=owner, observed=NOW - 40 * DAY)
    session.add_all(
        [
            ReviewQueueItem(kind="entity_match", payload={}, created_at=NOW - 2 * DAY),
            ReviewQueueItem(kind="entity_match", payload={}, created_at=NOW - 60 * DAY),
        ]
    )
    session.flush()


def test_gold_table_from_the_real_methods_page() -> None:
    rows, measured = ops.parse_gold_table()
    assert measured == "2026-10-05"
    assert [r.n for r in rows] == [50, 300]
    assert all(r.precision >= 0.97 and r.recall >= 0.85 for r in rows)


def test_gold_table_missing(tmp_path: Path) -> None:
    assert ops.parse_gold_table(tmp_path / "absent.md") == ([], None)


def test_numbers_with_manual_intervention(db_session: Session, tmp_path: Path) -> None:
    _world(db_session, manual=True)
    doc = tmp_path / "er.md"
    doc.write_text(GOLD_DOC, encoding="utf-8")

    since, manual = ops.manual_anchor(db_session, NOW)
    assert manual
    assert since == NOW - 3 * DAY + HOUR / 20  # finished 3 minutes later
    assert ops.success_by_source(db_session, NOW - ops.PERIOD, NOW) == [
        ("edgar", 2, 2),
        ("greenhouse", 4, 3),
    ]
    assert ops.review_queue_rate(db_session, NOW - ops.PERIOD, NOW) == (1, 3)
    assert ops.freshness_n(db_session, NOW) == 2

    report = ops.build_report(db_session, NOW, gold_doc=doc)
    text = render_report(report)
    assert report.n == 6
    assert "quotable: false" in text
    # (3 days - 3 minutes) / 7 = 0.4283 weeks.
    assert "divided by 7): 0.4 weeks" in text
    assert "Weeks since collection started (period covered by the report): 9.5 weeks" in text
    assert (
        "Success rate ok / (ok + failed), percent, overall: 83.3% (N=6; too small to quote)" in text
    )
    assert f"{ops.FRESHNESS_LABEL}: 2.9 h (N=2; too small to quote)" in text
    assert "Collector events observed in the 24-hour freshness window: 2" in text
    assert "| edgar | 2 | 2 | 0 | 0 | 0 | 2.9 h (N=2; too small to quote) |" in text
    assert "Companies monitored (active board or a CIK): 2" in text
    assert "Postings tracked (closed_at is null): 1" in text
    assert (
        "Review-queue items per 1,000 events, trailing 28 days: 333.33 (N=3; too small to quote)"
        in text
    )
    assert "| public sample (gold_sample.csv), names | 100.0% (N=50) | 98.0% (N=50) |" in text
    assert "Gold set measured 2026-10-05" in text
    assert "the last run marked manual" in text
    assert "[" not in text


def test_without_manual_runs_starts_at_first_run(db_session: Session, tmp_path: Path) -> None:
    _world(db_session, manual=False)
    since, manual = ops.manual_anchor(db_session, NOW)
    assert not manual
    assert since == datetime.fromisoformat("2026-08-01T02:17:00+00:00")
    report = ops.build_report(db_session, NOW, gold_doc=tmp_path / "absent.md")
    text = render_report(report)
    assert "the first run (2026-08-01T02:17:00+00:00)" in text
    assert "No measured gold-set table found" in text


def test_intake_runs_are_not_pipeline_runs(db_session: Session) -> None:
    _world(db_session, manual=False)
    assert ops.first_run_at(db_session, NOW) == datetime.fromisoformat("2026-08-01T02:17:00+00:00")
    assert "intake" not in {
        s for s, _, _ in ops.success_by_source(db_session, NOW - 200 * DAY, NOW)
    }
    report = ops.build_report(db_session, NOW)
    assert report.n == 6
    assert "intake" not in render_report(report).replace("intake reader", "")


def test_run_lifecycle_marker_anchors_the_report(db_session: Session, tmp_path: Path) -> None:
    """The marker the lifecycle writes under ``--manual`` is what ``manual_anchor`` reads."""
    _world(db_session, manual=False)
    collectors_base.set_manual_run(True)
    try:
        run_id = collectors_base.start_run(db_session, "greenhouse", now=NOW - 2 * HOUR)
        row = collectors_base.finish_run(
            db_session, run_id, status="ok", fetched=1, inserted=0, skipped=1, now=NOW - HOUR
        )
    finally:
        collectors_base.set_manual_run(None)
    assert row.notes == {"manual": True}
    since, manual = ops.manual_anchor(db_session, NOW)
    assert (since, manual) == (NOW - HOUR, True)
    text = render_report(ops.build_report(db_session, NOW, gold_doc=tmp_path / "absent.md"))
    assert "the last run marked manual" in text
    assert "divided by 7): 0.0 weeks" in text


def test_empty_database(db_session: Session, tmp_path: Path) -> None:
    assert ops.manual_anchor(db_session, NOW) == (None, False)
    report = ops.build_report(db_session, NOW, gold_doc=tmp_path / "absent.md")
    text = render_report(report)
    assert report.n == 0
    assert "starts at no run yet" in text


def test_report_on_the_2026_10_19_window(db_session: Session, tmp_path: Path) -> None:
    """The window that printed 23298.1 h now prints tell's lag, its N and what was left out."""
    window = load_window(db_session)
    report = ops.build_report(db_session, window.now, gold_doc=tmp_path / "absent.md")
    text = render_report(report)
    assert f"{ops.FRESHNESS_LABEL}: 103.0 h (N=16; too small to quote)" in text
    assert "Collector events observed in the 24-hour freshness window: 34" in text
    assert (
        "Left out of freshness, first looks (tell was not yet watching the board, company feed, "
        "Google News query or source): 15"
    ) in text
    assert (
        "Left out of freshness, first-seen-only (no source timestamp: tell dated the event when "
        "it saw it): 1"
    ) in text
    assert (
        "Left out of freshness, backfills (archive loads and widened Google News windows): 2"
    ) in text
    assert "### Freshness by source" in text
    assert "| ashby | 5 | 1 | 4 | 0 | 0 | 8.0 h (N=1; too small to quote) |" in text
    assert "| edgar | 1 | 0 | 0 | 0 | 1 | n/a (N=0; too small to quote) |" in text
    assert "| greenhouse | 4 | 3 | 0 | 1 | 0 | 10.8 h (N=3; too small to quote) |" in text
    assert "| news | 24 | 12 | 11 | 0 | 1 | 105.4 h (N=12; too small to quote) |" in text
    assert "ADR 0043" in text
    assert "Freshness is tell's lag" in text  # the template's intro
    assert "[" not in text
