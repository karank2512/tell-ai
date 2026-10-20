"""Quarterly Form D data sets: URL, contact guard, download cache, window filter, idempotency.

Every HTTP call goes through ``respx``; nothing here reaches sec.gov. The data-set ZIP is
built in the test from the synthetic ``tests/fixtures/edgar/quarterly/*.tsv`` (two D, one
D/A filed 2026-01-12, 2026-02-03 and 2026-03-16).
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.backtest.ops import freshness_n
from tell.collectors.base import Event, start_run, write_events
from tell.collectors.edgar_backfill import (
    DEFAULT_SINCE_DAYS,
    FORMD_DATASET_URL,
    FORMD_DATASET_URLS,
    SOURCE,
    DatasetNotPublishedError,
    Quarter,
    SecContactMissingError,
    backfill_formd,
    dataset_url,
    dataset_urls,
    download_dataset,
    parse_quarter,
    require_sec_contact,
)
from tell.collectors.edgar_formd import FORM_D_FILED
from tell.config import get_settings
from tell.http import RateBudget
from tell.models import Company, EventRow
from tell.ops.runlog import NON_PIPELINE_SOURCES
from tell.publish.health import freshness_p95_h, health_sources

QUARTERLY = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "quarterly"
NOW = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
REAL_AGENT = "tell/0.1 (backfill-test@tell.invalid)"
Q1 = Quarter(2026, 1)


def build_zip(path: Path, *, folder: str = "2026q1_d") -> Path:
    """A data-set ZIP shaped like the SEC's: one folder holding the TSV files."""
    with zipfile.ZipFile(path, "w") as archive:
        for tsv in sorted(QUARTERLY.glob("*.tsv")):
            archive.write(tsv, f"{folder}/{tsv.name}")
    return path


def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for tsv in sorted(QUARTERLY.glob("*.tsv")):
            archive.write(tsv, f"2026q1_d/{tsv.name}")
    return buffer.getvalue()


def _budget() -> RateBudget:
    return RateBudget(5.0, sleep=lambda _seconds: None)


def _real_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", REAL_AGENT)
    get_settings.cache_clear()


def _count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(EventRow)).scalar_one()


# --- URL and quarters ----------------------------------------------------------------------------


def test_dataset_url_pattern() -> None:
    # As linked from sec.gov/dera/data/form-d on 2026-10-13: 2026 Q2 on under
    # datastandardsinnovation/, 2026 Q1 and earlier under structureddata/.
    assert FORMD_DATASET_URLS[0] == FORMD_DATASET_URL
    assert all(t.startswith("https://www.sec.gov/files/") for t in FORMD_DATASET_URLS)
    new = "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q2_d.zip"
    old = "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2026q2_d.zip"
    assert dataset_urls(2026, 2) == (new, old)
    assert dataset_url(2026, 2) == new
    assert dataset_urls(2026, 1) == (
        "https://www.sec.gov/files/structureddata/data/form-d-data-sets/2026q1_d.zip",
        "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q1_d.zip",
    )
    assert Q1.url == dataset_url(2026, 1) == Q1.urls[0]
    assert (Q1.label, Q1.filename) == ("2026Q1", "2026q1_d.zip")
    with pytest.raises(ValueError, match="quarter must be 1-4"):
        dataset_url(2026, 5)


@pytest.mark.parametrize("text", ["2026Q2", "2026q2", "2026-Q2", " 2026 q2 "])
def test_parse_quarter_spellings(text: str) -> None:
    assert parse_quarter(text) == Quarter(2026, 2)


@pytest.mark.parametrize("text", ["2026Q5", "26Q1", "Q2 2026", "2026", ""])
def test_parse_quarter_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="not a quarter"):
        parse_quarter(text)


def test_quarters_sort_chronologically() -> None:
    assert sorted([Quarter(2026, 1), Quarter(2025, 4), Quarter(2026, 2)]) == [
        Quarter(2025, 4),
        Quarter(2026, 1),
        Quarter(2026, 2),
    ]


# --- the contact guard ---------------------------------------------------------------------------


def test_placeholder_user_agent_is_refused() -> None:
    assert "example.invalid" in get_settings().sec_user_agent  # the shipped default
    with pytest.raises(SecContactMissingError, match="SEC_USER_AGENT is still the placeholder"):
        require_sec_contact()
    with pytest.raises(SecContactMissingError):
        require_sec_contact("tell/0.1 (contact@EXAMPLE.INVALID)")
    with pytest.raises(SecContactMissingError):
        require_sec_contact("   ")
    assert require_sec_contact(REAL_AGENT) == REAL_AGENT


def test_download_refuses_without_contact_and_makes_no_request(tmp_path: Path) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(SecContactMissingError):
            download_dataset(Q1, tmp_path, budget=_budget())
        assert mock.calls.call_count == 0
    assert list(tmp_path.iterdir()) == []


