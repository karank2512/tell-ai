"""Greenhouse collector: contract, parse, fetch (respx) and the ingest path (spec section 3.1)."""

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

from tell.collectors import greenhouse
from tell.collectors.base import RunResult, content_hash, discover_collectors
from tell.collectors.greenhouse import (
    COLLECTOR,
    BoardNotFoundError,
    GreenhouseJobsResponse,
    content_to_text,
    fetch_board,
    fetch_board_meta,
    infer_remote,
    ingest_board,
    parse,
    run_fixture,
)
from tell.http import RateBudget, make_client
from tell.models import AtsBoard, Company, EventRow, Posting, PostingObservation
from tests.collectors import _board_liveness as liveness

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse"
NORMAL = FIXTURES / "normal.json"
NORMAL_AIRBNB = FIXTURES / "normal_airbnb.json"
EMPTY = FIXTURES / "empty_board.json"
DRIFT = FIXTURES / "drift.json"
UPDATED = FIXTURES / "updated.json"
BOARD_META = FIXTURES / "board_meta.json"
NOT_FOUND = FIXTURES / "not_found.json"

JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/stripe"
USER_AGENT = "tell/0.1 (+https://github.com/karank2512/tell-ai)"

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def budget() -> RateBudget:
    """A generous bucket with a frozen clock so tests never wait."""
    return RateBudget(1000, burst=1000, clock=lambda: 0.0, sleep=lambda _s: None)


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


# --- registration and contract ---------------------------------------------------------


def test_collector_is_registered_as_ats() -> None:
    assert COLLECTOR.source == "greenhouse"
    assert COLLECTOR.kind == "ats"
    assert COLLECTOR.response_model is GreenhouseJobsResponse
    assert COLLECTOR.fixture_dir == FIXTURES
    assert COLLECTOR.drift_missing_field == "updated_at"
    assert "greenhouse" in [spec.source for spec in discover_collectors()]


@pytest.mark.parametrize("path", [NORMAL, NORMAL_AIRBNB, UPDATED, EMPTY], ids=lambda p: p.name)
def test_fixtures_validate(path: Path) -> None:
    GreenhouseJobsResponse.model_validate_json(path.read_text(encoding="utf-8"))


def test_fixtures_are_small_and_scrubbed() -> None:
    for path in FIXTURES.iterdir():
        assert path.stat().st_size < 50_000, path.name
        text = path.read_text(encoding="utf-8")
        assert "@" not in text
    # Five jobs per the spec's "trimmed to 5 jobs"; Airbnb covers Workplace Type.
    assert len(_json(NORMAL)["jobs"]) == 5
    assert len(_json(NORMAL_AIRBNB)["jobs"]) == 3


def test_drift_fixture_fails_naming_updated_at() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        parse(DRIFT.read_text(encoding="utf-8"))
    assert "updated_at" in str(excinfo.value)


@pytest.mark.parametrize(
    "field", ["id", "title", "location", "absolute_url", "departments", "offices", "content"]
)
def test_every_spec_field_is_required(field: str) -> None:
    doc = _json(NORMAL)
    del doc["jobs"][0][field]
    with pytest.raises(pydantic.ValidationError) as excinfo:
        parse(doc)
    assert field in str(excinfo.value)


def test_first_published_and_metadata_may_be_null_but_not_missing() -> None:
    doc = _json(NORMAL)
    doc["jobs"][0]["first_published"] = None
    assert parse(doc)[0].posted_at is None
    del doc["jobs"][0]["first_published"]
    with pytest.raises(pydantic.ValidationError, match="first_published"):
        parse(doc)


# --- parse -------------------------------------------------------------------------------


