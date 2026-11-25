"""Source eligibility on the site's company selection and the JSON API's news coverage flag.

Synthetic rows only. A "Google News" row here carries the collector's three provenance
markers (``payload.match.rule = news_query``, a ``news.google.com`` query and item URL) with
invented values; nothing is fetched. Signals are inserted directly as well as derived, so a
signal stored before the exclusion existed is covered too.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tell.collectors.base import Event, write_events
from tell.models import Company, CompanyFeed, EventRow, Signal
from tell.publish.json_api import company_facts_many
from tell.publish.site import published_company_ids
from tell.signals.scoring import score_instant
from tests.publish import scenario as sc
from tests.source_feed import TEST_FEED, feed_provenance

pytestmark = pytest.mark.db

GOOGLE_QUERY = "https://news.google.com/rss/search?q=%22Synthetic+Co%22+when%3A7d"


def google_payload() -> dict[str, Any]:
    return {
        "title": "Synthetic Co launches a thing",
        "match": {"rule": "news_query", "guard": "name_in_title"},
        "query": GOOGLE_QUERY,
    }


def _company(session: Session, domain: str) -> Company:
    company = Company(domain=domain, name=domain.split(".", maxsplit=1)[0].title())
    session.add(company)
    session.flush()
    return company


def _event_with_signal(
    session: Session,
    company: Company,
    *,
    source: str,
    type_: str,
    url: str,
    payload: dict[str, Any],
    signal_type: str,
    occurred: datetime,
) -> str:
    """Store one event and a signal on it directly (as one stored before the exclusion)."""
    event = Event(
        source=source,
        type=type_,
        external_id=url,
        version="1",
        occurred_at=occurred,
        observed_at=occurred,
        url=url,
        payload=payload,
        run_id="synthetic",
        company_id=company.id,
    )
    write_events(session, [event])
    session.add(
        Signal(
            company_id=company.id,
            event_id=event.event_id,
            signal_type=signal_type,
            role_family=None,
            weight=Decimal("1"),
            magnitude=Decimal("1"),
            half_life_days=30,
            starts_at=occurred,
        )
    )
    session.flush()
    return event.event_id


@pytest.mark.parametrize("permit", [False, True], ids=["no-permit", "test-feed-permit"])
def test_site_publishes_only_companies_with_an_eligible_signal(
    db_session: Session, request: pytest.FixtureRequest, permit: bool
) -> None:
    if permit:
        request.getfixturevalue("permitted_test_feed")
    occurred = sc.NOW - timedelta(days=3)
    google_only = _company(db_session, "googleonly.example")
    filer = _company(db_session, "filer.example")
    feed_co = _company(db_session, "feedco.example")
    mixed = _company(db_session, "mixed.example")
    _event_with_signal(
        db_session,
        google_only,
        source="news",
        type_="news_launch",
        url="https://news.google.com/rss/articles/SYNTH-1",
        payload=google_payload(),
        signal_type="launch",
        occurred=occurred,
    )
    _event_with_signal(
        db_session,
        filer,
        source="edgar",
        type_="form_d_filed",
        url="https://www.sec.gov/Archives/edgar/data/1/synthetic-1",
        payload={"is_amendment": False},
        signal_type="funding",
        occurred=occurred,
    )
    _event_with_signal(
        db_session,
        feed_co,
        source="news",
        type_="news_launch",
        url="https://news.example.test/post-1",
        payload=feed_provenance({"title": "Feed Co launches"}),
        signal_type="launch",
        occurred=occurred,
    )
    for n, (source, type_, url, payload, kind) in enumerate(
        (
            (
                "news",
                "news_launch",
                "https://news.google.com/rss/articles/SYNTH-2",
                google_payload(),
                "launch",
            ),
            (
                "edgar",
                "form_d_filed",
                "https://www.sec.gov/Archives/edgar/data/2/synthetic-2",
                {"is_amendment": False},
                "funding",
            ),
        )
    ):
        _event_with_signal(
            db_session,
            mixed,
            source=source,
            type_=type_,
            url=url,
            payload=payload,
            signal_type=kind,
            occurred=occurred - timedelta(hours=n),
        )

    published = published_company_ids(db_session, sc.AS_OF)

    expected = {filer.id, mixed.id} | ({feed_co.id} if permit else set())
    assert set(published) == expected
    assert google_only.id not in published
    assert published == sorted(published)


@pytest.mark.parametrize("permit", [False, True], ids=["no-permit", "test-feed-permit"])
def test_news_coverage_flag_counts_only_permitted_feeds_and_eligible_events(
    db_session: Session, request: pytest.FixtureRequest, permit: bool
) -> None:
    if permit:
        request.getfixturevalue("permitted_test_feed")
    occurred = sc.NOW - timedelta(days=2)
    google_only = _company(db_session, "googlenews.example")
    unpermitted_feed = _company(db_session, "ownblog.example")
    permitted_feed = _company(db_session, "permittedfeed.example")
    feed_event = _company(db_session, "feedevent.example")
    nothing = _company(db_session, "quiet.example")
    write_events(
        db_session,
        [
            Event(
                source="news",
                type="news_launch",
                external_id="https://news.google.com/rss/articles/SYNTH-3",
                version="1",
                occurred_at=occurred,
                observed_at=occurred,
                url="https://news.google.com/rss/articles/SYNTH-3",
                payload=google_payload(),
                run_id="synthetic",
                company_id=google_only.id,
            ),
            Event(
                source="news",
                type="news_launch",
                external_id="https://news.example.test/post-2",
                version="1",
                occurred_at=occurred,
                observed_at=occurred,
                url="https://news.example.test/post-2",
                payload=feed_provenance({"title": "x"}),
                run_id="synthetic",
                company_id=feed_event.id,
            ),
        ],
    )
    db_session.add_all(
        [
            CompanyFeed(company_id=unpermitted_feed.id, feed_url="https://ownblog.example/feed"),
            CompanyFeed(company_id=permitted_feed.id, feed_url=TEST_FEED),
        ]
    )
    db_session.flush()
    assert db_session.query(EventRow).count() == 2

    companies = [google_only, unpermitted_feed, permitted_feed, feed_event, nothing]
    facts = company_facts_many(db_session, companies, score_instant(sc.AS_OF))

    news = {company.domain: facts[company.id].news for company in companies}
    assert news == {
        "googlenews.example": False,  # Google News rows never show as coverage
        "ownblog.example": False,  # a stored feed without a recorded permit
        "permittedfeed.example": permit,
        "feedevent.example": permit,
        "quiet.example": False,
    }


@pytest.mark.usefixtures("permitted_test_feed")
def test_unknown_provenance_and_non_object_payloads_never_count(db_session: Session) -> None:
    """Even with a feed permit: news without provenance markers and a non-object payload."""
    occurred = sc.NOW - timedelta(days=4)
    unknown = _company(db_session, "unknownnews.example")
    malformed = _company(db_session, "malformed.example")
    _event_with_signal(
        db_session,
        unknown,
        source="news",
        type_="news_launch",
        url="https://unknownnews.example/post",
        payload={"title": "Unknownnews launches"},  # no match/query: unknown provenance
        signal_type="launch",
        occurred=occurred,
    )
    _event_with_signal(
        db_session,
        malformed,
        source="edgar",
        type_="form_d_filed",
        url="https://www.sec.gov/Archives/edgar/data/3/synthetic-3",
        payload=["not", "an", "object"],  # the malformed case under test
        signal_type="funding",
        occurred=occurred,
    )
    assert published_company_ids(db_session, sc.AS_OF) == []
    facts = company_facts_many(db_session, [unknown, malformed], score_instant(sc.AS_OF))
    assert (facts[unknown.id].news, facts[malformed.id].news) == (False, False)
