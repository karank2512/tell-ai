"""Ashby Job Posting API collector (spec section 3.3): contract, parse, state machine, HTTP."""

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

from tell.collectors import ashby
from tell.collectors.base import ObservedPosting, RunResult, start_run
from tell.config import get_settings
from tell.http import RateBudget, RetryExhausted, make_client
from tell.models import AtsBoard, Company, EventRow, Posting
from tests.collectors import _board_liveness as liveness

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ashby"
NORMAL = FIXTURES / "normal.json"
EMPTY = FIXTURES / "empty_board.json"
DRIFT = FIXTURES / "drift.json"
UPDATED = FIXTURES / "updated.json"
NULLS = FIXTURES / "normal_nulls.json"

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)

BOARD = "ramp"
LIST_URL = "https://api.ashbyhq.com/posting-api/job-board/ramp"
FULL_URL = f"{LIST_URL}?includeCompensation=true"

ID_SECURITY = "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
ID_ARCHITECT = "4c24a55a-ea3b-4cea-a5b5-7862938616bf"
ID_AE = "1515fe6d-1d8e-475b-a5ee-cefe43e78cb7"
ID_IT = "4bc09b14-0389-40cd-9d5b-f5557f451d50"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _by_id(postings: list[ObservedPosting]) -> dict[str, ObservedPosting]:
    return {p.external_id: p for p in postings}