def test_parse_normal_exact_values() -> None:
    postings = parse(NORMAL.read_text(encoding="utf-8"))
    edt = timedelta(hours=-4)
    got = [
        (p.external_id, p.title, p.location, p.department, p.remote, p.posted_at, p.url, p.version)
        for p in postings
    ]
    assert got == [
        (
            "8172510",
            "Abuse Investigator",
            "Seattle, San Francisco, New York City",
            "8611 Security Analytics",
            None,
            datetime(2026, 9, 9, 14, 50, 29, tzinfo=UTC),
            "https://stripe.com/jobs/search?gh_jid=8172510",
            "2026-09-25T20:45:00+00:00",
        ),
        (
            "7844214",
            "AEO and GEO Marketing Manager",
            "Remote in the US",
            "2314 Performance Marketing",
            True,
            datetime(2026, 4, 23, 18, 21, 56, tzinfo=UTC),
            "https://stripe.com/jobs/search?gh_jid=7844214",
            "2026-09-25T20:44:40+00:00",
        ),
        (
            "6042172",
            "Backend Engineer, Core Technology",
            "US-Remote, Chicago, Seattle, San Francisco",
            "8127 Core Infrastructure",
            True,
            datetime(2024, 6, 14, 22, 4, 50, tzinfo=UTC),
            "https://stripe.com/jobs/search?gh_jid=6042172",
            "2026-09-25T20:44:31+00:00",
        ),
        (
            "8197886",
            "Machine Learning Engineer, Link",
            "New York City",  # the provider sends a trailing space
            "8570 Consumer Network - Eng",
            None,
            datetime(2026, 9, 21, 16, 52, 34, tzinfo=UTC),
            "https://stripe.com/jobs/search?gh_jid=8197886",
            "2026-09-25T20:45:05+00:00",
        ),
        (
            "8237366",
            "Customer Success Manager, Revenue Suite",
            "Chicago",
            "1340 Customer Success Management",
            None,
            datetime(2026, 10, 5, 9, 44, 45, tzinfo=UTC),
            "https://stripe.com/jobs/search?gh_jid=8237366",
            "2026-10-05T09:44:45+00:00",
        ),
    ]
    # posted_at keeps the provider's offset; it is the same instant as the UTC value.
    assert postings[0].posted_at is not None
    assert postings[0].posted_at.utcoffset() == edt
    assert postings[0].extra == {
        "departments": ["8611 Security Analytics"],
        "offices": ["US"],
        "workplace_type": None,
    }


def test_parse_reads_workplace_type_metadata() -> None:
    postings = parse(NORMAL_AIRBNB.read_text(encoding="utf-8"))
    got = [(p.external_id, p.location, p.remote, p.extra["workplace_type"]) for p in postings]
    assert got == [
        ("7995153", "Berlin, Germany", False, "Hybrid"),
        # Location text says nothing about remote; the metadata does.
        ("8214444", "San Francisco, CA", True, "Remote"),
        ("8214375", "San Francisco, CA", False, "Onsite"),
    ]
    assert postings[1].extra["offices"] == ["United States"]


def test_parse_accepts_model_dict_and_bytes() -> None:
    raw = NORMAL.read_bytes()
    model = GreenhouseJobsResponse.model_validate_json(raw)
    assert parse(model) == parse(raw) == parse(json.loads(raw))


def test_empty_board_parses_to_nothing() -> None:
    assert parse(EMPTY.read_text(encoding="utf-8")) == []


def test_content_is_decoded_once_then_stripped_and_discarded() -> None:
    doc = _json(NORMAL)
    raw = doc["jobs"][0]["content"]
    assert raw.startswith("&lt;h2&gt;")  # entity-encoded HTML, as Greenhouse sends it
    assert "&amp;amp;" in raw  # an HTML entity inside the encoded HTML
    expected = (
        "Who we are Stripe is a financial infrastructure platform for businesses. "
        "About the team Abuse Operations & Investigations is the front-line incident "
        "response team."
    )
    assert content_to_text(raw) == expected
    posting = parse(doc)[0]
    assert posting.content_hash == content_hash(expected)
    assert posting.description_plain is None
    assert "content" not in posting.extra


def test_content_to_text_handles_nbsp_and_skips_script_and_style() -> None:
    raw = (
        "&lt;style&gt;p {color: red}&lt;/style&gt;&lt;p&gt;Risk&amp;nbsp;&amp;amp; "
        "Compliance&lt;/p&gt;&lt;script&gt;track()&lt;/script&gt;&lt;ul&gt;&lt;li&gt;"
        "Python&lt;/li&gt;&lt;/ul&gt;"
    )
    assert content_to_text(raw) == "Risk & Compliance Python"
    assert content_to_text("") == ""


