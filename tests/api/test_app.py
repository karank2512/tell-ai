"""The FastAPI v1 service (spec section 10) against the temporary database.

A small synthetic world is written through the production paths (``write_events`` then
``build_signals``): four companies on the owner's watchlist ``pilot``, a public list and
another owner's private list. The app runs with a fixed clock (:data:`NOW`); every
domain, title and URL is synthetic and nothing names a person.

Expected scores at :data:`NOW` under the ``default`` profile:

* ``alpha.example``: Form D (10-01, $10M) plus a sales hiring spike (10-03): about 30.
* ``beta.example``: a launch on 09-20 only: about 1.2, no signal in the last 7 days.
* ``gamma.example``: an engineering hiring spike (10-04, other family): about 2.9.
* ``delta.example``: a Form D on 08-01 only: about 16, nothing in the last 7 days.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.api import app as app_module
from tell.api import auth, cli
from tell.api.app import MAX_EVENTS, PROBLEM_MEDIA_TYPE, create_app, normalize_domain
from tell.collectors.base import Event, write_events
from tell.db import SessionLocal
from tell.models import ApiKey, ApiUsage, Company, Feedback, Signal, Watchlist, WatchlistItem
from tell.publish.json_api import (
    build_company_json,
    validate_company_json,
    validate_health_json,
)
from tell.signals.profiles import get_profile
from tell.signals.scoring import build_signals, score_companies

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
OWNER = "acme-team"
OTHER = "other-team"

ALPHA = "alpha.example"
BETA = "beta.example"
GAMMA = "gamma.example"
DELTA = "delta.example"
LONER = "loner.example"
"""Known to tell but on nobody's watchlist."""

runner = CliRunner()


# --- world ---------------------------------------------------------------------------------------


@dataclass
class World:
    companies: dict[str, int]
    pilot: int
    public: int
    secret: int
    signals: dict[str, int]
    """``{domain: id of its first signal}``."""