class FakeTime:
    """Clock + sleeper pair for RateBudget / fetch; sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def budget(fake_time: FakeTime) -> RateBudget:
    return RateBudget(8.0, clock=fake_time.clock, sleep=fake_time.sleep)


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


# --- contract --------------------------------------------------------------------------


def test_collector_registration() -> None:
    spec = ashby.COLLECTOR
    assert spec.source == "ashby"
    assert spec.kind == "ats"
    assert spec.response_model is ashby.AshbyJobBoard
    assert spec.fixture_dir == FIXTURES
    assert spec.drift_missing_field == "publishedAt"
    for path in (NORMAL, EMPTY, DRIFT, UPDATED, NULLS):
        assert path.stat().st_size < 50_000, path.name


def test_normal_fixture_validates() -> None:
    board = ashby.AshbyJobBoard.model_validate_json(NORMAL.read_bytes())
    assert board.api_version == "1"
    assert len(board.jobs) == 4
    assert all(job.is_listed for job in board.jobs)


def test_empty_board_validates_and_yields_nothing() -> None:
    board = ashby.AshbyJobBoard.model_validate_json(EMPTY.read_bytes())
    assert board.jobs == []
    assert ashby.parse(EMPTY.read_text(encoding="utf-8")) == []


def test_drift_fixture_fails_naming_published_at() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ashby.AshbyJobBoard.model_validate_json(DRIFT.read_bytes())
    text = str(excinfo.value)
    assert "jobs.1.publishedAt" in text
    assert "Field required" in text


@pytest.mark.parametrize(
    "field", ["id", "title", "jobUrl", "isListed", "location", "secondaryLocations", "isRemote"]
)
def test_each_required_job_field_is_enforced(field: str) -> None:
    data = _load(NORMAL)
    del data["jobs"][0][field]
    with pytest.raises(pydantic.ValidationError, match=field):
        ashby.AshbyJobBoard.model_validate(data)


def test_missing_jobs_or_api_version_fails() -> None:
    with pytest.raises(pydantic.ValidationError, match="jobs"):
        ashby.AshbyJobBoard.model_validate({"apiVersion": "1"})
    with pytest.raises(pydantic.ValidationError, match="apiVersion"):
        ashby.AshbyJobBoard.model_validate({"jobs": []})


# --- parse -----------------------------------------------------------------------------


def test_parse_normal_exact_values() -> None:
    postings = ashby.parse(NORMAL.read_bytes())
    assert [p.external_id for p in postings] == [ID_SECURITY, ID_ARCHITECT, ID_AE, ID_IT]
    by_id = _by_id(postings)

    security = by_id[ID_SECURITY]
    assert security.title == "Security Engineer, Cloud"  # leading space stripped
    assert security.department == "Engineering"
    assert security.location == "New York, NY (HQ)"
    assert security.remote is False  # Hybrid, although Ashby says isRemote: true
    assert security.posted_at == datetime(2026, 4, 7, 17, 12, 35, 753000, tzinfo=UTC)
    assert security.version == "2026-04-07T17:12:35.753000+00:00"
    assert security.url == f"https://jobs.ashbyhq.com/ramp/{ID_SECURITY}"
    assert security.content_hash is not None
    assert security.description_plain is None
    assert security.extra == {
        "team": "Backend",
        "employmentType": "FullTime",
        "workplaceType": "Hybrid",
        "isRemote": True,
        "secondaryLocations": ["Remote (Canada)", "Remote (US)", "Miami, FL"],
        "applyUrl": f"https://jobs.ashbyhq.com/ramp/{ID_SECURITY}/application",
        "compensation": {
            "compensationTierSummary": "$211.4K \u2013 $290.6K \u2022 Offers Equity",
            "scrapeableCompensationSalarySummary": "$211.4K - $290.6K",
            "summaryComponents": [
                {
                    "compensationType": "EquityPercentage",
                    "interval": "NONE",
                    "currencyCode": None,
                    "minValue": None,
                    "maxValue": None,
                },
                {
                    "compensationType": "Salary",
                    "interval": "1 YEAR",
                    "currencyCode": "USD",
                    "minValue": 211400,
                    "maxValue": 290600,
                },
            ],
        },
    }

    architect = by_id[ID_ARCHITECT]
    assert architect.title == "Procurement Solutions Architect"
    assert architect.department == "Sales"
    assert architect.location == "Remote (US)"
    assert architect.remote is True
    assert architect.version == "2026-09-21T20:33:59.665000+00:00"
    assert architect.url == f"https://jobs.ashbyhq.com/ramp/{ID_ARCHITECT}"

    account_exec = by_id[ID_AE]
    assert account_exec.title == "Account Executive"
    assert account_exec.location == "London"
    assert account_exec.remote is False
    assert account_exec.posted_at == datetime(2026, 6, 8, 19, 17, 53, 481000, tzinfo=UTC)
    assert account_exec.version == "2026-06-08T19:17:53.481000+00:00"
    assert "compensation" not in account_exec.extra  # empty compensation object

    it_site = by_id[ID_IT]
    assert it_site.title == "IT Site Specialist"
    assert it_site.department == "IT"
    assert it_site.location == "San Francisco, CA"
    assert it_site.remote is False
    assert it_site.extra["employmentType"] == "Temporary"
    assert it_site.extra["compensation"]["compensationTierSummary"] == "$145,760 \u2013 $200,420"

    assert len({p.content_hash for p in postings}) == 4


def test_parse_accepts_a_validated_model() -> None:
    board = ashby.AshbyJobBoard.model_validate_json(NORMAL.read_bytes())
    assert [p.external_id for p in ashby.parse(board)] == [
        p.external_id for p in ashby.parse(NORMAL.read_text(encoding="utf-8"))
    ]


def test_unlisted_and_blank_title_postings_are_skipped() -> None:
    data = _load(NORMAL)
    data["jobs"][1]["isListed"] = False
    data["jobs"][3]["title"] = "   "
    postings = ashby.parse(json.dumps(data))
    assert [p.external_id for p in postings] == [ID_SECURITY, ID_AE]


@pytest.mark.parametrize(
    ("workplace", "is_remote", "location", "expected"),
    [
        ("Remote", False, "New York", True),
        ("OnSite", True, "Remote (US)", False),
        ("Hybrid", True, "London", False),
        (None, True, "Anywhere", True),
        (None, False, "Berlin", False),
        (None, None, "remote", True),
        (None, None, "United States (remote)", True),
        (None, None, "Berlin", None),
        (None, None, "Remotely Berlin", None),
    ],
)
def test_remote_flag_inference(
    workplace: str | None, is_remote: bool | None, location: str, expected: bool | None
) -> None:
    data = _load(NORMAL)
    job = data["jobs"][0]
    job["workplaceType"] = workplace
    job["isRemote"] = is_remote
    job["location"] = location
    (posting, *_) = ashby.parse(json.dumps(data))
    assert posting.remote is expected


ID_NULLS_ONSITE = "14af59ec-6dd0-4d32-a7d7-8506ba93d5a0"
ID_NULLS_REMOTE = "1400d1a6-6638-4a3e-bff7-1899e3769af2"
ID_NULLS_UNKNOWN = "59ed9c30-26a6-4a72-bf8a-48af7bbafd75"


def test_nulls_fixture_parses_with_unknown_remote_flags() -> None:
    """Real 2026-10-14 jobs with ``isRemote``/``workplaceType``/``address`` null."""
    board = ashby.AshbyJobBoard.model_validate_json(NULLS.read_bytes())
    assert [job.is_remote for job in board.jobs] == [False, None, None]
    by_id = _by_id(ashby.parse(board))
    assert set(by_id) == {ID_NULLS_ONSITE, ID_NULLS_REMOTE, ID_NULLS_UNKNOWN}
    assert by_id[ID_NULLS_ONSITE].remote is False
    assert by_id[ID_NULLS_REMOTE].remote is True  # location "remote", both flags null
    assert by_id[ID_NULLS_REMOTE].location == "remote"
    assert by_id[ID_NULLS_UNKNOWN].remote is None  # location "Berlin", both flags null
    assert by_id[ID_NULLS_UNKNOWN].extra["isRemote"] is None
    assert by_id[ID_NULLS_UNKNOWN].extra["workplaceType"] is None
    assert by_id[ID_NULLS_UNKNOWN].location == "Berlin"


def test_is_remote_may_be_null_but_not_missing() -> None:
    data = _load(NORMAL)
    data["jobs"][0]["isRemote"] = None
    assert ashby.AshbyJobBoard.model_validate(data).jobs[0].is_remote is None
    del data["jobs"][0]["isRemote"]
    with pytest.raises(pydantic.ValidationError, match="isRemote"):
        ashby.AshbyJobBoard.model_validate(data)


def test_validate_board_skips_one_invalid_job_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="tell.collectors.ashby"):
        board = ashby.validate_board(DRIFT.read_bytes(), identifier="ramp")
    assert [job.id for job in board.jobs] == [ID_SECURITY, ID_AE, ID_IT]
    assert board.invalid_jobs == 1
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert "ramp" in record.getMessage()
    assert ID_ARCHITECT in record.getMessage()
    assert "publishedAt (missing)" in record.getMessage()


def test_validate_board_counts_a_non_object_job() -> None:
    data = _load(NORMAL)
    data["jobs"].append("not a job")
    board = ashby.validate_board(json.dumps(data), identifier="ramp")
    assert len(board.jobs) == 4
    assert board.invalid_jobs == 1


def test_validate_board_fails_when_every_job_fails() -> None:
    data = _load(NORMAL)
    for job in data["jobs"]:
        del job["publishedAt"]
    with pytest.raises(pydantic.ValidationError, match=r"jobs\.0\.publishedAt"):
        ashby.validate_board(json.dumps(data), identifier="ramp")


def test_validate_board_envelope_stays_strict() -> None:
    with pytest.raises(pydantic.ValidationError, match="apiVersion"):
        ashby.validate_board(json.dumps({"jobs": []}), identifier="ramp")
    with pytest.raises(pydantic.ValidationError, match="jobs"):
        ashby.validate_board(json.dumps({"apiVersion": "1"}), identifier="ramp")
    assert ashby.validate_board(EMPTY.read_bytes(), identifier="ramp").invalid_jobs == 0


def test_compensation_hidden_by_employer_or_absent_is_not_kept() -> None:
    data = _load(NORMAL)
    data["jobs"][0]["shouldDisplayCompensationOnJobPostings"] = False
    del data["jobs"][1]["compensation"]
    by_id = _by_id(ashby.parse(json.dumps(data)))
    assert "compensation" not in by_id[ID_SECURITY].extra
    assert "compensation" not in by_id[ID_ARCHITECT].extra
    assert "compensation" in by_id[ID_IT].extra


def test_displayed_but_empty_compensation_is_not_kept() -> None:
    data = _load(NORMAL)
    data["jobs"][2]["shouldDisplayCompensationOnJobPostings"] = True
    data["jobs"][2]["compensation"] = {
        "compensationTierSummary": None,
        "scrapeableCompensationSalarySummary": None,
        "compensationTiers": [],
        "summaryComponents": [],
    }
    by_id = _by_id(ashby.parse(json.dumps(data)))
    assert "compensation" not in by_id[ID_AE].extra


def test_long_strings_in_extra_are_clipped() -> None:
    data = _load(NORMAL)
    data["jobs"][0]["compensation"]["compensationTierSummary"] = "x" * 900
    data["jobs"][0]["team"] = "t" * 600
    (posting, *_) = ashby.parse(json.dumps(data))
    assert posting.extra["compensation"]["compensationTierSummary"] == "x" * 500
    assert posting.extra["team"] == "t" * 500


def test_naive_published_at_is_read_as_utc_and_null_department_kept_null() -> None:
    data = _load(NORMAL)
    data["jobs"][0]["publishedAt"] = "2026-04-07T17:12:35"
    data["jobs"][0]["department"] = None
    (posting, *_) = ashby.parse(json.dumps(data))
    assert posting.posted_at == datetime(2026, 4, 7, 17, 12, 35, tzinfo=UTC)
    assert posting.version == "2026-04-07T17:12:35+00:00"
    assert posting.department is None


def test_payload_never_carries_description_text() -> None:
    raw = NORMAL.read_text(encoding="utf-8")
    for posting in ashby.parse(raw):
        dumped = json.dumps(posting.extra)
        assert "ABOUT THE ROLE" not in dumped
        assert "description" not in dumped.lower()


# --- fixture replay / state machine ---------------------------------------------------------


def _count(session: Session, model: type[Any], **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return session.execute(stmt).scalar_one()


@pytest.mark.db
def test_run_fixture_twice_is_idempotent(db_session: Session) -> None:
    first = ashby.run_fixture(db_session, NORMAL, run_id=start_run(db_session, "ashby"), now=T0)
    assert first.fetched == 4
    assert first.inserted == 4
    assert first.skipped == 0
    assert {e.type for e in first.events} == {"job_opened"}
    counts = (_count(db_session, EventRow), _count(db_session, Posting))
    assert counts == (4, 4)

    second = ashby.run_fixture(
        db_session, NORMAL, run_id=start_run(db_session, "ashby", now=T0 + 6 * H), now=T0 + 6 * H
    )
    assert second.inserted == 0
    assert second.skipped == 0
    assert second.events == []
    assert (_count(db_session, EventRow), _count(db_session, Posting)) == counts
    assert _count(db_session, AtsBoard) == 1
    assert _count(db_session, Company) == 1


@pytest.mark.db
def test_run_fixture_event_shape(db_session: Session) -> None:
    result = ashby.run_fixture(db_session, NORMAL, run_id=start_run(db_session, "ashby"), now=T0)
    event = next(e for e in result.events if e.payload["posting_external_id"] == ID_SECURITY)
    assert event.source == "ashby"
    assert event.external_id == f"ashby:tell-fixture:{ID_SECURITY}"
    assert event.version == "2026-04-07T17:12:35.753000+00:00"
    assert event.occurred_at == datetime(2026, 4, 7, 17, 12, 35, 753000, tzinfo=UTC)
    assert event.url == f"https://jobs.ashbyhq.com/ramp/{ID_SECURITY}"
    assert event.payload["vendor"] == "ashby"
    assert event.payload["board_identifier"] == "tell-fixture"
    assert event.payload["title"] == "Security Engineer, Cloud"
    assert event.payload["remote"] is False
    assert event.payload["version_marker"] == "2026-04-07T17:12:35.753000+00:00"
    assert event.payload["extra"]["team"] == "Backend"
    assert event.company_id is not None
    board = db_session.scalars(select(AtsBoard)).one()
    assert board.last_polled_at == T0
    assert board.last_verified_at is None  # discovery owns it (ADR 0015)


@pytest.mark.db
def test_empty_board_fixture_emits_no_events(db_session: Session) -> None:
    result = ashby.run_fixture(db_session, EMPTY, run_id=start_run(db_session, "ashby"), now=T0)
    assert result.fetched == 0
    assert result.inserted == 0
    assert result.events == []
    assert _count(db_session, EventRow) == 0
    assert _count(db_session, Posting) == 0


@pytest.mark.db
def test_normal_then_updated_yields_one_job_updated(db_session: Session) -> None:
    ashby.run_fixture(db_session, NORMAL, run_id=start_run(db_session, "ashby"), now=T0)
    later = T0 + 6 * H
    result = ashby.run_fixture(
        db_session, UPDATED, run_id=start_run(db_session, "ashby", now=later), now=later
    )
    assert result.inserted == 1
    assert [e.type for e in result.events] == ["job_updated"]
    (updated,) = result.events
    assert updated.external_id == f"ashby:tell-fixture:{ID_AE}"
    assert updated.version == "2026-09-30T09:12:44.118000+00:00"
    assert _count(db_session, EventRow, type="job_opened") == 4
    assert _count(db_session, EventRow, type="job_updated") == 1
    posting = db_session.scalars(select(Posting).where(Posting.external_id == ID_AE)).one()
    # job_opened stays keyed on the first publishedAt seen.
    assert posting.posted_at == datetime(2026, 6, 8, 19, 17, 53, 481000, tzinfo=UTC)
    assert posting.version_marker == "2026-09-30T09:12:44.118000+00:00"


# --- HTTP ----------------------------------------------------------------------------


def test_board_url_quotes_and_rejects_empty() -> None:
    assert ashby.board_url("ramp") == LIST_URL
    assert ashby.board_url("a b/c") == f"{ashby.API_BASE}/a%20b%2Fc"
    with pytest.raises(ValueError, match="non-empty"):
        ashby.board_url("")


def test_fetch_board_exact_url_and_user_agent(
    respx_mock: respx.MockRouter, client: httpx.Client, budget: RateBudget
) -> None:
    route = respx_mock.get(FULL_URL).mock(
        return_value=httpx.Response(200, content=NORMAL.read_bytes())
    )
    board = ashby.fetch_board(client, BOARD, budget=budget)
    assert board is not None
    assert len(board.jobs) == 4
    request = route.calls.last.request
    assert str(request.url) == FULL_URL
    assert request.url.params["includeCompensation"] == "true"
    assert request.headers["User-Agent"] == "tell/0.1 (+https://github.com/karank2512/tell-ai)"
    assert request.headers["User-Agent"] == get_settings().tell_http_user_agent


def test_fetch_board_backs_off_on_429(
    respx_mock: respx.MockRouter, client: httpx.Client, budget: RateBudget, fake_time: FakeTime
) -> None:
    route = respx_mock.get(FULL_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, content=EMPTY.read_bytes()),
        ]
    )
    board = ashby.fetch_board(client, BOARD, budget=budget, sleep=fake_time.sleep)
    assert board is not None
    assert board.jobs == []
    assert route.call_count == 3
    # Backoff 1 s, then Retry-After 7 s; the rate budget never had to wait on top.
    assert fake_time.sleeps == [1.0, 7.0]


def test_fetch_board_gives_up_after_persistent_429(
    respx_mock: respx.MockRouter, client: httpx.Client, budget: RateBudget, fake_time: FakeTime
) -> None:
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RetryExhausted):
        ashby.fetch_board(client, BOARD, budget=budget, sleep=fake_time.sleep)
    assert fake_time.sleeps == [1.0, 2.0, 4.0, 8.0]


def test_fetch_board_404_returns_none_and_403_raises(
    respx_mock: respx.MockRouter, client: httpx.Client, budget: RateBudget
) -> None:
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(404))
    assert ashby.fetch_board(client, BOARD, budget=budget) is None
    respx_mock.get(f"{ashby.API_BASE}/locked?includeCompensation=true").mock(
        return_value=httpx.Response(403)
    )
    with pytest.raises(httpx.HTTPStatusError):
        ashby.fetch_board(client, "locked", budget=budget)


def test_fetch_board_skips_a_drifted_job(
    respx_mock: respx.MockRouter, client: httpx.Client, budget: RateBudget
) -> None:
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, content=DRIFT.read_bytes()))
    board = ashby.fetch_board(client, BOARD, budget=budget)
    assert board is not None
    assert len(board.jobs) == 3
    assert board.invalid_jobs == 1


def test_fetch_board_drifted_envelope_raises(
    respx_mock: respx.MockRouter, client: httpx.Client, budget: RateBudget
) -> None:
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
    with pytest.raises(pydantic.ValidationError, match="apiVersion"):
        ashby.fetch_board(client, BOARD, budget=budget)


def test_default_budget_uses_ats_rate_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELL_RATE_ATS_RPS", "3.5")
    get_settings.cache_clear()
    ashby._default_budget.cache_clear()
    try:
        assert ashby._default_budget().rps == 3.5
        assert ashby._default_budget() is ashby._default_budget()
    finally:
        ashby._default_budget.cache_clear()


def test_fetch_board_uses_default_budget(
    respx_mock: respx.MockRouter, client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired: list[int] = []

    class CountingBudget(RateBudget):
        def acquire(self) -> float:
            acquired.append(1)
            return 0.0

    monkeypatch.setattr(ashby, "_default_budget", lambda: CountingBudget(1.0))
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, content=EMPTY.read_bytes()))
    assert ashby.fetch_board(client, BOARD) is not None
    assert acquired == [1]


# --- ingest_board -------------------------------------------------------------------------


def _board(session: Session, identifier: str = BOARD, vendor: str = "ashby") -> AtsBoard:
    company = Company(domain=f"{identifier}.test", name=identifier.title())
    session.add(company)
    session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=vendor,
        identifier=identifier,
        discovered_from=f"https://jobs.ashbyhq.com/{identifier}",
        confidence=Decimal("0.95"),
    )
    session.add(board)
    session.flush()
    return board


def _respond(*bodies: int | dict[str, Any] | bytes) -> list[httpx.Response]:
    responses: list[httpx.Response] = []
    for body in bodies:
        if isinstance(body, int):
            responses.append(httpx.Response(body))
        elif isinstance(body, bytes):
            responses.append(httpx.Response(200, content=body))
        else:
            responses.append(httpx.Response(200, json=body))
    return responses


@pytest.mark.db
def test_ingest_board_success(
    respx_mock: respx.MockRouter, db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    board = _board(db_session)
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, content=NORMAL.read_bytes()))
    run_id = start_run(db_session, "ashby", now=T0)
    result = ashby.ingest_board(
        db_session, board, run_id=run_id, now=T0, client=client, budget=budget
    )
    assert not client.is_closed
    assert result.fetched == 4
    assert result.inserted == 4
    assert {e.external_id for e in result.events} == {
        f"ashby:ramp:{job_id}" for job_id in (ID_SECURITY, ID_ARCHITECT, ID_AE, ID_IT)
    }
    assert all(e.company_id == board.company_id for e in result.events)
    assert board.last_polled_at == T0
    assert board.last_verified_at is None
    assert board.status == "active"


@pytest.mark.db
def test_ingest_board_404_records_the_streak_and_nothing_else(
    respx_mock: respx.MockRouter, db_session: Session, budget: RateBudget
) -> None:
    board = _board(db_session)
    respx_mock.get(FULL_URL).mock(side_effect=_respond(404, 404))
    run_id = start_run(db_session, "ashby", now=T0)

    first = ashby.ingest_board(db_session, board, run_id=run_id, now=T0, budget=budget)
    assert (first.fetched, first.inserted, first.skipped, first.events) == (0, 0, 0, [])
    assert (board.status, board.not_found_count, board.first_not_found_at) == ("active", 1, T0)
    assert board.last_polled_at is None
    assert board.last_verified_at is None

    ashby.ingest_board(db_session, board, run_id=run_id, now=T0 + 6 * H, budget=budget)
    assert (board.status, board.not_found_count) == ("active", 2)  # only 6h apart
    assert board.last_polled_at is None
    assert _count(db_session, EventRow) == 0


@pytest.mark.db
def test_ingest_board_success_between_404s_resets_the_streak(
    respx_mock: respx.MockRouter, db_session: Session, budget: RateBudget
) -> None:
    board = _board(db_session)
    respx_mock.get(FULL_URL).mock(side_effect=_respond(404, NORMAL.read_bytes(), 404))
    run_id = start_run(db_session, "ashby", now=T0)
    ashby.ingest_board(db_session, board, run_id=run_id, now=T0, budget=budget)
    result = ashby.ingest_board(db_session, board, run_id=run_id, now=T0 + 6 * H, budget=budget)
    assert result.inserted == 4
    assert (board.not_found_count, board.last_polled_at) == (0, T0 + 6 * H)
    ashby.ingest_board(db_session, board, run_id=run_id, now=T0 + 18 * H, budget=budget)
    assert (board.status, board.not_found_count) == ("active", 1)


@pytest.mark.db
def test_a_404_between_polls_does_not_count_as_an_absence(
    respx_mock: respx.MockRouter, db_session: Session, budget: RateBudget
) -> None:
    board = _board(db_session)
    without_ae = _load(NORMAL)
    without_ae["jobs"] = [job for job in without_ae["jobs"] if job["id"] != ID_AE]
    respx_mock.get(FULL_URL).mock(
        side_effect=_respond(NORMAL.read_bytes(), 404, without_ae, without_ae)
    )
    run_id = start_run(db_session, "ashby", now=T0)
    ashby.ingest_board(db_session, board, run_id=run_id, now=T0, budget=budget)
    ashby.ingest_board(db_session, board, run_id=run_id, now=T0 + 12 * H, budget=budget)

    # First real absence: the 404 at +12h is not a poll that missed the posting.
    third = ashby.ingest_board(db_session, board, run_id=run_id, now=T0 + 24 * H, budget=budget)
    assert third.events == []
    assert board.last_polled_at == T0 + 24 * H

    # Second consecutive real absence, 12h later: now it closes.
    fourth = ashby.ingest_board(db_session, board, run_id=run_id, now=T0 + 36 * H, budget=budget)
    assert [(e.type, e.payload["posting_external_id"]) for e in fourth.events] == [
        ("job_closed", ID_AE)
    ]
    assert board.status == "active"


@pytest.mark.db
def test_ingest_board_builds_its_own_client(
    respx_mock: respx.MockRouter, db_session: Session, budget: RateBudget
) -> None:
    board = _board(db_session)
    route = respx_mock.get(FULL_URL).mock(
        return_value=httpx.Response(200, content=EMPTY.read_bytes())
    )
    run_id = start_run(db_session, "ashby", now=T0)
    result = ashby.ingest_board(db_session, board, run_id=run_id, now=T0, budget=budget)
    assert result.fetched == 0
    assert route.calls.last.request.headers["User-Agent"].startswith("tell/0.1")


@pytest.mark.db
def test_ingest_board_rejects_other_vendors(db_session: Session) -> None:
    board = _board(db_session, identifier="acme", vendor="lever")
    with pytest.raises(ValueError, match="lever"):
        ashby.ingest_board(db_session, board, run_id="r", now=T0)


@pytest.mark.db
def test_ingest_board_one_invalid_job_is_counted_not_fatal(
    respx_mock: respx.MockRouter, db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    board = _board(db_session)
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, content=DRIFT.read_bytes()))
    run_id = start_run(db_session, "ashby", now=T0)
    result = ashby.ingest_board(
        db_session, board, run_id=run_id, now=T0, client=client, budget=budget
    )
    assert isinstance(result, ashby.BoardRunResult)
    assert result.fetched == 4
    assert result.inserted == 3
    assert result.errors == 1
    assert {e.external_id for e in result.events} == {
        f"ashby:ramp:{job_id}" for job_id in (ID_SECURITY, ID_AE, ID_IT)
    }
    assert board.last_polled_at == T0


@pytest.mark.db
def test_ingest_board_nulls_board_is_polled(
    respx_mock: respx.MockRouter, db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    board = _board(db_session)
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, content=NULLS.read_bytes()))
    run_id = start_run(db_session, "ashby", now=T0)
    result = ashby.ingest_board(
        db_session, board, run_id=run_id, now=T0, client=client, budget=budget
    )
    assert (result.fetched, result.inserted, result.errors) == (3, 3, 0)
    remote = db_session.scalars(
        select(Posting.remote).where(Posting.board_id == board.id).order_by(Posting.external_id)
    ).all()
    assert remote == [True, False, None]  # 1400d1a6, 14af59ec, 59ed9c30


@pytest.mark.db
def test_ingest_board_with_given_client_and_drift_propagates(
    respx_mock: respx.MockRouter, db_session: Session, client: httpx.Client, budget: RateBudget
) -> None:
    board = _board(db_session)
    data = _load(NORMAL)
    for job in data["jobs"]:
        del job["publishedAt"]
    respx_mock.get(FULL_URL).mock(return_value=httpx.Response(200, json=data))
    with pytest.raises(pydantic.ValidationError):
        ashby.ingest_board(db_session, board, run_id="r", now=T0, client=client, budget=budget)
    assert not client.is_closed
    assert board.last_polled_at is None


# --- ADR 0015: the shared 404 / dead-board rule ------------------------------------------


@pytest.fixture
def liveness_poll(
    respx_mock: respx.MockRouter, db_session: Session, budget: RateBudget
) -> tuple[AtsBoard, liveness.Poll]:
    bodies = {"present": NORMAL.read_bytes(), "absent": EMPTY.read_bytes()}
    route = respx_mock.get(FULL_URL)
    board = _board(db_session)

    def poll(now: datetime, outcome: liveness.Outcome) -> RunResult:
        if outcome == "404":
            route.mock(return_value=httpx.Response(404))
        else:
            route.mock(return_value=httpx.Response(200, content=bodies[outcome]))
        return ashby.ingest_board(db_session, board, run_id=f"r-{now:%d%H}", now=now, budget=budget)

    return board, poll


@pytest.mark.db
def test_liveness_404_between_polls_never_closes(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.a_404_between_polls_never_closes(db_session, *liveness_poll, T0)


@pytest.mark.db
def test_liveness_two_404s_under_12h_stay_active(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.two_404s_under_12h_stay_active(db_session, *liveness_poll, T0)


@pytest.mark.db
def test_liveness_two_404s_12h_apart_kill_once(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.two_404s_12h_apart_kill_once(db_session, *liveness_poll, T0)


@pytest.mark.db
def test_liveness_a_later_200_revives_and_resets(
    db_session: Session, liveness_poll: tuple[AtsBoard, liveness.Poll]
) -> None:
    liveness.a_later_200_revives_and_resets(db_session, *liveness_poll, T0)