def test_whitespace_title_and_blank_location_are_cleaned() -> None:
    doc = _json(NORMAL)
    job = doc["jobs"][0]
    job["title"] = "  Account   Executive "
    job["location"]["name"] = "   "
    job["departments"] = []
    job["offices"].append({"id": 1, "name": " ", "location": None})
    posting = parse(doc)[0]
    assert posting.title == "Account Executive"
    assert posting.location is None
    assert posting.department is None
    assert posting.extra["departments"] == []
    assert posting.extra["offices"] == ["US"]


def test_first_department_is_the_posting_department() -> None:
    doc = _json(NORMAL)
    doc["jobs"][0]["departments"].append({"id": 2, "name": "Risk", "child_ids": []})
    posting = parse(doc)[0]
    assert posting.department == "8611 Security Analytics"
    assert posting.extra["departments"] == ["8611 Security Analytics", "Risk"]


@pytest.mark.parametrize(
    ("metadata", "workplace_type", "remote"),
    [
        ([{"id": 1, "name": "Workplace Type", "value": "Remote"}], "Remote", True),
        ([{"id": 1, "name": "workplace type ", "value": "On-site"}], "On-site", False),
        (
            [{"id": 1, "name": "Workplace Type", "value": ["Remote", "Hybrid"]}],
            "Remote, Hybrid",
            True,
        ),
        ([{"id": 1, "name": "Workplace Type", "value": None}], None, None),
        ([{"id": 1, "name": "Workplace Type", "value": "  "}], None, None),
        ([{"id": 1, "name": "Workplace Type", "value": True}], None, None),
        ([{"id": 1, "name": "Workplace Type", "value": "Flexible"}], "Flexible", None),
        ([{"id": 1, "name": "Cost Center", "value": "Remote"}], None, None),
        ([], None, None),
    ],
)
def test_remote_from_metadata(
    metadata: list[dict[str, Any]], workplace_type: str | None, remote: bool | None
) -> None:
    doc = _json(NORMAL)
    doc["jobs"][0]["metadata"] = metadata  # job 0's location does not mention remote
    posting = parse(doc)[0]
    assert posting.extra["workplace_type"] == workplace_type
    assert posting.remote is remote


@pytest.mark.parametrize(
    ("workplace_type", "location", "expected"),
    [
        ("Remote", "Berlin", True),
        ("Hybrid", "Remote in the US", False),  # metadata wins over location text
        ("Onsite", None, False),
        (None, "US-Remote, Chicago", True),
        (None, "Poland - Remote OR Romania - Remote", True),
        (None, "Remotely Possible City", None),  # whole word only
        (None, "Chicago", None),
        (None, None, None),
        ("Flexible", "Remote", True),
    ],
)
def test_infer_remote(
    workplace_type: str | None, location: str | None, expected: bool | None
) -> None:
    assert infer_remote(workplace_type, location) is expected


# --- fetch (respx) -----------------------------------------------------------------------


@respx.mock
def test_fetch_board_builds_documented_url_with_user_agent(
    client: httpx.Client, budget: RateBudget
) -> None:
    route = respx.get(JOBS_URL, params={"content": "true"}).mock(
        return_value=httpx.Response(200, content=NORMAL.read_bytes())
    )
    response = fetch_board(client, "stripe", budget=budget)
    assert route.call_count == 1
    request = route.calls.last.request
    assert str(request.url) == f"{JOBS_URL}?content=true"
    assert request.headers["User-Agent"] == USER_AGENT
    assert [job.id for job in response.jobs] == [8172510, 7844214, 6042172, 8197886, 8237366]
    assert response.meta.total == 5


@respx.mock
def test_fetch_board_quotes_the_token(client: httpx.Client, budget: RateBudget) -> None:
    route = respx.get("https://boards-api.greenhouse.io/v1/boards/a%2Fb/jobs").mock(
        return_value=httpx.Response(200, content=EMPTY.read_bytes())
    )
    assert fetch_board(client, "a/b", budget=budget).jobs == []
    assert route.call_count == 1