def _at(day: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


def _event(
    company_id: int,
    *,
    source: str,
    type_: str,
    external_id: str,
    occurred: datetime,
    payload: dict[str, Any],
    magnitude: float = 1,
    url: str | None = None,
) -> Event:
    return Event(
        source=source,
        type=type_,
        external_id=external_id,
        version=occurred.isoformat(),
        occurred_at=occurred,
        observed_at=occurred,
        url=url or f"https://receipts.example/{source}/{external_id}",
        payload=payload,
        run_id="test-run",
        magnitude=magnitude,
        company_id=company_id,
    )


def _velocity(company_id: int, family: str, day: str, new7: int) -> Event:
    occurred = _at(day)
    postings = [
        {
            "title": f"{family.title()} Role {i}",
            "posted_at": (occurred - timedelta(days=1)).isoformat(),
            "url": f"https://jobs.example/{company_id}/{family}/{i}",
            "external_id": f"{company_id}-{family}-{i}",
        }
        for i in range(new7)
    ]
    return _event(
        company_id,
        source="tell",
        type_="hiring_velocity",
        external_id=f"{company_id}:{family}",
        occurred=occurred,
        magnitude=new7,
        url=f"https://jobs.example/{company_id}",
        payload={
            "role_family": family,
            "new7": new7,
            "base": 0.5,
            "ratio": new7 / 0.5,
            "open_postings": 12,
            "episode_start": day,
            "counted_postings": postings,
        },
    )


def _form_d(company_id: int, day: str, amount: int) -> Event:
    return _event(
        company_id,
        source="edgar_formd",
        type_="form_d_filed",
        external_id=f"formd-{company_id}-{day}",
        occurred=_at(day),
        payload={"is_amendment": False, "total_offering_amount": amount},
    )


def build_world(session: Session) -> World:
    companies: dict[str, int] = {}
    for domain in (ALPHA, BETA, GAMMA, DELTA, LONER):
        company = Company(domain=domain, name=domain.split(".")[0].title())
        session.add(company)
        session.flush()
        companies[domain] = company.id
    a, b, g, d = (companies[x] for x in (ALPHA, BETA, GAMMA, DELTA))
    write_events(
        session,
        [
            _form_d(a, "2026-10-01", 10_000_000),
            _velocity(a, "sales", "2026-10-03", 4),
            _event(
                b,
                source="news",
                type_="news_launch",
                external_id="beta-launch",
                occurred=_at("2026-09-20"),
                payload={"headline": "Beta launches its product"},
            ),
            _velocity(g, "engineering", "2026-10-04", 10),
            _form_d(d, "2026-08-01", 10_000_000),
            _event(
                companies[LONER],
                source="news",
                type_="news_launch",
                external_id="loner-launch",
                occurred=_at("2026-10-05"),
                payload={"headline": "Loner ships"},
            ),
        ],
    )
    build_signals(session, now=NOW)
    pilot = Watchlist(owner=OWNER, name="pilot", icp_profile="default")
    public = Watchlist(
        owner=OTHER, name="open list", icp_profile="default", slug="open-list", is_public=True
    )
    secret = Watchlist(owner=OTHER, name="secret", icp_profile="default", slug="secret")
    session.add_all([pilot, public, secret])
    session.flush()
    for domain in (ALPHA, BETA, GAMMA, DELTA):
        session.add(WatchlistItem(watchlist_id=pilot.id, company_id=companies[domain]))
    session.add(WatchlistItem(watchlist_id=public.id, company_id=a))
    session.add(WatchlistItem(watchlist_id=secret.id, company_id=companies[LONER]))
    session.flush()
    signals: dict[str, int] = {}
    for domain, company_id in companies.items():
        first = session.scalars(
            select(Signal.id).where(Signal.company_id == company_id).order_by(Signal.id)
        ).first()
        if first is not None:
            signals[domain] = int(first)
    return World(companies, pilot.id, public.id, secret.id, signals)


# --- fixtures ------------------------------------------------------------------------------------


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world(engine: Engine) -> World:
    with SessionLocal(bind=engine) as session:
        built = build_world(session)
        session.commit()
    return built


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def client(engine: Engine, clock: Clock) -> Iterator[TestClient]:
    with TestClient(create_app(engine=engine, clock=clock)) as test_client:
        yield test_client


def _new_key(engine: Engine, owner: str = OWNER, quota: int = 1000) -> auth.NewKey:
    with SessionLocal(bind=engine) as session:
        new = auth.create_key(session, owner, quota)
        session.commit()
    return new


@pytest.fixture
def key(engine: Engine) -> str:
    return _new_key(engine).key


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _problem(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["title"]
    return body


def _usage(engine: Engine, key_id: int) -> dict[date, int]:
    with SessionLocal(bind=engine) as session:
        rows = session.execute(
            select(ApiUsage.day, ApiUsage.calls).where(ApiUsage.key_id == key_id)
        ).all()
    return dict(rows)


# --- keys ------------------------------------------------------------------------------------------


def test_generated_keys_have_the_spec_format() -> None:
    keys = {auth.generate_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        assert re.fullmatch(r"tell_[A-Za-z0-9_-]{43}", key)
        assert "=" not in key
        assert auth.hash_key(key) == hashlib.sha256(key.encode()).hexdigest()
        assert auth.key_prefix(key) == key[:12]
        assert auth.parse_bearer(f"Bearer {key}") == key
        assert auth.parse_bearer(f"bearer   {key} ") == key


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Basic abc", "Bearer tell_short", "Token tell_" + "a" * 43],
)
def test_parse_bearer_rejects_malformed(header: str | None) -> None:
    assert auth.parse_bearer(header) is None


def test_owner_must_be_a_handle() -> None:
    assert auth.validate_owner("  Acme-Team ") == "acme-team"
    assert auth.validate_owner("a.b_c-9") == "a.b_c-9"
    for bad in ("ops" + "@" + "example.invalid", "two words", "", "-lead", "x" * 65):
        with pytest.raises(ValueError, match="handle"):
            auth.validate_owner(bad)


def test_create_key_stores_only_the_hash(engine: Engine) -> None:
    new = _new_key(engine, owner="Acme-Team", quota=50)
    assert new.owner == OWNER
    with SessionLocal(bind=engine) as session:
        row = session.get(ApiKey, new.id)
        assert row is not None
        assert row.key_hash == auth.hash_key(new.key)
        assert row.key_prefix == new.key[:12] == new.key_prefix
        assert row.daily_quota == 50
        assert row.created_at is not None
        assert row.last_used_at is None
        assert row.revoked_at is None
        stored = session.execute(select(ApiKey.__table__)).all()
    assert new.key not in json.dumps([list(r) for r in stored], default=str)
    with SessionLocal(bind=engine) as session, pytest.raises(ValueError, match="daily_quota"):
        auth.create_key(session, OWNER, -1)


def test_revoke_key_by_id_or_prefix(engine: Engine) -> None:
    first = _new_key(engine)
    second = _new_key(engine)
    with SessionLocal(bind=engine) as session:
        row = auth.revoke_key(session, str(first.id), NOW)
        assert row.revoked_at == NOW
        again = auth.revoke_key(session, first.key_prefix, NOW + timedelta(days=1))
        assert again.revoked_at == NOW  # the first revocation stands
        with pytest.raises(LookupError, match="no API key"):
            auth.revoke_key(session, "tell_nothing", NOW)
        clone = ApiKey(key_hash="0" * 64, key_prefix=second.key_prefix, owner=OWNER)
        session.add(clone)
        session.flush()
        with pytest.raises(LookupError, match="revoke by id"):
            auth.revoke_key(session, second.key_prefix, NOW)


# --- authentication ----------------------------------------------------------------------------------


def test_missing_or_bad_credentials_are_401(client: TestClient, key: str) -> None:
    unknown = auth.generate_key()
    for headers in (
        {},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "Bearer not-a-tell-key"},
        _h(unknown),
        {"Authorization": f"Bearer {key}x"},
    ):
        response = client.get(f"/v1/companies/{ALPHA}", headers=headers)
        body = _problem(response, 401)
        assert response.headers["www-authenticate"] == "Bearer"
        assert "Bearer" in body["detail"]


def test_revoked_key_is_401(client: TestClient, engine: Engine, world: World) -> None:
    new = _new_key(engine)
    assert client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)).status_code == 200
    with SessionLocal(bind=engine) as session:
        auth.revoke_key(session, new.key_prefix, NOW)
        session.commit()
    _problem(client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)), 401)


