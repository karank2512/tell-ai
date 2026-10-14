"""SmartRecruiters Posting API collector (spec section 3.4), against recorded fixtures.

No network: every httpx call goes through ``respx``; sleeps are injected.
"""

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

from tell.collectors import smartrecruiters as sr
from tell.collectors.base import RunResult, content_hash, start_run
from tell.config import get_settings
from tell.http import RateBudget, make_client
from tell.models import AtsBoard, EventRow, Posting
from tests.collectors import _board_liveness as liveness

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "smartrecruiters"
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)
UA = "tell/0.1 (+https://github.com/karank2512/tell-ai)"
LIST_URL = "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"
DETAIL_ID = "744000153523769"
DETAIL_TEXT = (
    "Shape the future of smart manufacturing. Your Mission We are looking for an "
    "Automation & Robotics Engineer who is enthusiastic about robotics, automation, and "
    "modern manufacturing technologies. Support and improve existing robotic and cobot "
    "applications. Participate in commissioning of new automation solutions."
)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _json(name: str) -> Any:
    return json.loads(_text(name))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def budget() -> RateBudget:
    # A huge bucket never waits, so tests do not depend on the wall clock.
    return RateBudget(1000.0, burst=1000)


@pytest.fixture
def mocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


def _page(router: respx.MockRouter, name: str, *, offset: int = 0) -> respx.Route:
    return router.get(LIST_URL, params={"limit": "100", "offset": str(offset)}).respond(
        200, json=_json(name)
    )


def _board(session: Session, identifier: str = "BoschGroup") -> AtsBoard:
    board = AtsBoard(vendor="smartrecruiters", identifier=identifier, confidence=Decimal("0.95"))
    session.add(board)
    session.flush()
    return board


def _count(session: Session, type_: str | None = None) -> int:
    stmt = select(func.count()).select_from(EventRow)
    if type_ is not None:
        stmt = stmt.where(EventRow.type == type_)
    return session.execute(stmt).scalar_one()


# --- contract -----------------------------------------------------------------------------


def test_collector_registration() -> None:
    spec = sr.COLLECTOR
    assert spec.source == "smartrecruiters"
    assert spec.kind == "ats"
    assert spec.response_model is sr.SmartRecruitersPostingList
    assert spec.fixture_dir == FIXTURES
    assert spec.drift_missing_field == "releasedDate"


@pytest.mark.parametrize(
    "name",
    [
        "normal.json",
        "normal_nulls.json",
        "updated.json",
        "empty_board.json",
        "paged_1.json",
        "paged_2.json",
    ],
)
def test_list_fixtures_validate(name: str) -> None:
    sr.SmartRecruitersPostingList.model_validate_json(_text(name))


def test_drift_fixture_fails_naming_released_date() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        sr.SmartRecruitersPostingList.model_validate_json(_text("drift.json"))
    assert "releasedDate" in str(excinfo.value)


def test_fixtures_are_small_and_hold_no_creator() -> None:
    for path in FIXTURES.iterdir():
        assert path.stat().st_size < 50_000, path.name
        assert "creator" not in path.read_text(encoding="utf-8"), path.name


# --- parse --------------------------------------------------------------------------------


def test_parse_normal_exact_values() -> None:
    postings = sr.parse(_text("normal.json"))
    assert [p.external_id for p in postings] == [
        "744000153546489",
        "744000153523769",
        "744000153532695",
    ]
    first = postings[0]
    assert first.title == "Digital Marketing Coordinator"
    assert first.location == "Lincolnshire, IL, United States"
    assert first.remote is False
    assert first.posted_at == datetime(2026, 10, 5, 15, 27, 17, 13000, tzinfo=UTC)
    assert first.version == "2026-10-05T15:27:17.013000+00:00"
    assert first.url == "https://jobs.smartrecruiters.com/BoschGroup/744000153546489"
    # department is empty on this board, so function.label stands in.
    assert first.department == "Manufacturing"
    assert first.content_hash is None
    assert first.extra == {
        "ref": "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings/744000153546489",
        "ref_number": "REF286341H",
        "city": "Lincolnshire",
        "region": "IL",
        "country": "us",
        "hybrid": True,
        "function": "Manufacturing",
        "type_of_employment": "Full-time",
        "experience_level": "Associate",
        "industry": "Mechanical Or Industrial Engineering",
    }
    second = postings[1]
    assert second.title == "Automation & Robotics Engineer"
    assert second.location == "Blaj, Alba, Romania"
    assert second.department == "Engineering"
    assert second.version == "2026-10-05T14:18:33.038000+00:00"
    third = postings[2]
    assert third.title == "Verfahrenstechniker 100% (w/m/div.) -  REF297511W"
    assert third.location == "Sankt Niklaus, VS, Switzerland"


