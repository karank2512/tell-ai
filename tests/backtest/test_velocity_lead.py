"""13.2 velocity-lead: the spike rule, control matching, the three reconstructions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tell.backtest import velocity_lead as vl
from tell.backtest.common import render_report
from tell.backtest.velocity_lead import Filing
from tell.backtest.wayback import Snapshot, WaybackClient
from tests.backtest.helpers import (
    DAY,
    HOUR,
    NOW,
    board,
    company,
    formd,
    job_opened,
    midnight,
    posting,
    run,
)

QUARTERLY = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "quarterly"
WEEK = timedelta(days=7)


# --- the rule (no database) -------------------------------------------------------------------


def test_weekly_counts_and_spike_lead() -> None:
    filed = datetime(2026, 9, 20, tzinfo=UTC)
    openings = [
        filed,  # the filing instant itself is week 1
        filed - 3 * DAY,
        filed - WEEK,  # boundary: week 1 is (F - 7d, F], so F - 7d is week 2
        filed - 12 * WEEK + HOUR,  # week 12
        filed - 12 * WEEK,  # outside
        filed + HOUR,  # after the filing
    ]
    counts = vl.weekly_counts(openings, filed)
    assert counts == [2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    assert vl.spike_lead(counts) is None  # week 1 has 2 < 3
    # Baseline weeks 9-12 mean 1.0: week 3 with 3 fires (ratio 3.0), week 5 with 3 too.
    assert vl.spike_lead([0, 0, 3, 0, 3, 0, 0, 0, 1, 1, 1, 1]) == 35
    # Baseline 2.5: 3 / 2.5 = 1.2 < 1.5, no spike; 4 / 2.5 = 1.6 fires.
    assert vl.spike_lead([3, 0, 0, 0, 0, 0, 0, 0, 2, 3, 2, 3]) is None
    assert vl.spike_lead([4, 0, 0, 0, 0, 0, 0, 0, 2, 3, 2, 3]) == 7
    # Empty baseline floors at 0.5.
    assert vl.spike_lead([0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0]) == 56


def test_deciles_keep_ties_together() -> None:
    counts = {1: 0, 2: 0, 3: 0, 4: 5, 5: 5, 6: 9, 7: 20, 8: 20, 9: 30, 10: 40}
    d = vl.deciles(counts)
    assert d[1] == d[2] == d[3] == 0
    assert d[4] == d[5] == 3
    assert d[7] == d[8] == 6
    assert d[10] == 9


def _fake_openings(table: dict[int, list[datetime]]) -> vl.OpeningsFn:
    def openings(
        ids: Sequence[int], _since: datetime, _until: datetime
    ) -> dict[int, list[datetime]]:
        return {cid: table.get(cid, []) for cid in ids}

    return openings


def spike(day: date) -> list[datetime]:
    """Three openings two days before ``day``: a week-1 spike over an empty baseline."""
    return [midnight(day) - 2 * DAY] * 3


def test_run_study_with_matched_controls() -> None:
    f1 = Filing(1, date(2026, 9, 1), "a1")
    f2 = Filing(2, date(2026, 9, 15), "a2")
    f3 = Filing(3, date(2026, 9, 29), "a3")
    table = {
        1: spike(f1.filed_on),
        2: [],
        3: spike(f3.filed_on),
        # controls: 10 and 11 in the low decile with filer 2, 12 high with filer 1 and 3.
        10: spike(date(2026, 9, 15)),
        11: [],
        12: [],
    }
    counts = {1: 50, 3: 50, 12: 50, 2: 0, 10: 0, 11: 0}
    result = vl.run_study(
        [f1, f2, f3],
        _fake_openings(table),
        [10, 11, 12, 1],
        lambda ids, _at: {i: counts[i] for i in ids},
    )
    assert (result.filers, result.spiked, result.leads) == (3, 2, [7, 7])
    assert result.with_history == 2
    assert result.controls == 3
    assert result.controls_spiked == 1
    # Weighted by filers per decile: low decile (1 filer) 1/2, high decile (2 filers) 0/1.
    assert result.control_share == pytest.approx((1 * 0.5 + 2 * 0.0) / 3)
    assert result.difference == pytest.approx(2 / 3 - 1 / 6)


def test_run_study_without_controls() -> None:
    result = vl.run_study([], _fake_openings({}), [1, 2], lambda ids, _at: {})
    assert (result.filers, result.share, result.difference) == (0, None, None)
    only = vl.run_study([Filing(1, date(2026, 9, 1), "x")], _fake_openings({}), [])
    assert only.control_share is None


def test_choose_snapshots() -> None:
    filed = datetime(2026, 6, 1, tzinfo=UTC)

    def snap(at: datetime) -> Snapshot:
        return Snapshot(at.strftime("%Y%m%d%H%M%S"), "https://boards.greenhouse.io/x")

    snaps = [
        snap(filed - 100 * DAY),
        snap(filed - 90 * DAY),  # anchor: latest at or before F - 84d
        snap(filed - 10 * DAY),
        snap(filed - 9 * DAY),  # later one in week 2 wins
        snap(filed - DAY),
        snap(filed + DAY),  # after the filing
    ]
    chosen = vl.choose_snapshots(snaps, filed)
    assert [s.at for s in chosen] == [filed - 90 * DAY, filed - 9 * DAY, filed - DAY]


class FakeWayback(WaybackClient):
    """Snapshots and ids from memory; ``None`` means "not cached"."""

    def __init__(
        self, listings: dict[str, list[Snapshot] | None], ids: dict[str, frozenset[str]]
    ) -> None:
        super().__init__(Path("/nonexistent"))
        self.listings = listings
        self.ids = ids

    def snapshots(self, url: str, *, from_year: int) -> list[Snapshot] | None:
        return self.listings.get(url)

    def job_ids(self, snapshot: Snapshot, vendor: str, identifier: str) -> frozenset[str] | None:
        return self.ids.get(snapshot.timestamp)


def test_wayback_study_hand_calculated() -> None:
    filed = date(2026, 6, 1)
    at = midnight(filed)
    weekly = [Snapshot((at - 90 * DAY).strftime("%Y%m%d%H%M%S"), "u")]
    weekly += [
        Snapshot((at - k * WEEK + DAY).strftime("%Y%m%d%H%M%S"), "u") for k in range(12, 0, -1)
    ]
    ids = {s.timestamp: frozenset({"1", "2"}) for s in weekly}
    week2 = weekly[-2].timestamp
    week1 = weekly[-1].timestamp
    ids[week2] = frozenset({"1", "2", "3", "4", "5", "6"})
    ids[week1] = frozenset({"1", "2", "3", "4", "5", "6", "7"})
    sparse = weekly[-3:]
    client = FakeWayback(
        {
            "boards.greenhouse.io/full": weekly,
            "jobs.lever.co/sparse": sparse,
            "boards.greenhouse.io/nothing": [],
        },
        ids,
    )
    boards = {
        1: [("greenhouse", "full")],
        2: [("lever", "sparse")],
        3: [("ashby", "spa")],
        4: [("greenhouse", "uncached")],
        5: [("greenhouse", "nothing")],
    }
    filings = [Filing(c, filed, f"a{c}") for c in (1, 2, 3, 4, 5)]
    result = vl.wayback_study(filings, boards, client)
    assert (result.filers, result.supported, result.listed, result.not_cached) == (5, 4, 3, 1)
    assert (result.with_snapshots, result.covered) == (2, 1)
    assert (result.study.filers, result.study.spiked, result.study.leads) == (1, 1, [14])


# --- database -----------------------------------------------------------------------------------


@pytest.mark.db
def test_prospective_not_runnable_before_60_days(db_session: Session, tmp_path: Path) -> None:
    run(db_session, "greenhouse", NOW - 12 * DAY)
    run(db_session, "edgar", NOW - 200 * DAY)  # not an ATS source
    report = vl.build_report(db_session, NOW, wayback=WaybackClient(tmp_path))
    text = render_report(report)
    assert report.n == 0
    assert not report.quotable
    assert "Not yet runnable: 12 days of collection < 60." in text
    assert "quotable: false" in text
    assert "[" not in text


@pytest.mark.db
def test_prospective_with_controls(db_session: Session, tmp_path: Path) -> None:
    start = NOW - 100 * DAY
    run(db_session, "greenhouse", start)
    filer = company(db_session, "filer.example")
    quiet = company(db_session, "quiet.example")
    busy = company(db_session, "busy.example")
    early = company(db_session, "early.example")
    for owner in (filer, quiet, busy, early):
        board(db_session, owner, owner.domain.split(".")[0])
    filed = date(2026, 9, 20)
    at = midnight(filed)
    formd(db_session, filer, filed)
    formd(db_session, early, (start + 30 * DAY).date())  # before day 60: not a filer, not a control
    formd(db_session, filer, date(2026, 9, 25), amendment=True)
    # Filer: 4 distinct requisitions in week 2, one repost-style duplicate fingerprint.
    for k in range(4):
        job_opened(db_session, filer, at - 10 * DAY + k * HOUR, f"f{k}")
    job_opened(db_session, filer, at - 9 * DAY, "f0")  # same requisition within 30 days
    job_opened(db_session, filer, at - 9 * DAY, "intern", is_intern=True)
    # Quiet control: only first-poll baselines, never counted.
    for k in range(5):
        job_opened(db_session, quiet, at - 2 * DAY, f"q{k}", first_poll=True)
    # Busy control: 3 in week 1 before the filing date.
    for k in range(3):
        job_opened(db_session, busy, at - 2 * DAY, f"b{k}")

    report = vl.build_report(db_session, NOW, wayback=WaybackClient(tmp_path))
    text = render_report(report)
    assert report.n == 1
    assert (
        "Prospective: share of filings preceded by a spike in the 8 weeks before: 100.0% (N=1; too small to quote)"
        in text
    )
    assert (
        "Prospective: median lead, days from the first spike week to the filing: 14.0 days (N=1; too small to quote)"
        in text
    )
    assert (
        "Prospective: control share preceded by a spike (matched by open-posting decile): 50.0% (N=2; too small to quote)"
        in text
    )
    assert (
        "Prospective: difference, filers minus controls, percentage points: 50.00 (N=1; too small to quote)"
        in text
    )
    assert "Days of collection: 100" in text
    assert "is the number to quote" in text


@pytest.mark.db
def test_retrospective_posted_at_and_quarterly(db_session: Session, tmp_path: Path) -> None:
    # Quarterly fixture: CIK 9990101 filed 2026-01-12, CIK 9990102 filed 2026-02-03.
    labs = company(db_session, "quarterlylabs.example", cik="0009990101", cik_source="manual")
    fintech = company(db_session, "samplefintech.example", cik="0009990102", cik_source="r1")
    control = company(db_session, "control.example")
    labs_board = board(db_session, labs, "quarterlylabs")
    board(db_session, fintech, "samplefintech", vendor="ashby")
    control_board = board(db_session, control, "control")
    filed = midnight(date(2026, 1, 12))
    for k in range(3):
        posting(db_session, labs_board, f"l{k}", first_seen=NOW, posted=filed - 3 * DAY)
    for k in range(3):
        posting(
            db_session, control_board, f"c{k}", first_seen=NOW, posted=midnight(date(2026, 2, 1))
        )
    # The same accession seen by the daily index too: counted once.
    formd(db_session, labs, date(2026, 1, 12), accession="0009990101-26-000001")

    report = vl.build_report(
        db_session, NOW, formd_paths=[QUARTERLY], wayback=WaybackClient(tmp_path)
    )
    text = render_report(report)
    assert (
        "Filings: 1 from database events, 2 from 1 quarterly data sets; 2 by companies with an active board."
        in text
    )
    assert (
        "Retrospective (a) coverage: filings with a reconstructed posting in the 12 weeks before: 50.0% (N=2; too small to quote)"
        in text
    )
    assert (
        "Retrospective (a): share of filings preceded by a spike in the 8 weeks before: 50.0% (N=2; too small to quote)"
        in text
    )
    # Deciles at 2026-01-12: labs (3 open) alone high, fintech and control low; the control
    # is evaluated at fintech's filing date and spikes (3 posted 2026-02-01).
    assert (
        "Retrospective (a): control share preceded by a spike (matched by open-posting decile): 100.0% (N=1; too small to quote)"
        in text
    )
    assert (
        "Retrospective (a): difference, filers minus controls, percentage points: -50.00 (N=1; too small to quote)"
        in text
    )
    # Wayback: labs is on Greenhouse (not cached), fintech on Ashby (unsupported).
    assert (
        "Of 2 filings, 1 have a Greenhouse or Lever board; 0 have a CDX listing in the cache (1 not fetched yet)"
        in text
    )
    assert "Not yet runnable: 0 days of collection < 60." in text


@pytest.mark.db
def test_quarterly_filings_link_only_a_trusted_cik(db_session: Session) -> None:
    """ADR 0035: a CIK of unknown origin (``cik_source`` NULL) maps no filing to a company."""
    labs = company(db_session, "quarterlylabs.example", cik="0009990101", cik_source="manual")
    company(db_session, "samplefintech.example", cik="0009990102")
    filings = vl.quarterly_filings(db_session, [QUARTERLY], date(2025, 10, 1), NOW)
    assert [f.company_id for f in filings] == [labs.id]
