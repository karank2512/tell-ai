"""News collector: parsing, name guard, events, block policy, feed discovery, rotation.

Every HTTP call goes through ``respx``; the rate budget's sleeper is a no-op so the
1 rps limit costs no wall time. The leadership-redaction tests (ADR 0016) use
placeholder names ("Jane Doe" style) in these test strings only, never in fixtures.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from alembic import command
from freezegun import freeze_time
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from tell.collectors import news
from tell.collectors.base import Event, sha256_hex, start_run, utcnow
from tell.collectors.news import (
    FALLBACK_PATHS,
    MAX_COMPANIES,
    MAX_GOOGLE_REQUESTS,
    RUN_BUDGET,
    CompanyRef,
    NewsFeed,
    NewsItem,
    company_key,
    derive_events,
    discover_feed,
    feed_links,
    google_news_url,
    headline,
    ingest,
    is_block_only_failure,
    last_polled,
    name_in_title,
    news_key,
    on_domain,
    parse_feed_items,
    recently_polled,
    run_fixture,
    select_companies,
)
from tell.config import get_settings
from tell.db import make_engine
from tell.http import HostRateBudgets, make_client
from tell.models import Company, CompanyFeed, EventRow, Run, Watchlist, WatchlistItem
from tests.conftest import admin_url, alembic_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # a Monday
NAME = "Quillstone Analytics"
DOMAIN = "quillstone.example"
GNEWS = google_news_url(NAME)
BLOG = f"https://{DOMAIN}/blog"
HOME = f"https://{DOMAIN}/"
ATOM = f"https://{DOMAIN}/blog/atom.xml"
RSS_HEADERS = {"Content-Type": "application/rss+xml; charset=utf-8"}
ATOM_HEADERS = {"Content-Type": "application/atom+xml; charset=utf-8"}
HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8"}
PAYLOAD_KEYS = {
    "title",
    "title_redacted",
    "source",
    "matched_regex",
    "category",
    "query",
    "match",
    "key",
}


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _budgets() -> HostRateBudgets:
    return HostRateBudgets(1.0, sleep=lambda _seconds: None)


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


def _company(session: Session, name: str = NAME, domain: str = DOMAIN) -> Company:
    company = Company(domain=domain, name=name)
    session.add(company)
    session.flush()
    return company


def _urls(mock: respx.MockRouter) -> list[str]:
    return [str(call.request.url) for call in mock.calls]


def _count(session: Session, model: type[Any]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# --- URL building -----------------------------------------------------------------------------


def test_google_news_url_quotes_the_name_and_asks_for_seven_days() -> None:
    assert google_news_url("Quillstone Analytics") == (
        "https://news.google.com/rss/search?q=%22Quillstone+Analytics%22+when%3A7d"
        "&hl=en-US&gl=US&ceid=US:en"
    )


def test_google_news_url_escapes_reserved_characters() -> None:
    url = google_news_url('Ampersand & "Sons" Co')
    query = parse_qs(urlsplit(url).query)
    assert query["q"] == ['"Ampersand & "Sons" Co" when:7d']
    assert query["hl"] == ["en-US"]
    assert query["gl"] == ["US"]
    assert query["ceid"] == ["US:en"]
    assert "&" not in urlsplit(url).query.split("&hl=")[0]


def test_news_key_is_the_spec_form() -> None:
    url = "https://news.google.com/rss/articles/abc?oc=5"
    assert news_key(url) == f"news:{sha256_hex(url)}"


# --- parsing ----------------------------------------------------------------------------------


def test_normal_xml_parses_to_its_json_rendering() -> None:
    items, title = parse_feed_items(_bytes("normal.xml"))
    rendered = json.loads(_bytes("normal.json"))
    assert items == rendered["items"]
    assert title == '""Quillstone Analytics" when:7d" - Google News'
    feed = NewsFeed.model_validate(rendered)
    assert len(feed.items) == 8
    first = feed.items[0]
    assert first.pub_date == datetime(2026, 10, 1, 13, 5, tzinfo=UTC)
    assert first.source == "Ledger Daily"
    assert first.link.startswith("https://news.google.com/rss/articles/")


def test_empty_xml_has_no_items() -> None:
    items, _ = parse_feed_items(_bytes("empty.xml"))
    assert items == []
    assert NewsFeed.model_validate_json(_bytes("empty.json")).items == []


def test_company_atom_feed_parses_and_converts_offsets_to_utc() -> None:
    items, title = parse_feed_items(_bytes("company_feed.xml"))
    assert title == "Quillstone Analytics Blog"
    assert len(items) == 5
    by_link = {item["link"]: item for item in items}
    assert by_link[f"{BLOG}/austin"]["pub_date"] == "2026-09-30T21:00:00Z"
    assert "pub_date" not in by_link[f"{BLOG}/release-notes-template"]
    assert all(item["source"] is None for item in items)
    # Summaries are never read.
    assert all(set(item) <= {"title", "link", "pub_date", "source"} for item in items)


def test_drift_fixture_omits_pub_date() -> None:
    with pytest.raises(ValueError, match="pub_date"):
        NewsFeed.model_validate_json(_bytes("drift.json"))


def test_parse_garbage_yields_nothing() -> None:
    assert parse_feed_items(b"<html><body>not a feed</body></html>")[0] == []
    assert parse_feed_items(b"")[0] == []


# --- name guard -------------------------------------------------------------------------------


def test_company_key_strips_punctuation_and_legal_suffix() -> None:
    assert company_key("Quillstone Analytics, Inc.") == "quillstone analytics"
    assert company_key("Acme.ai") == "acme ai"
    assert company_key("Co") == "co"
    assert company_key("  ") == ""


@pytest.mark.parametrize(
    ("name", "title", "expected"),
    [
        (NAME, "Quillstone Analytics raises $24M Series A", True),
        (NAME, "QUILLSTONE ANALYTICS opens Berlin office", True),
        (NAME, "Quillstone Analytics' new office opens", True),
        (NAME, "Quillstone Ridge outfitter opens new office", False),
        (NAME, "Quillstone Analyticsware launches", False),
        ("Acme.ai", "Acme.ai launches agents", True),
        ("Acme.ai", "Acme launches agents", False),
        ("Quillstone Analytics, Inc.", "Quillstone Analytics appoints CFO", True),
        ("...", "anything at all", False),
    ],
)
def test_name_in_title(name: str, title: str, expected: bool) -> None:
    assert name_in_title(name, title) is expected


def test_headline_strips_the_publisher_suffix_only() -> None:
    pub = datetime(2026, 10, 1, tzinfo=UTC)
    item = NewsItem(
        title="Acme raises $5M - Ledger Daily",
        link="https://a.test/x",
        pub_date=pub,
        source="Ledger Daily",
    )
    assert headline(item) == "Acme raises $5M"
    other = NewsItem(
        title="Acme raises $5M - Other",
        link="https://a.test/x",
        pub_date=pub,
        source="Ledger Daily",
    )
    assert headline(other) == "Acme raises $5M - Other"
    bare = NewsItem(title=" - Ledger Daily", link="https://a.test/x", pub_date=pub, source=None)
    assert headline(bare) == " - Ledger Daily"


def test_on_domain() -> None:
    assert on_domain("https://quillstone.example/feed", DOMAIN)
    assert on_domain("https://blog.quillstone.example/rss", DOMAIN)
    assert not on_domain("https://notquillstone.example/feed", DOMAIN)
    assert not on_domain("https://medium.example/quillstone.example", DOMAIN)
    assert not on_domain("https://quillstone.example/feed", "")


# --- events (fixture replay) ---------------------------------------------------------------------


@pytest.mark.db
def test_run_fixture_normal_writes_six_classified_events(db_session: Session) -> None:
    run_id = start_run(db_session, "news", now=NOW)
    result = run_fixture(db_session, FIXTURES / "normal.json", run_id=run_id, now=NOW)

    assert (result.fetched, result.inserted, result.skipped) == (8, 6, 2)
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    rows = db_session.scalars(select(EventRow).order_by(EventRow.occurred_at)).all()
    assert sorted(row.type for row in rows) == [
        "news_expansion",
        "news_funding",
        "news_launch",
        "news_launch",
        "news_leadership",
        "news_leadership",
    ]
    for event in result.events:
        assert event.source == "news"
        assert event.version == "1"
        assert event.external_id == event.url
        assert event.event_id == sha256_hex(f"news|{event.url}|1")
        assert set(event.payload) == PAYLOAD_KEYS
        assert event.payload["key"] == f"news:{sha256_hex(event.url)}"
        assert event.payload["match"] == {"rule": "news_query", "guard": "name_in_title"}
        assert event.payload["query"] == GNEWS
        assert event.payload["category"] == event.type.removeprefix("news_")
        assert event.payload["matched_regex"].startswith(event.payload["category"] + ".")
        assert event.company_id == company.id
        assert event.observed_at == NOW
        assert float(event.magnitude) == 1
    funding = next(e for e in result.events if e.type == "news_funding")
    assert funding.occurred_at == datetime(2026, 10, 1, 13, 5, tzinfo=UTC)
    assert funding.payload["source"] == "Ledger Daily"
    assert funding.payload["matched_regex"] == "funding.raises"
    # Funding / launch / expansion keep the raw title; leadership is redacted (ADR 0016).
    assert funding.payload["title"] == (
        "Quillstone Analytics raises $24M Series A to expand forecasting platform - Ledger Daily"
    )
    assert funding.payload["title_redacted"] is False
    leadership = sorted(
        (e.payload["title"], e.payload["title_redacted"])
        for e in result.events
        if e.type == "news_leadership"
    )
    assert leadership == [
        ("appoints new chief revenue officer", True),
        ("names as chief financial officer", True),
    ]
    for event in result.events:
        assert event.payload["title_redacted"] is (event.type == "news_leadership")
    stored = json.dumps([row.payload for row in rows])
    assert "description" not in stored
    assert "font color" not in stored
    assert "Quillstone Ridge" not in stored  # name guard
    assert "data quality" not in stored  # unclassified


@pytest.mark.db
def test_run_fixture_company_feed_links_with_rule_r1(db_session: Session) -> None:
    run_id = start_run(db_session, "news", now=NOW)
    result = run_fixture(db_session, FIXTURES / "normal_company_feed.json", run_id=run_id, now=NOW)
    assert (result.fetched, result.inserted, result.skipped) == (4, 3, 1)
    assert {e.type for e in result.events} == {"news_launch", "news_expansion", "news_funding"}
    for event in result.events:
        assert event.payload["match"] == {"rule": "R1", "confidence": 0.98}
        assert event.payload["source"] == "Quillstone Analytics Blog"


@pytest.mark.db
def test_run_fixture_empty(db_session: Session) -> None:
    run_id = start_run(db_session, "news", now=NOW)
    result = run_fixture(db_session, FIXTURES / "empty.json", run_id=run_id, now=NOW)
    assert (result.fetched, result.inserted, result.skipped, result.events) == (0, 0, 0, [])


def test_off_domain_company_feed_is_not_r1() -> None:
    feed = NewsFeed(
        origin="company_feed",
        feed_url="https://medium.example/feed/quillstone",
        company=CompanyRef(name=NAME, domain=DOMAIN),
        items=[
            NewsItem(
                title="Introducing Quillstone Forecast 2.0",
                link="https://medium.example/p/1",
                pub_date=NOW,
                source="Medium",
            )
        ],
    )
    events, dropped = derive_events(feed, company_id=None, run_id="r", now=NOW)
    assert dropped == 0
    assert events[0].payload["match"] == {"rule": "company_feed"}


# --- collect / ingest over HTTP -------------------------------------------------------------------


def _mock_full_company(mock: respx.MockRouter) -> None:
    mock.get(GNEWS).mock(
        return_value=httpx.Response(200, content=_bytes("normal.xml"), headers=RSS_HEADERS)
    )
    mock.get(BLOG).mock(
        return_value=httpx.Response(200, content=_bytes("blog.html"), headers=HTML_HEADERS)
    )
    mock.get(ATOM).mock(
        return_value=httpx.Response(
            200,
            content=_bytes("company_feed.xml"),
            headers={
                **ATOM_HEADERS,
                "ETag": '"v1"',
                "Last-Modified": "Sun, 04 Oct 2026 16:00:00 GMT",
            },
        )
    )


@pytest.mark.db
def test_ingest_end_to_end_then_rerun_inserts_nothing(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    with respx.mock(assert_all_called=True) as mock:
        _mock_full_company(mock)
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
        assert _urls(mock) == [GNEWS, BLOG, ATOM]

    assert run.status == "ok"
    assert run.finished_at is not None
    assert run.finished_at >= NOW  # the start plus the real time the run took
    assert (run.fetched, run.inserted, run.skipped, run.errors) == (13, 9, 4, 0)
    assert run.notes is not None
    assert run.notes["polled"] == {str(company.id): "2026-10-05T12:00:00+00:00"}
    assert run.notes["blocked"] is False
    assert run.notes["no_feed"] == []
    feed = db_session.scalars(select(CompanyFeed)).one()
    assert (feed.feed_url, feed.discovered_from, feed.status) == (ATOM, BLOG, "active")
    assert (feed.etag, feed.last_modified) == ('"v1"', "Sun, 04 Oct 2026 16:00:00 GMT")
    assert feed.last_polled_at == NOW
    assert _count(db_session, EventRow) == 9
    first_ids = set(db_session.scalars(select(EventRow.event_id)))

    # Within 6 hours: the company is skipped entirely, no request is made.
    with respx.mock(assert_all_called=False) as mock:
        _mock_full_company(mock)
        soon = ingest(
            db_session,
            now=NOW + timedelta(hours=1),
            client=client,
            company_ids=[company.id],
            budgets=_budgets(),
        )
        assert mock.calls.call_count == 0
    assert (soon.fetched, soon.inserted) == (0, 0)
    assert soon.notes is not None
    assert soon.notes["selected"] == 0

    # After 6 hours: same items, conditional feed request answered 304, nothing new.
    later = NOW + timedelta(hours=7)
    with respx.mock(assert_all_called=True) as mock:
        gnews = mock.get(GNEWS).mock(
            return_value=httpx.Response(200, content=_bytes("normal.xml"), headers=RSS_HEADERS)
        )
        atom = mock.get(ATOM).mock(return_value=httpx.Response(304))
        again = ingest(
            db_session, now=later, client=client, company_ids=[company.id], budgets=_budgets()
        )
        assert gnews.call_count == 1
        assert atom.calls.last.request.headers["If-None-Match"] == '"v1"'
        assert (
            atom.calls.last.request.headers["If-Modified-Since"] == "Sun, 04 Oct 2026 16:00:00 GMT"
        )
    assert (again.fetched, again.inserted, again.skipped) == (8, 0, 8)
    assert set(db_session.scalars(select(EventRow.event_id))) == first_ids
    db_session.refresh(feed)
    assert feed.last_polled_at == later


@pytest.mark.db
@pytest.mark.parametrize("status", [429, 503])
def test_block_stops_the_run_without_retry(
    db_session: Session, client: httpx.Client, status: int
) -> None:
    first = _company(db_session)
    second = _company(db_session, name="Brightloom", domain="brightloom.example")
    with respx.mock(assert_all_called=False) as mock:
        blocked = mock.get(GNEWS).mock(return_value=httpx.Response(status))
        other = mock.get(google_news_url("Brightloom")).mock(
            return_value=httpx.Response(200, content=_bytes("empty.xml"))
        )
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[first.id, second.id],
            budgets=_budgets(),
        )
        assert blocked.call_count == 1
        assert other.call_count == 0
        assert mock.calls.call_count == 1
    # A block fails the run so the SPEC section 11 dead-man sees a persistent block.
    assert run.status == "failed"
    assert run.errors == 1
    assert run.notes is not None
    assert run.notes["blocked"] is True
    assert run.notes["blocked_by"] == {"status": status, "url": GNEWS, "company_id": first.id}
    assert run.notes["polled"] == {}
    assert run.notes["polled_count"] == 0
    assert run.notes["deferred"] == 1
    assert run.notes["budget_exhausted"] is False
    assert run.notes["google_requests"] == 1
    assert _count(db_session, EventRow) == 0
    # The only failure is the block: `tell ingest --source news` exits 0 for it.
    assert is_block_only_failure(run)


@pytest.mark.db
def test_block_after_some_polls_keeps_them_and_fails(
    db_session: Session, client: httpx.Client
) -> None:
    first = _company(db_session, name="Brightloom", domain="brightloom.example")
    second = _company(db_session)
    third = _company(db_session, name="Corvana", domain="corvana.example")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(google_news_url("Brightloom")).mock(
            return_value=httpx.Response(200, content=_bytes("empty.xml"))
        )
        mock.get(url__regex=r"https://brightloom\.example/.*").mock(
            return_value=httpx.Response(404)
        )
        mock.get(GNEWS).mock(return_value=httpx.Response(429))
        later = mock.get(google_news_url("Corvana"))
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[first.id, second.id, third.id],
            budgets=_budgets(),
        )
        assert later.call_count == 0
    assert run.status == "failed"
    assert run.notes is not None
    assert run.notes["blocked"] is True
    assert run.notes["polled"] == {str(first.id): "2026-10-05T12:00:00+00:00"}
    assert run.notes["polled_count"] == 1
    assert run.notes["deferred"] == 1
    # The blocked run's polls still count for the rotation.
    assert last_polled(db_session, NOW + timedelta(hours=1)) == {first.id: NOW}


@pytest.mark.db
def test_other_errors_count_and_the_run_continues(
    db_session: Session, client: httpx.Client
) -> None:
    first = _company(db_session)
    second = _company(db_session, name="Brightloom", domain="brightloom.example")
    third = _company(db_session, name="Corvana", domain="corvana.example")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(500))
        mock.get(google_news_url("Brightloom")).mock(side_effect=httpx.ConnectError("boom"))
        mock.get(google_news_url("Corvana")).mock(return_value=httpx.Response(404))
        mock.get(url__regex=r"https://(quillstone|brightloom|corvana)\.example/.*").mock(
            return_value=httpx.Response(404)
        )
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[first.id, second.id, third.id],
            budgets=_budgets(),
        )
    assert run.status == "ok"
    assert run.errors == 3
    assert run.notes is not None
    assert run.notes["polled"] == {}
    assert sorted(run.notes["no_feed"]) == sorted([first.id, second.id, third.id])


@pytest.mark.db
def test_contract_drift_fails_the_run(db_session: Session, client: httpx.Client) -> None:
    company = _company(db_session)
    broken = (
        _bytes("normal.xml")
        .replace(b"<pubDate>", b"<lastSeen>")
        .replace(b"</pubDate>", b"</lastSeen>")
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=broken))
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
    assert run.status == "failed"
    assert run.notes is not None
    assert run.notes["drift"].startswith("google_news:")
    assert _count(db_session, EventRow) == 0
    assert not is_block_only_failure(run)  # a contract failure is a real error


@pytest.mark.db
def test_discovery_miss_is_remembered_for_thirty_days(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        site = mock.get(url__regex=rf"https://{DOMAIN}/.*").mock(return_value=httpx.Response(404))
        first = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
        assert site.call_count == 6
        second = ingest(
            db_session,
            now=NOW + timedelta(days=1),
            client=client,
            company_ids=[company.id],
            budgets=_budgets(),
        )
        assert site.call_count == 6  # not probed again
        third = ingest(
            db_session,
            now=NOW + timedelta(days=31),
            client=client,
            company_ids=[company.id],
            budgets=_budgets(),
        )
        assert site.call_count == 12
    assert first.notes is not None
    assert first.notes["no_feed"] == [company.id]
    assert second.notes is not None
    assert second.notes["no_feed"] == []
    assert third.notes is not None
    assert third.notes["no_feed"] == [company.id]


@pytest.mark.db
@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectTimeout("slow"), httpx.ConnectError("dns"), httpx.Response(429)],
    ids=["timeout", "dead-host", "slow-down"],
)
def test_unreachable_site_is_a_remembered_miss_not_an_error(
    db_session: Session, client: httpx.Client, answer: httpx.Response | Exception
) -> None:
    company = _company(db_session)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        blog = mock.get(BLOG)
        if isinstance(answer, Exception):
            blog.mock(side_effect=answer)
        else:
            blog.mock(return_value=answer)
        rest = mock.get(url__regex=rf"https://{DOMAIN}/.*").mock(return_value=httpx.Response(404))
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
        # The first failure ends the site: no further probe waits on a dead host.
        assert (blog.call_count, rest.call_count) == (1, 0)
        again = ingest(
            db_session,
            now=NOW + timedelta(days=8),
            client=client,
            company_ids=[company.id],
            budgets=_budgets(),
        )
        assert (blog.call_count, rest.call_count) == (1, 0)  # remembered for 30 days
    assert (run.status, run.errors) == ("ok", 0)
    assert run.notes is not None
    assert run.notes["no_feed"] == [company.id]
    assert run.notes["site"]["unreachable"] == 1
    assert run.notes["site"]["no_feed"] == 0
    assert run.notes["polled"] == {str(company.id): "2026-10-05T12:00:00+00:00"}
    assert again.notes is not None
    assert again.notes["site"]["discovery_not_due"] == 1
    assert again.errors == 0


@pytest.mark.db
def test_no_feed_is_counted_in_notes_never_in_errors(
    db_session: Session, client: httpx.Client
) -> None:
    companies = _three_companies(db_session)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(200, content=_bytes("empty.xml"))
        )
        mock.get(url__regex=r"https://budget-a\.example/.*").mock(return_value=httpx.Response(404))
        # 5xx on one probe is still "no feed here", not a failed run.
        mock.get("https://budget-b.example/rss").mock(return_value=httpx.Response(500))
        mock.get(url__regex=r"https://budget-b\.example/.*").mock(return_value=httpx.Response(404))
        mock.get(url__regex=r"https://budget-c\.example/.*").mock(
            side_effect=httpx.ReadTimeout("slow")
        )
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
        )
    assert (run.status, run.errors) == ("ok", 0)
    assert run.notes is not None
    assert sorted(run.notes["no_feed"]) == sorted(c.id for c in companies)
    site = run.notes["site"]
    assert (site["no_feed"], site["unreachable"], site["discovered"]) == (2, 1, 0)
    assert run.notes["site_workers"] == news.SITE_WORKERS


# --- conditional polls of stored feeds --------------------------------------------------------------


def _stored_feed(session: Session, company: Company, **values: Any) -> CompanyFeed:
    row = CompanyFeed(company_id=company.id, feed_url=ATOM, discovered_from=BLOG, **values)
    session.add(row)
    session.flush()
    return row


@pytest.mark.db
def test_stored_feed_sends_validators_and_refreshes_them(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    row = _stored_feed(
        db_session, company, etag='"old"', last_modified="Sat, 03 Oct 2026 00:00:00 GMT"
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        atom = mock.get(ATOM).mock(
            return_value=httpx.Response(
                200,
                content=_bytes("company_feed.xml"),
                headers={"ETag": '"new"', "Last-Modified": "Sun, 04 Oct 2026 16:00:00 GMT"},
            )
        )
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
        sent = atom.calls.last.request.headers
        assert sent["If-None-Match"] == '"old"'
        assert sent["If-Modified-Since"] == "Sat, 03 Oct 2026 00:00:00 GMT"
    assert run.inserted == 3
    assert (row.etag, row.last_modified, row.last_polled_at) == (
        '"new"',
        "Sun, 04 Oct 2026 16:00:00 GMT",
        NOW,
    )
    matches = {
        json.dumps(p["match"], sort_keys=True) for p in db_session.scalars(select(EventRow.payload))
    }
    assert matches == {'{"confidence": 0.98, "rule": "R1"}'}


@pytest.mark.db
def test_stored_feed_without_validators_sends_no_conditional_headers(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    _stored_feed(db_session, company)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        atom = mock.get(ATOM).mock(return_value=httpx.Response(304))
        ingest(db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets())
        sent = atom.calls.last.request.headers
        assert "If-None-Match" not in sent
        assert "If-Modified-Since" not in sent


@pytest.mark.db
@pytest.mark.parametrize(
    ("response", "status", "errors", "polled"),
    [
        (httpx.Response(404), "dead", 0, True),
        (httpx.Response(410), "dead", 0, True),
        (httpx.Response(403), "active", 1, True),
        (httpx.Response(500), "active", 1, True),
        (httpx.ConnectError("down"), "active", 1, False),
    ],
)
def test_stored_feed_failures(
    db_session: Session,
    client: httpx.Client,
    response: httpx.Response | Exception,
    status: str,
    errors: int,
    polled: bool,
) -> None:
    company = _company(db_session)
    row = _stored_feed(db_session, company)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        route = mock.get(ATOM)
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
        assert route.call_count == 1
    assert row.status == status
    assert run.errors == errors
    assert (row.last_polled_at == NOW) is polled


@pytest.mark.db
def test_fresh_dead_and_paused_feeds_are_not_polled(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    _stored_feed(db_session, company, last_polled_at=NOW - timedelta(hours=2))
    db_session.add(CompanyFeed(company_id=company.id, feed_url=f"{BLOG}/dead.xml", status="dead"))
    db_session.add(
        CompanyFeed(company_id=company.id, feed_url=f"{BLOG}/paused.xml", status="paused")
    )
    db_session.flush()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        ingest(db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets())
        assert _urls(mock) == [GNEWS]


# --- feed discovery ----------------------------------------------------------------------------------


def test_discovery_reads_link_rel_alternate_on_the_blog(client: httpx.Client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(200, content=_bytes("blog.html")))
        mock.get(ATOM).mock(
            return_value=httpx.Response(
                200, content=_bytes("company_feed.xml"), headers={"ETag": '"e"'}
            )
        )
        found = discover_feed(client, DOMAIN, budgets=_budgets())
        assert _urls(mock) == [BLOG, ATOM]
    assert found is not None
    assert (found.feed_url, found.discovered_from, found.etag, found.last_modified) == (
        ATOM,
        BLOG,
        '"e"',
        None,
    )


def test_discovery_falls_back_in_order(client: httpx.Client) -> None:
    base = f"https://{DOMAIN}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(404))
        mock.get(HOME).mock(return_value=httpx.Response(200, content=b"<html><head></head></html>"))
        mock.get(f"{base}/feed").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/rss").mock(
            return_value=httpx.Response(200, content=b"<html>not a feed</html>")
        )
        mock.get(f"{base}/blog/rss.xml").mock(
            return_value=httpx.Response(200, content=_bytes("company_feed.xml"))
        )
        last = mock.get(f"{base}/blog/feed").mock(return_value=httpx.Response(200))
        found = discover_feed(client, DOMAIN, budgets=_budgets())
        assert _urls(mock) == [BLOG, HOME, f"{base}/feed", f"{base}/rss", f"{base}/blog/rss.xml"]
        assert last.call_count == 0
    assert found is not None
    assert found.feed_url == f"{base}/blog/rss.xml"
    assert found.discovered_from == f"{base}/blog/rss.xml"


def test_discovery_tries_every_fallback_then_gives_up(client: httpx.Client) -> None:
    base = f"https://{DOMAIN}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{base}/.*").mock(return_value=httpx.Response(404))
        assert discover_feed(client, DOMAIN, budgets=_budgets()) is None
        assert _urls(mock) == [BLOG, HOME, *(f"{base}{path}" for path in FALLBACK_PATHS)]


def test_discovery_ignores_off_domain_and_empty_feeds(client: httpx.Client) -> None:
    page = (
        b"<html><head>"
        b'<link rel="alternate" type="application/rss+xml" href="https://medium.example/feed/q">'
        b'<link rel="alternate" type="application/rss+xml" href="/empty.xml">'
        b"</head></html>"
    )
    base = f"https://{DOMAIN}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(200, content=page))
        mock.get(f"{base}/empty.xml").mock(
            return_value=httpx.Response(200, content=_bytes("empty.xml"))
        )
        mock.get(HOME).mock(return_value=httpx.Response(200, content=page))
        mock.get(f"{base}/feed").mock(
            return_value=httpx.Response(200, content=_bytes("company_feed.xml"))
        )
        found = discover_feed(client, DOMAIN, budgets=_budgets())
        # The homepage advertises the same empty feed: it is not fetched twice.
        assert _urls(mock) == [BLOG, f"{base}/empty.xml", HOME, f"{base}/feed"]
    assert found is not None
    assert found.feed_url == f"{base}/feed"


def test_discovery_rejects_a_feed_that_redirects_off_domain(client: httpx.Client) -> None:
    base = f"https://{DOMAIN}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(404))
        mock.get(HOME).mock(return_value=httpx.Response(404))
        mock.get(f"{base}/feed").mock(
            return_value=httpx.Response(301, headers={"Location": "https://medium.example/feed"})
        )
        mock.get("https://medium.example/feed").mock(
            return_value=httpx.Response(200, content=_bytes("company_feed.xml"))
        )
        mock.get(url__regex=rf"{base}/(rss|blog/rss\.xml|blog/feed)").mock(
            return_value=httpx.Response(404)
        )
        assert discover_feed(client, DOMAIN, budgets=_budgets()) is None


class _RecordingBudgets(HostRateBudgets):
    def __init__(self) -> None:
        super().__init__(1.0, sleep=lambda _seconds: None)
        self.charged: list[str] = []

    def acquire(self, url: str) -> float:
        self.charged.append(urlsplit(url).hostname or "")
        return super().acquire(url)


def test_site_redirect_hops_are_charged_to_each_host(client: httpx.Client) -> None:
    budgets = _RecordingBudgets()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://{DOMAIN}/feed").mock(
            return_value=httpx.Response(
                301, headers={"Location": "https://www.quillstone.example/feed"}
            )
        )
        mock.get("https://www.quillstone.example/feed").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.example/feed.xml"})
        )
        mock.get("https://cdn.example/feed.xml").mock(
            return_value=httpx.Response(200, content=_bytes("company_feed.xml"))
        )
        response = news._site_get(client, f"https://{DOMAIN}/feed", budgets, accept="*/*")
    assert response.status_code == 200
    assert str(response.url) == "https://cdn.example/feed.xml"
    assert budgets.charged == [DOMAIN, "www.quillstone.example", "cdn.example"]


def test_site_redirect_loop_is_cut_off(client: httpx.Client) -> None:
    budgets = _RecordingBudgets()
    url = f"https://{DOMAIN}/feed"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).mock(return_value=httpx.Response(302, headers={"Location": url}))
        with pytest.raises(httpx.TooManyRedirects):
            news._site_get(client, url, budgets, accept="*/*")
    assert len(budgets.charged) == news.SITE_MAX_REDIRECTS + 1


def test_feed_links() -> None:
    links = feed_links(_bytes("blog.html"), BLOG)
    assert links == [ATOM]
    page = (
        b'<html><head><link rel="Alternate feed" type="application/rss+xml; charset=utf-8" href="rss.xml">'
        b'<link rel="alternate" type="application/rss+xml" href="rss.xml">'
        b'<link rel="alternate" type="application/rss+xml" href="">'
        b"</head></html>"
    )
    assert feed_links(page, "https://a.test/blog/") == ["https://a.test/blog/rss.xml"]
    assert feed_links(b"", BLOG) == []
    assert feed_links(b"   ", BLOG) == []


def test_discover_feed_uses_default_site_budgets(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Company sites have their own per-host budget; the Google News rate does not apply.
    monkeypatch.setenv("TELL_RATE_NEWS_RPS", "0.001")
    monkeypatch.setattr(news, "SITE_RPS", 1000.0)
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(200, content=_bytes("blog.html")))
        atom = mock.get(ATOM).mock(
            return_value=httpx.Response(200, content=_bytes("company_feed.xml"))
        )
        found = discover_feed(client, DOMAIN)
        # Every company-site request carries the 10-second site timeout.
        assert atom.calls.last.request.extensions["timeout"]["connect"] == news.SITE_TIMEOUT_S
    assert found is not None


def test_discovery_stops_at_the_first_unanswered_probe(client: httpx.Client) -> None:
    base = f"https://{DOMAIN}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(404))
        mock.get(HOME).mock(side_effect=httpx.ReadTimeout("slow"))
        later = mock.get(url__regex=rf"{base}/(feed|rss|blog/.*)").mock(
            return_value=httpx.Response(200, content=_bytes("company_feed.xml"))
        )
        assert discover_feed(client, DOMAIN, budgets=_budgets()) is None
        assert later.call_count == 0


def test_discovery_with_an_invalid_domain_is_unreachable(client: httpx.Client) -> None:
    with respx.mock(assert_all_called=False):
        assert discover_feed(client, "bad domain\x00.example", budgets=_budgets()) is None


# --- rotation ----------------------------------------------------------------------------------------


def _companies(session: Session, ids: range) -> None:
    for company_id in ids:
        session.add(
            Company(id=company_id, domain=f"c{company_id}.example", name=f"Company {company_id}")
        )
    session.flush()


def _event(session: Session, company_id: int, occurred_at: datetime) -> None:
    session.add(
        EventRow(
            event_id=secrets.token_hex(8),
            company_id=company_id,
            source="greenhouse",
            type="job_opened",
            occurred_at=occurred_at,
            observed_at=occurred_at,
            url="https://example.test/job",
            payload={},
            run_id="r0",
        )
    )


def _news_run(session: Session, started_at: datetime, polled: dict[str, str]) -> None:
    session.add(
        Run(
            run_id=f"news-{secrets.token_hex(4)}",
            source="news",
            started_at=started_at,
            finished_at=started_at,
            status="ok",
            notes={"polled": polled},
        )
    )


@pytest.mark.db
def test_rotation_selection(db_session: Session) -> None:
    _companies(db_session, range(1, 15))
    with freeze_time("2026-10-07 09:00:00+00:00"):  # a Wednesday: weekday() == 2
        now = utcnow()
        _event(db_session, 3, now - timedelta(days=10))  # active in 30 days
        _event(db_session, 5, now - timedelta(days=40))  # too old to count
        _event(db_session, 6, now + timedelta(days=2))  # future-dated: ignored
        watchlist = Watchlist(owner="owner-handle", name="pilot")
        db_session.add(watchlist)
        db_session.flush()
        db_session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=11))
        db_session.flush()

        chosen = [c.id for c in select_companies(db_session, now)]
        # watchlist first, then active in 30 days, then the id % 7 == 2 bucket
        assert chosen == [11, 3, 2, 9]

        _news_run(
            db_session, now - timedelta(hours=2), {"3": (now - timedelta(hours=2)).isoformat()}
        )
        _news_run(
            db_session, now - timedelta(hours=7), {"11": (now - timedelta(hours=7)).isoformat()}
        )
        _news_run(db_session, now - timedelta(hours=3), {"9": "not a date", "x": now.isoformat()})
        db_session.flush()
        # Priority companies are polled at most once per 24 hours, not every 6.
        assert [c.id for c in select_companies(db_session, now)] == [2, 9]
        assert recently_polled(db_session, now) == {3: now - timedelta(hours=2)}
        assert last_polled(db_session, now) == {
            3: now - timedelta(hours=2),
            11: now - timedelta(hours=7),
        }

        # Explicit ids replace the rotation and only respect the 6-hour cache.
        explicit = select_companies(db_session, now, company_ids=[3, 5, 11, 12])
        assert [c.id for c in explicit] == [5, 11, 12]


@pytest.mark.db
def test_rotation_intervals_daily_for_priority_weekly_for_the_tail(db_session: Session) -> None:
    _companies(db_session, range(1, 15))
    now = NOW  # a Monday: the tail bucket is id % 7 == 0, i.e. 7 and 14
    for company_id in (1, 2, 3, 4):
        _event(db_session, company_id, now - timedelta(days=5))
    db_session.flush()

    def ago(**delta: float) -> str:
        return (now - timedelta(**delta)).isoformat()

    _news_run(
        db_session,
        now - timedelta(days=6, hours=23, minutes=30),
        {"7": ago(days=6, hours=23, minutes=30), "4": ago(days=6)},
    )
    _news_run(db_session, now - timedelta(days=3), {"14": ago(days=3)})
    _news_run(db_session, now - timedelta(hours=25), {"1": ago(hours=25)})
    _news_run(db_session, now - timedelta(hours=23, minutes=30), {"2": ago(hours=23, minutes=30)})
    _news_run(db_session, now - timedelta(hours=22), {"3": ago(hours=22)})
    db_session.flush()

    chosen = [c.id for c in select_companies(db_session, now)]
    # 3 polled 22h ago: not due. 2 at 23h30 is due (one hour of slack for cron jitter).
    # Within the active group the least recently polled come first: 4 (6 days), 1, 2.
    # Tail: 7 was polled a week ago (less slack): due; 14 three days ago: not due.
    assert chosen == [4, 1, 2, 7]


@pytest.mark.db
def test_polls_older_than_the_lookback_are_ignored(db_session: Session) -> None:
    _companies(db_session, range(7, 8))
    _news_run(db_session, NOW - timedelta(days=8), {"7": (NOW - timedelta(days=8)).isoformat()})
    db_session.flush()
    assert last_polled(db_session, NOW) == {}
    assert [c.id for c in select_companies(db_session, NOW)] == [7]


@pytest.mark.db
def test_rotation_buckets_cover_every_company_in_a_week(db_session: Session) -> None:
    _companies(db_session, range(1, 22))
    seen: list[int] = []
    for day in range(7):
        seen.extend(c.id for c in select_companies(db_session, NOW + timedelta(days=day)))
    assert sorted(seen) == list(range(1, 22))


@pytest.mark.db
def test_ingest_without_company_ids_uses_rotation(
    db_session: Session, client: httpx.Client
) -> None:
    db_session.add(Company(id=7, domain=DOMAIN, name=NAME))  # 7 % 7 == 0 == Monday
    db_session.add(Company(id=8, domain="other.example", name="Other"))
    db_session.flush()
    with respx.mock(assert_all_called=False) as mock:
        _mock_full_company(mock)
        run = ingest(db_session, now=NOW, client=client, budgets=_budgets())
        assert GNEWS in _urls(mock)
        assert google_news_url("Other") not in _urls(mock)
    assert run.notes is not None
    assert run.notes["selected"] == 1


@pytest.mark.db
def test_collect_builds_and_closes_its_own_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[httpx.Client] = []

    def factory() -> httpx.Client:
        http = make_client()
        created.append(http)
        return http

    monkeypatch.setattr(news, "make_client", factory)
    run_id = start_run(db_session, "news", now=NOW)
    result = news.collect(db_session, run_id=run_id, now=NOW, company_ids=[])
    assert result.selected == 0
    assert len(created) == 1
    assert created[0].is_closed


# --- run budget ----------------------------------------------------------------------------------


def test_budget_defaults() -> None:
    budget_minutes = RUN_BUDGET.total_seconds() / 60
    google_news_minutes = MAX_COMPANIES / 60  # one query per company at 1 rps
    assert (MAX_COMPANIES, budget_minutes) == (1200, 25)
    # The Google News queries alone fit the 30-minute filings job with margin.
    assert google_news_minutes <= budget_minutes < 30


def test_request_cap_keeps_four_runs_a_day_under_the_spec_guidance() -> None:
    assert MAX_GOOGLE_REQUESTS == 600
    assert 4 * MAX_GOOGLE_REQUESTS < 3000  # SPEC section 14: under 3,000 requests a day
    assert MAX_GOOGLE_REQUESTS <= MAX_COMPANIES


def test_is_block_only_failure_reads_status_and_notes() -> None:
    def run(status: str, **notes: Any) -> Run:
        return Run(run_id="r", source="news", status=status, notes=notes, started_at=NOW)

    assert is_block_only_failure(run("failed", blocked=True))
    assert not is_block_only_failure(run("ok", blocked=False))
    assert not is_block_only_failure(run("failed", blocked=False, drift="google_news: 1"))
    assert not is_block_only_failure(run("failed", blocked=True, drift="google_news: 1"))
    assert not is_block_only_failure(run("failed", blocked=True, error="RuntimeError: x"))
    assert not is_block_only_failure(run("failed", error="RuntimeError: x"))
    other = Run(run_id="r", source="edgar", status="failed", notes={"blocked": True})
    assert not is_block_only_failure(other)


def _three_companies(session: Session) -> list[Company]:
    return [
        _company(session, name=f"Budget {letter}", domain=f"budget-{letter}.example")
        for letter in ("a", "b", "c")
    ]


def _mock_empty(mock: respx.MockRouter) -> None:
    mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
        return_value=httpx.Response(200, content=_bytes("empty.xml"))
    )
    mock.get(url__regex=r"https://budget-[abc]\.example/.*").mock(return_value=httpx.Response(404))


@pytest.mark.db
def test_request_cap_stops_the_run_cleanly_and_defers_the_rest(
    db_session: Session, client: httpx.Client
) -> None:
    companies = _three_companies(db_session)
    with respx.mock(assert_all_called=False) as mock:
        _mock_empty(mock)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
            max_requests=2,
        )
        queried = [u for u in _urls(mock) if u.startswith("https://news.google.com/")]
        assert queried == [google_news_url(c.name) for c in companies[:2]]
    assert run.status == "ok"
    assert run.notes is not None
    assert run.notes["google_requests"] == 2
    assert run.notes["request_cap_reached"] is True
    assert run.notes["deferred"] == 1
    assert run.notes["polled_count"] == 2
    assert run.notes["budget_exhausted"] is False


@pytest.mark.db
def test_max_companies_caps_the_run_and_defers_the_rest(
    db_session: Session, client: httpx.Client
) -> None:
    companies = _three_companies(db_session)
    with respx.mock(assert_all_called=False) as mock:
        _mock_empty(mock)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
            max_companies=2,
        )
        queried = [u for u in _urls(mock) if u.startswith("https://news.google.com/")]
        assert queried == [google_news_url(c.name) for c in companies[:2]]
    assert run.status == "ok"
    assert run.notes is not None
    assert run.notes["selected"] == 3
    assert run.notes["deferred"] == 1
    assert run.notes["polled_count"] == 2
    assert run.notes["budget_exhausted"] is False


@pytest.mark.db
def test_wall_clock_budget_stops_cleanly(db_session: Session, client: httpx.Client) -> None:
    companies = _three_companies(db_session)
    ticks = iter([0.0, 0.0, 10.0, 26 * 60.0, 27 * 60.0])
    with respx.mock(assert_all_called=False) as mock:
        _mock_empty(mock)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
            clock=lambda: next(ticks),
        )
        queried = [u for u in _urls(mock) if u.startswith("https://news.google.com/")]
        assert queried == [google_news_url(c.name) for c in companies[:2]]
    assert run.status == "ok"
    assert run.notes is not None
    assert run.notes["budget_exhausted"] is True
    assert run.notes["deferred"] == 1
    assert run.notes["polled"] == {
        str(companies[0].id): "2026-10-05T12:00:00+00:00",
        str(companies[1].id): "2026-10-05T12:00:00+00:00",
    }


@pytest.mark.db
def test_budget_keeps_priority_order(db_session: Session, client: httpx.Client) -> None:
    _companies(db_session, range(1, 15))
    _event(db_session, 3, NOW - timedelta(days=2))
    watchlist = Watchlist(owner="owner-handle", name="pilot")
    db_session.add(watchlist)
    db_session.flush()
    db_session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=12))
    db_session.flush()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(200, content=_bytes("empty.xml"))
        )
        mock.get(url__regex=r"https://c\d+\.example/.*").mock(return_value=httpx.Response(404))
        run = ingest(db_session, now=NOW, client=client, budgets=_budgets(), max_companies=2)
        queried = [u for u in _urls(mock) if u.startswith("https://news.google.com/")]
    # Watchlist member, then the active company; the Monday tail bucket (7, 14) waits.
    assert queried == [google_news_url("Company 12"), google_news_url("Company 3")]
    assert run.notes is not None
    assert (run.notes["selected"], run.notes["deferred"]) == (4, 2)


# --- company-site concurrency, separate budgets, short transactions --------------------------------


class _CountingBudgets(HostRateBudgets):
    """Records every host a token was taken for; never sleeps."""

    def __init__(self) -> None:
        super().__init__(1.0, sleep=lambda _seconds: None)
        self.hosts: list[str] = []

    def for_host(self, host: str) -> news.RateBudget:
        self.hosts.append(host.lower())
        return super().for_host(host)


@pytest.mark.db
def test_google_news_and_company_sites_use_separate_budgets(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    google, sites = _CountingBudgets(), _CountingBudgets()
    with respx.mock(assert_all_called=True) as mock:
        _mock_full_company(mock)
        ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[company.id],
            budgets=google,
            site_budgets=sites,
        )
    assert google.hosts == ["news.google.com"]
    assert sites.hosts == [DOMAIN, DOMAIN]


@pytest.mark.db
def test_dead_hosts_are_waited_on_in_parallel(db_session: Session, client: httpx.Client) -> None:
    companies = [
        _company(db_session, name=f"Dead {index}", domain=f"dead-{index}.example")
        for index in range(12)
    ]
    hang = 0.2

    def dead_host(request: httpx.Request) -> httpx.Response:
        time.sleep(hang)  # stands in for the 10-second connect timeout
        raise httpx.ConnectTimeout("no answer", request=request)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(200, content=_bytes("empty.xml"))
        )
        sites = mock.get(url__regex=r"https://dead-\d+\.example/.*").mock(side_effect=dead_host)
        began = time.monotonic()
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
            site_workers=6,
        )
        elapsed = time.monotonic() - began
        assert sites.call_count == len(companies)  # one probe per dead host, then stop
    # One at a time this is 12 x 0.2 s = 2.4 s; six workers wait on them together.
    assert elapsed < len(companies) * hang / 2
    assert (run.status, run.errors) == ("ok", 0)
    assert run.notes is not None
    assert run.notes["site"]["unreachable"] == len(companies)
    assert run.notes["polled_count"] == len(companies)


@pytest.mark.db
def test_one_worker_with_a_full_backlog_still_records_every_company(
    db_session: Session, client: httpx.Client
) -> None:
    companies = _three_companies(db_session)
    with respx.mock(assert_all_called=False) as mock:
        _mock_empty(mock)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
            site_workers=1,
        )
    assert run.notes is not None
    assert sorted(run.notes["no_feed"]) == sorted(c.id for c in companies)
    with pytest.raises(ValueError, match="site_workers"):
        news.collect(db_session, run_id="r", now=NOW, company_ids=[], site_workers=0)


@pytest.mark.db
def test_no_transaction_is_open_during_network_io(
    db_session: Session, client: httpx.Client, engine: Any
) -> None:
    companies = [
        _company(db_session),
        _company(db_session, name="Brightloom", domain="brightloom.example"),
        _company(db_session, name="Corvana", domain="corvana.example"),
    ]
    db_session.commit()
    open_during_query: list[bool] = []
    seen_by_others: list[int] = []

    def google(request: httpx.Request) -> httpx.Response:
        open_during_query.append(db_session.in_transaction())
        with Session(bind=engine) as other:  # what another connection sees right now
            seen_by_others.append(_count(other, EventRow))
        return httpx.Response(200, content=_bytes("normal.xml"), headers=RSS_HEADERS)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(side_effect=google)
        mock.get(url__regex=r"https://(quillstone|brightloom|corvana)\.example/.*").mock(
            return_value=httpx.Response(404)
        )
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
        )
    assert open_during_query == [False, False, False]
    assert seen_by_others[0] == 0
    assert seen_by_others[1] > 0  # the first company was committed before the second query
    assert run.status == "ok"
    with Session(bind=engine) as other:
        row = other.get(Run, run.run_id)
        assert row is not None
        assert row.status == "ok"


@pytest.mark.db
def test_no_transaction_is_open_while_waiting_for_site_workers(
    db_session: Session, client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backlog full after Google News wrote events, and the final drain: both commit first."""
    companies = [
        _company(db_session, domain=f"q{index}.example") for index in range(5)
    ]  # one name: every company gets Google News events
    db_session.commit()
    open_during_wait: list[bool] = []
    real_wait = news.wait

    def spy(*args: Any, **kwargs: Any) -> Any:
        open_during_wait.append(db_session.in_transaction())
        return real_wait(*args, **kwargs)

    def slow_site(request: httpx.Request) -> httpx.Response:
        time.sleep(0.2)
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(news, "wait", spy)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(
            return_value=httpx.Response(200, content=_bytes("normal.xml"), headers=RSS_HEADERS)
        )
        mock.get(url__regex=r"https://q\d\.example/.*").mock(side_effect=slow_site)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
            site_workers=1,
        )
    assert run.status == "ok"
    assert run.inserted > 0
    assert len(open_during_wait) >= 2  # the full backlog and the final drain both waited
    assert not any(open_during_wait)


