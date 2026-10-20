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
    95th percentile (linear interpolation, ``percentile_cont``) of tell's lag in hours over
    the source's events observed in the last 24 hours, rounded to 0.1 h; ``null`` without
    a counted event. The lag is ``observed_at`` minus the earliest moment tell could have
    seen the event, and events whose age is not tell's lag are left out (ADR 0043, which
    extends ADR 0023's first-poll rule): see :func:`freshness`.
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

Freshness (ADR 0043)
====================

SPEC section 12 writes freshness as ``observed_at - occurred_at``. Taken literally that
measures how old a document is when tell first reads it: the first dated operations report
printed a p95 of 23,298 h because company blogs read on discovery carry posts from 2015
on. tell's lag is measured instead, from the earliest moment tell could have seen the event:

* the source's own timestamp, ``occurred_at``; for EDGAR, which lists a filing in its
  daily index only from about 02:02 UTC the day after its filing date (``occurred_at`` is
  that date at 00:00 UTC), the filing date plus :data:`EDGAR_INDEX_LAG`;
* moved forward to tell's previous look at the same place when that look is on record and
  lists the place in full, so the event was not there yet: the board's previous poll
  (``posting_observations``) for an ATS event, tell's previous Google News query for the
  company (``runs.notes.polled``) for a Google News item.

Left out, and counted (:class:`Freshness`):

``first_look``
    tell was not watching yet, so the event's age is not tell's lag: a board's first poll
    (``payload.first_poll``, ADR 0023) or any ATS event without an earlier poll of its
    board, a Google News item without an earlier query for its company, a company-feed
    item dated before tell discovered the feed (``company_feeds.created_at``: discovery
    reads the feed's whole archive), and an EDGAR filing, or any other source's event, that
    became visible before tell's first run of that source.
``first_seen_only``
    No source timestamp: tell dated the event when it saw it (``occurred_at =
    observed_at``: ``job_updated``, ``job_closed``, postings without a ``posted_at``,
    keywords of a re-worded posting), so its lag is zero by construction.
``backfill``
    Loaded from an archive or a deliberately widened look-back: ``payload.backfill`` (the
    quarterly Form D data sets of ``tell backfill formd``) and Google News items of news
    runs whose window was wider than the scheduled ``when:7d`` (``runs.notes.when_days``,
    the SPEC 13.1 cohort fill).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    Text,
    and_,
    case,
    column,
    func,
    null,
    select,
    true,
    union_all,
    values,
)
from sqlalchemy.orm import Session

from tell.collectors.base import FIRST_POLL_KEY, discover_collectors, iso_utc
from tell.collectors.edgar_formd import SOURCE as EDGAR_SOURCE
from tell.collectors.news import DEFAULT_WHEN_DAYS, GOOGLE_NEWS_HOST
from tell.collectors.news import SOURCE as NEWS_SOURCE
from tell.models import (
    ATS_VENDORS,
    AtsBoard,
    CompanyFeed,
    EventRow,
    Posting,
    PostingObservation,
    Run,
)

