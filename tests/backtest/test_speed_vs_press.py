"""13.1 speed vs press: the shipped SQL against a hand-calculated synthetic cohort."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tell.backtest import speed_vs_press as svp
from tell.backtest.common import render_report
from tell.models import Company, EventRow
from tests.backtest.helpers import DAY, HOUR, NOW, company, formd, midnight, news

MINUTE = HOUR / 60

pytestmark = pytest.mark.db


def _match(session: Session, row: EventRow, **fields: Any) -> EventRow:
    """Replace the event's ``payload.match`` with ``fields`` (``guard_failed`` and the like)."""
    row.payload = {**row.payload, "match": {"confidence": 0.95, **fields}}
    session.flush()
    return row


def _cohort(session: Session) -> None:
    """Six filings in the cohort, ten that are not (see the expectations below)."""
    a = company(session, "alpha.example")
    b = company(session, "bravo.example", cik="0000000002")
    c = company(session, "charlie.example")
    d = company(session, "delta.example")
    e = company(session, "echo.example")
    g = company(session, "golf.example")

    # A: R2, observed 08:17 on the filing day; press 10 days later (filing first).
    formd(
        session, a, date(2026, 8, 1), observed=midnight(date(2026, 8, 1)) + 8 * HOUR + MINUTE * 17
    )
    news(session, a, datetime.fromisoformat("2026-08-11T09:00:00+00:00"))
    news(session, a, datetime.fromisoformat("2026-08-20T09:00:00+00:00"))
    # B: linked by CIK (no match record) seen the next day at 02:17; its CIK was set by an
    # R2 link of an older filing (outside the 90 days). Press 7 days before.
    formd(session, b, NOW.date() - 120 * DAY, cik="0000000002")
    formd(
        session,
        b,
        date(2026, 9, 1),
        observed=midnight(date(2026, 9, 2)) + 2 * HOUR + MINUTE * 17,
        rule=None,
        cik="0000000002",
    )
    news(session, b, datetime.fromisoformat("2026-08-25T15:00:00+00:00"))
    # C: R3 from the quarterly backfill: estimated, 26 h + 6 h. Press the same day.
    formd(session, c, date(2026, 9, 10), rule="R3", backfill=True, observed=NOW)
    news(session, c, datetime.fromisoformat("2026-09-10T13:00:00+00:00"))
    # D: R4, seen at 20:00; the only press is 19 days before (outside the window).
    formd(
        session, d, date(2026, 9, 20), rule="R4", observed=midnight(date(2026, 9, 20)) + 20 * HOUR
    )
    news(session, d, datetime.fromisoformat("2026-09-01T10:00:00+00:00"))
    news(session, d, datetime.fromisoformat("2026-09-21T10:00:00+00:00"), type_="news_launch")
    # E: a reviewer's link of an R5 candidate whose guard had failed (the human wins), loaded
    # by a catch-up run six days later: estimated, 32 h. Press 20 days later.
    _match(
        session,
        formd(session, e, date(2026, 9, 6), observed=midnight(date(2026, 9, 12)) + 3 * HOUR),
        rule="manual",
        guard_failed=True,
        review_id=1,
    )
    news(session, e, datetime.fromisoformat("2026-09-26T08:00:00+00:00"))
    # G: R2 with an explicit guard_failed false, seen the next day at 03:00 (27 h). No press.
    _match(
        session,
        formd(session, g, date(2026, 9, 15), observed=midnight(date(2026, 9, 16)) + 3 * HOUR),
        rule="R2",
        guard_failed=False,
    )

    # Not in the cohort.
    formd(session, a, date(2026, 9, 5), amendment=True)
    formd(session, a, NOW.date() - 100 * DAY)
    formd(session, None, date(2026, 9, 7))
    h = company(session, "hotel.example")
    _match(session, formd(session, h, date(2026, 9, 8)), rule="R2", guard_failed=True)
    i = company(session, "india.example")
    _match(session, formd(session, i, date(2026, 9, 9)), rule="R3", guard_failed=["state"])
    j = company(session, "juliet.example")
    _match(session, formd(session, j, date(2026, 9, 11)), rule="R5")
    _match(session, formd(session, j, date(2026, 9, 12)), rule="R6")
    # K: a CIK written by a guard-failed R3 link does not vouch for later CIK matches.
    k = company(session, "kilo.example", cik="0000000003")
    _match(
        session,
        formd(session, k, NOW.date() - 120 * DAY, cik="0000000003"),
        rule="R3",
        guard_failed={"year": "incorporated after the batch"},
    )
    formd(session, k, date(2026, 9, 13), rule=None, cik="0000000003")
    # L: a CIK no link set; M: a CIK-less company with a CIK-only filing.
    lima = company(session, "lima.example", cik="0000000004")
    formd(session, lima, date(2026, 9, 14), rule=None, cik="0000000004")
    m = company(session, "mike.example")
    formd(session, m, date(2026, 9, 16), rule=None, cik="0000000005")
    for owner in (h, i, j, k, lima, m):
        news(session, owner, datetime.fromisoformat("2026-09-20T09:00:00+00:00"))