@pytest.mark.db
def test_progress_notes_are_committed_while_the_run_is_open(
    db_session: Session, client: httpx.Client, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news, "PROGRESS_EVERY", 1)
    companies = _three_companies(db_session)
    db_session.commit()
    snapshots: list[dict[str, Any]] = []

    def google(request: httpx.Request) -> httpx.Response:
        with Session(bind=engine) as other:
            row = other.scalars(select(Run).where(Run.source == "news")).one()
            snapshots.append({"status": row.status, **(row.notes or {})})
        return httpx.Response(200, content=_bytes("empty.xml"))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://news\.google\.com/rss/search.*").mock(side_effect=google)
        mock.get(url__regex=r"https://budget-[abc]\.example/.*").mock(
            return_value=httpx.Response(404)
        )
        ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
        )
    assert [snap["status"] for snap in snapshots] == ["running"] * 3
    assert "polled" not in snapshots[0]
    assert list(snapshots[2]["polled"]) == [str(companies[0].id), str(companies[1].id)]
    # A run killed now would still keep the rotation and the discovery memory.
    assert last_polled(db_session, NOW + timedelta(hours=1)).keys() >= {companies[0].id}


@pytest.mark.db
def test_a_crash_keeps_committed_companies_and_fails_the_run(
    db_session: Session, client: httpx.Client, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _company(db_session)
    second = _company(db_session, name="Brightloom", domain="brightloom.example")
    db_session.commit()
    real = news._poll_google_news

    def flaky(run: Any, company: Company) -> bool:
        if company.id == second.id:
            msg = "database went away"
            raise RuntimeError(msg)
        return real(run, company)

    monkeypatch.setattr(news, "_poll_google_news", flaky)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GNEWS).mock(
            return_value=httpx.Response(200, content=_bytes("normal.xml"), headers=RSS_HEADERS)
        )
        mock.get(url__regex=r"https://(quillstone|brightloom)\.example/.*").mock(
            return_value=httpx.Response(404)
        )
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[first.id, second.id],
            budgets=_budgets(),
        )
    assert run.status == "failed"
    assert run.notes is not None
    assert run.notes["error"] == "RuntimeError: database went away"
    assert run.errors == 1
    assert run.inserted == 6
    with Session(bind=engine) as other:
        assert _count(other, EventRow) == 6  # the first company's events were committed