# --- download and cache --------------------------------------------------------------------------


def test_download_writes_the_cache_with_the_sec_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_contact(monkeypatch)
    cache = tmp_path / "cache"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(Q1.url).mock(return_value=httpx.Response(200, content=zip_bytes()))
        path = download_dataset(Q1, cache, budget=_budget())
        request = route.calls.last.request
        assert request.headers["User-Agent"] == REAL_AGENT
        assert "gzip" in request.headers["Accept-Encoding"]
    assert path == cache / "2026q1_d.zip"
    assert zipfile.is_zipfile(path)
    assert not (cache / "2026q1_d.zip.part").exists()

    # Cached: no request at all, even with the placeholder contact back.
    monkeypatch.delenv("SEC_USER_AGENT")
    get_settings.cache_clear()
    with respx.mock(assert_all_mocked=True) as mock:
        assert download_dataset(Q1, cache, budget=_budget()) == path
        assert mock.calls.call_count == 0


def test_download_refresh_refetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_contact(monkeypatch)
    build_zip(tmp_path / Q1.filename)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(Q1.url).mock(return_value=httpx.Response(200, content=zip_bytes()))
        download_dataset(Q1, tmp_path, budget=_budget(), refresh=True)
        assert route.call_count == 1


def test_download_404_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_contact(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        for url in Q1.urls:
            mock.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(DatasetNotPublishedError, match="2026Q1 Form D data set"):
            download_dataset(Q1, tmp_path, budget=_budget())
    assert list(tmp_path.iterdir()) == []


def test_download_403_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_contact(monkeypatch)
    first, second = Q1.urls
    with respx.mock(assert_all_called=True) as mock:
        mock.get(first).mock(return_value=httpx.Response(403, text="Undeclared Automated Tool"))
        mock.get(second).mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError) as caught:
            download_dataset(Q1, tmp_path, budget=_budget())
    assert caught.value.response.status_code == 403


def test_download_other_errors_do_not_try_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_contact(monkeypatch)
    first, second = Q1.urls
    with respx.mock(assert_all_called=False) as mock:
        mock.get(first).mock(return_value=httpx.Response(400))
        fallback = mock.get(second).mock(return_value=httpx.Response(200, content=zip_bytes()))
        with pytest.raises(httpx.HTTPStatusError):
            download_dataset(Q1, tmp_path, budget=_budget())
        assert fallback.call_count == 0


@pytest.mark.parametrize("status", [403, 404])
def test_download_falls_back_to_the_other_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # sec.gov answers a file that is not there with 403 as often as 404.
    _real_contact(monkeypatch)
    quarter = Quarter(2026, 2)
    first, second = quarter.urls
    assert "/datastandardsinnovation/" in first
    with respx.mock(assert_all_called=True) as mock:
        mock.get(first).mock(return_value=httpx.Response(status))
        mock.get(second).mock(return_value=httpx.Response(200, content=zip_bytes()))
        path = download_dataset(quarter, tmp_path, budget=_budget())
    assert path == tmp_path / "2026q2_d.zip"
    assert zipfile.is_zipfile(path)


def test_download_rejects_a_body_that_is_not_a_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_contact(monkeypatch)
    cache = tmp_path / "cache"
    with respx.mock(assert_all_called=True) as mock:
        mock.get(Q1.url).mock(
            return_value=httpx.Response(
                200, text="<html>moved</html>", headers={"Content-Type": "text/html"}
            )
        )
        with pytest.raises(ValueError, match="did not return a ZIP"):
            download_dataset(Q1, cache, budget=_budget())
    assert not cache.exists()


def test_a_corrupt_cached_file_is_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_contact(monkeypatch)
    (tmp_path / Q1.filename).write_bytes(b"truncated")
    with respx.mock(assert_all_called=True) as mock:
        mock.get(Q1.url).mock(return_value=httpx.Response(200, content=zip_bytes()))
        path = download_dataset(Q1, tmp_path, budget=_budget())
    assert zipfile.is_zipfile(path)


# --- writing -------------------------------------------------------------------------------------