def test_numbers_match_hand_calculation(db_session: Session) -> None:
    _cohort(db_session)
    result = svp.compute(db_session, NOW)
    assert result.n == 6
    assert result.with_press == 4
    # Lags in days: A +10, B -7, C 0, E +20 -> median 5, p75 = 10 + 0.25 * 10.
    assert result.median_lag_days == 5.0
    assert result.p75_lag_days == 12.5
    assert result.filing_first == 2
    assert result.filing_first_share_with_press == 0.5
    assert result.filing_first_share_all == pytest.approx(4 / 6)  # A, E, plus D and G
    assert result.press_share == pytest.approx(4 / 6)
    # Alert hours: A 8.2833, D 20, B 26.2833, G 27 observed; C 32, E 32 estimated.
    assert result.alert_observed == 4
    assert result.alert_estimated == 2
    assert not result.mostly_estimated
    assert result.median_alert_hours == pytest.approx((26 + 17 / 60 + 27) / 2)
    assert result.median_observed_alert_hours == pytest.approx((20 + 26 + 17 / 60) / 2)


def test_daily_cadence_widens_the_window_and_the_estimate(db_session: Session) -> None:
    owner = company(db_session, "daily.example")
    filed = date(2026, 9, 1)
    # Seen by the 15:17 UTC daily run the day after: 39.3 h, past 36 h but within 54 h.
    formd(db_session, owner, filed, observed=midnight(filed) + DAY + 15 * HOUR + 17 * MINUTE)
    late = company(db_session, "late.example")
    formd(db_session, late, filed, observed=midnight(filed) + 3 * DAY)  # catch-up run

    six = svp.compute(db_session, NOW)
    assert (six.alert_observed, six.alert_estimated) == (0, 2)
    assert six.median_alert_hours == 32.0
    assert six.median_observed_alert_hours is None

    daily = svp.compute(db_session, NOW, cadence_hours=24)
    assert (daily.alert_observed, daily.alert_estimated) == (1, 1)
    assert daily.median_alert_hours == pytest.approx((39 + 17 / 60 + 50) / 2)
    assert daily.median_observed_alert_hours == pytest.approx(39 + 17 / 60)


def test_observed_window_and_cadence_helpers() -> None:
    assert svp.observed_window_hours(svp.ALERT_CADENCE_HOURS) == 36
    assert svp.observed_window_hours(24) == 54
    assert svp.ingest_cadence_hours("4") == 6
    assert svp.ingest_cadence_hours(" 4 ") == 6
    for value in (None, "", "1", "2"):
        assert svp.ingest_cadence_hours(value) == 24