def test_parse_empty_board() -> None:
    assert sr.parse(_json("empty_board.json")) == []


def test_parse_accepts_model_mapping_and_bytes() -> None:
    model = sr.SmartRecruitersPostingList.model_validate_json(_text("normal.json"))
    by_model = sr.parse(model)
    assert by_model == sr.parse(_json("normal.json"))
    assert by_model == sr.parse(_text("normal.json").encode())


def test_updated_fixture_changes_exactly_one_version() -> None:
    normal = {p.external_id: p.version for p in sr.parse(_text("normal.json"))}
    updated = {p.external_id: p.version for p in sr.parse(_text("updated.json"))}
    assert normal.keys() == updated.keys()
    changed = [key for key in normal if normal[key] != updated[key]]
    assert changed == [DETAIL_ID]


def _item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "1",
        "name": "  Platform Engineer  ",
        "releasedDate": "2026-10-01T08:00:00Z",
        "location": {"country": "de", "city": "Berlin"},
        "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/1",
    }
    item.update(overrides)
    return item


def _listing(*items: dict[str, Any]) -> dict[str, Any]:
    return {"offset": 0, "limit": 100, "totalFound": len(items), "content": list(items)}


def test_parse_remote_flag_and_location_fallbacks() -> None:
    explicit = _item(location={"country": "us", "city": "Austin", "remote": True})
    by_text = _item(id="2", location={"country": "us", "fullLocation": "Remote, United States"})
    by_title = _item(id="3", name="Remote Data Engineer")
    unknown = _item(id="4", location={"country": "de", "city": "Berlin", "region": "BE"})
    bare = _item(id="5", location={"country": ""})
    postings = sr.parse(_listing(explicit, by_text, by_title, unknown, bare), identifier="Acme")
    assert [p.remote for p in postings] == [True, True, True, None, None]
    assert [p.location for p in postings] == [
        "Austin, US",
        "Remote, United States",
        "Berlin, DE",
        "Berlin, BE, DE",
        None,
    ]
    assert postings[0].title == "Platform Engineer"


def test_nulls_fixture_parses_without_region() -> None:
    """Real 2026-10-14 postings (``BukuWarung``) whose ``location`` has no ``region``."""
    listing = sr.SmartRecruitersPostingList.model_validate_json(_text("normal_nulls.json"))
    assert all(item.location.region is None for item in listing.content)
    postings = sr.parse(listing, identifier="bukuwarung")
    assert [p.location for p in postings] == [
        "Jakarta, Indonesia",  # fullLocation "Jakarta, , Indonesia"
        "Jakarta, Indonesia",
        "Jakarta Pusat, Indonesia",
    ]
    assert [p.remote for p in postings] == [False, False, False]
    assert [p.department for p in postings] == ["Marketing", "Analyst", "Marketing"]
    assert "region" not in postings[0].extra


def test_validate_page_skips_one_invalid_item_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="tell.collectors.smartrecruiters"):
        page = sr.validate_page(_text("drift.json"), identifier="BoschGroup")
    assert len(page.content) == 2
    assert page.invalid_jobs == 1
    assert page.total_found == 3
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    message = record.getMessage()
    assert "BoschGroup" in message
    assert "content.0" in message
    assert "releasedDate (missing)" in message


def test_validate_page_fails_loudly_when_every_item_fails() -> None:
    doc = _json("normal.json")
    for item in doc["content"]:
        del item["releasedDate"]
    with pytest.raises(pydantic.ValidationError, match=r"content\.0\.releasedDate"):
        sr.validate_page(json.dumps(doc), identifier="BoschGroup")
    with pytest.raises(pydantic.ValidationError, match="totalFound"):
        sr.validate_page(json.dumps({"offset": 0, "limit": 100, "content": []}), identifier="x")
    doc = _json("normal.json")
    doc["content"].append(42)
    assert sr.validate_page(json.dumps(doc), identifier="BoschGroup").invalid_jobs == 1