__all__ = [
    "BACKFILL",
    "BACKFILL_KEY",
    "EDGAR_INDEX_LAG",
    "FIRST_LOOK",
    "FIRST_SEEN_ONLY",
    "FRESHNESS_PERCENTILE",
    "INTERNAL_JOBS",
    "STALE_AFTER",
    "Freshness",
    "FreshnessEvent",
    "Health",
    "SourceHealth",
    "compute_health",
    "freshness",
    "freshness_by_source",
    "freshness_events",
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


BACKFILL_KEY = "backfill"
"""``payload.backfill``: an event loaded from an archive (the quarterly Form D data sets)."""

EDGAR_INDEX_LAG = timedelta(hours=26)
"""EDGAR lists a filing in its daily form index from about 02:02 UTC the day after the filing
date, so no run sees it before filing date 00:00 UTC + 26 h (SPEC 13.1's ``alert_at`` rule,
:data:`tell.backtest.speed_vs_press.INDEX_LAG_HOURS`)."""

FIRST_LOOK = "first_look"
FIRST_SEEN_ONLY = "first_seen_only"
BACKFILL = "backfill"
"""Why an event is left out of freshness (module docstring, "Freshness")."""

_GOOGLE_NEWS_PREFIX = f"https://{GOOGLE_NEWS_HOST}/"
_ATS_SOURCES = frozenset(ATS_VENDORS)


@dataclass(frozen=True)
class Freshness:
    """Freshness over one 24-hour window: the p95 of tell's lag, its N, what was left out."""

    p95_h: float | None
    """95th percentile of the lag in hours, rounded to 0.1 h; ``None`` when ``n`` is 0."""
    n: int
    """Events behind the p95."""
    first_look: int = 0
    first_seen_only: int = 0
    backfill: int = 0

    @property
    def excluded(self) -> int:
        return self.first_look + self.first_seen_only + self.backfill

    @property
    def observed(self) -> int:
        """Every event observed in the window, counted or left out."""
        return self.n + self.excluded


@dataclass(frozen=True)
class FreshnessEvent:
    """One event observed in the window: why it is left out of freshness, or its lag."""

    event_id: str
    source: str
    reason: str | None
    """``None`` when the event counts; else :data:`FIRST_LOOK`, :data:`FIRST_SEEN_ONLY` or
    :data:`BACKFILL`."""
    lag_h: float | None
    """tell's lag in hours (unrounded) for a counted event; ``None`` for one left out."""


@dataclass(frozen=True)
class _Seen:
    """One event observed in the window, with what classifying it needs."""

    event_id: str
    source: str
    occurred: datetime
    observed: datetime
    company_id: int | None
    run_id: str
    backfill: bool
    first_poll: bool
    board: str | None
    """``payload.board_identifier`` (ATS events)."""
    query: str | None
    """``payload.query`` (news items: the Google News URL or the company feed's URL)."""

    @property
    def google(self) -> bool:
        return self.source == NEWS_SOURCE and (self.query or "").startswith(_GOOGLE_NEWS_PREFIX)

    @property
    def visible(self) -> datetime:
        """The source's own timestamp of the moment the event could be seen."""
        if self.source == EDGAR_SOURCE:
            return self.occurred + EDGAR_INDEX_LAG
        return self.occurred


def _flag(key: str) -> Any:
    """``payload.<key>`` is ``true`` (never ``NULL``)."""
    return EventRow.payload[key].astext.is_not_distinct_from("true")


def _window(session: Session, now: datetime, sources: Sequence[str] | None) -> list[_Seen]:
    scope = EventRow.source.in_(list(sources)) if sources is not None else EventRow.source != "tell"
    stmt = select(
        EventRow.event_id,
        EventRow.source,
        EventRow.occurred_at,
        EventRow.observed_at,
        EventRow.company_id,
        EventRow.run_id,
        _flag(BACKFILL_KEY),
        _flag(FIRST_POLL_KEY),
        EventRow.payload["board_identifier"].astext,
        EventRow.payload["query"].astext,
    ).where(EventRow.observed_at > now - STALE_AFTER, EventRow.observed_at <= now, scope)
    return [_Seen(*row) for row in session.execute(stmt)]


def _previous_polls(
    session: Session, keys: set[tuple[str, str, datetime]]
) -> dict[tuple[str, str, datetime], datetime]:
    """``(vendor, identifier, instant) -> the board's latest poll before that instant``.

    Every poll writes a ``posting_observations`` row for each posting it lists, so the
    board's previous poll is the newest observation of any of its postings before the
    instant (one index probe per posting). Boards with no earlier observation are absent.
    """
    if not keys:
        return {}
    polls = values(
        column("vendor", Text),
        column("identifier", Text),
        column("at", DateTime(timezone=True)),
        name="polls",
    ).data(sorted(keys))
    latest = (
        select(PostingObservation.observed_at)
        .where(
            PostingObservation.posting_id == Posting.id, PostingObservation.observed_at < polls.c.at
        )
        .order_by(PostingObservation.observed_at.desc())
        .limit(1)
        .lateral("latest")
    )
    stmt = (
        select(polls.c.vendor, polls.c.identifier, polls.c.at, func.max(latest.c.observed_at))
        .select_from(polls)
        .join(
            AtsBoard,
            and_(AtsBoard.vendor == polls.c.vendor, AtsBoard.identifier == polls.c.identifier),
        )
        .join(Posting, Posting.board_id == AtsBoard.id)
        .join(latest, true())
        .group_by(polls.c.vendor, polls.c.identifier, polls.c.at)
    )
    return {
        (str(vendor), str(identifier), at): found
        for vendor, identifier, at, found in session.execute(stmt)
    }


def _news_polls(
    session: Session, company_ids: set[int], since: datetime, now: datetime
) -> dict[int, list[datetime]]:
    """``company id -> starts of the news runs that queried Google News for it``.

    Read from ``runs.notes.polled`` (company id -> poll instant, written when Google News
    answered). Only what the window ``(since, now]`` needs: each company's latest poll at or
    before ``since`` and every poll after it.
    """
    if not company_ids:
        return {}
    notes = Run.notes["polled"]
    keys = (
        func.jsonb_object_keys(case((func.jsonb_typeof(notes) == "object", notes), else_=null()))
        .table_valued("key")
        .render_derived(name="polled_key")
    )
    polls = (
        select(keys.c.key.label("company"), Run.started_at.label("at"))
        .select_from(Run)
        .join(keys, true())
        .where(
            Run.source == NEWS_SOURCE,
            Run.started_at <= now,
            keys.c.key.in_([str(company_id) for company_id in sorted(company_ids)]),
        )
        .cte("news_polls")
    )
    stmt = union_all(
        select(polls.c.company, func.max(polls.c.at))
        .where(polls.c.at <= since)
        .group_by(polls.c.company),
        select(polls.c.company, polls.c.at).where(polls.c.at > since),
    )
    found: dict[int, list[datetime]] = {}
    for company, at in session.execute(stmt):
        found.setdefault(int(company), []).append(at)
    return found


def _widened_runs(session: Session, run_ids: set[str]) -> set[str]:
    """News runs among ``run_ids`` whose Google News window was wider than ``when:7d``."""
    if not run_ids:
        return set()
    stmt = select(Run.run_id, Run.notes["when_days"]).where(
        Run.source == NEWS_SOURCE, Run.run_id.in_(sorted(run_ids))
    )
    return {
        str(run_id)
        for run_id, days in session.execute(stmt)
        if isinstance(days, int | float) and not isinstance(days, bool) and days > DEFAULT_WHEN_DAYS
    }


def _feeds_discovered(session: Session, urls: set[str]) -> dict[str, datetime]:
    if not urls:
        return {}
    stmt = select(CompanyFeed.feed_url, CompanyFeed.created_at).where(
        CompanyFeed.feed_url.in_(sorted(urls))
    )
    return {str(url): created for url, created in session.execute(stmt)}


def _first_runs(session: Session, sources: set[str]) -> dict[str, datetime]:
    if not sources:
        return {}
    stmt = (
        select(Run.source, func.min(Run.started_at))
        .where(Run.source.in_(sorted(sources)))
        .group_by(Run.source)
    )
    return {str(source): first for source, first in session.execute(stmt)}


def freshness_events(
    session: Session, now: datetime, sources: Sequence[str] | None = None
) -> list[FreshnessEvent]:
    """Every event observed in the 24 hours before ``now``: why it is left out, or its lag.

    The per-event audit behind :func:`freshness` (module docstring, "Freshness"); ``sources``
    as there.
    """
    since = now - STALE_AFTER
    seen = _window(session, now, sources)
    open_ = [s for s in seen if not s.backfill and not s.first_poll]
    dated = [s for s in open_ if s.occurred != s.observed]
    boards = [s for s in dated if s.source in _ATS_SOURCES and s.board]
    google = [s for s in dated if s.google]
    feeds = [s for s in dated if s.source == NEWS_SOURCE and not s.google and s.query]
    others = [s for s in dated if s.source not in _ATS_SOURCES and s.source != NEWS_SOURCE]
    previous = _previous_polls(session, {(s.source, s.board or "", s.observed) for s in boards})
    news_polls = _news_polls(
        session, {s.company_id for s in google if s.company_id is not None}, since, now
    )
    widened = _widened_runs(session, {s.run_id for s in open_ if s.google})
    discovered = _feeds_discovered(session, {s.query or "" for s in feeds})
    first_runs = _first_runs(session, {s.source for s in others})

    out: list[FreshnessEvent] = []
    for s in seen:
        looked: datetime | None = None
        if s.backfill or (s.google and s.run_id in widened):
            reason: str | None = BACKFILL
        elif s.first_poll:
            reason = FIRST_LOOK
        elif s.occurred == s.observed:
            reason = FIRST_SEEN_ONLY
        elif s.source in _ATS_SOURCES:
            looked = previous.get((s.source, s.board or "", s.observed))
            reason = FIRST_LOOK if looked is None else None
        elif s.google:
            polls = news_polls.get(s.company_id, []) if s.company_id is not None else []
            looked = max((at for at in polls if at < s.observed), default=None)
            reason = FIRST_LOOK if looked is None else None
        else:
            start = (
                discovered.get(s.query or "")
                if s.source == NEWS_SOURCE
                else first_runs.get(s.source)
            )
            reason = FIRST_LOOK if start is None or s.visible < start else None
        lag_h = None
        if reason is None:
            seen_from = s.visible if looked is None else max(s.visible, looked)
            lag_h = max((s.observed - seen_from).total_seconds(), 0.0) / 3600.0
        out.append(FreshnessEvent(s.event_id, s.source, reason, lag_h))
    return out


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (PostgreSQL ``percentile_cont``)."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _summarize(classified: Sequence[FreshnessEvent]) -> Freshness:
    lags = [c.lag_h for c in classified if c.reason is None and c.lag_h is not None]
    p95 = _percentile(lags, FRESHNESS_PERCENTILE)
    reasons = [c.reason for c in classified]
    return Freshness(
        p95_h=None if p95 is None else round(p95, 1),
        n=len(lags),
        first_look=reasons.count(FIRST_LOOK),
        first_seen_only=reasons.count(FIRST_SEEN_ONLY),
        backfill=reasons.count(BACKFILL),
    )


def freshness(session: Session, now: datetime, sources: Sequence[str] | None = None) -> Freshness:
    """Freshness over the events observed in the 24 hours before ``now`` (ADR 0043).

    ``sources`` narrows to those ``events.source`` values; ``None`` means every source
    except tell's own derived events (``source = 'tell'``). Negative lags count as zero.
    """
    return _summarize(freshness_events(session, now, sources))


def freshness_by_source(
    session: Session, now: datetime, sources: Sequence[str] | None = None
) -> dict[str, Freshness]:
    """:func:`freshness` per ``events.source`` with an event in the window, sorted by source."""
    grouped: dict[str, list[FreshnessEvent]] = {}
    for item in freshness_events(session, now, sources):
        grouped.setdefault(item.source, []).append(item)
    return {source: _summarize(grouped[source]) for source in sorted(grouped)}


def freshness_p95_h(
    session: Session, now: datetime, sources: Sequence[str] | None = None
) -> float | None:
    """The p95 of :func:`freshness`, in hours (0.1); ``None`` without a counted event."""
    return freshness(session, now, sources).p95_h


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
    collectors = [name for name, kind in chosen if kind == "collector"]
    classified = freshness_events(session, now, collectors) if collectors else []
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
                    _summarize([c for c in classified if c.source == name]).p95_h
                    if kind == "collector"
                    else None
                ),
                stale=stale,
                status="stale" if stale else ("ok" if ever_ran else "never_run"),
            )
        )
    return Health(
        generated_at=iso_utc(now),
        sources=rows,
        freshness_p95_h=_summarize(classified).p95_h,
    )
