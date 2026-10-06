"""Wayback client: CDX listing, raw snapshots, cache, budget. respx only; never the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from tell.backtest import wayback
from tell.backtest.wayback import Snapshot, WaybackClient
from tell.http import RateBudget

BOARD = "boards.greenhouse.io/exampleco"
HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
CDX_ROWS = [
    HEADER,
    [
        "io,greenhouse,boards)/exampleco",
        "20260105120000",
        f"https://{BOARD}",
        "text/html",
        "200",
        "A",
        "1",
    ],
    [
        "io,greenhouse,boards)/exampleco",
        "20260112120000",
        f"https://{BOARD}",
        "text/html",
        "200",
        "B",
        "1",
    ],
    [
        "io,greenhouse,boards)/exampleco",
        "20260112120000",
        f"https://{BOARD}",
        "text/html",
        "200",
        "B",
        "1",
    ],
]
PAGE = (
    '<div class="opening"><a href="/exampleco/jobs/4001234">Account Executive</a></div>'
    '<div class="opening"><a href="https://boards.greenhouse.io/exampleco/jobs/4005678">SDR</a></div>'
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _client(tmp_path: Path, clock: FakeClock, **kwargs: object) -> WaybackClient:
    budget = RateBudget(wayback.WAYBACK_RPS, clock=clock.clock, sleep=clock.sleep)
    return WaybackClient(
        tmp_path / "cache",
        network=True,
        client=httpx.Client(),
        budget=budget,
        sleep=clock.sleep,
        **kwargs,  # type: ignore[arg-type]  # max_requests only
    )


def _exhausted(client: WaybackClient) -> bool:
    return client.exhausted  # a call, so mypy does not narrow the attribute between asserts


def test_cdx_params_and_snapshot_url() -> None:
    assert wayback.cdx_params(BOARD, 2026) == {
        "url": BOARD,
        "output": "json",
        "from": "2026",
        "filter": "statuscode:200",
    }
    snap = Snapshot("20260105120000", f"https://{BOARD}")
    assert (
        wayback.snapshot_url(snap)
        == f"https://web.archive.org/web/20260105120000id_/https://{BOARD}"
    )
    assert snap.at.isoformat() == "2026-01-05T12:00:00+00:00"


def test_parse_cdx() -> None:
    snaps = wayback.parse_cdx(CDX_ROWS)
    assert [s.timestamp for s in snaps] == ["20260105120000", "20260112120000"]
    assert wayback.parse_cdx([]) == []
    assert wayback.parse_cdx({"not": "a list"}) == []
    assert (
        wayback.parse_cdx([HEADER, ["short"], [*CDX_ROWS[1][:1], "bad-ts", "x", "", "", "", ""]])
        == []
    )
    with pytest.raises(ValueError, match="unexpected CDX header"):
        wayback.parse_cdx([["nope"]])


def test_extract_job_ids() -> None:
    assert wayback.extract_job_ids(PAGE, "greenhouse", "exampleco") == frozenset(
        {"4001234", "4005678"}
    )
    uuid = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    lever = f'<a href="https://jobs.lever.co/exampleco/{uuid}">Role</a><a href="https://jobs.lever.co/other/{uuid[:-1]}c">x</a>'
    assert wayback.extract_job_ids(lever, "lever", "exampleco") == {uuid}
    assert wayback.extract_job_ids(lever, "lever", "eu:exampleco") == {uuid}
    assert wayback.extract_job_ids(PAGE, "ashby", "exampleco") == frozenset()


@respx.mock
def test_fetches_once_then_reads_the_cache(tmp_path: Path) -> None:
    cdx = respx.get(wayback.CDX_URL, params={"url": BOARD, "from": "2026"}).mock(
        return_value=httpx.Response(200, json=CDX_ROWS)
    )
    page = respx.get(f"https://web.archive.org/web/20260105120000id_/https://{BOARD}").mock(
        return_value=httpx.Response(200, text=PAGE)
    )
    clock = FakeClock()
    with _client(tmp_path, clock) as client:
        snaps = client.snapshots(BOARD, from_year=2026)
        assert snaps is not None
        assert len(snaps) == 2
        assert client.job_ids(snaps[0], "greenhouse", "exampleco") == frozenset(
            {"4001234", "4005678"}
        )
        assert client.requests_made == 2
    assert cdx.call_count == 1
    assert page.call_count == 1
    assert clock.slept == [1.0]  # 1 request per second
    files = sorted(p.name for p in (tmp_path / "cache").iterdir())
    assert len(files) == 2
    assert all(name.endswith(".json") for name in files)
    snap_file = next(p for p in (tmp_path / "cache").iterdir() if p.name.startswith("snap-"))
    assert json.loads(snap_file.read_text(encoding="utf-8"))["job_ids"] == ["4001234", "4005678"]

    offline = WaybackClient(tmp_path / "cache")
    assert offline.snapshots(BOARD, from_year=2026) == snaps
    assert offline.job_ids(snaps[0], "greenhouse", "exampleco") == frozenset({"4001234", "4005678"})
    assert offline.cache_hits == 2
    assert offline.snapshots(BOARD, from_year=2025) is None  # not cached, no network
    assert offline.job_ids(snaps[1], "greenhouse", "exampleco") is None
    assert cdx.call_count == 1


@respx.mock
def test_empty_listing_errors_and_budget(tmp_path: Path) -> None:
    respx.get(wayback.CDX_URL, params={"url": BOARD}).mock(
        return_value=httpx.Response(200, text="")
    )
    respx.get(wayback.CDX_URL, params={"url": "jobs.lever.co/gone"}).mock(
        return_value=httpx.Response(404)
    )
    respx.get(wayback.CDX_URL, params={"url": "jobs.lever.co/down"}).mock(
        side_effect=httpx.ConnectError("refused")
    )
    clock = FakeClock()
    client = _client(tmp_path, clock, max_requests=3)
    assert client.snapshots(BOARD, from_year=2026) == []
    assert client.snapshots("jobs.lever.co/gone", from_year=2026) is None
    assert client.snapshots("jobs.lever.co/down", from_year=2026) is None
    assert clock.slept[-4:] == [1.0, 2.0, 4.0, 8.0]  # retry backoff, on the fake clock
    assert not _exhausted(client)
    assert client.snapshots("jobs.lever.co/more", from_year=2026) is None
    assert _exhausted(client)
    assert client.requests_made == 3
    client.close()
    # The empty listing is cached; the failures are not.
    assert len(list((tmp_path / "cache").iterdir())) == 1


def test_default_client_is_offline(tmp_path: Path) -> None:
    client = WaybackClient(tmp_path)
    assert client.network is False
    assert client.snapshots(BOARD, from_year=2026) is None
    assert client.requests_made == 0
    client.close()


@respx.mock
def test_builds_its_own_http_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(wayback.CDX_URL).mock(return_value=httpx.Response(200, json=[HEADER]))
    clock = FakeClock()
    monkeypatch.setattr(
        wayback, "RateBudget", lambda rps, sleep: RateBudget(rps, clock=clock.clock, sleep=sleep)
    )
    with WaybackClient(tmp_path, network=True, sleep=clock.sleep) as client:
        assert client.snapshots(BOARD, from_year=2026) == []