def test_parse_department_label_wins_over_function() -> None:
    item = _item(department={"id": "d1", "label": "R&D"}, function={"label": "Engineering"})
    blank = _item(id="2", department={"label": "  "}, function={"label": "Sales"})
    postings = sr.parse(_listing(item, blank), identifier="Acme")
    assert [p.department for p in postings] == ["R&D", "Sales"]
    assert postings[0].extra["function"] == "Engineering"


def test_parse_url_sources() -> None:
    # No company block: the board identifier builds the URL.
    built = sr.parse(_listing(_item()), identifier="Acme")[0]
    assert built.url == "https://jobs.smartrecruiters.com/Acme/1"
    # A postingUrl in the list item is used as-is.
    given = _item(postingUrl="https://jobs.smartrecruiters.com/Acme/1-platform-engineer")
    assert sr.parse(_listing(given))[0].url.endswith("/1-platform-engineer")
    with pytest.raises(ValueError, match="no company identifier"):
        sr.parse(_listing(_item()))


def test_parse_naive_released_date_is_utc() -> None:
    posting = sr.parse(_listing(_item(releasedDate="2026-10-01T08:00:00")), identifier="Acme")[0]
    assert posting.posted_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    assert posting.version == "2026-10-01T08:00:00+00:00"


def test_parse_hashes_descriptions_and_drops_them() -> None:
    postings = sr.parse(_text("normal.json"), descriptions={DETAIL_ID: DETAIL_TEXT})
    by_id = {p.external_id: p for p in postings}
    assert by_id[DETAIL_ID].content_hash == content_hash(DETAIL_TEXT)
    assert by_id[DETAIL_ID].description_plain is None


def test_detail_description_text() -> None:
    detail = sr.SmartRecruitersPostingDetail.model_validate_json(_text("detail.json"))
    assert detail.id == DETAIL_ID
    assert sr.description_text(detail) == DETAIL_TEXT
    no_section = sr.SmartRecruitersPostingDetail.model_validate(
        {"id": "1", "jobAd": {"sections": {}}}
    )
    assert sr.description_text(no_section) is None


def test_html_to_text() -> None:
    assert sr.html_to_text("<p>A&amp;B</p><ul><li>x</li><li>y</li></ul>z<br>w") == "A&B x y z w"
    assert sr.html_to_text("") == ""


# --- fetch --------------------------------------------------------------------------------


def test_fetch_board_exact_url_and_user_agent(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    route = _page(mocked, "normal.json")
    listing = sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps)
    assert route.call_count == 1
    request = route.calls.last.request
    assert str(request.url) == f"{LIST_URL}?limit=100&offset=0"
    assert request.headers["User-Agent"] == UA
    assert listing.total_found == 3
    assert listing.content[0].id == "744000153546489"
    assert sleeps.calls == []


def test_fetch_board_paginates_on_total_found(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    first = _page(mocked, "paged_1.json", offset=0)
    second = _page(mocked, "paged_2.json", offset=100)
    listing = sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps)
    assert first.call_count == 1
    assert second.call_count == 1
    assert str(second.calls.last.request.url) == f"{LIST_URL}?limit=100&offset=100"
    assert listing.total_found == 150
    assert [item.id for item in listing.content] == [
        "744000153539839",
        "744000153538751",
        "744000153538518",
        "744000153526329",
        "744000153521190",
    ]


def test_fetch_board_counts_invalid_items_across_pages(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    first = _json("paged_1.json")
    del first["content"][0]["location"]
    second = _json("paged_2.json")
    second["content"][1]["name"] = None
    mocked.get(LIST_URL, params={"limit": "100", "offset": "0"}).respond(200, json=first)
    mocked.get(LIST_URL, params={"limit": "100", "offset": "100"}).respond(200, json=second)
    listing = sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps)
    assert len(listing.content) == 3
    assert listing.invalid_jobs == 2
    assert listing.total_found == 150


