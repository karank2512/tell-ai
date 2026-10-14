"""News collector, SPEC 13.1 cohort mode: ``when:{N}d`` URLs, cohort selection, cache by window.

``respx`` answers every request; the recorded Google News fixture (``normal.xml``, eight
items about "Quillstone Analytics") stands in for the 120-day answer.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors import news
from tell.collectors.base import finish_run, start_run
from tell.collectors.edgar_formd import FORM_D_AMENDMENT, FORM_D_FILED
from tell.collectors.news import (
    COHORT_WINDOW,
    collect,
    formd_cohort,
    google_news_url,
    ingest,
    recently_polled,
    select_companies,
)
from tell.http import HostRateBudgets, make_client
from tell.models import Company, EventRow

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
NAME = "Quillstone Analytics"
DOMAIN = "quillstone.example"
RSS_HEADERS = {"Content-Type": "application/rss+xml; charset=utf-8"}


def _budgets() -> HostRateBudgets:
    return HostRateBudgets(1.0, sleep=lambda _seconds: None)


def _company(session: Session, name: str, domain: str) -> Company:
    company = Company(domain=domain, name=name)
    session.add(company)
    session.flush()
    return company


def _event(session: Session, company: Company | None, type_: str, occurred: datetime) -> None:
    session.add(
        EventRow(
            event_id=secrets.token_hex(16),
            company_id=company.id if company is not None else None,
            source="edgar" if type_.startswith("form_d") else "news",
            type=type_,
            occurred_at=occurred,
            observed_at=occurred,
            magnitude=Decimal(1),
            url=f"https://example.invalid/{secrets.token_hex(4)}",
            payload={},
            run_id="cohort-test",
        )
    )
    session.flush()


def _news_run(session: Session, at: datetime, company: Company, when_days: int | None) -> None:
    run_id = start_run(session, news.SOURCE, now=at)
    notes: dict[str, Any] = {"polled": {str(company.id): at.isoformat()}}
    if when_days is not None:
        notes["when_days"] = when_days
    finish_run(session, run_id, status="ok", fetched=0, inserted=0, skipped=0, notes=notes, now=at)


def _mock_company_sites(mock: respx.MockRouter) -> None:
    mock.get(url__regex=r"https://[a-z-]+\.example/.*").mock(return_value=httpx.Response(404))


# --- URL ------------------------------------------------------------------------------------------


def test_google_news_url_with_a_wider_window() -> None:
    assert google_news_url(NAME, 120) == (
        "https://news.google.com/rss/search?q=%22Quillstone+Analytics%22+when%3A120d"
        "&hl=en-US&gl=US&ceid=US:en"
    )
    assert google_news_url(NAME) == google_news_url(NAME, news.DEFAULT_WHEN_DAYS)
    assert "when%3A7d" in google_news_url(NAME)


@pytest.mark.parametrize("days", [0, -1, news.MAX_WHEN_DAYS + 1])
def test_google_news_url_rejects_a_bad_window(days: int) -> None:
    with pytest.raises(ValueError, match="when_days"):
        google_news_url(NAME, days)


# --- cohort selection ------------------------------------------------------------------------------


@pytest.mark.db
def test_formd_cohort_selects_recent_linked_filers(db_session: Session) -> None:
    recent = _company(db_session, "Recent Filer", "recent.example")
    edge = _company(db_session, "Edge Filer", "edge.example")
    old = _company(db_session, "Old Filer", "old.example")
    amended = _company(db_session, "Amendment Only", "amended.example")
    press = _company(db_session, "Press Only", "press.example")
    future = _company(db_session, "Future Filer", "future.example")
    _event(db_session, recent, FORM_D_FILED, NOW - 10 * DAY)
    _event(db_session, recent, FORM_D_FILED, NOW - 20 * DAY)  # counted once
    _event(db_session, edge, FORM_D_FILED, NOW - COHORT_WINDOW)
    _event(db_session, old, FORM_D_FILED, NOW - COHORT_WINDOW - DAY)
    _event(db_session, amended, FORM_D_AMENDMENT, NOW - 5 * DAY)
    _event(db_session, press, "news_funding", NOW - 5 * DAY)
    _event(db_session, future, FORM_D_FILED, NOW + DAY)
    _event(db_session, None, FORM_D_FILED, NOW - 3 * DAY)  # unlinked
    assert formd_cohort(db_session, NOW) == sorted([recent.id, edge.id])
    assert formd_cohort(db_session, NOW, window=15 * DAY) == [recent.id]


@pytest.mark.db
def test_a_seven_day_poll_does_not_hide_a_company_from_the_cohort(db_session: Session) -> None:
    company = _company(db_session, NAME, DOMAIN)
    _news_run(db_session, NOW - timedelta(hours=1), company, when_days=None)  # pre-field run
    assert recently_polled(db_session, NOW) == {company.id: NOW - timedelta(hours=1)}
    assert recently_polled(db_session, NOW, min_when_days=120) == {}
    ids = [company.id]
    assert select_companies(db_session, NOW, company_ids=ids) == []
    assert select_companies(db_session, NOW, company_ids=ids, when_days=120) == [company]

    # A 120-day poll covers any narrower window that follows within six hours.
    _news_run(db_session, NOW - timedelta(minutes=30), company, when_days=120)
    assert select_companies(db_session, NOW, company_ids=ids, when_days=120) == []
    assert select_companies(db_session, NOW, company_ids=ids, when_days=30) == []


# --- a cohort run ------------------------------------------------------------------------------------


@pytest.mark.db
def test_cohort_run_queries_120_days_writes_classified_events_and_is_idempotent(
    db_session: Session,
) -> None:
    filer = _company(db_session, NAME, DOMAIN)
    bystander = _company(db_session, "Bystander Co", "bystander.example")
    _event(db_session, filer, FORM_D_FILED, NOW - 30 * DAY)
    wide = google_news_url(NAME, 120)
    with make_client() as client, respx.mock(assert_all_mocked=True) as mock:
        route = mock.get(wide).mock(
            return_value=httpx.Response(
                200, content=(FIXTURES / "normal.xml").read_bytes(), headers=RSS_HEADERS
            )
        )
        _mock_company_sites(mock)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            budgets=_budgets(),
            cohort="formd",
            when_days=120,
        )
        assert route.call_count == 1
        assert not any("bystander" in str(call.request.url) for call in mock.calls)

    assert run.status == "ok"
    assert run.notes is not None
    assert run.notes["cohort"] == "formd"
    assert run.notes["cohort_size"] == 1
    assert run.notes["cohort_days"] == news.COHORT_DAYS
    assert run.notes["when_days"] == 120
    assert run.notes["polled"] == {str(filer.id): NOW.isoformat()}
    rows = list(db_session.scalars(select(EventRow).where(EventRow.source == "news")))
    assert run.inserted == len(rows) > 0
    assert {row.company_id for row in rows} == {filer.id}
    assert all(row.payload["query"] == wide for row in rows)
    assert any(row.type == "news_funding" for row in rows)
    leadership = [row for row in rows if row.type == "news_leadership"]
    assert leadership
    assert all(row.payload["title_redacted"] is True for row in leadership)
    assert bystander.id not in {row.company_id for row in rows}

    # Seven hours later the same answer inserts nothing (the blog-feed miss is remembered).
    with (
        make_client() as client,
        respx.mock(assert_all_mocked=True, assert_all_called=False) as mock,
    ):
        mock.get(wide).mock(
            return_value=httpx.Response(
                200, content=(FIXTURES / "normal.xml").read_bytes(), headers=RSS_HEADERS
            )
        )
        _mock_company_sites(mock)
        again = ingest(
            db_session,
            now=NOW + timedelta(hours=7),
            client=client,
            budgets=_budgets(),
            cohort="formd",
            when_days=120,
        )
    assert (again.status, again.inserted) == ("ok", 0)
    assert again.fetched > 0


@pytest.mark.db
def test_collect_rejects_bad_arguments_before_any_request(db_session: Session) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(ValueError, match="unknown cohort"):
            collect(db_session, run_id="r", now=NOW, cohort="everyone")
        with pytest.raises(ValueError, match="either company_ids or cohort"):
            collect(db_session, run_id="r", now=NOW, cohort="formd", company_ids=[1])
        with pytest.raises(ValueError, match="when_days"):
            collect(db_session, run_id="r", now=NOW, when_days=0)
        with pytest.raises(ValueError, match="cohort_days"):
            collect(db_session, run_id="r", now=NOW, cohort="formd", cohort_days=0)
        assert mock.calls.call_count == 0


@pytest.mark.db
def test_scheduled_runs_record_their_window(db_session: Session) -> None:
    with make_client() as client, respx.mock(assert_all_mocked=True):
        run = ingest(db_session, now=NOW, client=client, budgets=_budgets())
    assert run.notes is not None
    assert run.notes["when_days"] == 7
    assert "cohort" not in run.notes
