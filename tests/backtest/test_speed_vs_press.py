"""13.1 speed vs press: the shipped SQL against a hand-calculated synthetic cohort."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from tell.backtest import speed_vs_press as svp
from tell.backtest.common import render_report
from tests.backtest.helpers import DAY, HOUR, NOW, company, formd, midnight, news

MINUTE = HOUR / 60

pytestmark = pytest.mark.db


def _cohort(session: Session) -> None:
    """Four filings in the cohort, four that are not (see the expectations below)."""
    a = company(session, "alpha.example")
    b = company(session, "bravo.example", cik="0000000002")
    c = company(session, "charlie.example")
    d = company(session, "delta.example")
    e = company(session, "echo.example")

    # A: R2, observed 08:17 on the filing day; press 10 days later (filing first).
    formd(
        session, a, date(2026, 8, 1), observed=midnight(date(2026, 8, 1)) + 8 * HOUR + MINUTE * 17
    )
    news(session, a, datetime.fromisoformat("2026-08-11T09:00:00+00:00"))
    news(session, a, datetime.fromisoformat("2026-08-20T09:00:00+00:00"))
    # B: linked by CIK (no match record), seen the next day at 02:17; press 7 days before.
    formd(
        session,
        b,
        date(2026, 9, 1),
        observed=midnight(date(2026, 9, 2)) + 2 * HOUR + MINUTE * 17,
        rule=None,
        cik="0000000002",
    )
    news(session, b, datetime.fromisoformat("2026-08-25T15:00:00+00:00"))
    # C: R3 from the quarterly backfill (alert estimated: 00:00 + 6 h); press the same day.
    formd(session, c, date(2026, 9, 10), rule="R3", backfill=True, observed=NOW)
    news(session, c, datetime.fromisoformat("2026-09-10T13:00:00+00:00"))
    # D: R4, seen at 20:00; the only press is 19 days before (outside the window).
    formd(
        session, d, date(2026, 9, 20), rule="R4", observed=midnight(date(2026, 9, 20)) + 20 * HOUR
    )
    news(session, d, datetime.fromisoformat("2026-09-01T10:00:00+00:00"))
    news(session, d, datetime.fromisoformat("2026-09-21T10:00:00+00:00"), type_="news_launch")

    # Not in the cohort: an amendment, a reviewer-only link without CIK, a filing older
    # than 90 days, and an unlinked filing.
    formd(session, a, date(2026, 9, 5), amendment=True)
    formd(session, e, date(2026, 9, 6), rule="manual")
    formd(session, a, NOW.date() - 100 * DAY)
    formd(session, None, date(2026, 9, 7))


def test_numbers_match_hand_calculation(db_session: Session) -> None:
    _cohort(db_session)
    result = svp.compute(db_session, NOW)
    assert result.n == 4
    assert result.with_press == 3
    # Lags in days: A +10, B -7, C 0 -> median 0, p75 = 0 + 0.5 * 10.
    assert result.median_lag_days == 0.0
    assert result.p75_lag_days == 5.0
    assert result.filing_first == 1
    assert result.filing_first_share_with_press == pytest.approx(1 / 3)
    assert result.filing_first_share_all == 0.5  # A, plus D with no press
    assert result.press_share == 0.75
    # Alert hours: A 8.2833, B 26.2833, C 6 (estimated), D 20 -> median (8.2833 + 20) / 2.
    assert result.median_alert_hours == pytest.approx((8 + 17 / 60 + 20) / 2)
    assert result.alert_estimated == 1


def test_report_flags_small_sample(db_session: Session) -> None:
    _cohort(db_session)
    report = svp.build_report(db_session, NOW)
    text = render_report(report)
    assert report.n == 4
    assert not report.quotable
    assert "quotable: false" in text
    assert "Sample too small: N=4 filings, fewer than the 100 the method requires" in text
    assert "75.0% (N=4; too small to quote)" in text
    assert "0.0 days (N=3; too small to quote)" in text
    assert "14.1 h (N=4; too small to quote)" in text
    assert svp.SQL in text
    assert "news_funding" in text
    assert "[" not in text


def test_large_cohort_is_quotable(db_session: Session) -> None:
    for k in range(svp.MIN_COHORT):
        owner = company(db_session, f"co{k}.example")
        filed = date(2026, 9, 1)
        formd(db_session, owner, filed, observed=midnight(filed) + 6 * HOUR)
        news(db_session, owner, midnight(filed) + 3 * DAY)
    report = svp.build_report(db_session, NOW)
    assert report.n == svp.MIN_COHORT
    assert report.quotable
    assert not report.notices
    text = render_report(report)
    assert "quotable: true" in text
    assert "3.0 days (N=100)" in text
    assert "6.0 h (N=100)" in text
