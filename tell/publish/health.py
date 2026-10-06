"""Run-health per source (spec section 12 "Run-health in production"; section 9 health page).

For every source, from ``runs`` and ``events``:

``last_ok_at``
    Latest ``finished_at`` of a run with ``status = 'ok'`` (``null`` when none ever).
``success_rate_7d``
    ``ok / (ok + failed)`` over runs *started* in the last 7 days (``null`` with no
    finished run in the window; ``running`` rows are not counted).
``fetched_24h`` / ``inserted_24h``
    Sums over runs that finished in the last 24 hours.
``freshness_p95_h``
    95th percentile (linear interpolation, ``percentile_cont``) of
    ``observed_at - occurred_at`` in hours over the source's events observed in the
    last 24 hours, rounded to 0.1 h; ``null`` without events. First-poll baselines
    (``payload.first_poll``: postings already open when tell started watching a board,
    dated by their original ``posted_at``) are left out: they measure how old a job is,
    not how late tell saw it (ADR 0023).
``stale``
    The source has run before (at least one ``runs`` row) but has no successful run in
    the last 24 hours. Any stale source turns the health page's banner red and puts a
    red line at the top of the Slack digest.
``status``
    ``ok``, ``stale`` or ``never_run``. A source with no ``runs`` row at all (an optional
    collector such as ``github`` that the scheduled workflow does not run) is listed as
    ``never_run`` and is not stale, so an unscheduled source never paints the public page
    red; the dead-man step (Phase 10) is what fails a run for a silent scheduled source
    (ADR 0023).

Sources are the registered collectors (``discover_collectors``) plus tell's internal
jobs :data:`INTERNAL_JOBS` (``resolve``, ``score``, ``publish``), which write ``runs`` rows
under those source names. ``slack_digest`` and ``discovery`` runs are not health sources.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Float, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from tell.collectors.base import FIRST_POLL_KEY, discover_collectors, iso_utc
from tell.models import EventRow, Run

__all__ = [
    "FRESHNESS_PERCENTILE",
    "INTERNAL_JOBS",
    "STALE_AFTER",
    "Health",
    "SourceHealth",
    "compute_health",
    "freshness_p95_h",
    "health_sources",
]

INTERNAL_JOBS: tuple[str, ...] = ("resolve", "score", "publish")
"""tell's own pipeline steps, reported beside the collectors."""

STALE_AFTER = timedelta(hours=24)
FRESHNESS_PERCENTILE = 0.95
_WEEK = timedelta(days=7)


@dataclass(frozen=True)
class SourceHealth:
    """One row of ``health.json``."""

    source: str
    kind: str
    """``collector`` or ``job``."""
    last_ok_at: str | None
    success_rate_7d: float | None
    fetched_24h: int
    inserted_24h: int
    freshness_p95_h: float | None
    stale: bool
    status: str
    """``ok``, ``stale`` or ``never_run``."""


@dataclass(frozen=True)
class Health:
    """The whole health report at one instant."""

    generated_at: str
    sources: list[SourceHealth]
    freshness_p95_h: float | None
    """Across every collector's events in the last 24 hours (the Slack context line)."""

    @property
    def stale_sources(self) -> list[str]:
        return [s.source for s in self.sources if s.stale]

    @property
    def any_stale(self) -> bool:
        return any(s.stale for s in self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "stale_sources": self.stale_sources,
            "freshness_p95_h": self.freshness_p95_h,
            "sources": [asdict(s) for s in self.sources],
        }


def health_sources() -> list[tuple[str, str]]:
    """``[(source, kind)]``: registered collectors (sorted), then the internal jobs."""
    collectors = [(spec.source, "collector") for spec in discover_collectors()]
    return [*collectors, *((job, "job") for job in INTERNAL_JOBS)]


def _not_first_poll() -> Any:
    flag = EventRow.payload[FIRST_POLL_KEY].astext
    return or_(flag.is_(None), flag != "true")


