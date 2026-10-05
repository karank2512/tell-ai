"""YC directory seeder: normalisation, location parsing, idempotent upserts, fetch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pydantic
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.discovery import yc
from tell.discovery.yc import (
    YC_DIRECTORY_URL,
    YcCompany,
    YcRecord,
    fetch_directory,
    parse_directory,
    parse_location,
    seed_companies,
    seed_from_file,
)
from tell.http import make_client
from tell.models import Company

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
