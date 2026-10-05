"""News collector: parsing, name guard, events, block policy, feed discovery, rotation.

Every HTTP call goes through ``respx``; the rate budget's sleeper is a no-op so the
1 rps limit costs no wall time.
"""

from __future__ import annotations

import json
import secrets
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
from tell.collectors.base import sha256_hex, start_run, utcnow
from tell.collectors.news import (
    FALLBACK_PATHS,
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
PAYLOAD_KEYS = {"title", "source", "matched_regex", "category", "query", "match", "key"}


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
    assert run.status == "ok"
    assert run.errors == 1
    assert run.notes is not None
    assert run.notes["blocked"] is True
    assert run.notes["blocked_by"] == {"status": status, "url": GNEWS, "company_id": first.id}
    assert run.notes["polled"] == {}
    assert _count(db_session, EventRow) == 0


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
def test_discovery_transport_error_is_not_a_miss(db_session: Session, client: httpx.Client) -> None:
    company = _company(db_session)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GNEWS).mock(return_value=httpx.Response(200, content=_bytes("empty.xml")))
        mock.get(BLOG).mock(side_effect=httpx.ConnectTimeout("slow"))
        mock.get(url__regex=rf"https://{DOMAIN}/.*").mock(return_value=httpx.Response(404))
        run = ingest(
            db_session, now=NOW, client=client, company_ids=[company.id], budgets=_budgets()
        )
    assert run.errors == 1
    assert run.notes is not None
    assert run.notes["no_feed"] == []
    assert run.notes["polled"] == {str(company.id): "2026-10-05T12:00:00+00:00"}


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


def test_discover_feed_uses_default_budgets(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELL_RATE_NEWS_RPS", "1000")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).mock(return_value=httpx.Response(200, content=_bytes("blog.html")))
        mock.get(ATOM).mock(return_value=httpx.Response(200, content=_bytes("company_feed.xml")))
        found = discover_feed(client, DOMAIN)
    assert found is not None


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
        # priority (active + watchlist) first, then the id % 7 == 2 bucket
        assert chosen == [3, 11, 2, 9]

        _news_run(
            db_session, now - timedelta(hours=2), {"3": (now - timedelta(hours=2)).isoformat()}
        )
        _news_run(
            db_session, now - timedelta(hours=7), {"11": (now - timedelta(hours=7)).isoformat()}
        )
        _news_run(db_session, now - timedelta(hours=3), {"9": "not a date", "x": now.isoformat()})
        db_session.flush()
        assert [c.id for c in select_companies(db_session, now)] == [11, 2, 9]
        assert recently_polled(db_session, now) == {3: now - timedelta(hours=2)}

        explicit = select_companies(db_session, now, company_ids=[3, 5, 12])
        assert [c.id for c in explicit] == [5, 12]


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


# --- migrations ------------------------------------------------------------------------------------


@pytest.mark.db
def test_company_feeds_table_is_migrated(db_session: Session) -> None:
    conn = db_session.connection()
    insp = inspect(conn)
    assert "company_feeds" in insp.get_table_names()
    assert "version_marker" in {c["name"] for c in insp.get_columns("postings")}
    assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003"


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