def test_every_v1_route_requires_a_key(client: TestClient) -> None:
    calls = [
        ("GET", f"/v1/companies/{ALPHA}", None),
        ("GET", "/v1/in-market?watchlist=1", None),
        ("GET", f"/v1/companies/{ALPHA}/events", None),
        ("POST", "/v1/watchlists", {"name": "x", "domains": []}),
        ("POST", "/v1/feedback", {"signal_id": 1, "verdict": "useful"}),
    ]
    for method, url, body in calls:
        _problem(client.request(method, url, json=body), 401)


# --- metering ----------------------------------------------------------------------------------------


def test_metering_counts_calls_per_day_and_touches_last_used(
    client: TestClient, engine: Engine, world: World, clock: Clock
) -> None:
    new = _new_key(engine)
    assert client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)).status_code == 200
    _problem(client.get("/v1/companies/nowhere.example", headers=_h(new.key)), 404)
    assert _usage(engine, new.id) == {TODAY: 2}  # a failed handler still counts
    with SessionLocal(bind=engine) as session:
        row = session.get(ApiKey, new.id)
        assert row is not None
        assert row.last_used_at == NOW

    clock.now = NOW + timedelta(seconds=30)
    client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key))
    with SessionLocal(bind=engine) as session:
        row = session.get(ApiKey, new.id)
        assert row is not None
        assert row.last_used_at == NOW  # throttled: under a minute since the last write

    clock.now = NOW + timedelta(days=1)
    client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key))
    assert _usage(engine, new.id) == {TODAY: 3, TODAY + timedelta(days=1): 1}
    with SessionLocal(bind=engine) as session:
        row = session.get(ApiKey, new.id)
        assert row is not None
        assert row.last_used_at == NOW + timedelta(days=1)


