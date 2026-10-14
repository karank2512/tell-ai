"""YC directory seeder: normalisation, location parsing, idempotent upserts, fetch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pydantic
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.discovery import (
    PLATFORM_DOMAINS,
    cli,
    is_platform_domain,
    platform_domain,
    registrable_domain,
    yc,
)
from tell.discovery.yc import (
    YC_DIRECTORY_URL,
    YcCompany,
    YcRecord,
    fetch_directory,
    parse_directory,
    parse_location,
    platform_companies,
    prune_platform_companies,
    seed_companies,
    seed_from_file,
)
from tell.http import make_client
from tell.models import (
    AtsBoard,
    Company,
    CompanyAlias,
    CompanyFeed,
    EventRow,
    Feedback,
    Score,
    Signal,
    Watchlist,
    WatchlistItem,
)

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "yc" / "sample.json"
ALLOWED_KEYS = {"name", "website", "batch", "all_locations"}


def _sample() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return data


def _companies(session: Session) -> dict[str, Company]:
    return {c.domain: c for c in session.scalars(select(Company))}


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Company)) or 0


# --- fixture ------------------------------------------------------------------------------------


def test_sample_fixture_is_trimmed_and_scrubbed() -> None:
    raw = SAMPLE.read_text(encoding="utf-8")
    assert SAMPLE.stat().st_size < 50_000
    assert "@" not in raw
    records = _sample()
    assert 20 <= len(records) <= 25
    for record in records:
        assert set(record) == ALLOWED_KEYS


def test_record_model_drops_every_other_field() -> None:
    record = YcRecord.model_validate(
        {"name": "Acme", "website": "https://acme.com", "one_liner": "x", "founders": ["x"]}
    )
    assert record.model_dump() == {
        "name": "Acme",
        "website": "https://acme.com",
        "batch": None,
        "all_locations": None,
    }
    with pytest.raises(pydantic.ValidationError, match="name"):
        YcRecord.model_validate({"website": "https://acme.com"})


# --- parsing --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("San Francisco, CA, USA", ("San Francisco", "CA", "US")),
        ("Boston, MA, USA; Remote", ("Boston", "MA", "US")),
        ("New York City, NY, USA; New York, NY, USA", ("New York City", "NY", "US")),
        ("  Austin,  TX ,  USA ", ("Austin", "TX", "US")),
        ("Seattle, WA, United States", ("Seattle", "WA", "US")),
        ("Bengaluru, KA, India", (None, None, None)),
        ("Paris, Île-de-France, France", (None, None, None)),
        ("MH, India", (None, None, None)),
        ("Remote", (None, None, None)),
        ("Springfield, Illinois, USA", (None, None, None)),
        (", CA, USA", (None, None, None)),
        ("", (None, None, None)),
        (None, (None, None, None)),
    ],
)
def test_parse_location(text: str | None, expected: tuple[str | None, ...]) -> None:
    assert parse_location(text) == expected


def test_parse_directory_normalises_domains_and_skips_missing_websites() -> None:
    companies, skipped = parse_directory(_sample())
    assert skipped == 1  # Arcane lists no website
    by_name = {c["name"]: c for c in companies}
    assert len(by_name) == 20
    assert by_name["Flexport"]["domain"] == "flexport.com"  # path dropped
    assert by_name["Sendwave"]["domain"] == "sendwave.com"  # www dropped
    assert by_name["GitLab"]["domain"] == "gitlab.com"  # trailing slash dropped
    assert by_name["Groww"]["domain"] == "groww.in"
    assert by_name["Gusto"] == {
        "name": "Gusto",
        "domain": "gusto.com",
        "batch": "Winter 2012",
        "locations": "San Francisco, CA, USA",
    }
    assert by_name["Caper"]["locations"] is None
    assert all(c["domain"] == c["domain"].lower() for c in companies)
    assert not any(c["domain"].startswith("www.") for c in companies)


def test_parse_directory_rejects_garbage_websites_and_names() -> None:
    companies, skipped = parse_directory(
        [
            {"name": "Local", "website": "http://localhost:3000"},
            {"name": "   ", "website": "https://blank.com"},
            {"name": "Ok", "website": "HTTPS://WWW.OK.IO/"},
            {"name": "Nothing", "website": None},
        ]
    )
    assert skipped == 3
    assert [c["domain"] for c in companies] == ["ok.io"]


# --- seeding ----------------------------------------------------------------------------------------


def test_seed_from_sample(db_session: Session) -> None:
    stats = seed_from_file(db_session, SAMPLE)
    assert (stats.read, stats.inserted, stats.updated, stats.unchanged) == (21, 20, 0, 0)
    assert (stats.skipped_no_website, stats.duplicates) == (1, 0)
    assert stats.skipped_platform_domain == 0  # GitLab's own gitlab.com is not a platform page

    rows = _companies(db_session)
    assert len(rows) == 20
    gusto = rows["gusto.com"]
    assert (gusto.name, gusto.hq_city, gusto.hq_state, gusto.hq_country) == (
        "Gusto",
        "San Francisco",
        "CA",
        "US",
    )
    algolia = rows["algolia.com"]
    assert (algolia.hq_city, algolia.hq_state, algolia.hq_country) == ("San Francisco", "CA", "US")
    razorpay = rows["razorpay.com"]
    assert (razorpay.hq_city, razorpay.hq_state, razorpay.hq_country) == (None, None, None)
    assert rows["flexport.com"].name == "Flexport"


def test_reseed_is_idempotent(db_session: Session) -> None:
    seed_from_file(db_session, SAMPLE)
    before = {d: (c.id, c.name, c.updated_at) for d, c in _companies(db_session).items()}
    db_session.expire_all()

    again = seed_from_file(db_session, SAMPLE)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 20)
    db_session.expire_all()
    after = {d: (c.id, c.name, c.updated_at) for d, c in _companies(db_session).items()}
    assert after == before
    assert _count(db_session) == 20


def test_reseed_updates_changed_rows_and_keeps_known_locations(db_session: Session) -> None:
    db_session.add(Company(domain="stripe.com", name="stripe.com", hq_city="South San Francisco"))
    db_session.flush()
    changed = [
        YcCompany(name="Stripe", domain="stripe.com", batch="Summer 2009", locations="Remote"),
        YcCompany(name="Gusto", domain="gusto.com", batch=None, locations=None),
        YcCompany(name="Gusto Again", domain="gusto.com", batch=None, locations=None),
    ]
    stats = seed_companies(db_session, changed)
    assert (stats.read, stats.inserted, stats.updated, stats.unchanged) == (3, 1, 1, 0)
    assert stats.duplicates == 1
    db_session.expire_all()
    rows = _companies(db_session)
    assert rows["stripe.com"].name == "Stripe"
    assert rows["stripe.com"].hq_city == "South San Francisco"  # YC had no city: kept
    assert rows["gusto.com"].name == "Gusto"  # first record for a domain wins


def test_seed_handles_more_rows_than_one_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(yc, "UPSERT_BATCH", 7)
    companies = [
        YcCompany(name=f"Company {i}", domain=f"company{i}.com", batch=None, locations=None)
        for i in range(20)
    ]
    stats = seed_companies(db_session, companies)
    assert stats.inserted == 20
    assert _count(db_session) == 20


# --- fetching --------------------------------------------------------------------------------------


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        yield mock


def test_fetch_directory(router: respx.MockRouter) -> None:
    route = router.get(YC_DIRECTORY_URL).respond(200, json=_sample())
    with make_client() as client:
        companies = fetch_directory(client)
    assert route.call_count == 1
    assert len(companies) == 20
    assert companies[0]["domain"] == "gusto.com"
    user_agent = route.calls[0].request.headers["User-Agent"]
    assert user_agent.startswith("tell/")


def test_fetch_directory_raises_on_http_error(router: respx.MockRouter) -> None:
    router.get(YC_DIRECTORY_URL).respond(404)
    with make_client() as client, pytest.raises(httpx.HTTPStatusError):
        fetch_directory(client)


# --- platform domains (ADR 0033) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("website", "platform"),
    [
        ("https://apps.apple.com/us/app/famegame/id1234567890", "apple.com"),
        ("https://play.google.com/store/apps/details?id=com.example", "google.com"),
        ("https://sites.google.com/view/recordbook", "google.com"),
        ("https://www.linkedin.com/company/acme", "linkedin.com"),
        ("https://acme.github.io", "github.io"),
        ("https://github.com/acme", "github.com"),
        ("https://acme.notion.site/Acme-123", "notion.site"),
        ("https://acme.vercel.app", "vercel.app"),
        ("https://acme.netlify.app", "netlify.app"),
        ("https://acme.herokuapp.com", "herokuapp.com"),
        ("https://acme.webflow.io", "webflow.io"),
        ("https://acme.framer.website", "framer.website"),
        ("https://acme.wixsite.com/home", "wixsite.com"),
        ("https://medium.com/@acme", "medium.com"),
        ("https://acme.substack.com", "substack.com"),
        ("https://linktr.ee/acme", "linktr.ee"),
        ("https://x.com/acme", "x.com"),
        ("https://twitter.com/acme", "twitter.com"),
        ("https://www.instagram.com/acme", "instagram.com"),
        ("https://www.facebook.com/acme", "facebook.com"),
        ("https://www.youtube.com/@acme", "youtube.com"),
        ("https://acme.com", None),
        ("https://gitlab.com", None),
        ("https://acme.co.uk/about", None),
        ("", None),
        ("localhost", None),
    ],
)
def test_platform_domain(website: str, platform: str | None) -> None:
    assert platform_domain(website) == platform
    if platform is not None:
        assert platform in PLATFORM_DOMAINS
        assert is_platform_domain(website)
        assert is_platform_domain(website, name="Acme")


def test_platform_companies_own_domain_is_exempt() -> None:
    assert not is_platform_domain("substack.com", name="Substack")
    assert not is_platform_domain("https://www.substack.com", name="Sub-Stack")
    assert is_platform_domain("acme.substack.com", name="Substack")  # a newsletter page
    assert is_platform_domain("apple.com", name="FameGame")
    assert not is_platform_domain("acme.com", name="Acme")


def test_parse_keeps_platform_domains_and_seed_skips_them(db_session: Session) -> None:
    raw = [
        {"name": "FameGame", "website": "https://apps.apple.com/us/app/famegame/id123"},
        {"name": "RecordBook", "website": "https://sites.google.com/view/recordbook"},
        {"name": "Zylo", "website": "https://zylo.vercel.app/"},
        {"name": "Substack", "website": "https://substack.com"},
        {"name": "Acme", "website": "https://acme.com"},
        {"name": "Nothing", "website": None},
    ]
    companies, skipped = parse_directory(raw)
    assert skipped == 1
    assert [c["domain"] for c in companies] == [
        "apple.com",
        "google.com",
        "zylo.vercel.app",
        "substack.com",
        "acme.com",
    ]
    stats = seed_companies(db_session, companies, read=len(raw))
    assert (stats.read, stats.inserted, stats.skipped_platform_domain) == (6, 2, 3)
    assert (stats.skipped_no_website, stats.duplicates) == (1, 0)
    assert set(_companies(db_session)) == {"substack.com", "acme.com"}


# --- prune-platform-companies --------------------------------------------------------------------

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _company(session: Session, domain: str, name: str) -> Company:
    company = Company(domain=domain, name=name)
    session.add(company)
    session.flush()
    return company


def _event(session: Session, event_id: str, company: Company) -> EventRow:
    row = EventRow(
        event_id=event_id,
        company_id=company.id,
        source="edgar",
        type="form_d_filed",
        occurred_at=NOW,
        observed_at=NOW,
        magnitude=Decimal(1),
        url=f"https://example.test/{event_id}",
        payload={},
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def platformed(db_session: Session) -> dict[str, Company]:
    """Platform-domain companies seeded before the guard, some still referenced."""
    rows = {
        "famegame": _company(db_session, "apple.com", "FameGame"),
        "recordbook": _company(db_session, "google.com", "RecordBook"),
        "zylo": _company(db_session, "zylo.vercel.app", "Zylo"),
        "boarded": _company(db_session, "acme.github.io", "Acme"),
        "watched": _company(db_session, "linkedin.com", "Watched Co"),
        "substack": _company(db_session, "substack.com", "Substack"),
        "real": _company(db_session, "real.com", "Real Co"),
    }
    famegame, recordbook = rows["famegame"], rows["recordbook"]
    db_session.add(CompanyAlias(company_id=famegame.id, alias="apple", source="manual"))
    db_session.add(CompanyFeed(company_id=famegame.id, feed_url="https://apple.com/feed"))
    db_session.add(
        Score(
            company_id=famegame.id,
            icp_profile="default",
            as_of=date(2026, 10, 13),
            score=Decimal(0),
            contributions=[],
        )
    )
    event = _event(db_session, "ev-recordbook", recordbook)
    signal = Signal(
        company_id=recordbook.id,
        event_id=event.event_id,
        signal_type="funding",
        weight=Decimal(1),
        magnitude=Decimal(1),
        half_life_days=30,
        starts_at=NOW,
    )
    db_session.add(signal)
    watchlist = Watchlist(owner="o-1", name="list")
    db_session.add(watchlist)
    db_session.flush()
    db_session.add(Feedback(watchlist_id=watchlist.id, signal_id=signal.id, verdict="useful"))
    db_session.add(
        Score(
            company_id=recordbook.id,
            icp_profile="default",
            as_of=date(2026, 10, 13),
            score=Decimal(3),
            contributions=[],
        )
    )
    db_session.add(
        AtsBoard(
            company_id=rows["boarded"].id,
            vendor="ashby",
            identifier="acme",
            confidence=Decimal("0.90"),
        )
    )
    db_session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=rows["watched"].id))
    db_session.flush()
    return rows


def test_platform_companies_lists_blockers(
    db_session: Session, platformed: dict[str, Company]
) -> None:
    found = {c.domain: c for c in platform_companies(db_session)}
    assert set(found) == {
        "apple.com",
        "google.com",
        "zylo.vercel.app",
        "acme.github.io",
        "linkedin.com",
    }
    assert found["apple.com"].deletable
    assert found["zylo.vercel.app"].to_dict() == {
        "company_id": platformed["zylo"].id,
        "domain": "zylo.vercel.app",
        "name": "Zylo",
        "platform": "vercel.app",
        "blockers": {},
        "deletable": True,
    }
    assert found["google.com"].to_dict()["blockers"] == {
        "events": 1,
        "signals": 1,
        "feedback": 1,
        "scores": 1,
    }
    assert found["acme.github.io"].to_dict()["blockers"] == {"boards": 1}
    assert found["linkedin.com"].to_dict()["blockers"] == {"watchlist_items": 1}


def test_prune_platform_companies_dry_run_then_apply(
    db_session: Session, platformed: dict[str, Company]
) -> None:
    dry = prune_platform_companies(db_session)
    assert (dry["dry_run"], dry["platform_companies"], dry["deletable"], dry["deleted"]) == (
        True,
        5,
        2,
        0,
    )
    assert dry["kept_referenced"] == 3
    assert len(_companies(db_session)) == 7

    applied = prune_platform_companies(db_session, apply=True)
    assert (applied["dry_run"], applied["deleted"]) == (False, 2)
    db_session.expire_all()
    assert set(_companies(db_session)) == {
        "google.com",
        "acme.github.io",
        "linkedin.com",
        "substack.com",
        "real.com",
    }
    famegame = platformed["famegame"].id
    for model in (CompanyAlias, CompanyFeed, Score):
        stmt = select(func.count()).select_from(model).where(model.company_id == famegame)
        assert db_session.scalar(stmt) == 0
    # idempotent: the referenced ones stay listed, nothing more to delete
    again = prune_platform_companies(db_session, apply=True)
    assert (again["platform_companies"], again["deleted"]) == (3, 0)


def test_prune_platform_companies_command(
    db_session: Session, platformed: dict[str, Company], monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(cli, "session_scope", scope)
    runner = CliRunner()
    for args in (["prune-platform-companies"], ["prune-platform-companies", "--dry-run"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert (report["dry_run"], report["deleted"], report["deletable"]) == (True, 0, 2)
    result = runner.invoke(cli.app, ["prune-platform-companies", "--apply"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["deleted"] == 2
    assert registrable_domain("apple.com") not in _companies(db_session)