@pytest.mark.db
def test_backfill_writes_links_by_cik_and_is_idempotent(
    db_session: Session, tmp_path: Path
) -> None:
    owner = Company(
        domain="quarterly-labs.example",
        name="Example Quarterly Labs",
        cik="0009990101",
        cik_source="guarded_r2",
    )
    # A CIK of unknown origin (cik_source NULL) links nothing at ingest (ADR 0035).
    untrusted = Company(domain="fintech.example", name="Sample Fintech", cik="0009990102")
    db_session.add_all([owner, untrusted])
    db_session.flush()
    archive = build_zip(tmp_path / "2026q1_d.zip")

    first = backfill_formd(db_session, [archive], run_id="bf-1", now=NOW)
    assert (first.fetched, first.inserted, first.skipped, first.outside_window) == (2, 2, 0, 0)
    assert first.linked_by_cik == 1
    assert first.notes == {
        "datasets": ["2026q1_d.zip"],
        "since_days": DEFAULT_SINCE_DAYS,
        "cutoff": "2025-12-02",
        "outside_window": 0,
        "linked_by_cik": 1,
        "by_type": {FORM_D_FILED: 2},
    }
    rows = {
        row.payload["accession_number"]: row
        for row in db_session.scalars(select(EventRow).where(EventRow.source == "edgar"))
    }
    assert set(rows) == {"0009990101-26-000001", "0009990102-26-000001"}
    linked = rows["0009990101-26-000001"]
    assert linked.company_id == owner.id
    assert linked.payload["backfill"] is True
    assert linked.payload["total_amount_sold"] == 7_500_000  # the D/A merged in memory
    assert rows["0009990102-26-000001"].company_id is None  # left to `tell resolve`

    # Rerun (the ZIP and the unpacked directory are the same data set): nothing new.
    again = backfill_formd(db_session, [archive, QUARTERLY], run_id="bf-2", now=NOW)
    assert (again.fetched, again.inserted, again.skipped) == (4, 0, 4)
    assert again.datasets == ["2026q1_d.zip", "quarterly"]
    assert _count(db_session) == 2


@pytest.mark.db
def test_since_days_keeps_only_recent_filings(db_session: Session) -> None:
    # Cutoff 2026-01-31: the 2026-02-03 filing stays, the 2026-01-12 one is dropped.
    result = backfill_formd(db_session, [QUARTERLY], run_id="bf", now=NOW, since_days=60)
    assert (result.fetched, result.inserted, result.outside_window) == (2, 1, 1)
    assert result.cutoff == "2026-01-31"
    (row,) = db_session.scalars(select(EventRow))
    assert row.payload["accession_number"] == "0009990102-26-000001"

    # Boundary: a filing dated exactly on the cutoff day is kept.
    edge = NOW.replace(month=3, day=3)  # 2026-03-03 minus 28 days = 2026-02-03
    kept = backfill_formd(db_session, [QUARTERLY], run_id="bf", now=edge, since_days=27)
    assert (kept.cutoff, kept.outside_window) == ("2026-02-04", 2)
    kept = backfill_formd(db_session, [QUARTERLY], run_id="bf", now=edge, since_days=28)
    assert (kept.cutoff, kept.outside_window, kept.skipped) == ("2026-02-03", 1, 1)


@pytest.mark.db
def test_backfill_skips_filings_the_daily_index_already_wrote(db_session: Session) -> None:
    daily = backfill_formd(db_session, [QUARTERLY], run_id="daily", now=NOW)
    assert daily.inserted == 2
    result = backfill_formd(db_session, [QUARTERLY], run_id="bf", now=NOW + timedelta(days=1))
    assert (result.inserted, result.skipped) == (0, 2)


def test_backfill_rejects_a_non_positive_window(tmp_path: Path) -> None:
    no_session = cast("Session", object())  # the window is checked before any query
    with pytest.raises(ValueError, match="since_days"):
        backfill_formd(no_session, [tmp_path], run_id="bf", now=NOW, since_days=0)


@pytest.mark.db
def test_backfilled_events_stay_out_of_freshness(db_session: Session) -> None:
    observed = NOW - timedelta(hours=1)
    backfill_formd(db_session, [QUARTERLY], run_id="bf", now=observed)
    assert freshness_p95_h(db_session, NOW, ["edgar"]) is None
    assert freshness_n(db_session, NOW) == 0
    # A filing the scheduled daily index brought in: filed 2026-03-31, listed from
    # 2026-04-01 02:00 UTC (filing date + 26 h, ADR 0043), recorded at 11:00.
    start_run(db_session, "edgar", now=datetime(2026, 3, 20, 2, 17, tzinfo=UTC))
    write_events(
        db_session,
        [
            Event(
                source="edgar",
                type=FORM_D_FILED,
                external_id="0009990199-26-000001",
                version="0009990199-26-000001",
                occurred_at=datetime(2026, 3, 31, tzinfo=UTC),
                observed_at=observed,
                url="https://www.sec.gov/Archives/edgar/data/9990199/000999019926000001/",
                payload={"backfill": False},
                run_id="daily",
            )
        ],
    )
    assert freshness_p95_h(db_session, NOW, ["edgar"]) == 9.0
    assert freshness_n(db_session, NOW) == 1


def test_backfill_runs_are_neither_pipeline_nor_health_sources() -> None:
    assert SOURCE == "edgar_backfill"
    assert SOURCE in NON_PIPELINE_SOURCES  # never counted in success rates or "unattended"
    assert SOURCE not in {name for name, _kind in health_sources()}