@pytest.mark.db
def test_a_crash_without_commit_propagates(db_session: Session, client: httpx.Client) -> None:
    with pytest.raises(ValueError, match="unknown cohort"):
        ingest(db_session, now=NOW, client=client, cohort="nope", commit=None)


@pytest.mark.db
def test_a_failing_site_job_is_one_error_and_the_run_continues(
    db_session: Session, client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    companies = _three_companies(db_session)
    real = news._site_work

    def flaky(http: httpx.Client, budgets: HostRateBudgets, plan: Any) -> Any:
        if plan.company.domain == "budget-b.example":
            msg = "parser exploded"
            raise RuntimeError(msg)
        return real(http, budgets, plan)

    monkeypatch.setattr(news, "_site_work", flaky)
    with respx.mock(assert_all_called=False) as mock:
        _mock_empty(mock)
        run = ingest(
            db_session,
            now=NOW,
            client=client,
            company_ids=[c.id for c in companies],
            budgets=_budgets(),
        )
    assert (run.status, run.errors) == ("ok", 1)
    assert run.notes is not None
    assert sorted(run.notes["no_feed"]) == sorted([companies[0].id, companies[2].id])


@pytest.mark.db
def test_a_dead_feed_is_rediscovered_after_thirty_days(
    db_session: Session, client: httpx.Client
) -> None:
    company = _company(db_session)
    row = _stored_feed(db_session, company, status="dead", last_polled_at=NOW - timedelta(days=3))
    with respx.mock(assert_all_called=False) as mock:
        _mock_full_company(mock)
        soon = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
        assert _urls(mock) == [GNEWS]
        later = NOW + timedelta(days=30)
        again = ingest(
            db_session, now=later, client=client, company_ids=[company.id], budgets=_budgets()
        )
        assert _urls(mock)[1:] == [GNEWS, BLOG, ATOM]
    assert soon.notes is not None
    assert soon.notes["site"]["discovery_not_due"] == 1
    assert again.notes is not None
    assert again.notes["site"]["discovered"] == 1
    db_session.refresh(row)
    assert (row.status, row.last_polled_at, row.etag) == ("active", later, '"v1"')


@pytest.mark.db
def test_a_feed_stored_for_another_company_is_read_but_not_owned(
    db_session: Session, client: httpx.Client
) -> None:
    owner = _company(db_session, name="Quillstone Holdings", domain="holdings.example")
    company = _company(db_session)
    other = _stored_feed(db_session, owner, last_polled_at=NOW)
    with respx.mock(assert_all_called=False) as mock:
        _mock_full_company(mock)
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
    assert run.notes is not None
    assert run.notes["no_feed"] == [company.id]
    assert run.notes["site"]["discovered"] == 1
    assert other.company_id == owner.id
    assert run.inserted > 0


@pytest.mark.db
def test_fresh_feeds_are_counted_as_cached(db_session: Session, client: httpx.Client) -> None:
    company = _company(db_session)
    _stored_feed(db_session, company, last_polled_at=NOW - timedelta(hours=1))
    with respx.mock(assert_all_called=True) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
    assert run.notes is not None
    assert run.notes["site"]["feeds_cached"] == 1


# --- leadership redaction (ADR 0016, provisional) --------------------------------------------------

LEADERSHIP_HEADLINES = [
    "Quillstone Analytics appoints Jane Doe as chief revenue officer",
    "Quillstone Analytics Appoints Jane Doe As Chief Revenue Officer",
    "Quillstone Analytics names John Roe as its new chief financial officer",
    "Quillstone Analytics hires Ada Example as head of sales",
    "Quillstone Analytics promotes Max Placeholder to president",
    "Jane Doe joins Quillstone Analytics as chief technology officer",
    "Quillstone Analytics appoints Jane Doe as CFO and John Roe as COO",
    "Quillstone Analytics appoints Jane Doe, former rival executive, as CFO",
    "Quillstone Analytics names former payments executive as chief financial officer",
]


def _leadership_events(titles: list[str]) -> list[Event]:
    feed = NewsFeed(
        origin="google_news",
        feed_url=GNEWS,
        company=CompanyRef(name=NAME, domain=DOMAIN),
        items=[
            NewsItem(
                title=f"{title} - Ledger Daily",
                link=f"https://news.google.com/rss/articles/lead-{index}",
                pub_date=NOW,
                source="Ledger Daily",
            )
            for index, title in enumerate(titles)
        ],
    )
    events, dropped = derive_events(feed, company_id=None, run_id="r", now=NOW)
    assert dropped == 0
    return events


def test_leadership_titles_are_stored_redacted() -> None:
    events = _leadership_events(LEADERSHIP_HEADLINES)
    assert [e.type for e in events] == ["news_leadership"] * len(LEADERSHIP_HEADLINES)
    titles = [e.payload["title"] for e in events]
    assert titles == [
        "Quillstone Analytics appoints [person] as chief revenue officer",
        "Quillstone Analytics Appoints [person] As Chief Revenue Officer",
        "Quillstone Analytics names [person] as its new chief financial officer",
        "Quillstone Analytics hires [person] as head of sales",
        "Quillstone Analytics promotes [person] to president",
        "[person] joins Quillstone Analytics as chief technology officer",
        "Quillstone Analytics appoints [person] as CFO and [person] as COO",
        "appoints as CFO",
        "names as chief financial officer",
    ]
    for event, raw in zip(events, LEADERSHIP_HEADLINES, strict=True):
        assert event.payload["title_redacted"] is True
        assert raw not in event.payload["title"]
        assert set(event.payload) == PAYLOAD_KEYS
        assert event.url == event.external_id  # the URL stays the receipt


def test_no_leadership_payload_title_contains_the_capitalised_run() -> None:
    names = ["Jane Doe", "John Roe", "Ada Example", "Max Placeholder"]
    events = _leadership_events(LEADERSHIP_HEADLINES)
    stored = json.dumps([e.payload for e in events])
    for name in names:
        for word in name.split():
            assert word not in stored, word


def test_non_leadership_titles_are_unchanged() -> None:
    feed = NewsFeed.model_validate_json(_bytes("normal.json"))
    events, _ = derive_events(feed, company_id=None, run_id="r", now=NOW)
    raw = {item.link: item.title for item in feed.items}
    for event in events:
        if event.type != "news_leadership":
            assert event.payload["title"] == raw[event.url]
            assert event.payload["title_redacted"] is False


# --- migrations ------------------------------------------------------------------------------------


@pytest.mark.db
def test_company_feeds_table_is_migrated(db_session: Session) -> None:
    conn = db_session.connection()
    insp = inspect(conn)
    assert "company_feeds" in insp.get_table_names()
    assert "version_marker" in {c["name"] for c in insp.get_columns("postings")}
    # At or past 0003 (later phases add migrations on top).
    assert int(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()) >= 3


@pytest.mark.db
def test_migration_0003_upgrades_and_downgrades() -> None:
    admin = make_url(admin_url())
    name = f"tell_test_news_{secrets.token_hex(4)}"
    admin_engine = make_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    url = admin.set(database=name).render_as_string(hide_password=False)
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0002")
        engine = make_engine(url)
        try:
            with engine.connect() as conn:
                assert "version_marker" in {
                    c["name"] for c in inspect(conn).get_columns("postings")
                }
                assert "company_feeds" not in inspect(conn).get_table_names()
            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                assert "company_feeds" in inspect(conn).get_table_names()
            command.downgrade(cfg, "0002")
            with engine.connect() as conn:
                assert "company_feeds" not in inspect(conn).get_table_names()
        finally:
            engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin_engine.dispose()
