"""Run log readouts: the latest run line per source and the weekly operations line.

``tell runlog --source X`` prints :func:`run_line` of the source's latest run: the
spec section 1 JSON line (``run_id, source, started_at, finished_at, fetched,
inserted, skipped, errors``) plus ``status``; ``ingest.yml`` appends it to the step
summary.

``tell runlog --week`` prints :func:`ops_line`, the spec section 12 / 13.4 operations
line, over the trailing 7 days:

``weeks_unattended``
    Spec 13.4 counts "days since the last manual intervention ... needed to keep ingest
    green, from the runs table". The runs table cannot see a human, so the proxy is the
    dead-man rule: an intervention was needed whenever a scheduled source
    (:data:`tell.ops.deadman.SCHEDULED_SOURCES`) went more than 24 hours without a
    successful run. The unattended period starts at the successful run that ended the
    most recent such gap (the first run ever when there was none) and is ``0`` while a
    scheduled source is silent right now. ``null`` before any scheduled source has run.
``success_rate`` / ``success_rate_by_source``
    ``ok / (ok + failed)`` over runs started in the period, overall and per source
    (sources with no finished run in the period are left out; ``running`` rows never
    count).
``freshness_p95_h``
    Spec section 12's p95 of ``observed_at - occurred_at`` over the last 24 hours of
    collector events (:func:`tell.publish.health.freshness_p95_h`).
``companies_monitored`` / ``postings_tracked``
    Companies with an ``active`` board or a CIK; postings with ``closed_at IS NULL``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.models import AtsBoard, Company, Posting, Run
from tell.ops.deadman import SCHEDULED_SOURCES, SILENT_AFTER
from tell.publish.health import freshness_p95_h

__all__ = [
    "OPS_PERIOD",
    "RUN_LINE_KEYS",
    "OpsLine",
    "companies_monitored",
    "latest_run",
    "ops_line",
    "postings_tracked",
    "run_line",
    "success_rates",
    "weeks_unattended",
]

RUN_LINE_KEYS: tuple[str, ...] = (
    "run_id",
    "source",
    "started_at",
    "finished_at",
    "fetched",
    "inserted",
    "skipped",
    "errors",
)
"""The spec section 1 run-log keys, in that order."""

OPS_PERIOD = timedelta(days=7)
_WEEK_S = timedelta(days=7).total_seconds()


def run_line(run: Run) -> dict[str, Any]:
    """The run's JSON log line (spec section 1 keys) plus its ``status``."""
    return {
        "run_id": run.run_id,
        "source": run.source,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at) if run.finished_at is not None else None,
        "fetched": int(run.fetched or 0),
        "inserted": int(run.inserted or 0),
        "skipped": int(run.skipped or 0),
        "errors": int(run.errors or 0),
        "status": run.status,
    }


def latest_run(session: Session, source: str) -> Run | None:
    """The source's most recently started run, or ``None`` when it never ran."""
    stmt = (
        select(Run)
        .where(Run.source == source)
        .order_by(Run.started_at.desc(), Run.run_id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def success_rates(
    session: Session, since: datetime, now: datetime
) -> tuple[float | None, dict[str, float]]:
    """``(overall, {source: rate})`` over runs started in ``(since, now]``."""
    finished = Run.status.in_(("ok", "failed"))
    stmt = (
        select(Run.source, func.count(), func.count().filter(Run.status == "ok"))
        .where(Run.started_at > since, Run.started_at <= now, finished)
        .group_by(Run.source)
        .order_by(Run.source)
    )
    by_source: dict[str, float] = {}
    total = oks = 0
    for source, count, ok in session.execute(stmt):
        by_source[str(source)] = round(int(ok) / int(count), 4)
        total += int(count)
        oks += int(ok)
    return (round(oks / total, 4) if total else None), by_source


def weeks_unattended(
    session: Session, now: datetime, sources: Sequence[str] = SCHEDULED_SOURCES
) -> tuple[float | None, datetime | None]:
    """``(weeks, since)``: the unattended period at ``now`` (see the module docstring)."""
    rows = session.execute(
        select(Run.source, Run.started_at, Run.finished_at, Run.status)
        .where(Run.source.in_(list(sources)), Run.started_at <= now)
        .order_by(Run.source, Run.started_at)
    ).all()
    first_start: dict[str, datetime] = {}
    oks: dict[str, list[datetime]] = defaultdict(list)
    for source, started, finished, status in rows:
        first_start.setdefault(str(source), started)
        if status == "ok" and finished is not None and finished <= now:
            oks[str(source)].append(finished)
    if not first_start:
        return None, None
    anchor = min(first_start.values())
    for source, started in first_start.items():
        previous = started
        for finished in sorted(oks[source]):
            if finished - previous > SILENT_AFTER:
                anchor = max(anchor, finished)
            previous = max(previous, finished)
        if now - previous > SILENT_AFTER:
            return 0.0, now
    return round((now - anchor).total_seconds() / _WEEK_S, 1), anchor


def companies_monitored(session: Session) -> int:
    """Companies with an ``active`` ATS board or a CIK (spec 13.4)."""
    has_board = exists().where(AtsBoard.company_id == Company.id, AtsBoard.status == "active")
    stmt = select(func.count()).select_from(Company).where(or_(has_board, Company.cik.is_not(None)))
    return int(session.scalar(stmt) or 0)


def postings_tracked(session: Session) -> int:
    """Postings still open (``closed_at IS NULL``)."""
    stmt = select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
    return int(session.scalar(stmt) or 0)


@dataclass(frozen=True)
class OpsLine:
    """The weekly operations line (spec sections 12 and 13.4)."""

    as_of: str
    period_days: int
    weeks_unattended: float | None
    unattended_since: str | None
    success_rate: float | None
    success_rate_by_source: dict[str, float]
    freshness_p95_h: float | None
    companies_monitored: int
    postings_tracked: int

    @property
    def summary(self) -> str:
        """One human-readable line for the weekly summary."""
        weeks = "n/a" if self.weeks_unattended is None else f"{self.weeks_unattended:g}"
        rate = "n/a" if self.success_rate is None else f"{self.success_rate * 100:.1f}%"
        fresh = "n/a" if self.freshness_p95_h is None else f"{self.freshness_p95_h:g}h"
        return (
            f"{weeks} weeks unattended · success rate {rate} ({self.period_days}d) · "
            f"freshness p95 {fresh} · {self.companies_monitored:,} companies monitored · "
            f"{self.postings_tracked:,} postings tracked"
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "summary": self.summary}


def ops_line(session: Session, now: datetime, period: timedelta = OPS_PERIOD) -> OpsLine:
    """The operations line at ``now`` over the trailing ``period``. Reads only."""
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be timezone-aware, got naive {now!r}"
        raise ValueError(msg)
    overall, by_source = success_rates(session, now - period, now)
    weeks, since = weeks_unattended(session, now)
    return OpsLine(
        as_of=iso_utc(now),
        period_days=round(period.total_seconds() / 86400),
        weeks_unattended=weeks,
        unattended_since=iso_utc(since) if since is not None else None,
        success_rate=overall,
        success_rate_by_source=by_source,
        freshness_p95_h=freshness_p95_h(session, now),
        companies_monitored=companies_monitored(session),
        postings_tracked=postings_tracked(session),
    )