def test_cadence_must_be_positive(db_session: Session) -> None:
    with pytest.raises(ValueError, match="cadence_hours must be positive"):
        svp.compute(db_session, NOW, cadence_hours=0)


def test_report_flags_small_sample(db_session: Session) -> None:
    _cohort(db_session)
    report = svp.build_report(db_session, NOW)
    text = render_report(report)
    assert report.n == 6
    assert not report.quotable
    assert "quotable: false" in text
    assert "Sample too small: N=6 filings, fewer than the 100 the method requires" in text
    assert "66.7% (N=6; too small to quote)" in text
    assert "5.0 days (N=4; too small to quote)" in text
    assert "- Median alert_at - filing_date, hours: 26.6 h (N=6; too small to quote)" in text
    assert "observed filings only: 23.1 h (N=4; too small to quote)" in text
    assert "a scheduled run within 36 h of the filing date): 4" in text
    assert "6-hour cadence, 32 h after the filing date): 2" in text
    assert "Observed: 4; estimated: 2." in text
    assert "more than 14 days before the filing" in text
    assert "alert median is an estimate" not in text
    assert ":cadence_hours = 6, :observed_window_hours = 36)" in text
    assert svp.SQL in text
    assert "news_funding" in text
    assert "[" not in text


def _estimated_cohort(session: Session, n: int, *, observed: int) -> None:
    filed = date(2026, 9, 1)
    for k in range(n):
        owner = company(session, f"co{k}.example")
        if k < observed:
            formd(session, owner, filed, observed=midnight(filed) + 26 * HOUR + 17 * MINUTE)
        else:
            formd(session, owner, filed, backfill=True, observed=NOW)
        news(session, owner, midnight(filed) + 3 * DAY)


def test_mostly_estimated_alert_row_is_labelled(db_session: Session) -> None:
    _estimated_cohort(db_session, 5, observed=2)
    report = svp.build_report(db_session, NOW)
    text = render_report(report)
    label = (
        "Median alert_at - filing_date, hours, estimated (3 of 5 alert times are estimates, "
        "not observations): 32.0 h"
    )
    assert label in text
    assert "alert_at is estimated for 3 of 5 filings" in text
    assert "the alert median is an estimate, not a measurement." in text


def test_half_estimated_is_not_mostly(db_session: Session) -> None:
    _estimated_cohort(db_session, 4, observed=2)
    result = svp.compute(db_session, NOW)
    assert (result.alert_observed, result.alert_estimated) == (2, 2)
    assert not result.mostly_estimated
    text = render_report(svp.build_report(db_session, NOW))
    assert "- Median alert_at - filing_date, hours: " in text


def test_daily_cadence_report_text(db_session: Session) -> None:
    _estimated_cohort(db_session, 2, observed=0)
    text = render_report(svp.build_report(db_session, NOW, cadence_hours=24))
    assert "24-hour cadence, 50 h after the filing date): 2" in text
    assert "within 54 h of the filing date" in text
    assert "TELL_RUNS_PER_DAY not 4" not in text


def test_large_cohort_is_quotable(db_session: Session) -> None:
    _estimated_cohort(db_session, svp.MIN_COHORT, observed=svp.MIN_COHORT)
    report = svp.build_report(db_session, NOW)
    assert report.n == svp.MIN_COHORT
    assert report.quotable
    assert not report.notices
    text = render_report(report)
    assert "quotable: true" in text
    assert "3.0 days (N=100)" in text
    assert "26.3 h (N=100)" in text


def test_cik_vouched_by_manual_link(db_session: Session) -> None:
    owner: Company = company(db_session, "november.example", cik="0000000009")
    _match(
        db_session,
        formd(db_session, owner, NOW.date() - 200 * DAY, cik="0000000009"),
        rule="manual",
    )
    formd(db_session, owner, date(2026, 9, 1), rule=None, cik="0000000009")
    assert svp.compute(db_session, NOW).n == 1