def test_quota_is_enforced_before_the_handler(
    client: TestClient, engine: Engine, world: World, clock: Clock
) -> None:
    new = _new_key(engine, quota=2)
    for _ in range(2):
        assert client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)).status_code == 200
    with SessionLocal(bind=engine) as session:
        before = session.scalar(select(func.count()).select_from(Watchlist))
    refused = client.post(
        "/v1/watchlists", headers=_h(new.key), json={"name": "late", "domains": [ALPHA]}
    )
    body = _problem(refused, 429)
    assert "quota" in body["detail"]
    assert int(refused.headers["retry-after"]) == 12 * 3600  # NOW is noon UTC
    with SessionLocal(bind=engine) as session:
        assert session.scalar(select(func.count()).select_from(Watchlist)) == before
    assert _usage(engine, new.id) == {TODAY: 2}  # refused calls are not counted

    clock.now = NOW + timedelta(days=1)
    assert client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)).status_code == 200


def test_zero_quota_key_is_always_429(client: TestClient, engine: Engine, world: World) -> None:
    new = _new_key(engine, quota=0)
    _problem(client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)), 429)
    assert _usage(engine, new.id) == {}


# --- GET /v1/companies/{domain} ------------------------------------------------------------------------


def test_company_is_the_static_object(
    client: TestClient, engine: Engine, world: World, key: str
) -> None:
    response = client.get(f"/v1/companies/{ALPHA}", headers=_h(key))
    assert response.status_code == 200, response.text
    body = response.json()
    validate_company_json(body)
    with SessionLocal(bind=engine) as session:
        company = session.get(Company, world.companies[ALPHA])
        assert company is not None
        expected = build_company_json(session, company, get_profile("default"), TODAY)
    assert body == json.loads(json.dumps(expected))
    assert "heat" not in body
    assert body["score"] > 25
    assert [c["type"] for c in body["contributions"]] == ["funding", "hiring_velocity"]
    velocity = body["contributions"][1]
    assert velocity["role_family"] == "sales"
    assert len(velocity["postings"]) == 4
    assert "role_family" not in body["contributions"][0]
    assert round(sum(c["contribution"] for c in body["contributions"]), 1) == body["score"]


def test_company_lookup_normalizes_the_domain(client: TestClient, world: World, key: str) -> None:
    for spelled in ("WWW.Alpha.Example", "www.alpha.example."):
        response = client.get(f"/v1/companies/{spelled}", headers=_h(key))
        assert response.status_code == 200, spelled
        assert response.json()["domain"] == ALPHA
    assert normalize_domain("https://www.Alpha.example:443/careers?x=1") == ALPHA
    assert normalize_domain("  ") == ""


def test_company_lookup_falls_back_to_the_registrable_domain(
    client: TestClient, engine: Engine, key: str
) -> None:
    with SessionLocal(bind=engine) as session:
        session.add(Company(domain="acme.co.uk", name="Acme"))
        session.commit()
    response = client.get("/v1/companies/jobs.acme.co.uk", headers=_h(key))
    assert response.status_code == 200
    assert response.json()["domain"] == "acme.co.uk"


def test_company_404_and_422(client: TestClient, world: World, key: str) -> None:
    body = _problem(client.get("/v1/companies/unknown.example", headers=_h(key)), 404)
    assert "unknown.example" in body["detail"]
    _problem(client.get("/v1/companies/%20", headers=_h(key)), 404)
    body = _problem(client.get(f"/v1/companies/{ALPHA}?profile=nope", headers=_h(key)), 422)
    assert "nope" in body["detail"]


def test_company_under_another_profile(client: TestClient, world: World, key: str) -> None:
    response = client.get(f"/v1/companies/{GAMMA}?profile=job-seeker", headers=_h(key))
    assert response.status_code == 200
    assert response.json()["profile"] == "job-seeker"


# --- GET /v1/in-market --------------------------------------------------------------------------------


