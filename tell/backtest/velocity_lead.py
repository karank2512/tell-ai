"""Spec 13.2 hiring velocity as a leading indicator of a Form D filing.

Definitions shared by every version of the study (company level: all role families of a
company are counted together, because the Wayback reconstruction cannot tell families
apart and the three versions have to be comparable):

* ``F`` is the filing date at 00:00 UTC. Week ``k`` before the filing is ``(F - 7k days,
  F - 7(k-1) days]``. ``new7`` of week ``k`` counts the company's net-new postings opened
  in it (same rules as :mod:`tell.signals.velocity`: interns and reposts are not counted,
  and a fingerprint re-opened on the same board within 30 days is the same requisition).
* Baseline: mean ``new7`` of weeks 9 to 12, floored at 0.5. A **spike** is any of weeks
  1 to 8 with ``new7 >= 3`` and ``new7 / baseline >= 1.5``. The **lead** of a spiking
  filing is ``7k`` days for the earliest spiking week ``k`` (from the start of that week
  to the filing date).

Versions:

* **Prospective** (the result to quote). Collection starts at the first ATS ``runs`` row.
  Runnable once collection has run :data:`MIN_COLLECTION_DAYS` days; until then the report
  says "Not yet runnable: N days of collection < 60". Cohort: every new Form D
  (``form_d_filed``, not an amendment) filed after day 60 by a company with an active
  board. Postings come from ``job_opened`` events; first-poll baselines (ADR 0019) are never
  counted. Control group: companies with an active board and no filing since collection
  started, matched to the filers by decile of open-posting count at the median filing date;
  each control is evaluated at the median filing date of the filers in its decile and the
  control share is weighted by the filers' decile distribution. The number to quote is the
  difference between the two shares.
* **Retrospective (a), current postings.** The same rule over postings reconstructed from
  ``postings.posted_at`` (survivorship bias: roles that closed before tell started watching
  are invisible, so spikes are undercounted). Filings since :data:`RETRO_SINCE` (2025 Q4)
  from the database (daily index, plus any backfill) and from quarterly Form D data sets
  passed as ``--formd`` (loaded in memory with
  :func:`tell.collectors.edgar_formd.load_formd_quarterly`, linked by CIK only to a
  company whose ``cik_source`` is trusted, as at ingest; ADR 0035). Same control
  matching. Coverage: the share of filers with at least one reconstructed posting in the
  12 weeks before the filing.
* **Retrospective (b), Wayback snapshots.** For filers with a Greenhouse or Lever board,
  archived board pages from the CDX API (:mod:`tell.backtest.wayback`): the latest snapshot
  in each of the 12 weeks plus the latest one before them; job ids new since the previous
  snapshot are openings dated at the snapshot. A filing is covered when at least
  :data:`MIN_WAYBACK_WEEKS` of the 12 weeks have a snapshot. No control group (non-filers
  would need their own archive fetches). Coverage is stated per step.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tell.backtest.common import Metric, Query, Report, Section, median, share
from tell.backtest.wayback import SUPPORTED_VENDORS, Snapshot, WaybackClient
from tell.collectors.base import FIRST_POLL_KEY, JOB_OPENED
from tell.collectors.edgar_formd import FORM_D_FILED, company_ids_by_cik, load_formd_quarterly
from tell.models import AtsBoard, EventRow, Posting, Run
from tell.signals.velocity import BASE_FLOOR, MIN_NEW7, MIN_RATIO, SAME_REQUISITION_WINDOW
from tell.signals.velocity import board_url as public_board_url

__all__ = [
    "ATS_SOURCES",
    "BASELINE_WEEKS",
    "HISTORY_WEEKS",
    "MIN_COLLECTION_DAYS",
    "MIN_WAYBACK_WEEKS",
    "NAME",
    "PRE_WEEKS",
    "RETRO_SINCE",
    "Filing",
    "StudyResult",
    "WaybackResult",
    "board_companies",
    "build_report",
    "choose_snapshots",
    "collection_start",
    "db_filings",
    "deciles",
    "event_openings",
    "merge_filings",
    "open_counts",
    "posting_openings",
    "quarterly_filings",
    "run_study",
    "spike_lead",
    "wayback_openings",
    "wayback_study",
    "weekly_counts",
]

NAME = "velocity-lead"
SCRIPT = "tell/backtest/velocity_lead.py"
MIN_COLLECTION_DAYS = 60
PRE_WEEKS = 8
BASELINE_WEEKS = 4
HISTORY_WEEKS = PRE_WEEKS + BASELINE_WEEKS
MIN_WAYBACK_WEEKS = 8
RETRO_SINCE = date(2025, 10, 1)
"""Start of the retrospective filing window: the 2025 Q4 Form D data set (spec 13.2)."""
ATS_SOURCES: tuple[str, ...] = ("greenhouse", "lever", "ashby", "smartrecruiters")
SMALL_DIFFERENCE = 0.10
"""Below this absolute difference (10 points) the report says the difference is small."""

_WEEK = timedelta(days=7)
_HISTORY = _WEEK * HISTORY_WEEKS

OpeningsFn = Callable[[Sequence[int], datetime, datetime], dict[int, list[datetime]]]
OpenCountsFn = Callable[[Sequence[int], datetime], dict[int, int]]


@dataclass(frozen=True)
class Filing:
    """One new Form D of a company (or a control's pseudo-filing date)."""

    company_id: int
    filed_on: date
    accession: str

    @property
    def at(self) -> datetime:
        return datetime.combine(self.filed_on, time(0, 0), tzinfo=UTC)


# --- the rule ---------------------------------------------------------------------------------


def weekly_counts(
    openings: Iterable[datetime], filed_at: datetime, weeks: int = HISTORY_WEEKS
) -> list[int]:
    """``new7`` of weeks 1..``weeks`` before ``filed_at`` (index 0 is week 1)."""
    counts = [0] * weeks
    for opened in openings:
        delta = filed_at - opened
        if delta < timedelta(0):
            continue
        k = int(delta // _WEEK) + 1
        if k <= weeks:
            counts[k - 1] += 1
    return counts


def spike_lead(counts: Sequence[int]) -> int | None:
    """Lead in days (``7k`` for the earliest spiking week ``k``), ``None`` without a spike."""
    baseline = max(sum(counts[PRE_WEEKS:HISTORY_WEEKS]) / BASELINE_WEEKS, BASE_FLOOR)
    spiking = [
        k
        for k in range(1, PRE_WEEKS + 1)
        if counts[k - 1] >= MIN_NEW7 and counts[k - 1] / baseline >= MIN_RATIO
    ]
    return 7 * max(spiking) if spiking else None


def _chain(items: Iterable[tuple[Any, datetime, bool]], since: datetime) -> list[datetime]:
    """Net-new opening times: ``(key, at, counted)`` with the 30-day same-requisition rule."""
    last: dict[Any, datetime] = {}
    kept: list[datetime] = []
    for key, at, counted in sorted(items, key=lambda i: (i[1], str(i[0]))):
        previous = last.get(key)
        last[key] = at
        if previous is not None and at - previous <= SAME_REQUISITION_WINDOW:
            continue
        if counted and at > since:
            kept.append(at)
    return kept


# --- data access ------------------------------------------------------------------------------


def event_openings(
    session: Session, company_ids: Sequence[int], since: datetime, until: datetime
) -> dict[int, list[datetime]]:
    """Counted ``job_opened`` times in ``(since, until]`` per company (prospective)."""
    if not company_ids:
        return {}
    stmt = select(EventRow.company_id, EventRow.occurred_at, EventRow.payload).where(
        EventRow.type == JOB_OPENED,
        EventRow.company_id.in_(list(company_ids)),
        EventRow.occurred_at > since - SAME_REQUISITION_WINDOW,
        EventRow.occurred_at <= until,
    )
    items: dict[int, list[tuple[Any, datetime, bool]]] = defaultdict(list)
    for company_id, occurred_at, payload in session.execute(stmt):
        if payload.get("is_intern") is True or payload.get("repost_of_external_id"):
            continue
        key = (payload.get("vendor"), payload.get("board_identifier"), payload.get("fingerprint"))
        items[int(company_id)].append((key, occurred_at, payload.get(FIRST_POLL_KEY) is not True))
    return {cid: _chain(rows, since) for cid, rows in items.items()}


def posting_openings(
    session: Session, company_ids: Sequence[int], since: datetime, until: datetime
) -> dict[int, list[datetime]]:
    """Opening times reconstructed from ``postings.posted_at`` in ``(since, until]``."""
    if not company_ids:
        return {}
    stmt = (
        select(AtsBoard.company_id, Posting.board_id, Posting.fingerprint, Posting.posted_at)
        .select_from(Posting)
        .join(AtsBoard, AtsBoard.id == Posting.board_id)
        .where(
            AtsBoard.company_id.in_(list(company_ids)),
            Posting.posted_at.is_not(None),
            Posting.repost_of.is_(None),
            Posting.posted_at > since - SAME_REQUISITION_WINDOW,
            Posting.posted_at <= until,
        )
    )
    items: dict[int, list[tuple[Any, datetime, bool]]] = defaultdict(list)
    for company_id, board_id, fingerprint, posted_at in session.execute(stmt):
        if company_id is not None and posted_at is not None:
            items[int(company_id)].append(((board_id, fingerprint), posted_at, True))
    return {cid: _chain(rows, since) for cid, rows in items.items()}


def open_counts(session: Session, company_ids: Sequence[int], at: datetime) -> dict[int, int]:
    """Open postings per company at ``at`` (zero for companies with none)."""
    counts = dict.fromkeys(company_ids, 0)
    if not company_ids:
        return counts
    stmt = (
        select(AtsBoard.company_id, func.count())
        .select_from(Posting)
        .join(AtsBoard, AtsBoard.id == Posting.board_id)
        .where(
            AtsBoard.company_id.in_(list(company_ids)),
            func.coalesce(Posting.posted_at, Posting.first_seen_at) <= at,
            or_(Posting.closed_at.is_(None), Posting.closed_at > at),
        )
        .group_by(AtsBoard.company_id)
    )
    for company_id, count in session.execute(stmt):
        if company_id is not None:
            counts[int(company_id)] = int(count)
    return counts


def board_companies(session: Session) -> dict[int, list[tuple[str, str]]]:
    """``{company_id: [(vendor, identifier)]}`` over active boards."""
    stmt = (
        select(AtsBoard.company_id, AtsBoard.vendor, AtsBoard.identifier)
        .where(AtsBoard.status == "active", AtsBoard.company_id.is_not(None))
        .order_by(AtsBoard.company_id, AtsBoard.vendor, AtsBoard.identifier)
    )
    out: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for company_id, vendor, identifier in session.execute(stmt):
        if company_id is not None:
            out[int(company_id)].append((str(vendor), str(identifier)))
    return dict(out)


def collection_start(session: Session, now: datetime) -> datetime | None:
    """First ATS run, else the first posting tell saw; ``None`` before either."""
    first_run = session.scalar(
        select(func.min(Run.started_at)).where(Run.source.in_(ATS_SOURCES), Run.started_at <= now)
    )
    if first_run is not None:
        return first_run
    return session.scalar(
        select(func.min(Posting.first_seen_at)).where(Posting.first_seen_at <= now)
    )


def db_filings(session: Session, since: date, now: datetime) -> list[Filing]:
    """New Form D filings of linked companies filed in ``[since, now)``."""
    stmt = select(EventRow.company_id, EventRow.occurred_at, EventRow.payload).where(
        EventRow.type == FORM_D_FILED,
        EventRow.company_id.is_not(None),
        EventRow.occurred_at >= datetime.combine(since, time(0, 0), tzinfo=UTC),
        EventRow.occurred_at < now,
    )
    out: list[Filing] = []
    for company_id, occurred_at, payload in session.execute(stmt):
        if payload.get("is_amendment") is True:
            continue
        accession = str(payload.get("accession_number") or occurred_at.isoformat())
        out.append(Filing(int(company_id), occurred_at.astimezone(UTC).date(), accession))
    return out


def quarterly_filings(
    session: Session, paths: Sequence[Path], since: date, now: datetime
) -> list[Filing]:
    """New Form D filings from quarterly data sets, linked to companies by a trusted CIK."""
    out: list[Filing] = []
    for path in paths:
        events = [
            e
            for e in load_formd_quarterly(path, run_id="backtest-velocity-lead", now=now)
            if e.type == FORM_D_FILED and e.payload.get("is_amendment") is not True
        ]
        by_cik = company_ids_by_cik(session, [str(e.payload.get("cik") or "") for e in events])
        for event in events:
            company_id = by_cik.get(str(event.payload.get("cik") or ""))
            filed = event.occurred_at.date()
            if company_id is not None and since <= filed and event.occurred_at < now:
                out.append(Filing(company_id, filed, str(event.payload["accession_number"])))
    return out


def merge_filings(*groups: Iterable[Filing]) -> list[Filing]:
    """Union by accession number (the first occurrence wins), oldest first."""
    seen: dict[str, Filing] = {}
    for group in groups:
        for filing in group:
            seen.setdefault(filing.accession, filing)
    return sorted(seen.values(), key=lambda f: (f.filed_on, f.company_id, f.accession))


# --- the study --------------------------------------------------------------------------------


@dataclass
class StudyResult:
    """Spike share and lead for filers, and for the matched control group."""

    filers: int = 0
    spiked: int = 0
    leads: list[int] = field(default_factory=list)
    with_history: int = 0
    """Filers with at least one counted opening in the 12 weeks (coverage)."""
    controls: int = 0
    controls_spiked: int = 0
    control_share: float | None = None
    control_leads: list[int] = field(default_factory=list)

    @property
    def share(self) -> float | None:
        return share(self.spiked, self.filers)

    @property
    def difference(self) -> float | None:
        if self.share is None or self.control_share is None:
            return None
        return self.share - self.control_share


def _evaluate(filings: Sequence[Filing], openings: OpeningsFn) -> StudyResult:
    result = StudyResult(filers=len(filings))
    if not filings:
        return result
    since = min(f.at for f in filings) - _HISTORY
    until = max(f.at for f in filings)
    by_company = openings(sorted({f.company_id for f in filings}), since, until)
    for filing in filings:
        counts = weekly_counts(by_company.get(filing.company_id, ()), filing.at)
        if any(counts):
            result.with_history += 1
        lead = spike_lead(counts)
        if lead is not None:
            result.spiked += 1
            result.leads.append(lead)
    return result


def deciles(counts: dict[int, int]) -> dict[int, int]:
    """Decile 0-9 of each company's count; equal counts always share a decile."""
    values = sorted(counts.values())
    total = len(values)
    return {cid: min(9, bisect.bisect_left(values, v) * 10 // total) for cid, v in counts.items()}


def _median_filing(filings: Sequence[Filing]) -> Filing:
    ordered = sorted(filings, key=lambda f: (f.filed_on, f.company_id))
    return ordered[(len(ordered) - 1) // 2]


def run_study(
    filings: Sequence[Filing],
    openings: OpeningsFn,
    control_pool: Sequence[int] = (),
    counts_at: OpenCountsFn | None = None,
) -> StudyResult:
    """Filers' spike share and lead, plus the decile-matched control group when given."""
    result = _evaluate(filings, openings)
    pool = sorted(set(control_pool) - {f.company_id for f in filings})
    if not filings or not pool or counts_at is None:
        return result
    reference = _median_filing(filings).at
    population = sorted({f.company_id for f in filings} | set(pool))
    decile = deciles(counts_at(population, reference))
    filers_by: dict[int, list[Filing]] = defaultdict(list)
    for filing in filings:
        filers_by[decile[filing.company_id]].append(filing)
    controls_by: dict[int, list[int]] = defaultdict(list)
    for company_id in pool:
        controls_by[decile[company_id]].append(company_id)
    weighted = 0.0
    weight = 0
    for d, group in sorted(filers_by.items()):
        controls = controls_by.get(d)
        if not controls:
            continue
        pseudo_day = _median_filing(group).filed_on
        evaluated = _evaluate([Filing(c, pseudo_day, f"control-{c}") for c in controls], openings)
        weighted += len(group) * evaluated.spiked / len(controls)
        weight += len(group)
        result.controls += len(controls)
        result.controls_spiked += evaluated.spiked
        result.control_leads.extend(evaluated.leads)
    result.control_share = weighted / weight if weight else None
    return result


# --- Wayback reconstruction -----------------------------------------------------------------


@dataclass
class WaybackResult:
    """Coverage of the Wayback reconstruction and the spike rule over covered filings."""

    filers: int = 0
    supported: int = 0
    listed: int = 0
    not_cached: int = 0
    with_snapshots: int = 0
    covered: int = 0
    study: StudyResult = field(default_factory=StudyResult)


def choose_snapshots(snapshots: Sequence[Snapshot], filed_at: datetime) -> list[Snapshot]:
    """The latest snapshot per week 1..12 plus the latest one before week 12, oldest first."""
    window_start = filed_at - _HISTORY
    chosen: dict[int, Snapshot] = {}
    anchor: Snapshot | None = None
    for snap in sorted(snapshots, key=lambda s: s.timestamp):
        at = snap.at
        if at > filed_at:
            continue
        if at <= window_start:
            anchor = snap
            continue
        week = int((filed_at - at) // _WEEK) + 1
        chosen[week] = snap
    ordered = [chosen[k] for k in sorted(chosen, reverse=True)]
    return [anchor, *ordered] if anchor is not None else ordered


def wayback_openings(
    client: WaybackClient, snapshots: Sequence[Snapshot], vendor: str, identifier: str
) -> tuple[list[datetime], list[Snapshot]]:
    """``(openings, fetched)``: ids new since the previous fetched snapshot, dated at it."""
    openings: list[datetime] = []
    fetched: list[Snapshot] = []
    seen: set[str] | None = None
    for snap in snapshots:
        ids = client.job_ids(snap, vendor, identifier)
        if ids is None:
            continue
        fetched.append(snap)
        if seen is not None:
            openings.extend(snap.at for _ in ids - seen)
            seen |= ids
        else:
            seen = set(ids)
    return openings, fetched


def _cdx_url(vendor: str, identifier: str) -> str | None:
    url = public_board_url(vendor, identifier)
    return None if url is None else url.removeprefix("https://")


def wayback_study(
    filings: Sequence[Filing],
    boards: dict[int, list[tuple[str, str]]],
    client: WaybackClient,
) -> WaybackResult:
    """Spike rule over Wayback-reconstructed histories (see the module docstring)."""
    out = WaybackResult(filers=len(filings))
    covered: list[tuple[Filing, list[datetime]]] = []
    for filing in filings:
        supported = [b for b in boards.get(filing.company_id, []) if b[0] in SUPPORTED_VENDORS]
        if not supported:
            continue
        out.supported += 1
        vendor, identifier = supported[0]
        url = _cdx_url(vendor, identifier)
        if url is None:  # pragma: no cover - every supported vendor has a board URL
            continue
        snaps = client.snapshots(url, from_year=(filing.at - _HISTORY - _WEEK).year)
        if snaps is None:
            out.not_cached += 1
            continue
        out.listed += 1
        chosen = choose_snapshots(snaps, filing.at)
        if not chosen:
            continue
        openings, fetched = wayback_openings(client, chosen, vendor, identifier)
        weeks = {
            int((filing.at - s.at) // _WEEK) + 1 for s in fetched if s.at > filing.at - _HISTORY
        }
        if fetched:
            out.with_snapshots += 1
        if len(weeks) >= MIN_WAYBACK_WEEKS:
            out.covered += 1
            covered.append((filing, openings))
    by_company = {f.company_id: o for f, o in covered}
    out.study = _evaluate(
        [f for f, _ in covered],
        lambda ids, _since, _until: {cid: by_company.get(cid, []) for cid in ids},
    )
    return out


# --- report -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Inputs:
    now: datetime
    start: datetime | None
    days: int
    boards: dict[int, list[tuple[str, str]]]


def _prospective(session: Session, inputs: _Inputs) -> tuple[StudyResult | None, list[Filing]]:
    if inputs.start is None or inputs.days < MIN_COLLECTION_DAYS:
        return None, []
    first_day = (inputs.start + timedelta(days=MIN_COLLECTION_DAYS)).date()
    window = db_filings(session, inputs.start.date(), inputs.now)
    filers = [
        f
        for f in merge_filings(window)
        if f.filed_on >= first_day and f.company_id in inputs.boards
    ]
    filed = {f.company_id for f in window}
    pool = [cid for cid in inputs.boards if cid not in filed]
    result = run_study(
        filers,
        lambda ids, since, until: event_openings(session, ids, since, until),
        pool,
        lambda ids, at: open_counts(session, ids, at),
    )
    return result, filers


def _posted_at_coverage(session: Session) -> tuple[int, int]:
    total = session.scalar(select(func.count()).select_from(Posting)) or 0
    dated = session.scalar(
        select(func.count()).select_from(Posting).where(Posting.posted_at.is_not(None))
    )
    return int(dated or 0), int(total)


def _study_metrics(
    label: str, result: StudyResult, *, headline: bool, control: bool = True
) -> list[Metric]:
    metrics = [
        Metric(f"{label}: filings in the cohort", result.filers, kind="count", headline=headline),
        Metric(
            f"{label}: share of filings preceded by a spike in the 8 weeks before",
            result.share,
            result.filers,
            "share",
            headline,
        ),
        Metric(
            f"{label}: median lead, days from the first spike week to the filing",
            median(result.leads),
            len(result.leads),
            "days",
            headline,
        ),
    ]
    if control:
        metrics += [
            Metric(
                f"{label}: control share preceded by a spike (matched by open-posting decile)",
                result.control_share,
                result.controls,
                "share",
                headline,
            ),
            Metric(
                f"{label}: difference, filers minus controls, percentage points",
                None if result.difference is None else result.difference * 100,
                min(result.filers, result.controls),
                "number",
                headline,
            ),
        ]
    return metrics


def _difference_text(result: StudyResult) -> str:
    if result.difference is None:
        return "No control comparison yet."
    if abs(result.difference) < SMALL_DIFFERENCE:
        return (
            "The difference between filers and controls is small (under 10 percentage points): "
            "hiring spikes barely separate companies about to file from those that are not."
        )
    return "The difference between filers and controls is the number to quote, with both N."


def build_report(
    session: Session,
    now: datetime,
    *,
    formd_paths: Sequence[Path] = (),
    wayback: WaybackClient | None = None,
    retro_since: date = RETRO_SINCE,
) -> Report:
    """The 13.2 report at ``now`` (see the module docstring)."""
    start = collection_start(session, now)
    days = 0 if start is None else max((now - start).days, 0)
    inputs = _Inputs(now=now, start=start, days=days, boards=board_companies(session))
    prospective, _ = _prospective(session, inputs)

    from_db = db_filings(session, retro_since, now)
    from_tsv = quarterly_filings(session, formd_paths, retro_since, now)
    retro_filers = [f for f in merge_filings(from_db, from_tsv) if f.company_id in inputs.boards]
    filed = {f.company_id for f in merge_filings(from_db, from_tsv)}
    retro = run_study(
        retro_filers,
        lambda ids, since, until: posting_openings(session, ids, since, until),
        [cid for cid in inputs.boards if cid not in filed],
        lambda ids, at: open_counts(session, ids, at),
    )
    client = wayback if wayback is not None else WaybackClient(network=False)
    way = wayback_study(retro_filers, inputs.boards, client)
    dated, total_postings = _posted_at_coverage(session)

    report = Report(
        name=NAME,
        title="Hiring velocity as a leading indicator of a Form D filing",
        question=(
            "Does a hiring spike precede a funding filing more often for companies that file "
            "than for comparable companies that do not, and by how many days?"
        ),
        cohort=(
            "Prospective: every new Form D (isAmendment = false) filed after day 60 of "
            "collection by a company with an active board, against non-filers with an active "
            "board matched by open-posting decile. Retrospective: new Form D filings since "
            f"{retro_since.isoformat()} (database events plus quarterly data sets) by companies "
            "with an active board. Cohort N is the prospective cohort."
        ),
        n=prospective.filers if prospective is not None else 0,
        as_of=now.date(),
        generated_at=now,
        script=SCRIPT,
    )
    if prospective is None:
        line = f"Not yet runnable: {days} days of collection < {MIN_COLLECTION_DAYS}."
        report.notices.append(line)
        report.blockers.append(f"not yet runnable, {days} days of collection")
        prospective_section = Section(
            title="Prospective (the result to quote)",
            text=line
            + " The retrospective versions below are rougher and are never quoted as the result.",
            metrics=[Metric("Days of collection", days, kind="count", headline=False)],
        )
    else:
        prospective_section = Section(
            title="Prospective (the result to quote)",
            text=_difference_text(prospective),
            metrics=[
                Metric("Days of collection", days, kind="count", headline=False),
                *_study_metrics("Prospective", prospective, headline=True),
            ],
        )
    report.sections = [
        prospective_section,
        Section(
            title="Retrospective (a): current postings' posted_at (survivorship bias)",
            text=(
                f"Filings: {len(from_db):,} from database events, {len(from_tsv):,} from "
                f"{len(formd_paths)} quarterly data sets; {len(retro_filers):,} by companies "
                "with an active board. " + _difference_text(retro)
            ),
            metrics=[
                Metric(
                    "Retrospective (a) coverage: filings with a reconstructed posting in the "
                    "12 weeks before",
                    share(retro.with_history, retro.filers),
                    retro.filers,
                    "share",
                    headline=False,
                ),
                Metric(
                    "Postings carrying a provider posted_at",
                    share(dated, total_postings),
                    total_postings,
                    "share",
                    headline=False,
                ),
                *_study_metrics("Retrospective (a)", retro, headline=False),
            ],
        ),
        Section(
            title="Retrospective (b): Wayback Machine snapshots of the board page",
            text=(
                f"Of {way.filers:,} filings, {way.supported:,} have a Greenhouse or Lever board; "
                f"{way.listed:,} have a CDX listing in the cache ({way.not_cached:,} not "
                f"fetched yet), {way.with_snapshots:,} have a snapshot in the 12 weeks and "
                f"{way.covered:,} are covered (a snapshot in at least {MIN_WAYBACK_WEEKS} of "
                "the 12 weeks)."
            ),
            metrics=[
                Metric(
                    "Retrospective (b) coverage: covered filings of those with a supported board",
                    share(way.covered, way.supported),
                    way.supported,
                    "share",
                    headline=False,
                ),
                *_study_metrics("Retrospective (b)", way.study, headline=False, control=False),
            ],
        ),
    ]
    report.queries = [
        Query(
            "Spike rule (company level, all families)",
            "\n".join(
                [
                    "week k before filing F: F - 7k days < opened_at <= F - 7(k - 1) days, k = 1..12",
                    "new7(k) = net-new postings opened in week k (no interns, no reposts,",
                    "          same fingerprint on the same board within 30 days = one)",
                    "baseline = max(mean(new7(9..12)), 0.5)",
                    "spike    = any k in 1..8 with new7(k) >= 3 and new7(k) / baseline >= 1.5",
                    "lead     = 7 * max(spiking k) days",
                ]
            ),
            language="text",
        )
    ]
    report.caveats = [
        (
            "Company level: all role families are counted together; tell's live hiring_velocity "
            "signal is per family, so this is a coarser rule than the one that alerts."
        ),
        (
            "Filings between day 60 and day 84 of collection have a baseline that reaches back "
            "before collection started (floored at 0.5), which makes a spike easier to reach."
        ),
        (
            "Retrospective (a) only sees postings that were still open when tell started watching "
            "or opened since: closed roles are invisible, so spikes are undercounted."
        ),
        (
            "Retrospective (b) dates an opening at the snapshot that first shows it; a gap between "
            "snapshots lumps several weeks of openings into one week. Snapshots exist for a "
            "minority of boards, and Ashby and SmartRecruiters pages list no jobs in their HTML."
        ),
        (
            "The control group is matched by open-posting decile at the median filing date; "
            "companies that file without telling the SEC (or file under another name tell did not "
            "link) sit in the control group."
        ),
        (
            "Once the prospective study is runnable, it is the result; the retrospective numbers "
            "are context only."
        ),
    ]
    return report
