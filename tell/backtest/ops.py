"""Spec 13.4 operations and resolution.

* **Weeks unattended.** Days since the last manual intervention, divided by 7. An
  intervention is a ``runs`` row whose ``notes`` carry ``"manual": true``
  (:data:`tell.collectors.base.MANUAL_NOTE_KEY`). The run lifecycle writes that marker on
  every run recorded under ``tell --manual`` or with ``TELL_RUN_MANUAL=true`` in the
  environment, which ``ingest.yml`` exports on ``workflow_dispatch`` only (a hand-dispatched
  run after a fix) and never on the schedule; the period starts at the latest such row (its
  ``finished_at``, else ``started_at``) and, when there is none, at the first run ever. The dead-man proxy of ``tell runlog --week``
  (:func:`tell.ops.runlog.weeks_unattended`, which infers an intervention from a gap of more
  than 24 hours without a successful scheduled run) is printed beside it.
* **Weeks since collection started.** From the first ``runs`` row.
* **Success rate.** ``ok / (ok + failed)`` over runs started in the trailing
  :data:`PERIOD` (``running`` rows never count), overall and by source. Runs of
  :data:`tell.ops.runlog.NON_PIPELINE_SOURCES` (``intake``, ``edgar_backfill``) are left
  out of every query here: they run in ``tell-private`` and fail on a rotated sheet URL,
  not on ingestion.
* **Freshness p95.** Spec section 12's freshness as ADR 0043 reads it, from
  :func:`tell.publish.health.freshness`: tell's lag, from the earliest moment tell could
  have seen an event to the moment it recorded it, over the collector events observed in
  the 24 hours ending at the evaluation instant. First looks (a board's or a company's
  Google News query's first poll, a company feed's archive read on discovery, filings and
  other events visible before tell's first run of their source), first-seen-only events (no
  source timestamp) and backfills are left out and printed beside N, with the numbers per
  source (:func:`tell.publish.health.freshness_by_source`).
* **Companies monitored / postings tracked.** :func:`tell.ops.runlog.companies_monitored`
  and :func:`tell.ops.runlog.postings_tracked`.
* **Gold-set precision and recall.** The measured table in
  ``docs/methods/entity-resolution.md`` (written from the CI gold-set test,
  ``tests/test_resolve.py``); this report quotes the ``names`` rows, the headline metric.
* **Review-queue rate.** ``review_queue`` items created in the period per 1,000 events
  observed in the period.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.backtest.common import REPO_ROOT, Metric, Report, Section, Table, share
from tell.collectors.base import MANUAL_NOTE_KEY, iso_utc
from tell.models import EventRow, ReviewQueueItem, Run
from tell.ops.runlog import NON_PIPELINE_SOURCES, companies_monitored, postings_tracked
from tell.ops.runlog import weeks_unattended as deadman_weeks_unattended
from tell.publish.health import Freshness, freshness, freshness_by_source

__all__ = [
    "ENTITY_RESOLUTION_DOC",
    "FRESHNESS_LABEL",
    "NAME",
    "PERIOD",
    "GoldRow",
    "build_report",
    "first_run_at",
    "freshness_n",
    "manual_anchor",
    "parse_gold_table",
    "review_queue_rate",
    "success_by_source",
]

NAME = "ops"
SCRIPT = "tell/backtest/ops.py"
PERIOD = timedelta(days=28)
"""The trailing period for success rate and review-queue rate (four weeks)."""

ENTITY_RESOLUTION_DOC = REPO_ROOT / "docs" / "methods" / "entity-resolution.md"

FRESHNESS_LABEL = (
    "Freshness p95, hours (lag from the earliest moment tell could have seen the event to the "
    "moment it was recorded; first looks, first-seen-only events and backfills excluded)"
)
"""The headline freshness line; ``docs/launch/numbers.yaml`` quotes it by this exact label."""

_WEEK_S = timedelta(days=7).total_seconds()
_GOLD_ROW = re.compile(
    r"^\|\s*(?P<set>[^|]*names[^|]*)\|\s*(?P<n>\d+)\s*\|[^|]*\|[^|]*\|"
    r"\s*(?P<precision>[0-9.]+)\s*\|\s*(?P<recall>[0-9.]+)\s*\|"
)
_MEASURED_ON = re.compile(r"Measured (?P<day>\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class GoldRow:
    """One ``names`` row of the measured gold-set table."""

    label: str
    n: int
    precision: float
    recall: float


def parse_gold_table(path: Path = ENTITY_RESOLUTION_DOC) -> tuple[list[GoldRow], str | None]:
    """``(rows, measured_on)`` from the methods page; ``([], None)`` when it is missing."""
    if not path.is_file():
        return [], None
    text = path.read_text(encoding="utf-8")
    rows = [
        GoldRow(
            label=m.group("set").strip().replace("`", ""),
            n=int(m.group("n")),
            precision=float(m.group("precision")),
            recall=float(m.group("recall")),
        )
        for m in (_GOLD_ROW.match(line) for line in text.splitlines())
        if m is not None
    ]
    measured = _MEASURED_ON.search(text)
    return rows, measured.group("day") if measured else None


def _pipeline_runs() -> Any:
    return Run.source.not_in(NON_PIPELINE_SOURCES)


def first_run_at(session: Session, now: datetime) -> datetime | None:
    """Start of the first pipeline run at or before ``now`` (``None`` with no run yet)."""
    return session.scalar(
        select(func.min(Run.started_at)).where(Run.started_at <= now, _pipeline_runs())
    )


def manual_anchor(session: Session, now: datetime) -> tuple[datetime | None, bool]:
    """``(since, manual)``: the latest manual run's end, else the first run's start."""
    manual = session.execute(
        select(func.coalesce(Run.finished_at, Run.started_at))
        .where(Run.notes[MANUAL_NOTE_KEY].astext == "true", Run.started_at <= now, _pipeline_runs())
        .order_by(func.coalesce(Run.finished_at, Run.started_at).desc())
        .limit(1)
    ).scalar_one_or_none()
    if manual is not None:
        return min(manual, now), True
    return first_run_at(session, now), False


def success_by_source(
    session: Session, since: datetime, now: datetime
) -> list[tuple[str, int, int]]:
    """``[(source, finished, ok)]`` over runs started in ``(since, now]``."""
    stmt = (
        select(Run.source, func.count(), func.count().filter(Run.status == "ok"))
        .where(
            Run.started_at > since,
            Run.started_at <= now,
            Run.status.in_(("ok", "failed")),
            _pipeline_runs(),
        )
        .group_by(Run.source)
        .order_by(Run.source)
    )
    return [(str(s), int(c), int(o)) for s, c, o in session.execute(stmt)]


def freshness_n(session: Session, now: datetime) -> int:
    """Events behind the freshness p95 (:func:`tell.publish.health.freshness`)."""
    return freshness(session, now).n


def review_queue_rate(session: Session, since: datetime, now: datetime) -> tuple[int, int]:
    """``(items, events)`` created / observed in ``(since, now]``."""
    items = session.scalar(
        select(func.count())
        .select_from(ReviewQueueItem)
        .where(ReviewQueueItem.created_at > since, ReviewQueueItem.created_at <= now)
    )
    events = session.scalar(
        select(func.count())
        .select_from(EventRow)
        .where(EventRow.observed_at > since, EventRow.observed_at <= now)
    )
    return int(items or 0), int(events or 0)


def _weeks(since: datetime | None, now: datetime) -> float | None:
    return None if since is None else max((now - since).total_seconds(), 0.0) / _WEEK_S


def build_report(
    session: Session, now: datetime, *, gold_doc: Path = ENTITY_RESOLUTION_DOC
) -> Report:
    """The 13.4 report at ``now``."""
    since, manual = manual_anchor(session, now)
    first_run = first_run_at(session, now)
    proxy_weeks, _ = deadman_weeks_unattended(session, now)
    by_source = success_by_source(session, now - PERIOD, now)
    finished = sum(c for _, c, _ in by_source)
    oks = sum(o for _, _, o in by_source)
    fresh = freshness(session, now)
    fresh_by_source = freshness_by_source(session, now)
    items, events = review_queue_rate(session, now - PERIOD, now)
    gold, measured_on = parse_gold_table(gold_doc)
    days = PERIOD.days

    anchor_text = (
        f"the last run marked manual ({iso_utc(since)})"
        if manual and since is not None
        else (f"the first run ({iso_utc(since)})" if since is not None else "no run yet")
    )
    report = Report(
        name=NAME,
        title="Operations and resolution",
        question=(
            "Does tell run unattended, reliably and fresh, at what coverage, and how well does "
            "it resolve names to companies?"
        ),
        cohort=(
            f"Every pipeline runs row up to the evaluation instant (the nightly intake "
            f"reader's runs are left out); success rate and review-queue rate over the "
            f"trailing {days} days; freshness over the collector events observed in the 24 hours "
            "ending at the evaluation instant, as tell's lag, with first looks, first-seen-only "
            "events and backfills left out and counted (ADR 0043). "
            "Cohort N is the number of finished runs in the period."
        ),
        n=finished,
        as_of=now.date(),
        generated_at=now,
        script=SCRIPT,
    )
    report.sections = [
        Section(
            title="Unattended operation",
            text=f"The unattended period starts at {anchor_text}.",
            metrics=[
                Metric(
                    "Weeks unattended (days since the last manual intervention needed to keep "
                    "ingest green, divided by 7)",
                    _weeks(since, now),
                    kind="weeks",
                ),
                Metric(
                    "Weeks since collection started (period covered by the report)",
                    _weeks(first_run, now),
                    kind="weeks",
                ),
                Metric(
                    "Dead-man proxy (tell runlog --week): weeks since the last 24-hour gap",
                    proxy_weeks,
                    kind="weeks",
                    headline=False,
                ),
            ],
        ),
        Section(
            title="Reliability and freshness",
            metrics=[
                Metric(
                    "Success rate ok / (ok + failed), percent, overall",
                    share(oks, finished),
                    finished,
                    "share",
                ),
                Metric(FRESHNESS_LABEL, fresh.p95_h, fresh.n, "hours"),
                *_freshness_exclusions(fresh),
            ],
            table=Table(
                headers=["source", "finished runs", "ok", "success rate"],
                rows=[
                    [
                        source,
                        f"{count:,}",
                        f"{ok:,}",
                        Metric(f"success {source}", share(ok, count), count, "share", False),
                    ]
                    for source, count, ok in by_source
                ],
            ),
        ),
        _freshness_section(fresh_by_source),
        Section(
            title="Coverage",
            metrics=[
                Metric(
                    "Companies monitored (active board or a CIK)",
                    companies_monitored(session),
                    kind="count",
                ),
                Metric(
                    "Postings tracked (closed_at is null)",
                    postings_tracked(session),
                    kind="count",
                ),
            ],
        ),
        _resolution_section(gold, measured_on, items, events),
    ]
    report.caveats = [
        (
            "Weeks unattended depends on interventions being marked (runs.notes.manual = true, "
            "written for every run under `tell --manual` or TELL_RUN_MANUAL=true, which ingest.yml "
            "sets on workflow_dispatch); a fix applied without a marked run is invisible, so the "
            "dead-man proxy is printed beside it."
        ),
        "Success rate counts runs, not boards: one failed vendor leg is one failed run.",
        (
            "Freshness p95 is a 24-hour window ending at the evaluation instant; a quiet day has a "
            "small N. The events left out are counted beside it."
        ),
        (
            "Freshness is tell's lag, not the age of what tell reads (ADR 0043). It runs from the "
            "earliest moment tell could have seen an event (the source's own timestamp; for EDGAR "
            "the daily index, filing date + 26 h; moved to tell's previous poll of the same board "
            "or Google News query when that poll did not list it) to the moment tell recorded it. "
            "Reports written before ADR 0043 took SPEC section 12's observed_at - occurred_at "
            "literally and measured the age of company-blog archives read on discovery (23298.1 h "
            "on 2026-10-19); do not quote them."
        ),
        (
            "Sources run on different clocks: boards are polled every run, while Google News "
            "reaches the long tail of companies once a week, so news dominates the pooled p95; "
            "the table gives each source's own number."
        ),
        (
            "Company feeds keep no poll history: a feed item dated after tell discovered the feed "
            "counts from its own date, so a post that appears days after its date overstates the "
            "lag."
        ),
        (
            "Postings tracked and companies monitored are counts at the instant the report runs, "
            "not at the as-of date, when the report is regenerated for a past date."
        ),
        (
            "Gold-set numbers come from the methods page, measured by the CI test on the gold set; "
            "they describe the matcher, not the live link quality."
        ),
    ]
    return report


def _freshness_exclusions(fresh: Freshness) -> list[Metric]:
    """The counts printed beside the freshness p95 (never headline numbers)."""
    return [
        Metric(
            "Collector events observed in the 24-hour freshness window",
            fresh.observed,
            kind="count",
            headline=False,
        ),
        Metric(
            "Left out of freshness, first looks (tell was not yet watching the board, company "
            "feed, Google News query or source)",
            fresh.first_look,
            kind="count",
            headline=False,
        ),
        Metric(
            "Left out of freshness, first-seen-only (no source timestamp: tell dated the event "
            "when it saw it)",
            fresh.first_seen_only,
            kind="count",
            headline=False,
        ),
        Metric(
            "Left out of freshness, backfills (archive loads and widened Google News windows)",
            fresh.backfill,
            kind="count",
            headline=False,
        ),
    ]


def _freshness_section(by_source: dict[str, Freshness]) -> Section:
    return Section(
        title="Freshness by source",
        text=(
            "The same 24 hours, per source: events observed, events counted, events left out "
            "and the p95 of tell's lag (ADR 0043)."
        ),
        table=Table(
            headers=[
                "source",
                "events observed",
                "counted",
                "first looks",
                "first-seen-only",
                "backfills",
                "freshness p95",
            ],
            rows=[
                [
                    source,
                    f"{fresh.observed:,}",
                    f"{fresh.n:,}",
                    f"{fresh.first_look:,}",
                    f"{fresh.first_seen_only:,}",
                    f"{fresh.backfill:,}",
                    Metric(f"freshness p95 {source}", fresh.p95_h, fresh.n, "hours", False),
                ]
                for source, fresh in by_source.items()
            ],
        ),
    )


def _resolution_section(
    gold: list[GoldRow], measured_on: str | None, items: int, events: int
) -> Section:
    rows: list[list[Metric | str]] = [
        [
            row.label,
            Metric(f"precision {row.label}", row.precision, row.n, "share", headline=False),
            Metric(f"recall {row.label}", row.recall, row.n, "share", headline=False),
        ]
        for row in gold
    ]
    source = (
        f"Gold set measured {measured_on}, from docs/methods/entity-resolution.md."
        if measured_on
        else "No measured gold-set table found in docs/methods/entity-resolution.md."
    )
    rate = (items / events * 1000) if events else None
    return Section(
        title="Resolution",
        text=source,
        metrics=[
            Metric(
                f"Review-queue items per 1,000 events, trailing {PERIOD.days} days",
                rate,
                events,
                "number",
            ),
            Metric("Review-queue items created in the period", items, kind="count", headline=False),
        ],
        table=Table(headers=["gold set, names", "precision", "recall"], rows=rows)
        if rows
        else None,
    )