@respx.mock
def test_fetch_board_backs_off_on_429(client: httpx.Client, budget: RateBudget) -> None:
    route = respx.get(JOBS_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, content=NORMAL.read_bytes()),
        ]
    )
    sleeps = SleepRecorder()
    response = fetch_board(client, "stripe", budget=budget, sleep=sleeps)
    assert route.call_count == 3
    assert sleeps.calls == [1.0, 3.0]
    assert len(response.jobs) == 5


@respx.mock
def test_fetch_board_404_raises_board_not_found(client: httpx.Client, budget: RateBudget) -> None:
    respx.get(JOBS_URL).mock(return_value=httpx.Response(404, content=NOT_FOUND.read_bytes()))
    sleeps = SleepRecorder()
    with pytest.raises(BoardNotFoundError) as excinfo:
        fetch_board(client, "stripe", budget=budget, sleep=sleeps)
    assert excinfo.value.board_token == "stripe"
    assert sleeps.calls == []  # 404 is final, never retried


@respx.mock
def test_fetch_board_other_4xx_raises_http_status_error(
    client: httpx.Client, budget: RateBudget
) -> None:
    respx.get(JOBS_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_board(client, "stripe", budget=budget)


@respx.mock
def test_fetch_board_drifted_response_raises_validation_error(
    client: httpx.Client, budget: RateBudget
) -> None:
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, content=DRIFT.read_bytes()))
    with pytest.raises(pydantic.ValidationError, match="updated_at"):
        fetch_board(client, "stripe", budget=budget)


@respx.mock
def test_fetch_board_uses_the_shared_ats_budget_by_default(client: httpx.Client) -> None:
    greenhouse.shared_budget.cache_clear()
    try:
        respx.get(JOBS_URL).mock(return_value=httpx.Response(200, content=EMPTY.read_bytes()))
        assert fetch_board(client, "stripe").jobs == []
        shared = greenhouse.shared_budget()
        assert shared is greenhouse.shared_budget()
        assert shared.rps == 8.0  # TELL_RATE_ATS_RPS default
    finally:
        greenhouse.shared_budget.cache_clear()


@respx.mock
def test_fetch_board_acquires_the_budget_per_attempt(client: httpx.Client) -> None:
    acquired: list[float] = []

    class CountingBudget(RateBudget):
        def acquire(self) -> float:
            acquired.append(1.0)
            return 0.0

    respx.get(JOBS_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=EMPTY.read_bytes())]
    )
    fetch_board(client, "stripe", budget=CountingBudget(1), sleep=SleepRecorder())
    assert len(acquired) == 2


@respx.mock
def test_fetch_board_meta_returns_the_board_name(client: httpx.Client, budget: RateBudget) -> None:
    route = respx.get(BOARD_URL).mock(
        return_value=httpx.Response(200, content=BOARD_META.read_bytes())
    )
    assert fetch_board_meta(client, "stripe", budget=budget) == "Stripe"
    request = route.calls.last.request
    assert str(request.url) == BOARD_URL
    assert request.headers["User-Agent"] == USER_AGENT


@respx.mock
def test_fetch_board_meta_404(client: httpx.Client, budget: RateBudget) -> None:
    respx.get(BOARD_URL).mock(return_value=httpx.Response(404, content=NOT_FOUND.read_bytes()))
    with pytest.raises(BoardNotFoundError):
        fetch_board_meta(client, "stripe", budget=budget)


# --- database: ingest_board and run_fixture ----------------------------------------------


def _board(session: Session, token: str = "stripe", **kwargs: Any) -> AtsBoard:
    company = Company(domain=f"{token}.test", name=token.title())
    session.add(company)
    session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor="greenhouse",
        identifier=token,
        confidence=Decimal("0.95"),
        status="active",
        **kwargs,
    )
    session.add(board)
    session.flush()
    return board


def _count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _types(session: Session) -> list[str]:
    return sorted(session.scalars(select(EventRow.type)))