def test_fetch_board_stops_on_empty_page_and_max_pages(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    lying = {"offset": 0, "limit": 100, "totalFound": 500, "content": []}
    route = mocked.get(LIST_URL).respond(200, json=lying)
    assert sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps).content == []
    assert route.call_count == 1

    full = _json("paged_1.json")
    full["totalFound"] = 10_000
    route.respond(200, json=full)
    listing = sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps, max_pages=2)
    assert route.call_count == 3
    assert len(listing.content) == 6


def test_fetch_board_backs_off_on_429(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    route = mocked.get(LIST_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=_json("normal.json"))]
    )
    listing = sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps)
    assert route.call_count == 2
    assert sleeps.calls == [1.0]
    assert len(listing.content) == 3


def test_fetch_board_404_and_other_4xx(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    route = mocked.get(LIST_URL).respond(404)
    with pytest.raises(sr.BoardNotFoundError) as excinfo:
        sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps)
    assert excinfo.value.identifier == "BoschGroup"
    assert excinfo.value.url == f"{LIST_URL}?limit=100&offset=0"
    route.respond(403)
    with pytest.raises(httpx.HTTPStatusError):
        sr.fetch_board(client, "BoschGroup", budget=budget, sleep=sleeps)
    assert sleeps.calls == []


def test_fetch_posting_detail(
    mocked: respx.MockRouter, client: httpx.Client, budget: RateBudget, sleeps: Sleeps
) -> None:
    route = mocked.get(f"{LIST_URL}/{DETAIL_ID}").respond(200, json=_json("detail.json"))
    gone = mocked.get(f"{LIST_URL}/999").respond(404)
    detail = sr.fetch_posting_detail(client, "BoschGroup", DETAIL_ID, budget=budget, sleep=sleeps)
    assert detail is not None
    assert detail.posting_url == (
        "https://jobs.smartrecruiters.com/BoschGroup/744000153523769-automation-robotics-engineer"
    )
    assert route.calls.last.request.headers["User-Agent"] == UA
    assert sr.fetch_posting_detail(client, "BoschGroup", "999", budget=budget, sleep=sleeps) is None
    assert gone.call_count == 1


def test_default_budget_is_shared_and_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "_budget", None)
    monkeypatch.setenv("TELL_RATE_ATS_RPS", "3")
    get_settings.cache_clear()
    bucket = sr.default_budget()
    assert bucket.rps == 3.0
    assert bucket.burst == 3
    assert sr.default_budget() is bucket