def _in_market(client: TestClient, key: str, query: str) -> dict[str, Any]:
    response = client.get(f"/v1/in-market?{query}", headers=_h(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_in_market_defaults(client: TestClient, world: World, key: str) -> None:
    body = _in_market(client, key, f"watchlist={world.pilot}")
    assert body["watchlist"] == {
        "id": world.pilot,
        "name": "pilot",
        "slug": None,
        "icp_profile": "default",
        "is_public": False,
    }
    assert body["profile"] == "default"
    assert body["as_of"] == TODAY.isoformat()
    assert body["since"] == (TODAY - timedelta(days=7)).isoformat()
    assert body["min_score"] == 6.0
    assert body["families"] == []
    assert [c["domain"] for c in body["companies"]] == [ALPHA]
    alpha = body["companies"][0]
    assert alpha["heat"] == 100
    assert alpha["name"] == "Alpha"
    assert 1 <= len(alpha["top_contributions"]) <= 3
    assert alpha["top_contributions"][0]["type"] == "funding"
    assert alpha["top_contributions"][0]["url"].startswith("https://")
    assert "postings" not in alpha["top_contributions"][1]


def test_in_market_filters(client: TestClient, world: World, key: str) -> None:
    pilot = f"watchlist={world.pilot}"
    low = _in_market(client, key, f"{pilot}&min_score=0")
    assert [c["domain"] for c in low["companies"]] == [ALPHA, GAMMA]
    scores = [c["score"] for c in low["companies"]]
    assert scores == sorted(scores, reverse=True)

    engineering = _in_market(client, key, f"{pilot}&min_score=0&family=engineering")
    assert [c["domain"] for c in engineering["companies"]] == [GAMMA]
    assert engineering["families"] == ["engineering"]
    both = _in_market(client, key, f"{pilot}&min_score=0&family=engineering,sales")
    assert [c["domain"] for c in both["companies"]] == [ALPHA, GAMMA]
    repeated = _in_market(client, key, f"{pilot}&min_score=0&family=sales&family=engineering")
    assert repeated["families"] == ["sales", "engineering"]

    everything = _in_market(client, key, f"{pilot}&min_score=0&since=2026-07-01")
    assert [c["domain"] for c in everything["companies"]] == [ALPHA, DELTA, GAMMA, BETA]
    assert [c["heat"] for c in everything["companies"]] == [100, 67, 33, 0]
    high = _in_market(client, key, f"{pilot}&since=2026-07-01&min_score=10")
    assert [c["domain"] for c in high["companies"]] == [ALPHA, DELTA]


def test_in_market_size_band_excludes(
    client: TestClient, world: World, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = score_companies

    def out_of_band(*args: Any, **kwargs: Any) -> Any:
        return [replace(r, in_band=False) for r in real(*args, **kwargs)]

    monkeypatch.setattr(app_module, "score_companies", out_of_band)
    body = _in_market(client, key, f"watchlist={world.pilot}&min_score=0")
    assert body["companies"] == []


def test_in_market_visibility(client: TestClient, world: World, key: str) -> None:
    public = _in_market(client, key, "watchlist=open-list&min_score=0")
    assert public["watchlist"]["is_public"] is True
    assert [c["domain"] for c in public["companies"]] == [ALPHA]
    by_id = _in_market(client, key, f"watchlist={world.public}&min_score=0")
    assert by_id["watchlist"]["slug"] == "open-list"
    for ref in ("secret", str(world.secret), "missing", "999999"):
        _problem(client.get(f"/v1/in-market?watchlist={ref}", headers=_h(key)), 404)


def test_in_market_422s(client: TestClient, world: World, key: str) -> None:
    pilot = f"watchlist={world.pilot}"
    body = _problem(client.get("/v1/in-market", headers=_h(key)), 422)
    assert body["errors"][0]["loc"] == ["query", "watchlist"]
    for query in (
        f"{pilot}&family=astronauts",
        f"{pilot}&since=2027-01-01",
        f"{pilot}&since=yesterday",
        f"{pilot}&min_score=-1",
    ):
        _problem(client.get(f"/v1/in-market?{query}", headers=_h(key)), 422)


def test_in_market_unknown_profile_is_422(
    client: TestClient, engine: Engine, world: World, key: str
) -> None:
    with SessionLocal(bind=engine) as session:
        wl = Watchlist(owner=OWNER, name="odd", icp_profile="no-such-profile")
        session.add(wl)
        session.commit()
        wl_id = wl.id
    _problem(client.get(f"/v1/in-market?watchlist={wl_id}", headers=_h(key)), 422)


# --- GET /v1/companies/{domain}/events -----------------------------------------------------------------


def test_events_newest_first_with_filters(client: TestClient, world: World, key: str) -> None:
    response = client.get(f"/v1/companies/{ALPHA}/events", headers=_h(key))
    assert response.status_code == 200
    body = response.json()
    assert body["domain"] == ALPHA
    assert body["since"] is None
    assert body["types"] == []
    assert [e["type"] for e in body["events"]] == ["hiring_velocity", "form_d_filed"]
    first = body["events"][0]
    assert first["url"].startswith("https://")
    assert first["payload"]["role_family"] == "sales"
    assert first["magnitude"] == 4.0
    assert set(first) == {
        "event_id",
        "source",
        "type",
        "occurred_at",
        "observed_at",
        "magnitude",
        "url",
        "payload",
    }

    since = client.get(f"/v1/companies/{ALPHA}/events?since=2026-10-02", headers=_h(key)).json()
    assert [e["type"] for e in since["events"]] == ["hiring_velocity"]
    assert since["since"] == "2026-10-02"
    typed = client.get(
        f"/v1/companies/{ALPHA}/events?types=form_d_filed,news_launch", headers=_h(key)
    ).json()
    assert [e["type"] for e in typed["events"]] == ["form_d_filed"]
    assert typed["types"] == ["form_d_filed", "news_launch"]
    repeated = client.get(
        f"/v1/companies/{ALPHA}/events?types=form_d_filed&types=hiring_velocity", headers=_h(key)
    ).json()
    assert len(repeated["events"]) == 2
    limited = client.get(f"/v1/companies/{ALPHA}/events?limit=1", headers=_h(key)).json()
    assert len(limited["events"]) == 1


def test_events_cap_at_200(client: TestClient, engine: Engine, key: str) -> None:
    with SessionLocal(bind=engine) as session:
        company = Company(domain="busy.example", name="Busy")
        session.add(company)
        session.flush()
        start = _at("2026-01-01")
        write_events(
            session,
            [
                _event(
                    company.id,
                    source="news",
                    type_="news_launch",
                    external_id=f"busy-{i}",
                    occurred=start + timedelta(hours=i),
                    payload={"n": i},
                )
                for i in range(MAX_EVENTS + 5)
            ],
        )
        session.commit()
    body = client.get("/v1/companies/busy.example/events", headers=_h(key)).json()
    assert len(body["events"]) == MAX_EVENTS
    assert body["events"][0]["payload"]["n"] == MAX_EVENTS + 4
    occurred = [e["occurred_at"] for e in body["events"]]
    assert occurred == sorted(occurred, reverse=True)
    _problem(client.get("/v1/companies/busy.example/events?limit=201", headers=_h(key)), 422)


def test_events_404_and_422(client: TestClient, world: World, key: str) -> None:
    _problem(client.get("/v1/companies/unknown.example/events", headers=_h(key)), 404)
    _problem(client.get(f"/v1/companies/{ALPHA}/events?since=notadate", headers=_h(key)), 422)


# --- POST /v1/watchlists -----------------------------------------------------------------------------


def test_create_watchlist_with_unknown_domains(
    client: TestClient, engine: Engine, world: World, key: str
) -> None:
    response = client.post(
        "/v1/watchlists",
        headers=_h(key),
        json={
            "name": "  q4 targets ",
            "icp_profile": "default",
            "domains": [ALPHA, "WWW.Beta.Example", "https://alpha.example/about", "new.example"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "q4 targets"
    assert body["icp_profile"] == "default"
    assert body["added"] == [ALPHA, BETA]
    assert body["unknown"] == ["new.example"]
    with SessionLocal(bind=engine) as session:
        wl = session.get(Watchlist, body["id"])
        assert wl is not None
        assert wl.owner == OWNER
        assert wl.is_public is False
        assert wl.slug is None
        members = set(
            session.scalars(
                select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == wl.id)
            )
        )
    assert members == {world.companies[ALPHA], world.companies[BETA]}
    listed = _in_market(client, key, f"watchlist={body['id']}&min_score=0")
    assert [c["domain"] for c in listed["companies"]] == [ALPHA]


def test_create_watchlist_defaults_and_empty(client: TestClient, world: World, key: str) -> None:
    response = client.post("/v1/watchlists", headers=_h(key), json={"name": "empty"})
    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "name": "empty",
        "icp_profile": "default",
        "added": [],
        "unknown": [],
    }


def test_create_watchlist_422s(client: TestClient, world: World, key: str) -> None:
    for body in (
        {"domains": [ALPHA]},
        {"name": ""},
        {"name": "x", "icp_profile": "nope"},
        {"name": "x", "owner": "someone-else"},
        {"name": "x", "domains": "alpha.example"},
        {"name": "x", "domains": ["d.example"] * 1001},
    ):
        _problem(client.post("/v1/watchlists", headers=_h(key), json=body), 422)
    raw = client.post(
        "/v1/watchlists",
        headers={**_h(key), "content-type": "application/json"},
        content=b"{not json",
    )
    _problem(raw, 422)


# --- POST /v1/feedback ---------------------------------------------------------------------------------


def test_feedback_is_written_for_an_owned_watchlist(
    client: TestClient, engine: Engine, world: World, key: str
) -> None:
    signal_id = world.signals[ALPHA]
    response = client.post(
        "/v1/feedback",
        headers=_h(key),
        json={"signal_id": signal_id, "verdict": "booked", "note": "meeting set"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["watchlist_id"] == world.pilot  # inferred: the owner's list holding ALPHA
    assert body["signal_id"] == signal_id
    assert body["verdict"] == "booked"
    assert body["note"] == "meeting set"
    assert body["created_at"]

    explicit = client.post(
        "/v1/feedback",
        headers=_h(key),
        json={"signal_id": signal_id, "verdict": "not_useful", "watchlist_id": world.pilot},
    )
    assert explicit.status_code == 201
    assert explicit.json()["note"] is None
    with SessionLocal(bind=engine) as session:
        rows = session.scalars(select(Feedback).order_by(Feedback.id)).all()
        assert [(r.watchlist_id, r.signal_id, r.verdict, r.note) for r in rows] == [
            (world.pilot, signal_id, "booked", "meeting set"),
            (world.pilot, signal_id, "not_useful", None),
        ]


def test_feedback_errors(client: TestClient, world: World, key: str) -> None:
    alpha = world.signals[ALPHA]
    loner = world.signals[LONER]
    cases: list[tuple[dict[str, Any], int]] = [
        ({"signal_id": alpha, "verdict": "meh"}, 422),
        ({"signal_id": 0, "verdict": "useful"}, 422),
        ({"verdict": "useful"}, 422),
        ({"signal_id": alpha, "verdict": "useful", "extra": 1}, 422),
        ({"signal_id": alpha, "verdict": "useful", "note": "x" * 1001}, 422),
        ({"signal_id": 999_999, "verdict": "useful"}, 404),
        ({"signal_id": alpha, "verdict": "useful", "watchlist_id": world.public}, 404),
        ({"signal_id": alpha, "verdict": "useful", "watchlist_id": 999_999}, 404),
        ({"signal_id": loner, "verdict": "useful"}, 422),
        ({"signal_id": loner, "verdict": "useful", "watchlist_id": world.pilot}, 422),
    ]
    for body, status in cases:
        _problem(client.post("/v1/feedback", headers=_h(key), json=body), status)


# --- /healthz, CORS, errors ----------------------------------------------------------------------------


def test_healthz_needs_no_key(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    validate_health_json(body)
    assert body["generated_at"].startswith("2026-10-06T12:00:00")
    assert {s["status"] for s in body["sources"]} == {"never_run"}


def test_cors_is_off(client: TestClient, world: World, key: str) -> None:
    response = client.get(
        f"/v1/companies/{ALPHA}", headers={**_h(key), "Origin": "https://elsewhere.example"}
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        f"/v1/companies/{ALPHA}",
        headers={"Origin": "https://elsewhere.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in preflight.headers


def test_unknown_route_and_method_are_problems(client: TestClient) -> None:
    _problem(client.get("/v2/anything"), 404)
    _problem(client.delete("/healthz"), 405)


def test_unhandled_error_is_a_500_problem(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        msg = "kaboom"
        raise RuntimeError(msg)

    monkeypatch.setattr(app_module, "compute_health", boom)
    with TestClient(create_app(engine=engine), raise_server_exceptions=False) as test_client:
        body = _problem(test_client.get("/healthz"), 500)
    assert body["detail"] == "internal error"
    assert "kaboom" not in json.dumps(body)


def test_problem_for_an_unlisted_status() -> None:
    response = app_module._problem(599, "odd")
    assert json.loads(bytes(response.body)) == {
        "type": "about:blank",
        "title": "Error",
        "status": 599,
        "detail": "odd",
    }


def test_module_app_is_lazy_and_documented() -> None:
    assert app_module.app.state.engine is None
    spec = app_module.app.openapi()
    assert set(spec["paths"]) == {
        "/healthz",
        "/v1/companies/{domain}",
        "/v1/in-market",
        "/v1/companies/{domain}/events",
        "/v1/watchlists",
        "/v1/feedback",
    }
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"


def test_default_session_uses_the_configured_engine(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    with TestClient(create_app(clock=lambda: NOW)) as test_client:
        assert test_client.get("/healthz").status_code == 200


# --- tell api (CLI) ------------------------------------------------------------------------------------


@pytest.fixture
def bound_cli(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    monkeypatch.setattr(cli, "utcnow", lambda: NOW)
    return engine


def test_cli_create_key_prints_it_once(bound_cli: Engine, client: TestClient) -> None:
    result = runner.invoke(cli.app, ["create-key", "--owner", OWNER, "--daily-quota", "5"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout.strip().splitlines()[0])
    assert record["owner"] == OWNER
    assert record["daily_quota"] == 5
    assert record["key_prefix"] == record["key"][:12]
    assert "shown once" in result.stderr
    with SessionLocal(bind=bound_cli) as session:
        row = session.get(ApiKey, record["id"])
        assert row is not None
        assert row.key_hash == auth.hash_key(record["key"])
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/companies/x.example", headers=_h(record["key"])).status_code == 404


def test_cli_create_key_refuses_an_email_owner(bound_cli: Engine) -> None:
    result = runner.invoke(cli.app, ["create-key", "--owner", "ops" + "@" + "example.invalid"])
    assert result.exit_code == 2
    with SessionLocal(bind=bound_cli) as session:
        assert session.scalar(select(func.count()).select_from(ApiKey)) == 0


def test_cli_revoke_and_usage(bound_cli: Engine, client: TestClient, world: World) -> None:
    new = _new_key(bound_cli)
    other = _new_key(bound_cli, owner=OTHER)
    for _ in range(3):
        client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key))
    client.get(f"/v1/companies/{ALPHA}", headers=_h(other.key))

    usage = runner.invoke(cli.app, ["usage"])
    assert usage.exit_code == 0, usage.output
    rows = [json.loads(line) for line in usage.stdout.splitlines()]
    assert [(r["owner"], r["calls"]) for r in rows] == [(OWNER, 3), (OTHER, 1)]
    assert rows[0] == {
        "key_id": new.id,
        "key_prefix": new.key_prefix,
        "owner": OWNER,
        "day": TODAY.isoformat(),
        "calls": 3,
        "daily_quota": 1000,
        "revoked": False,
    }
    assert new.key not in usage.stdout
    only = runner.invoke(cli.app, ["usage", "--owner", OTHER, "--days", "1"])
    assert [json.loads(line)["owner"] for line in only.stdout.splitlines()] == [OTHER]

    revoked = runner.invoke(cli.app, ["revoke-key", new.key_prefix])
    assert revoked.exit_code == 0, revoked.output
    record = json.loads(revoked.stdout)
    assert record["id"] == new.id
    assert record["revoked_at"].startswith("2026-10-06T12:00:00")
    _problem(client.get(f"/v1/companies/{ALPHA}", headers=_h(new.key)), 401)
    by_id = runner.invoke(cli.app, ["revoke-key", str(other.id)])
    assert by_id.exit_code == 0
    after = [json.loads(line) for line in runner.invoke(cli.app, ["usage"]).stdout.splitlines()]
    assert all(r["revoked"] for r in after)

    missing = runner.invoke(cli.app, ["revoke-key", "tell_missing"])
    assert missing.exit_code == 1
    assert "no API key" in missing.stderr