@pytest.mark.db
@respx.mock
def test_ingest_board_writes_postings_and_events(
    db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, content=NORMAL.read_bytes()))
    board = _board(db_session)
    result = ingest_board(db_session, board, run_id="r1", now=NOW, client=client, budget=budget)

    assert (result.fetched, result.inserted, result.skipped) == (5, 5, 0)
    assert [e.type for e in result.events] == ["job_opened"] * 5
    assert _count(db_session, Posting) == 5
    assert _count(db_session, PostingObservation) == 5
    assert board.last_polled_at == NOW
    assert board.last_verified_at is None  # discovery owns it (ADR 0015)
    assert board.status == "active"

    event = next(e for e in result.events if e.payload["posting_external_id"] == "8172510")
    assert event.external_id == "greenhouse:stripe:8172510"
    assert event.url == "https://stripe.com/jobs/search?gh_jid=8172510"
    assert event.company_id == board.company_id
    assert event.payload["version_marker"] == "2026-09-25T20:45:00+00:00"
    assert event.payload["department"] == "8611 Security Analytics"
    assert event.payload["extra"]["offices"] == ["US"]
    assert "financial infrastructure" not in json.dumps(event.payload)

    stored = db_session.scalars(select(Posting).where(Posting.external_id == "6042172")).one()
    assert stored.remote is True
    assert stored.version_marker == "2026-09-25T20:44:31+00:00"
    assert stored.content_hash is not None


@pytest.mark.db
@respx.mock
def test_ingest_board_creates_its_own_client_when_none_given(
    db_session: Session, budget: RateBudget
) -> None:
    route = respx.get(JOBS_URL).mock(return_value=httpx.Response(200, content=EMPTY.read_bytes()))
    board = _board(db_session)
    result = ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget)
    assert (result.fetched, result.inserted, result.events) == (0, 0, [])
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT
    assert _count(db_session, Posting) == 0
    assert _count(db_session, EventRow) == 0
    assert board.last_polled_at == NOW


@pytest.mark.db
@respx.mock
def test_ingest_board_unchanged_second_poll_writes_nothing(
    db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, content=NORMAL.read_bytes()))
    board = _board(db_session)
    ingest_board(db_session, board, run_id="r1", now=NOW, client=client, budget=budget)
    second = ingest_board(db_session, board, run_id="r2", now=LATER, client=client, budget=budget)
    assert (second.fetched, second.inserted, second.skipped, second.events) == (5, 0, 0, [])
    assert _count(db_session, EventRow) == 5
    assert _count(db_session, PostingObservation) == 10


@pytest.mark.db
@respx.mock
def test_ingest_board_404_records_the_streak_and_nothing_else(
    db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    route = respx.get(JOBS_URL).mock(
        return_value=httpx.Response(404, content=NOT_FOUND.read_bytes())
    )
    board = _board(db_session, last_verified_at=NOW - timedelta(days=1))

    first = ingest_board(db_session, board, run_id="r1", now=NOW, client=client, budget=budget)
    assert (first.fetched, first.inserted, first.skipped, first.events) == (0, 0, 0, [])
    assert (board.status, board.not_found_count, board.first_not_found_at) == ("active", 1, NOW)
    assert board.last_polled_at is None
    assert board.last_verified_at == NOW - timedelta(days=1)

    ingest_board(db_session, board, run_id="r2", now=LATER, client=client, budget=budget)
    assert (board.status, board.not_found_count) == ("active", 2)  # only 6h apart
    assert route.call_count == 2
    assert _count(db_session, EventRow) == 0


@pytest.mark.db
@respx.mock
def test_ingest_board_revives_a_dead_board_that_answers(
    db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, content=EMPTY.read_bytes()))
    board = _board(db_session)
    board.status = "dead"
    ingest_board(db_session, board, run_id="r1", now=NOW, client=client, budget=budget)
    assert board.status == "active"
    assert board.last_polled_at == NOW
    assert board.last_verified_at is None


# --- ADR 0015: the shared 404 / dead-board rule ------------------------------------------