def test_fetch_board_uses_default_budget(
    mocked: respx.MockRouter, client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = RateBudget(1000.0, burst=1000)
    monkeypatch.setattr(sr, "_budget", bucket)
    _page(mocked, "normal.json")
    mocked.get(f"{LIST_URL}/1").respond(404)
    sr.fetch_board(client, "BoschGroup")
    sr.fetch_posting_detail(client, "BoschGroup", "1")
    assert bucket.wait_time() == 0.0


# --- database: run_fixture and ingest_board ------------------------------------------------


@pytest.mark.db
def test_run_fixture_twice_inserts_nothing_the_second_time(db_session: Session) -> None:
    normal = FIXTURES / "normal.json"
    first = sr.run_fixture(
        db_session, normal, run_id=start_run(db_session, "smartrecruiters", now=NOW), now=NOW
    )
    assert first.fetched == 3
    assert first.inserted == 3
    assert {e.type for e in first.events} == {"job_opened"}
    assert first.events[0].external_id == "smartrecruiters:tell-fixture:744000153546489"
    assert first.events[0].url == "https://jobs.smartrecruiters.com/BoschGroup/744000153546489"
    postings_before = db_session.execute(select(func.count()).select_from(Posting)).scalar_one()
    events_before = _count(db_session)

    second = sr.run_fixture(
        db_session, normal, run_id=start_run(db_session, "smartrecruiters", now=LATER), now=LATER
    )
    assert second.inserted == 0
    assert second.events == []
    assert db_session.execute(select(func.count()).select_from(Posting)).scalar_one() == (
        postings_before
    )
    assert _count(db_session) == events_before
    boards = db_session.scalars(select(AtsBoard).where(AtsBoard.vendor == "smartrecruiters"))
    assert len(list(boards)) == 1


@pytest.mark.db
def test_run_fixture_empty_board_yields_nothing(db_session: Session) -> None:
    result = sr.run_fixture(
        db_session,
        FIXTURES / "empty_board.json",
        run_id=start_run(db_session, "smartrecruiters", now=NOW),
        now=NOW,
    )
    assert (result.fetched, result.inserted, result.skipped, result.events) == (0, 0, 0, [])
    assert _count(db_session) == 0


@pytest.mark.db
def test_normal_then_updated_yields_one_job_updated(db_session: Session) -> None:
    sr.run_fixture(
        db_session,
        FIXTURES / "normal.json",
        run_id=start_run(db_session, "smartrecruiters", now=NOW),
        now=NOW,
    )
    result = sr.run_fixture(
        db_session,
        FIXTURES / "updated.json",
        run_id=start_run(db_session, "smartrecruiters", now=LATER),
        now=LATER,
    )
    assert result.inserted == 1
    assert [(e.type, e.version) for e in result.events] == [
        ("job_updated", "2026-10-06T09:12:44.501000+00:00")
    ]
    assert result.events[0].external_id == f"smartrecruiters:tell-fixture:{DETAIL_ID}"
    assert _count(db_session, "job_opened") == 3
    assert _count(db_session, "job_updated") == 1


@pytest.mark.db
def test_ingest_board_fetches_details_only_for_new_ids(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget, sleeps: Sleeps
) -> None:
    board = _board(db_session)
    _page(mocked, "normal.json")
    detail = mocked.get(f"{LIST_URL}/{DETAIL_ID}").respond(200, json=_json("detail.json"))
    gone = mocked.get(f"{LIST_URL}/744000153546489").respond(404)
    flaky = mocked.get(f"{LIST_URL}/744000153532695").respond(503)

    first = sr.ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget, sleep=sleeps)
    assert (first.fetched, first.inserted, first.skipped) == (3, 3, 0)
    assert {e.type for e in first.events} == {"job_opened"}
    assert first.events[0].external_id == "smartrecruiters:BoschGroup:744000153546489"
    assert (detail.call_count, gone.call_count, flaky.call_count) == (1, 1, 5)
    assert sleeps.calls == [1.0, 2.0, 4.0, 8.0]
    assert board.last_verified_at is None  # discovery owns it (ADR 0015)
    assert board.last_polled_at == NOW
    rows = db_session.execute(
        select(Posting.external_id, Posting.content_hash).where(Posting.board_id == board.id)
    )
    hashes = {row.external_id: row.content_hash for row in rows}
    assert hashes == {
        "744000153546489": None,
        DETAIL_ID: content_hash(DETAIL_TEXT),
        "744000153532695": None,
    }

    second = sr.ingest_board(db_session, board, run_id="r2", now=LATER, budget=budget, sleep=sleeps)
    assert (second.fetched, second.inserted, second.skipped, second.events) == (3, 0, 0, [])
    assert (detail.call_count, gone.call_count, flaky.call_count) == (1, 1, 5)
    posting = db_session.scalars(select(Posting).where(Posting.external_id == DETAIL_ID)).one()
    assert posting.content_hash == content_hash(DETAIL_TEXT)
    assert board.last_polled_at == LATER
    assert board.last_verified_at is None


@pytest.mark.db
def test_ingest_board_skips_and_counts_one_invalid_item(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget, sleeps: Sleeps
) -> None:
    board = _board(db_session)
    mocked.get(LIST_URL, params={"limit": "100", "offset": "0"}).respond(
        200, json=_json("drift.json")
    )
    mocked.get(url__startswith=f"{LIST_URL}/").respond(404)
    result = sr.ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget, sleep=sleeps)
    assert isinstance(result, sr.BoardRunResult)
    assert (result.fetched, result.inserted, result.errors) == (3, 2, 1)
    assert board.last_polled_at == NOW


@pytest.mark.db
def test_ingest_board_owns_its_client_when_none_given(
    db_session: Session, mocked: respx.MockRouter, budget: RateBudget, sleeps: Sleeps
) -> None:
    board = _board(db_session)
    route = _page(mocked, "empty_board.json")
    result = sr.ingest_board(db_session, board, run_id="r1", now=NOW, budget=budget, sleep=sleeps)
    assert (result.fetched, result.inserted, result.events) == (0, 0, [])
    assert route.calls.last.request.headers["User-Agent"] == UA
    assert board.status == "active"


