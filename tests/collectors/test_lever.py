"""Lever Postings collector: contract, parse mapping, state machine replay, HTTP behaviour."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pydantic
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors import lever
from tell.collectors.base import JOB_OPENED, JOB_UPDATED, RunResult, content_hash, start_run
from tell.collectors.lever import (
    COLLECTOR,
    BoardNotFoundError,
    LeverPostings,
    board_url,
    commitment_tags,
    fetch_board,
    infer_remote,
    ingest_board,
    parse,
    parse_identifier,
    run_fixture,
)
from tell.config import get_settings
from tell.http import RateBudget, RetryExhausted
from tell.models import AtsBoard, Company, EventRow, Posting, ReviewQueueItem
from tests.collectors import _board_liveness as liveness

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lever"
NORMAL = FIXTURES / "normal.json"
EMPTY = FIXTURES / "empty_board.json"
DRIFT = FIXTURES / "drift.json"
UPDATED = FIXTURES / "updated.json"
NULLS = FIXTURES / "normal_nulls.json"

GLOBAL_URL = "https://api.lever.co/v0/postings/spotify"
EU_URL = "https://api.eu.lever.co/v0/postings/spotify"

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)

ID_REMOTE = "65d6caca-6d7c-4049-8256-a128e0e7249e"
ID_CONTRACT = "5992c673-e493-48b1-bb63-9b82a4c31876"
ID_INTERN = "e7f6e680-bf86-4da6-8711-afe44b84fa41"
ID_HYBRID = "be2d7d1e-939e-47f9-8db7-278caa101a60"


def _normal_raw() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(NORMAL.read_text(encoding="utf-8"))
    return data


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeper() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def budget(sleeper: FakeSleep) -> RateBudget:
    # A generous fake bucket: rate limiting is exercised by tests/test_http.py.
    return RateBudget(1000.0, burst=100, clock=lambda: 0.0, sleep=sleeper)


@pytest.fixture
def mocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


# --- fixtures and contract --------------------------------------------------------------------


def test_fixture_ids_match_constants() -> None:
    assert [p["id"] for p in _normal_raw()] == [ID_REMOTE, ID_CONTRACT, ID_INTERN, ID_HYBRID]


def test_fixtures_are_small_and_scrubbed() -> None:
    for path in FIXTURES.iterdir():
        assert path.stat().st_size < 50_000, path.name
        assert "@" not in path.read_text(encoding="utf-8"), path.name


def test_collector_spec() -> None:
    assert COLLECTOR.source == "lever"
    assert COLLECTOR.kind == "ats"
    assert COLLECTOR.response_model is LeverPostings
    assert COLLECTOR.fixture_dir == FIXTURES
    assert COLLECTOR.drift_missing_field == "createdAt"


def test_drift_raises_naming_created_at() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        LeverPostings.model_validate_json(DRIFT.read_text(encoding="utf-8"))
    assert "createdAt" in str(excinfo.value)
    assert excinfo.value.errors()[0]["loc"] == (0, "createdAt")


@pytest.mark.parametrize("field", ["id", "text", "hostedUrl", "categories", "descriptionPlain"])
def test_other_required_fields_are_enforced(field: str) -> None:
    raw = _normal_raw()
    del raw[2][field]
    with pytest.raises(pydantic.ValidationError, match=field):
        LeverPostings.model_validate(raw)


def test_nulls_fixture_parses() -> None:
    """Real 2026-10-14 postings: ``country`` null, no ``department``, no ``commitment``."""
    raw = json.loads(NULLS.read_text(encoding="utf-8"))
    assert raw[0]["country"] is None
    assert "department" not in raw[1]["categories"]
    assert "commitment" not in raw[2]["categories"]
    by_id = {p.external_id: p for p in parse(NULLS.read_bytes())}
    payroll = by_id["fade4c5e-b0b0-4a51-a6e4-f7a12e837d0d"]
    assert payroll.extra["country"] is None
    assert payroll.department == "Company-Wide"
    assert payroll.remote is False  # hybrid
    sales = by_id["e3e061af-9c83-4db0-8e2f-0ac583102809"]
    assert sales.department == "Sales"  # falls back to categories.team
    assert sales.extra["department"] is None
    manager = by_id["aecbb044-eca1-4469-a341-0095d09bb22f"]
    assert manager.extra["commitment"] is None
    assert manager.extra["commitment_tags"] == []
    assert manager.extra["salary_range"] is None


def test_validate_board_skips_one_invalid_posting_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="tell.collectors.lever"):
        postings = lever.validate_board(DRIFT.read_bytes(), identifier="spotify")
    assert [p.id for p in postings.root] == [ID_CONTRACT, ID_INTERN, ID_HYBRID]
    assert postings.invalid_jobs == 1
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert "spotify" in record.getMessage()
    assert ID_REMOTE in record.getMessage()
    assert "createdAt (missing)" in record.getMessage()


def test_validate_board_fails_loudly_when_every_posting_fails() -> None:
    raw = _normal_raw()
    for posting in raw:
        del posting["createdAt"]
    with pytest.raises(pydantic.ValidationError, match=r"0\.createdAt"):
        lever.validate_board(json.dumps(raw), identifier="spotify")
    with pytest.raises(pydantic.ValidationError):
        lever.validate_board(json.dumps({"postings": []}), identifier="spotify")
    with_junk: list[Any] = [*_normal_raw(), "not a posting"]
    assert lever.validate_board(json.dumps(with_junk), identifier="spotify").invalid_jobs == 1
    assert lever.validate_board(EMPTY.read_bytes(), identifier="spotify").invalid_jobs == 0


def test_updated_differs_from_normal_in_one_created_at_only() -> None:
    normal = _normal_raw()
    updated = json.loads(UPDATED.read_text(encoding="utf-8"))
    diffs = [
        (a["id"], key)
        for a, b in zip(normal, updated, strict=True)
        for key in a
        if a[key] != b[key]
    ]
    assert diffs == [(ID_HYBRID, "createdAt")]


# --- parse --------------------------------------------------------------------------------------


def test_parse_normal_exact_values() -> None:
    observed = parse(NORMAL.read_text(encoding="utf-8"))
    assert [o.external_id for o in observed] == [ID_REMOTE, ID_CONTRACT, ID_INTERN, ID_HYBRID]
    by_id = {o.external_id: o for o in observed}

    remote = by_id[ID_REMOTE]
    assert remote.title == "Backend Engineer - Music"
    assert remote.department == "Engineering"
    assert remote.location == "New York, NY"
    assert remote.remote is True
    assert remote.posted_at == datetime(2026, 9, 29, 13, 23, 55, 240000, tzinfo=UTC)
    assert remote.url == f"https://jobs.lever.co/spotify/{ID_REMOTE}"
    assert remote.version == "1790688235240"
    assert remote.extra == {
        "team": "Music",
        "department": "Engineering",
        "commitment": "Permanent",
        "commitment_tags": [],
        "workplace_type": "remote",
        "all_locations": ["New York, NY", "Boston, MA", "Miami, FL"],
        "country": "US",
        "salary_range": None,
    }

    contract = by_id[ID_CONTRACT]
    assert contract.title == "Fraud Analyst (Revenue Protection) - 12-Month Fixed-Term"
    assert contract.department == "Markets and Subscriber Growth"
    assert contract.location == "London"
    assert contract.remote is False
    assert contract.posted_at == datetime(2026, 7, 29, 15, 8, 17, 614000, tzinfo=UTC)
    assert contract.version == "1785337697614"
    assert contract.extra["commitment"] == "Full Time Contractor"
    assert contract.extra["commitment_tags"] == ["contract"]

    intern = by_id[ID_INTERN]
    assert intern.title == "RecSys 2026 — Intern"
    assert intern.remote is True
    assert intern.posted_at == datetime(2026, 9, 24, 16, 44, 3, 188000, tzinfo=UTC)
    assert intern.version == "1790268243188"
    assert intern.extra["commitment"] == "Internship"
    assert intern.extra["commitment_tags"] == ["intern"]

    hybrid = by_id[ID_HYBRID]
    assert hybrid.title == "Staff Engineer - Content Platform"
    assert hybrid.location == "London"
    assert hybrid.remote is False
    assert hybrid.posted_at == datetime(2026, 9, 16, 8, 5, 41, 933000, tzinfo=UTC)
    assert hybrid.url == f"https://jobs.lever.co/spotify/{ID_HYBRID}"
    assert hybrid.version == "1789545941933"
    assert hybrid.extra["commitment"] is None
    assert hybrid.extra["commitment_tags"] == []
    assert hybrid.extra["workplace_type"] == "hybrid"


def test_description_is_hashed_and_dropped() -> None:
    raw = _normal_raw()
    observed = parse(LeverPostings.model_validate(raw))
    for posting, obs in zip(raw, observed, strict=True):
        assert obs.content_hash == content_hash(posting["descriptionPlain"])
        assert obs.description_plain is None
        assert "description" not in json.dumps(obs.extra).lower()


def test_parse_accepts_bytes_and_model() -> None:
    as_bytes = parse(NORMAL.read_bytes())
    as_model = parse(LeverPostings.model_validate_json(NORMAL.read_bytes()))
    assert [o.external_id for o in as_bytes] == [o.external_id for o in as_model]


def test_parse_empty_board() -> None:
    assert parse(EMPTY.read_text(encoding="utf-8")) == []


def test_department_falls_back_to_team_and_salary_range_lands_in_extra() -> None:
    raw = _normal_raw()[:1]
    del raw[0]["categories"]["department"]
    raw[0]["salaryRange"] = {
        "currency": "USD",
        "interval": "per-year-salary",
        "min": 151942,
        "max": 189927,
    }
    (obs,) = parse(LeverPostings.model_validate(raw))
    assert obs.department == "Music"
    assert obs.extra["department"] is None
    assert obs.extra["salary_range"] == {
        "currency": "USD",
        "interval": "per-year-salary",
        "min": 151942,
        "max": 189927,
    }


def test_long_extra_strings_are_clipped() -> None:
    raw = _normal_raw()[:1]
    raw[0]["categories"]["team"] = "x" * 900
    (obs,) = parse(LeverPostings.model_validate(raw))
    assert obs.extra["team"] == "x" * 200


@pytest.mark.parametrize(
    ("workplace_type", "locations", "expected"),
    [
        ("remote", ("London",), True),
        ("onsite", ("Remote",), False),
        ("on-site", (None,), False),
        ("hybrid", ("Stockholm",), False),
        ("unspecified", ("Remote - US",), True),
        ("unspecified", ("Berlin", "Remote, EU"), True),
        ("unspecified", ("Berlin",), None),
        (None, (None,), None),
    ],
)
def test_infer_remote(
    workplace_type: str | None, locations: tuple[str | None, ...], expected: bool | None
) -> None:
    assert infer_remote(workplace_type, *locations) is expected


@pytest.mark.parametrize(
    ("commitment", "tags"),
    [
        ("Internship", ["intern"]),
        ("Intern - Summer", ["intern"]),
        ("Contract", ["contract"]),
        ("Full Time Contractor", ["contract"]),
        ("Contract-to-hire internship", ["intern", "contract"]),
        ("Full-time", []),
        (None, []),
    ],
)
def test_commitment_tags(commitment: str | None, tags: list[str]) -> None:
    assert commitment_tags(commitment) == tags


# --- identifiers --------------------------------------------------------------------------------


def test_parse_identifier_global_and_eu() -> None:
    assert parse_identifier("spotify") == ("https://api.lever.co/v0/postings", "spotify")
    assert parse_identifier("eu:spotify") == ("https://api.eu.lever.co/v0/postings", "spotify")
    assert parse_identifier(" EU:acme-co ") == ("https://api.eu.lever.co/v0/postings", "acme-co")
    assert board_url("spotify") == GLOBAL_URL
    assert board_url("eu:spotify") == EU_URL


@pytest.mark.parametrize("bad", ["", "eu:", "../etc", "a/b", "site?x=1", "-lead", "us:site"])
def test_parse_identifier_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="Lever site identifier"):
        parse_identifier(bad)


# --- fetch_board (respx) --------------------------------------------------------------------------


def test_fetch_board_exact_url_and_user_agent(mocked: respx.MockRouter, budget: RateBudget) -> None:
    route = mocked.get(GLOBAL_URL, params={"mode": "json"}).mock(
        return_value=httpx.Response(200, content=NORMAL.read_bytes())
    )
    with httpx.Client(headers={"User-Agent": get_settings().tell_http_user_agent}) as client:
        result = fetch_board(client, "spotify", budget=budget)
    assert len(result.root) == 4
    request = route.calls.last.request
    assert str(request.url) == "https://api.lever.co/v0/postings/spotify?mode=json"
    assert request.headers["User-Agent"] == "tell/0.1 (+https://github.com/karank2512/tell-ai)"


def test_fetch_board_eu_host_with_default_client(
    mocked: respx.MockRouter, budget: RateBudget
) -> None:
    route = mocked.get(EU_URL, params={"mode": "json"}).mock(
        return_value=httpx.Response(200, content=b"[]")
    )
    result = fetch_board(None, "eu:spotify", budget=budget)
    assert result.root == []
    request = route.calls.last.request
    assert str(request.url) == "https://api.eu.lever.co/v0/postings/spotify?mode=json"
    assert request.headers["User-Agent"] == get_settings().tell_http_user_agent


def test_fetch_board_uses_shared_ats_budget_by_default(mocked: respx.MockRouter) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(200, content=b"[]"))
    lever._host_budgets.cache_clear()
    try:
        fetch_board(None, "spotify")
        budgets = lever._host_budgets(get_settings().tell_rate_ats_rps)
        assert budgets.rps == 8.0
        assert budgets.for_host("api.lever.co") is budgets.for_url(GLOBAL_URL)
    finally:
        lever._host_budgets.cache_clear()


def test_fetch_board_backs_off_on_429(
    mocked: respx.MockRouter, budget: RateBudget, sleeper: FakeSleep
) -> None:
    route = mocked.get(GLOBAL_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, content=NORMAL.read_bytes()),
        ]
    )
    result = fetch_board(None, "spotify", budget=budget, sleep=sleeper)
    assert route.call_count == 3
    assert sleeper.calls == [1.0, 3.0]
    assert len(result.root) == 4


def test_fetch_board_gives_up_after_retries(
    mocked: respx.MockRouter, budget: RateBudget, sleeper: FakeSleep
) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(RetryExhausted):
        fetch_board(None, "spotify", budget=budget, sleep=sleeper)
    assert sleeper.calls == [1.0, 2.0, 4.0, 8.0]


def test_fetch_board_404_raises_board_not_found(
    mocked: respx.MockRouter, budget: RateBudget, sleeper: FakeSleep
) -> None:
    route = mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(BoardNotFoundError) as excinfo:
        fetch_board(None, "spotify", budget=budget, sleep=sleeper)
    assert route.call_count == 1
    assert sleeper.calls == []
    assert excinfo.value.identifier == "spotify"
    assert excinfo.value.url == "https://api.lever.co/v0/postings/spotify?mode=json"


def test_fetch_board_other_4xx_raises_http_status_error(
    mocked: respx.MockRouter, budget: RateBudget
) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_board(None, "spotify", budget=budget)


def test_fetch_board_skips_a_drifted_posting(mocked: respx.MockRouter, budget: RateBudget) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(200, content=DRIFT.read_bytes()))
    postings = fetch_board(None, "spotify", budget=budget)
    assert len(postings.root) == 3
    assert postings.invalid_jobs == 1


def test_fetch_board_drift_on_every_posting_raises_validation_error(
    mocked: respx.MockRouter, budget: RateBudget
) -> None:
    raw = _normal_raw()
    for posting in raw:
        del posting["createdAt"]
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(200, json=raw))
    with pytest.raises(pydantic.ValidationError, match="createdAt"):
        fetch_board(None, "spotify", budget=budget)


# --- database: fixture replay -----------------------------------------------------------------------


def _count(session: Session, model: type[Any]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _count_type(session: Session, type_: str) -> int:
    stmt = select(func.count()).select_from(EventRow).where(EventRow.type == type_)
    return session.execute(stmt).scalar_one()


@pytest.mark.db
def test_run_fixture_twice_inserts_nothing_the_second_time(db_session: Session) -> None:
    first = run_fixture(db_session, NORMAL, run_id=start_run(db_session, "lever", now=NOW), now=NOW)
    assert (first.fetched, first.inserted, first.skipped) == (4, 4, 0)
    assert {e.type for e in first.events} == {JOB_OPENED}
    opened = {e.payload["posting_external_id"]: e for e in first.events}
    assert opened[ID_HYBRID].external_id == f"lever:tell-fixture:{ID_HYBRID}"
    assert opened[ID_HYBRID].payload["version_marker"] == "1789545941933"
    assert opened[ID_INTERN].payload["extra"]["commitment_tags"] == ["intern"]
    counts = (_count(db_session, EventRow), _count(db_session, Posting))
    assert counts == (4, 4)

    second = run_fixture(
        db_session, NORMAL, run_id=start_run(db_session, "lever", now=LATER), now=LATER
    )
    assert (second.fetched, second.inserted, second.skipped, second.events) == (4, 0, 0, [])
    assert (_count(db_session, EventRow), _count(db_session, Posting)) == counts
    assert _count(db_session, AtsBoard) == 1
    assert _count(db_session, Company) == 1
    board = db_session.scalars(select(AtsBoard)).one()
    assert board.last_polled_at == LATER
    assert board.last_verified_at is None  # discovery owns it (ADR 0015)


@pytest.mark.db
def test_empty_board_yields_no_postings_and_no_events(db_session: Session) -> None:
    # The synthetic company may already exist (e.g. created by another run); it is reused.
    db_session.add(Company(domain=lever.FIXTURE_DOMAIN, name="Lever Fixture Co"))
    db_session.flush()
    result = run_fixture(db_session, EMPTY, run_id=start_run(db_session, "lever", now=NOW), now=NOW)
    assert (result.fetched, result.inserted, result.skipped, result.events) == (0, 0, 0, [])
    assert _count(db_session, Posting) == 0
    assert _count(db_session, EventRow) == 0
    assert _count(db_session, Company) == 1


@pytest.mark.db
def test_normal_then_updated_yields_one_job_updated(db_session: Session) -> None:
    run_fixture(db_session, NORMAL, run_id=start_run(db_session, "lever", now=NOW), now=NOW)
    result = run_fixture(
        db_session, UPDATED, run_id=start_run(db_session, "lever", now=LATER), now=LATER
    )
    assert result.inserted == 1
    (event,) = result.events
    assert event.type == JOB_UPDATED
    assert event.version == "1789632341933"
    assert event.payload["posting_external_id"] == ID_HYBRID
    assert _count_type(db_session, JOB_UPDATED) == 1
    assert _count_type(db_session, JOB_OPENED) == 4
    posting = db_session.scalars(select(Posting).where(Posting.external_id == ID_HYBRID)).one()
    assert posting.version_marker == "1789632341933"
    # job_opened stays keyed on the first posted_at seen.
    assert posting.posted_at == datetime(2026, 9, 16, 8, 5, 41, 933000, tzinfo=UTC)


# --- database: ingest_board ----------------------------------------------------------------------


def _board(session: Session, identifier: str = "spotify", status: str = "active") -> AtsBoard:
    company = Company(domain="spotify.example", name="Spotify")
    session.add(company)
    session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor="lever",
        identifier=identifier,
        confidence=Decimal("0.95"),
        status=status,
    )
    session.add(board)
    session.flush()
    return board


def _open_items(session: Session) -> list[ReviewQueueItem]:
    return list(session.scalars(select(ReviewQueueItem).where(ReviewQueueItem.status == "open")))


@pytest.mark.db
def test_ingest_board_writes_postings_and_events(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> None:
    mocked.get(EU_URL, params={"mode": "json"}).mock(
        return_value=httpx.Response(200, content=NORMAL.read_bytes())
    )
    board = _board(db_session, identifier="eu:spotify")
    run_id = start_run(db_session, "lever", now=NOW)
    result = ingest_board(db_session, board, run_id=run_id, now=NOW, budget=budget)
    assert (result.fetched, result.inserted, result.skipped) == (4, 4, 0)
    assert {e.external_id for e in result.events} == {
        f"lever:eu:spotify:{i}" for i in (ID_REMOTE, ID_CONTRACT, ID_INTERN, ID_HYBRID)
    }
    assert all(e.company_id == board.company_id for e in result.events)
    assert board.last_polled_at == NOW
    assert board.last_verified_at is None
    assert board.status == "active"
    assert _count(db_session, Posting) == 4


@pytest.mark.db
def test_ingest_board_skips_and_counts_one_invalid_posting(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(200, content=DRIFT.read_bytes()))
    board = _board(db_session)
    run_id = start_run(db_session, "lever", now=NOW)
    result = ingest_board(db_session, board, run_id=run_id, now=NOW, budget=budget)
    assert isinstance(result, lever.BoardRunResult)
    assert (result.fetched, result.inserted, result.errors) == (4, 3, 1)
    assert _count(db_session, Posting) == 3
    assert board.last_polled_at == NOW


@pytest.mark.db
def test_ingest_board_404_records_the_streak_and_nothing_else(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> None:
    route = mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(404))
    board = _board(db_session)

    first = ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget)
    assert (first.fetched, first.inserted, first.events) == (0, 0, [])
    assert (board.status, board.not_found_count, board.first_not_found_at) == ("active", 1, NOW)
    assert board.last_polled_at is None
    assert _open_items(db_session) == []

    ingest_board(db_session, board, run_id="r2", now=LATER, budget=budget)
    assert (board.status, board.not_found_count) == ("active", 2)  # only 6h apart
    assert _open_items(db_session) == []

    ingest_board(db_session, board, run_id="r3", now=NOW + timedelta(hours=12), budget=budget)
    assert board.status == "dead"
    (item,) = _open_items(db_session)
    assert item.payload["reason"] == "not_found_twice"

    # A dead board is still polled, so it can come back (ADR 0015).
    ingest_board(db_session, board, run_id="r4", now=NOW + timedelta(hours=18), budget=budget)
    assert route.call_count == 4
    assert len(_open_items(db_session)) == 1


@pytest.mark.db
def test_ingest_board_404_streak_is_per_board(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(404))
    mocked.get("https://api.lever.co/v0/postings/other").mock(return_value=httpx.Response(404))
    board = _board(db_session)
    other = AtsBoard(vendor="lever", identifier="other", confidence=Decimal("0.60"))
    db_session.add(other)
    db_session.flush()
    ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget)
    ingest_board(db_session, other, run_id="r1", now=NOW, budget=budget)
    ingest_board(db_session, other, run_id="r2", now=NOW + timedelta(hours=12), budget=budget)
    assert (board.status, board.not_found_count) == ("active", 1)
    assert (other.status, other.not_found_count) == ("dead", 2)
    (item,) = _open_items(db_session)
    assert item.payload["identifier"] == "other"


@pytest.mark.db
def test_ingest_board_propagates_other_http_errors(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> None:
    mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(401))
    board = _board(db_session)
    with pytest.raises(httpx.HTTPStatusError):
        ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget)
    assert board.status == "active"
    assert _open_items(db_session) == []


@pytest.mark.db
def test_ingest_board_skips_inactive_boards(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> None:
    route = mocked.get(GLOBAL_URL).mock(return_value=httpx.Response(200, content=b"[]"))
    board = _board(db_session, status="paused")
    result = ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget)
    assert result.fetched == 0
    assert route.call_count == 0


@pytest.mark.db
def test_ingest_board_rejects_other_vendors(db_session: Session) -> None:
    board = AtsBoard(vendor="greenhouse", identifier="spotify", confidence=Decimal("1.00"))
    with pytest.raises(ValueError, match="expects a 'lever' board"):
        ingest_board(db_session, board, run_id="r1", now=NOW)


# --- ADR 0015: the shared 404 / dead-board rule ------------------------------------------


@pytest.fixture
def liveness_poll(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget
) -> tuple[AtsBoard, liveness.Poll]:
    bodies = {"present": NORMAL.read_bytes(), "absent": EMPTY.read_bytes()}
    route = mocked.get(GLOBAL_URL)
    board = _board(db_session)

    def poll(now: datetime, outcome: liveness.Outcome) -> RunResult:
        if outcome == "404":
            route.mock(return_value=httpx.Response(404))
        else:
            route.mock(return_value=httpx.Response(200, content=bodies[outcome]))
        return ingest_board(db_session, board, run_id=f"r-{now:%d%H}", now=now, budget=budget)

    return board, poll


@pytest.mark.db
def test_liveness_404_between_polls_never_closes(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.a_404_between_polls_never_closes(db_session, *liveness_poll, NOW)


@pytest.mark.db
def test_liveness_two_404s_under_12h_stay_active(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.two_404s_under_12h_stay_active(db_session, *liveness_poll, NOW)


@pytest.mark.db
def test_liveness_two_404s_12h_apart_kill_once(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.two_404s_12h_apart_kill_once(db_session, *liveness_poll, NOW)


@pytest.mark.db
def test_liveness_a_later_200_revives_and_resets(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.a_later_200_revives_and_resets(db_session, *liveness_poll, NOW)