@pytest.fixture
def liveness_poll(
    db_session: Session, client: httpx.Client, budget: RateBudget
) -> Iterator[tuple[AtsBoard, liveness.Poll]]:
    bodies = {"present": NORMAL.read_bytes(), "absent": EMPTY.read_bytes()}
    with respx.mock(assert_all_called=False) as router:
        route = router.get(JOBS_URL)
        board = _board(db_session)

        def poll(now: datetime, outcome: liveness.Outcome) -> RunResult:
            if outcome == "404":
                route.mock(return_value=httpx.Response(404, content=NOT_FOUND.read_bytes()))
            else:
                route.mock(return_value=httpx.Response(200, content=bodies[outcome]))
            return ingest_board(
                db_session, board, run_id=f"r-{now:%d%H}", now=now, client=client, budget=budget
            )

        yield board, poll


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


@pytest.mark.db
def test_ingest_board_rejects_other_vendors(db_session: Session) -> None:
    board = AtsBoard(vendor="lever", identifier="stripe", confidence=Decimal("0.95"))
    with pytest.raises(ValueError, match="greenhouse"):
        ingest_board(db_session, board, run_id="r1", now=NOW)


@pytest.mark.db
def test_run_fixture_twice_inserts_nothing_the_second_time(db_session: Session) -> None:
    first = run_fixture(db_session, NORMAL, run_id="r1", now=NOW)
    assert (first.fetched, first.inserted, first.skipped) == (5, 5, 0)
    counts = (_count(db_session, EventRow), _count(db_session, Posting))

    second = run_fixture(db_session, NORMAL, run_id="r2", now=LATER)
    assert (second.fetched, second.inserted, second.skipped, second.events) == (5, 0, 0, [])
    assert (_count(db_session, EventRow), _count(db_session, Posting)) == counts
    # The synthetic company and board are created once and reused.
    assert _count(db_session, AtsBoard) == 1
    assert _count(db_session, Company) == 1
    board = db_session.scalars(select(AtsBoard)).one()
    assert (board.vendor, board.identifier) == ("greenhouse", greenhouse.FIXTURE_BOARD)
    assert board.last_polled_at == LATER


@pytest.mark.db
def test_run_fixture_reuses_a_preexisting_fixture_company(db_session: Session) -> None:
    db_session.add(Company(domain=greenhouse.FIXTURE_DOMAIN, name="Already here"))
    db_session.flush()
    run_fixture(db_session, EMPTY, run_id="r1", now=NOW)
    assert _count(db_session, Company) == 1
    assert _count(db_session, AtsBoard) == 1


@pytest.mark.db
def test_empty_board_yields_no_postings_and_no_events(db_session: Session) -> None:
    result = run_fixture(db_session, EMPTY, run_id="r1", now=NOW)
    assert (result.fetched, result.inserted, result.skipped, result.events) == (0, 0, 0, [])
    assert _count(db_session, Posting) == 0
    assert _count(db_session, EventRow) == 0


@pytest.mark.db
def test_normal_then_updated_emits_exactly_one_job_updated(db_session: Session) -> None:
    run_fixture(db_session, NORMAL, run_id="r1", now=NOW)
    assert _types(db_session) == ["job_opened"] * 5

    result = run_fixture(db_session, UPDATED, run_id="r2", now=LATER)
    assert result.inserted == 1
    assert [e.type for e in result.events] == ["job_updated"]
    updated = result.events[0]
    assert updated.payload["posting_external_id"] == "8197886"
    assert updated.version == "2026-10-02T15:05:12+00:00"
    assert updated.payload["version_marker"] == "2026-10-02T15:05:12+00:00"
    assert _types(db_session) == ["job_opened"] * 5 + ["job_updated"]

    posting = db_session.scalars(select(Posting).where(Posting.external_id == "8197886")).one()
    assert posting.version_marker == "2026-10-02T15:05:12+00:00"


@pytest.mark.db
def test_payload_carries_workplace_type_and_no_text(db_session: Session) -> None:
    result = run_fixture(db_session, NORMAL_AIRBNB, run_id="r1", now=NOW)
    by_id = {e.payload["posting_external_id"]: e.payload for e in result.events}
    assert by_id["8214444"]["remote"] is True
    assert by_id["8214444"]["extra"] == {
        "departments": ["Information Technology"],
        "offices": ["United States"],
        "workplace_type": "Remote",
    }
    assert by_id["7995153"]["remote"] is False
    for payload in by_id.values():
        assert "Airbnb was born" not in json.dumps(payload)