@pytest.mark.db
def test_ingest_board_marks_dead_after_two_404s_12h_apart(
    db_session: Session,
    mocked: respx.MockRouter,
    client: httpx.Client,
    budget: RateBudget,
    sleeps: Sleeps,
) -> None:
    board = _board(db_session)
    board.last_verified_at = NOW - timedelta(days=1)
    route = mocked.get(LIST_URL).respond(404)

    first = sr.ingest_board(
        db_session, board, run_id="r1", now=NOW, client=client, budget=budget, sleep=sleeps
    )
    assert (first.fetched, first.inserted, first.events) == (0, 0, [])
    assert (board.status, board.not_found_count, board.first_not_found_at) == ("active", 1, NOW)
    assert board.last_polled_at is None

    sr.ingest_board(
        db_session, board, run_id="r2", now=LATER, client=client, budget=budget, sleep=sleeps
    )
    assert (board.status, board.not_found_count) == ("active", 2)  # only 6h apart
    dead_at = NOW + timedelta(hours=12)
    sr.ingest_board(
        db_session, board, run_id="r3", now=dead_at, client=client, budget=budget, sleep=sleeps
    )
    assert board.status == "dead"
    assert route.call_count == 3
    assert sleeps.calls == []
    assert _count(db_session) == 0

    # The board comes back: it is revived; last_verified_at stays discovery's.
    route.respond(200, json=_json("normal.json"))
    mocked.get(url__startswith=f"{LIST_URL}/").respond(404)
    later = dead_at + timedelta(hours=6)
    revived = sr.ingest_board(
        db_session, board, run_id="r4", now=later, client=client, budget=budget, sleep=sleeps
    )
    assert revived.inserted == 3
    assert (board.status, board.not_found_count) == ("active", 0)
    polled: datetime | None = board.last_polled_at  # widen: mypy narrowed it to None above
    assert polled == later
    assert board.last_verified_at == NOW - timedelta(days=1)


@pytest.mark.db
def test_a_404_after_a_success_is_only_the_first(
    db_session: Session,
    mocked: respx.MockRouter,
    client: httpx.Client,
    budget: RateBudget,
    sleeps: Sleeps,
) -> None:
    board = _board(db_session)
    route = _page(mocked, "empty_board.json")
    sr.ingest_board(
        db_session, board, run_id="r1", now=NOW, client=client, budget=budget, sleep=sleeps
    )
    route.respond(404)
    sr.ingest_board(
        db_session, board, run_id="r2", now=LATER, client=client, budget=budget, sleep=sleeps
    )
    assert board.status == "active"


@pytest.mark.db
def test_never_verified_board_dies_on_second_404(
    db_session: Session,
    mocked: respx.MockRouter,
    client: httpx.Client,
    budget: RateBudget,
    sleeps: Sleeps,
) -> None:
    board = AtsBoard(vendor="smartrecruiters", identifier="Gone", confidence=Decimal("0.60"))
    mocked.get("https://api.smartrecruiters.com/v1/companies/Gone/postings").respond(404)
    for run_id, now in (("r1", NOW), ("r2", NOW + timedelta(hours=12))):
        sr.ingest_board(
            db_session, board, run_id=run_id, now=now, client=client, budget=budget, sleep=sleeps
        )
    assert board.status == "dead"
    assert board.id is not None  # a transient board is persisted by the 404 bookkeeping


# --- ADR 0015: the shared 404 / dead-board rule ------------------------------------------


@pytest.fixture
def liveness_poll(
    db_session: Session,
    mocked: respx.MockRouter,
    client: httpx.Client,
    budget: RateBudget,
    sleeps: Sleeps,
) -> tuple[AtsBoard, liveness.Poll]:
    bodies = {"present": _json("normal.json"), "absent": _json("empty_board.json")}
    route = mocked.get(LIST_URL)
    mocked.get(url__startswith=f"{LIST_URL}/").respond(404)  # details: hash unknown
    board = _board(db_session)

    def poll(now: datetime, outcome: liveness.Outcome) -> RunResult:
        if outcome == "404":
            route.respond(404)
        else:
            route.respond(200, json=bodies[outcome])
        return sr.ingest_board(
            db_session,
            board,
            run_id=f"r-{now:%d%H}",
            now=now,
            client=client,
            budget=budget,
            sleep=sleeps,
        )

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