def freshness_p95_h(
    session: Session, now: datetime, sources: Sequence[str] | None = None
) -> float | None:
    """p95 of ``observed_at - occurred_at`` (hours, 0.1) over events observed in the last 24h.

    ``sources`` narrows to those ``events.source`` values; ``None`` means every source
    except tell's own derived events (``source = 'tell'``). First-poll baselines are
    excluded (see the module docstring). Negative lags count as zero.
    """
    lag_h = func.greatest(func.extract("epoch", EventRow.observed_at - EventRow.occurred_at), 0)
    stmt = select(
        func.percentile_cont(FRESHNESS_PERCENTILE).within_group(cast(lag_h, Float) / 3600.0)
    ).where(
        EventRow.observed_at > now - STALE_AFTER,
        EventRow.observed_at <= now,
        _not_first_poll(),
    )
    stmt = (
        stmt.where(EventRow.source.in_(list(sources)))
        if sources is not None
        else stmt.where(EventRow.source != "tell")
    )
    value = session.execute(stmt).scalar_one_or_none()
    return None if value is None else round(float(value), 1)


def _run_stats(session: Session, now: datetime, names: Sequence[str]) -> dict[str, dict[str, Any]]:
    since_day = now - STALE_AFTER
    since_week = now - _WEEK
    ok = Run.status == "ok"
    finished_week = and_(Run.started_at > since_week, Run.status.in_(("ok", "failed")))
    in_day = and_(Run.finished_at.is_not(None), Run.finished_at > since_day)
    stmt = (
        select(
            Run.source,
            func.max(case((ok, Run.finished_at))),
            func.count().filter(finished_week),
            func.count().filter(and_(finished_week, ok)),
            func.coalesce(func.sum(case((in_day, Run.fetched), else_=0)), 0),
            func.coalesce(func.sum(case((in_day, Run.inserted), else_=0)), 0),
            func.count(),
        )
        .where(Run.source.in_(list(names)), Run.started_at <= now)
        .group_by(Run.source)
    )
    out: dict[str, dict[str, Any]] = {}
    for source, last_ok, finished, oks, fetched, inserted, total in session.execute(stmt):
        out[str(source)] = {
            "runs": int(total),
            "last_ok": last_ok,
            "rate": round(int(oks) / int(finished), 4) if finished else None,
            "fetched": int(fetched or 0),
            "inserted": int(inserted or 0),
        }
    return out


def compute_health(
    session: Session, now: datetime, sources: Sequence[tuple[str, str]] | None = None
) -> Health:
    """Health of every source at ``now`` (see the module docstring)."""
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be timezone-aware, got naive {now!r}"
        raise ValueError(msg)
    chosen = list(sources) if sources is not None else health_sources()
    stats = _run_stats(session, now, [name for name, _ in chosen])
    rows: list[SourceHealth] = []
    for name, kind in chosen:
        st = stats.get(name, {})
        last_ok: datetime | None = st.get("last_ok")
        ever_ran = int(st.get("runs", 0)) > 0
        stale = ever_ran and (last_ok is None or last_ok < now - STALE_AFTER)
        rows.append(
            SourceHealth(
                source=name,
                kind=kind,
                last_ok_at=iso_utc(last_ok) if last_ok is not None else None,
                success_rate_7d=st.get("rate"),
                fetched_24h=int(st.get("fetched", 0)),
                inserted_24h=int(st.get("inserted", 0)),
                freshness_p95_h=(
                    freshness_p95_h(session, now, [name]) if kind == "collector" else None
                ),
                stale=stale,
                status="stale" if stale else ("ok" if ever_ran else "never_run"),
            )
        )
    collectors = [name for name, kind in chosen if kind == "collector"]
    return Health(
        generated_at=iso_utc(now),
        sources=rows,
        freshness_p95_h=freshness_p95_h(session, now, collectors) if collectors else None,
    )
