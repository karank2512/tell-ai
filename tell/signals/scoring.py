"""Signals, decay, scores, heat and threshold crossings (spec section 8).

A score is a sum of decayed contributions, one per signal, computed from ``signals``
rows at scoring time. Nothing stored here cannot be rebuilt from ``events``:
``signals`` rows are derived from events (:func:`build_signals`), ``scores`` rows
from signals (:func:`score_companies`), and every stored contribution carries the
originating event's URL as its receipt. ``docs/methods/scoring.md`` is the prose
version of this module.

Signal derivation (:func:`build_signals`)
=========================================

================== ================================= ===================================
``signal_type``    from events of type               magnitude / role_family
================== ================================= ===================================
``hiring_velocity`` ``hiring_velocity``              ``event.magnitude`` (``min(new7, 10)``),
                                                     ``payload.role_family``
``funding``        ``form_d_filed`` with             ``log10(total_offering_amount)``
                   ``payload.is_amendment`` false    capped at 8; undisclosed / 0 → 6
``leadership``     ``news_leadership``;              1
                   ``form_8k_filed`` with
                   ``payload.item_5_02`` true
``launch``         ``news_launch``;                  1
                   ``github_release`` with
                   ``payload.is_flagship`` true
``expansion``      ``news_expansion``                1
``tech_adoption``  ``tech_mentioned_first_time``     1, ``role_family`` NULL
                   (not a first-poll baseline)
================== ================================= ===================================

Each row is stamped with the ``default`` profile's weight and half-life at creation
(the spec's ``weight`` column) and ``starts_at = event.occurred_at``. The natural key
is ``(event_id, signal_type, role_family)``; because ``role_family`` is NULL for
``tech_adoption`` (and NULLs never collide in a unique constraint) existing keys are
read first and skipped, so a rerun inserts nothing. Form D amendments merge into the
original event's payload upstream and never reach this table as events; when a D/A
changed the offering amount, the next build rewrites the existing ``funding`` signal's
``magnitude`` (and nothing else) from the current payload, so the stored number always
agrees with the receipt and with a rebuild from events (ADR 0021 item 8). There is never
a second funding signal for one filing (spec section 5 rule 4).

Scoring
=======

:func:`contribution` and :func:`score` are the spec's pseudo-code. The instant for a
calendar ``as_of`` date is the end of that UTC day (:func:`score_instant`), so every
event of the day counts. ``scores.contributions`` holds ``[{signal_id, signal_type,
role_family, contribution, url, occurred_at, summary}]`` sorted by contribution,
largest first; ``summary`` is a one-line receipt ("Form D: $18,000,000 offering,
$5,000,000 sold", "4 new sales roles in 7 days vs 0.5 baseline", a headline).

Size band
---------

The raw score is stored for every scored company, band or not: it stays explainable
and the sum test holds everywhere. The profile's ``size_band`` gates what is *acted
on*: a company outside ``[min_open_postings, max_open_postings]`` never emits
``score_crossed_threshold`` and is left off in-market lists (:func:`in_size_band`).
A company whose size is unknown (no active ATS board, so no open-posting count) is
treated as in band: the filter is about company size, not about what tell can see.

Threshold crossings
-------------------

:func:`emit_crossings` writes one ``score_crossed_threshold`` event (source ``tell``,
external id ``f"{company_id}:{profile}"``, version = the ``as_of`` ISO date) when the
most recent stored score before ``as_of`` was below the profile's ``alert_threshold``
and today's is at or above it, and hysteresis allows: no crossing event for the same
company and profile whose scored day (``payload.as_of``) is within the last 7 days of
``as_of``, and, once a crossing has fired, the score must have been stored below
``threshold - 0.5`` on some day since that crossing's ``as_of`` (re-arm). A score
oscillating around 6.0 by less than 0.5 therefore fires at most once. The event's
``occurred_at`` and ``observed_at`` are both the run instant (the crossing is something
tell observed, never a timestamp in the future); the scored day lives in ``version``
and ``payload.as_of``, which is what the cooldown and re-arm compare.

Heat
----

:func:`heat` is a 0-100 percentile rank of raw scores within one watchlist: the
share of other companies scoring strictly lower, ties counting half, so the unique
top is 100, the unique bottom 0, and a one-company list is 100 when its score is
positive. Heat is display-only and never stored.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tell.collectors.base import (
    FIRST_POLL_KEY,
    TECH_MENTIONED_FIRST_TIME,
    Event,
    iso_utc,
    new_run_id,
    utcnow,
    write_events,
)
from tell.db import upsert
from tell.models import AtsBoard, EventRow, Score, Signal, Watchlist, WatchlistItem
from tell.signals.profiles import (
    DEFAULT_PROFILE_NAME,
    Profile,
    default_profile,
    load_profiles,
)
from tell.signals.velocity import HIRING_VELOCITY, open_postings

__all__ = [
    "CROSSING_COOLDOWN_DAYS",
    "EXPIRY_HALF_LIVES",
    "FORM_8K_FILED",
    "FORM_D_FILED",
    "FUNDING_MAGNITUDE_CAP",
    "FUNDING_UNDISCLOSED_MAGNITUDE",
    "GITHUB_RELEASE",
    "NEWS_EXPANSION",
    "NEWS_LAUNCH",
    "NEWS_LEADERSHIP",
    "REARM_MARGIN",
    "SCORE_CROSSED_THRESHOLD",
    "SCORE_SOURCE",
    "SIGNAL_EVENT_TYPES",
    "WHY_NOT_HIGHER_DAYS",
    "ScoreResult",
    "SignalView",
    "active_signals",
    "build_signals",
    "contribution",
    "default_profile_companies",
    "emit_crossings",
    "funding_magnitude",
    "heat",
    "heat_for_watchlist",
    "in_size_band",
    "score",
    "score_all",
    "score_companies",
    "score_instant",
    "score_profile",
    "summarize",
    "watchlist_companies",
    "why_not_higher",
]

# --- constants ----------------------------------------------------------------------------

SCORE_SOURCE = "tell"
SCORE_CROSSED_THRESHOLD = "score_crossed_threshold"

FORM_D_FILED = "form_d_filed"
FORM_8K_FILED = "form_8k_filed"
NEWS_LEADERSHIP = "news_leadership"
NEWS_LAUNCH = "news_launch"
NEWS_EXPANSION = "news_expansion"
GITHUB_RELEASE = "github_release"

FUNDING = "funding"
LEADERSHIP = "leadership"
LAUNCH = "launch"
EXPANSION = "expansion"
TECH_ADOPTION = "tech_adoption"

SIGNAL_EVENT_TYPES: tuple[str, ...] = (
    HIRING_VELOCITY,
    FORM_D_FILED,
    FORM_8K_FILED,
    NEWS_LEADERSHIP,
    NEWS_LAUNCH,
    NEWS_EXPANSION,
    GITHUB_RELEASE,
    TECH_MENTIONED_FIRST_TIME,
)
"""Event types :func:`build_signals` reads. ``news_funding`` is deliberately absent: the
spec fires ``funding`` on Form D only (a headline about a round is not a receipt for it)."""

FUNDING_MAGNITUDE_CAP = 8.0
FUNDING_UNDISCLOSED_MAGNITUDE = 6.0
EXPIRY_HALF_LIVES = 4
"""Beyond this many half-lives a contribution is zero and dropped from the list."""

CROSSING_COOLDOWN_DAYS = 7
"""No second ``score_crossed_threshold`` for one company and profile within this many days."""

REARM_MARGIN = 0.5
"""After a crossing, the score must be stored below ``threshold - REARM_MARGIN`` to re-arm."""

WHY_NOT_HIGHER_DAYS = 30
"""How far back :func:`why_not_higher` looks for contributions that decayed to zero."""

_DAY = timedelta(days=1)
_SECONDS_PER_DAY = 86400.0

_Derived = tuple[str, str | None, float]
_Deriver = Callable[[EventRow, Mapping[str, Any]], _Derived | None]


# --- shapes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalView:
    """One ``signals`` row with the fields of its event that scoring needs."""

    id: int
    company_id: int
    event_id: str
    signal_type: str
    role_family: str | None
    magnitude: Decimal
    half_life_days: int
    starts_at: datetime
    url: str
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def keyword(self) -> str | None:
        """The tech keyword behind a ``tech_adoption`` signal (from the event payload)."""
        value = self.payload.get("keyword")
        return str(value) if value else None


@dataclass(frozen=True)
class ScoreResult:
    """The score of one company under one profile at one instant, plus its receipts."""

    company_id: int
    profile: str
    as_of: date
    score: float
    contributions: list[dict[str, Any]]
    in_band: bool = True
    """Whether the company passes the profile's ``size_band`` (unknown size counts as yes)."""

    @property
    def top_url(self) -> str | None:
        return str(self.contributions[0]["url"]) if self.contributions else None


# --- time -------------------------------------------------------------------------------------


def score_instant(as_of: date) -> datetime:
    """The instant a calendar ``as_of`` is scored at: the end of that UTC day."""
    return datetime.combine(as_of, time(23, 59, 59, 999999), tzinfo=UTC)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be a timezone-aware datetime, got naive {value!r}"
        raise ValueError(msg)


# --- signal derivation ----------------------------------------------------------------------


def funding_magnitude(total_offering_amount: float | int | None) -> float:
    """``log10(amount)`` capped at :data:`FUNDING_MAGNITUDE_CAP`; undisclosed or zero → 6."""
    if total_offering_amount is None:
        return FUNDING_UNDISCLOSED_MAGNITUDE
    amount = float(total_offering_amount)
    if not math.isfinite(amount) or amount <= 0:
        return FUNDING_UNDISCLOSED_MAGNITUDE
    return min(math.log10(amount), FUNDING_MAGNITUDE_CAP)


def _offering_amount(payload: Mapping[str, Any]) -> float | None:
    for key in ("total_offering_amount", "totalOfferingAmount"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _derive_velocity(row: EventRow, payload: Mapping[str, Any]) -> _Derived | None:
    family = payload.get("role_family")
    return (HIRING_VELOCITY, str(family), float(row.magnitude)) if family else None


def _derive_funding(_row: EventRow, payload: Mapping[str, Any]) -> _Derived | None:
    if payload.get("is_amendment") is True:
        return None
    return FUNDING, None, funding_magnitude(_offering_amount(payload))


def _derive_8k(_row: EventRow, payload: Mapping[str, Any]) -> _Derived | None:
    items = payload.get("items")
    flagged = payload.get("item_5_02") is True or (
        isinstance(items, list) and "5.02" in [str(i) for i in items]
    )
    return (LEADERSHIP, None, 1.0) if flagged else None


def _derive_release(_row: EventRow, payload: Mapping[str, Any]) -> _Derived | None:
    return (LAUNCH, None, 1.0) if payload.get("is_flagship") is True else None


def _derive_tech(_row: EventRow, payload: Mapping[str, Any]) -> _Derived | None:
    return None if payload.get(FIRST_POLL_KEY) is True else (TECH_ADOPTION, None, 1.0)


def _derive_unit(signal_type: str) -> _Deriver:
    def derive(_row: EventRow, _payload: Mapping[str, Any]) -> _Derived | None:
        return signal_type, None, 1.0

    return derive


_DERIVERS: dict[str, _Deriver] = {
    HIRING_VELOCITY: _derive_velocity,
    FORM_D_FILED: _derive_funding,
    NEWS_LEADERSHIP: _derive_unit(LEADERSHIP),
    FORM_8K_FILED: _derive_8k,
    NEWS_LAUNCH: _derive_unit(LAUNCH),
    GITHUB_RELEASE: _derive_release,
    NEWS_EXPANSION: _derive_unit(EXPANSION),
    TECH_MENTIONED_FIRST_TIME: _derive_tech,
}


def _derive(row: EventRow) -> _Derived | None:
    """``(signal_type, role_family, magnitude)`` for an event, or ``None`` when it yields none."""
    deriver = _DERIVERS.get(row.type)
    return deriver(row, row.payload or {}) if deriver is not None else None


_SignalKey = tuple[str, str, str | None]


def _existing_signals(
    session: Session, event_ids: Sequence[str]
) -> dict[_SignalKey, tuple[int, Decimal]]:
    """``{(event_id, signal_type, role_family): (id, magnitude)}`` for rows already present."""
    rows: dict[_SignalKey, tuple[int, Decimal]] = {}
    for start in range(0, len(event_ids), 500):
        chunk = event_ids[start : start + 500]
        stmt = select(
            Signal.event_id, Signal.signal_type, Signal.role_family, Signal.id, Signal.magnitude
        ).where(Signal.event_id.in_(chunk))
        for event_id, signal_type, family, signal_id, magnitude in session.execute(stmt):
            rows[(str(event_id), str(signal_type), family)] = (int(signal_id), Decimal(magnitude))
    return rows


def _decimal(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def build_signals(session: Session, *, now: datetime, profile: Profile | None = None) -> int:
    """Create ``signals`` rows from events (see the module table). Returns rows inserted.

    Reads every event of a signal-bearing type that is linked to a company and
    occurred at or before ``now``; skips keys already present. An existing ``funding``
    signal whose event payload now derives a different magnitude (a Form D amendment
    changed the offering amount) has its ``magnitude`` rewritten in place; nothing
    else on the row changes and such updates are not counted in the return value.
    ``weight`` and ``half_life_days`` are the ``default`` profile's (``profile``
    overrides it, for tests). Flushes; never commits.
    """
    _require_aware(now, "now")
    stamp = profile or default_profile()
    stmt = (
        select(EventRow)
        .where(
            EventRow.type.in_(SIGNAL_EVENT_TYPES),
            EventRow.company_id.is_not(None),
            EventRow.occurred_at <= now,
        )
        .order_by(EventRow.occurred_at, EventRow.event_id)
    )
    rows = list(session.scalars(stmt))
    if not rows:
        return 0
    existing = _existing_signals(session, [r.event_id for r in rows])
    values: list[dict[str, Any]] = []
    amended: list[dict[str, Any]] = []
    for row in rows:
        derived = _derive(row)
        if derived is None or row.company_id is None:
            continue
        signal_type, family, magnitude = derived
        key = (row.event_id, signal_type, family)
        present = existing.get(key)
        if present is not None:
            signal_id, stored = present
            # Only funding re-derives: its event payload is the one that changes after the
            # fact (D/A amounts merge into the original filing, spec section 5 rule 4).
            if signal_type == FUNDING and stored != _decimal(magnitude):
                amended.append({"id": signal_id, "magnitude": _decimal(magnitude)})
            continue
        existing[key] = (0, _decimal(magnitude))
        view = SignalView(
            id=0,
            company_id=row.company_id,
            event_id=row.event_id,
            signal_type=signal_type,
            role_family=family,
            magnitude=_decimal(magnitude),
            half_life_days=stamp.half_life_days[signal_type],
            starts_at=row.occurred_at,
            url=row.url,
            occurred_at=row.occurred_at,
            payload=row.payload or {},
        )
        values.append(
            {
                "company_id": row.company_id,
                "event_id": row.event_id,
                "signal_type": signal_type,
                "role_family": family,
                "weight": _decimal(stamp.weight_for(view, keyword=view.keyword)),
                "magnitude": _decimal(magnitude),
                "half_life_days": stamp.half_life_days[signal_type],
                "starts_at": row.occurred_at,
            }
        )
    inserted = 0
    for start in range(0, len(values), 500):
        inserted += upsert(
            session,
            Signal,
            values[start : start + 500],
            conflict_columns=["event_id", "signal_type", "role_family"],
        )
    for change in amended:  # rare (one per amended filing); ORM update keeps loaded rows in sync
        session.execute(
            update(Signal).where(Signal.id == change["id"]).values(magnitude=change["magnitude"])
        )
    session.flush()
    return inserted


# --- the spec's scoring functions --------------------------------------------------------


def contribution(sig: SignalView | Signal, profile: Profile, t: datetime) -> float:
    """``w * magnitude * 0.5 ** (age / hl)``; 0 before ``starts_at`` or past four half-lives."""
    keyword = sig.keyword if isinstance(sig, SignalView) else None
    w = profile.weight_for(sig, keyword=keyword)
    hl = profile.half_life_days[sig.signal_type]
    age_days = (t - sig.starts_at).total_seconds() / _SECONDS_PER_DAY
    if age_days < 0 or age_days > EXPIRY_HALF_LIVES * hl:
        return 0.0
    return w * float(sig.magnitude) * math.pow(0.5, age_days / hl)


def _load_views(
    session: Session, company_ids: Iterable[int], *, since: datetime, until: datetime
) -> dict[int, list[SignalView]]:
    """Signals of ``company_ids`` with ``since < starts_at <= until``, joined to their events."""
    ids = sorted(set(company_ids))
    out: dict[int, list[SignalView]] = defaultdict(list)
    for start in range(0, len(ids), 500):
        stmt = (
            select(Signal, EventRow.url, EventRow.occurred_at, EventRow.payload)
            .join(EventRow, EventRow.event_id == Signal.event_id)
            .where(
                Signal.company_id.in_(ids[start : start + 500]),
                Signal.starts_at <= until,
                Signal.starts_at > since,
            )
            .order_by(Signal.starts_at, Signal.id)
        )
        for sig, url, occurred_at, payload in session.execute(stmt):
            out[sig.company_id].append(
                SignalView(
                    id=sig.id,
                    company_id=sig.company_id,
                    event_id=sig.event_id,
                    signal_type=sig.signal_type,
                    role_family=sig.role_family,
                    magnitude=sig.magnitude,
                    half_life_days=sig.half_life_days,
                    starts_at=sig.starts_at,
                    url=url,
                    occurred_at=occurred_at,
                    payload=payload or {},
                )
            )
    return out


def active_signals(
    session: Session, company_id: int, t: datetime, *, profile: Profile | None = None
) -> list[SignalView]:
    """Signals with ``starts_at <= t`` within four of the profile's longest half-life."""
    prof = profile or default_profile()
    since = t - _DAY * (EXPIRY_HALF_LIVES * prof.half_life_days.longest)
    return _load_views(session, [company_id], since=since, until=t).get(company_id, [])


def score(
    session: Session, company_id: int, profile: Profile, t: datetime
) -> tuple[float, list[tuple[SignalView, float]]]:
    """The spec's ``score()``: the sum and the ``(signal, contribution)`` parts above zero."""
    return _score_views(active_signals(session, company_id, t, profile=profile), profile, t)


def _score_views(
    views: Sequence[SignalView], profile: Profile, t: datetime
) -> tuple[float, list[tuple[SignalView, float]]]:
    parts = [(s, contribution(s, profile, t)) for s in views]
    parts = [(s, c) for s, c in parts if c > 0]
    return sum(c for _, c in parts), parts


# --- receipts -------------------------------------------------------------------------------


def _money(value: Any) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"${value:,.0f}"
    return "undisclosed"


def _summarize_velocity(view: SignalView) -> str:
    p = view.payload
    family = (view.role_family or "").replace("_", " ")
    new7 = p.get("new7", int(view.magnitude))
    base = p.get("base")
    tail = f" vs {base} baseline" if base is not None else ""
    return f"{new7} new {family} roles in 7 days{tail}".strip()


def _summarize_funding(view: SignalView) -> str:
    p = view.payload
    offering = _money(_offering_amount(p))
    if p.get("total_offering_amount_indefinite") is True and offering == "undisclosed":
        offering = "indefinite"
    text = f"Form D: {offering} offering"
    sold = p.get("total_amount_sold")
    if isinstance(sold, int | float) and not isinstance(sold, bool):
        text += f", {_money(sold)} sold"
    return text


def _summarize_generic(view: SignalView) -> str:
    p = view.payload
    title = p.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    if "tag_name" in p or "repo" in p:
        name = p.get("name") or p.get("tag_name") or ""
        return f"Release {name} of {p.get('repo') or ''}".replace("  ", " ").strip()
    if "items" in p:
        return "8-K Item 5.02 (departure or appointment of officers)"
    return view.signal_type.replace("_", " ")


def summarize(view: SignalView) -> str:
    """One line a rep can read: what the signal is, with its numbers."""
    if view.signal_type == HIRING_VELOCITY:
        return _summarize_velocity(view)
    if view.signal_type == FUNDING:
        return _summarize_funding(view)
    if view.signal_type == TECH_ADOPTION:
        return f"First posting mentioning {view.keyword or 'a new tool'}"
    return _summarize_generic(view)


def _contribution_dict(view: SignalView, value: float) -> dict[str, Any]:
    return {
        "signal_id": view.id,
        "signal_type": view.signal_type,
        "role_family": view.role_family,
        "contribution": value,
        "url": view.url,
        "occurred_at": iso_utc(view.occurred_at),
        "summary": summarize(view),
    }


# --- size band -----------------------------------------------------------------------------


def _companies_with_boards(session: Session, company_ids: Iterable[int]) -> set[int]:
    ids = sorted(set(company_ids))
    known: set[int] = set()
    for start in range(0, len(ids), 500):
        stmt = (
            select(AtsBoard.company_id)
            .where(AtsBoard.company_id.in_(ids[start : start + 500]), AtsBoard.status == "active")
            .distinct()
        )
        known.update(int(r[0]) for r in session.execute(stmt) if r[0] is not None)
    return known


def in_size_band(session: Session, profile: Profile, company_id: int, t: datetime) -> bool:
    """Whether the company's open postings at ``t`` fall inside the profile's size band.

    A company with no active ATS board has no measurable size and counts as in band.
    """
    if company_id not in _companies_with_boards(session, [company_id]):
        return True
    return profile.size_band.contains(open_postings(session, company_id, t))


# --- scoring companies -------------------------------------------------------------------


def score_companies(
    session: Session,
    profile: Profile,
    as_of: date,
    company_ids: Iterable[int],
    *,
    write: bool = True,
) -> list[ScoreResult]:
    """Score ``company_ids`` under ``profile`` at the end of ``as_of``; upsert ``scores``.

    Companies with no active signal get a stored score of 0 with an empty
    contributions list (the day is still accounted for, which the crossing logic
    relies on). Flushes; never commits. ``write=False`` only computes.
    """
    t = score_instant(as_of)
    ids = sorted(set(company_ids))
    if not ids:
        return []
    since = t - _DAY * (EXPIRY_HALF_LIVES * profile.half_life_days.longest)
    views = _load_views(session, ids, since=since, until=t)
    sized = _companies_with_boards(session, ids)
    results: list[ScoreResult] = []
    for company_id in ids:
        total, parts = _score_views(views.get(company_id, []), profile, t)
        parts.sort(key=lambda sc: (-sc[1], sc[0].id))
        in_band = True
        if company_id in sized:
            in_band = profile.size_band.contains(open_postings(session, company_id, t))
        results.append(
            ScoreResult(
                company_id=company_id,
                profile=profile.name,
                as_of=as_of,
                score=total,
                contributions=[_contribution_dict(v, c) for v, c in parts],
                in_band=in_band,
            )
        )
    if write:
        rows = [
            {
                "company_id": r.company_id,
                "icp_profile": r.profile,
                "as_of": r.as_of,
                "score": _decimal(r.score),
                "contributions": r.contributions,
            }
            for r in results
        ]
        for start in range(0, len(rows), 500):
            upsert(
                session,
                Score,
                rows[start : start + 500],
                conflict_columns=["company_id", "icp_profile", "as_of"],
                update_columns=["score", "contributions"],
            )
        session.flush()
    return results


def default_profile_companies(session: Session, profile: Profile, as_of: date) -> list[int]:
    """Companies with at least one active signal at the end of ``as_of``."""
    t = score_instant(as_of)
    since = t - _DAY * (EXPIRY_HALF_LIVES * profile.half_life_days.longest)
    stmt = (
        select(Signal.company_id)
        .where(Signal.starts_at <= t, Signal.starts_at > since)
        .distinct()
        .order_by(Signal.company_id)
    )
    return [int(r[0]) for r in session.execute(stmt)]


def watchlist_companies(session: Session, profile_name: str) -> list[int]:
    """Companies on any watchlist whose ``icp_profile`` is ``profile_name``."""
    stmt = (
        select(WatchlistItem.company_id)
        .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
        .where(Watchlist.icp_profile == profile_name)
        .distinct()
        .order_by(WatchlistItem.company_id)
    )
    return [int(r[0]) for r in session.execute(stmt)]


def _companies_for(session: Session, profile: Profile, as_of: date) -> list[int]:
    if profile.name == DEFAULT_PROFILE_NAME:
        return default_profile_companies(session, profile, as_of)
    return watchlist_companies(session, profile.name)


def score_profile(
    session: Session,
    profile: Profile,
    as_of: date,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
    emit: bool = True,
) -> tuple[list[ScoreResult], list[Event]]:
    """The daily job for one profile: score its companies, then emit crossings.

    The ``default`` profile scores every company with an active signal (the public
    site needs them all); any other profile scores the companies on watchlists bound
    to it. Returns the results and the crossing events written.
    """
    companies = _companies_for(session, profile, as_of)
    results = score_companies(session, profile, as_of, companies)
    events = (
        emit_crossings(session, profile, as_of, results, now=now, run_id=run_id) if emit else []
    )
    return results, events


def score_all(
    session: Session,
    *,
    as_of: date,
    profiles: Mapping[str, Profile] | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, dict[str, int]]:
    """Score every profile for ``as_of``; ``{profile: {companies, crossings}}``.

    ``profiles`` defaults to :func:`load_profiles` (shipped plus ``tell-private``).
    """
    chosen = profiles if profiles is not None else load_profiles()
    run = run_id or new_run_id(SCORE_SOURCE, now)
    stats: dict[str, dict[str, int]] = {}
    for name in sorted(chosen):
        results, events = score_profile(session, chosen[name], as_of, now=now, run_id=run)
        stats[name] = {"companies": len(results), "crossings": len(events)}
    return stats


# --- crossings -------------------------------------------------------------------------------


def _previous_scores(
    session: Session, profile_name: str, company_ids: Sequence[int], before: date
) -> dict[int, tuple[date, float]]:
    """Most recent stored ``(as_of, score)`` strictly before ``before`` per company."""
    if not company_ids:
        return {}
    latest = (
        select(Score.company_id, func.max(Score.as_of).label("as_of"))
        .where(
            Score.icp_profile == profile_name,
            Score.company_id.in_(list(company_ids)),
            Score.as_of < before,
        )
        .group_by(Score.company_id)
        .subquery()
    )
    stmt = select(Score.company_id, Score.as_of, Score.score).join(
        latest,
        (Score.company_id == latest.c.company_id)
        & (Score.as_of == latest.c.as_of)
        & (Score.icp_profile == profile_name),
    )
    return {int(r[0]): (r[1], float(r[2])) for r in session.execute(stmt)}


def _last_crossings(
    session: Session, profile_name: str, company_ids: Sequence[int]
) -> dict[int, date]:
    """The most recent scored day (``payload.as_of``) with a crossing, per company.

    Compared by the scored day, not by ``occurred_at``: a late backfill run stamps an
    old day's crossing with today's instant, and hysteresis is about scored days.
    """
    if not company_ids:
        return {}
    stmt = select(EventRow.company_id, EventRow.occurred_at, EventRow.payload).where(
        EventRow.source == SCORE_SOURCE,
        EventRow.type == SCORE_CROSSED_THRESHOLD,
        EventRow.company_id.in_(list(company_ids)),
        EventRow.payload.contains({"profile": profile_name}),
    )
    out: dict[int, date] = {}
    for company_id, occurred_at, payload in session.execute(stmt):
        as_of_text = (payload or {}).get("as_of")
        as_of = date.fromisoformat(str(as_of_text)) if as_of_text else occurred_at.date()
        known = out.get(int(company_id))
        if known is None or as_of > known:
            out[int(company_id)] = as_of
    return out


def _rearmed(
    session: Session, profile: Profile, company_id: int, after: date, before: date
) -> bool:
    """A stored score below ``threshold - REARM_MARGIN`` exists in ``(after, before)``."""
    stmt = (
        select(func.count())
        .select_from(Score)
        .where(
            Score.icp_profile == profile.name,
            Score.company_id == company_id,
            Score.as_of > after,
            Score.as_of < before,
            Score.score < _decimal(profile.alert_threshold - REARM_MARGIN),
        )
    )
    return int(session.execute(stmt).scalar_one()) > 0


def emit_crossings(
    session: Session,
    profile: Profile,
    as_of: date,
    results: Sequence[ScoreResult],
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> list[Event]:
    """Write ``score_crossed_threshold`` events for ``results`` (see the module docstring).

    ``results`` must be the scores of ``as_of``. Events have ``occurred_at`` and
    ``observed_at`` both equal to ``now`` (default: the wall clock), the instant tell
    observed the crossing; ``version`` and ``payload.as_of`` carry the scored day, so a
    same-day rerun derives the same ``event_id``, finds it present and returns nothing.
    The 7-day cooldown and the re-arm compare scored days, never ``occurred_at``.
    Flushes; never commits. Returns the events written.
    """
    observed = now or utcnow()
    _require_aware(observed, "now")
    threshold = profile.alert_threshold
    candidates = [r for r in results if r.in_band and r.score >= threshold and r.contributions]
    if not candidates:
        return []
    ids = [r.company_id for r in candidates]
    previous = _previous_scores(session, profile.name, ids, as_of)
    last = _last_crossings(session, profile.name, ids)
    run = run_id or new_run_id(SCORE_SOURCE, observed)
    events: list[Event] = []
    for result in candidates:
        prev_as_of, prev_score = previous.get(result.company_id, (None, 0.0))
        if not prev_score < threshold:
            continue
        crossed_as_of = last.get(result.company_id)
        if crossed_as_of is not None:
            if crossed_as_of > as_of - _DAY * CROSSING_COOLDOWN_DAYS:
                continue
            if not _rearmed(session, profile, result.company_id, crossed_as_of, as_of):
                continue
        events.append(
            Event(
                source=SCORE_SOURCE,
                type=SCORE_CROSSED_THRESHOLD,
                external_id=f"{result.company_id}:{profile.name}",
                version=as_of.isoformat(),
                occurred_at=observed,
                observed_at=observed,
                url=result.top_url or "",
                magnitude=result.score,
                payload={
                    "profile": profile.name,
                    "as_of": as_of.isoformat(),
                    "score": result.score,
                    "previous_score": prev_score,
                    "previous_as_of": prev_as_of.isoformat() if prev_as_of else None,
                    "threshold": threshold,
                    "top_contributions": result.contributions[:2],
                },
                company_id=result.company_id,
                run_id=run,
            )
        )
    if not events:
        return []
    existing = set(
        session.scalars(
            select(EventRow.event_id).where(EventRow.event_id.in_([e.event_id for e in events]))
        )
    )
    fresh = [e for e in events if e.event_id not in existing]
    write_events(session, fresh)
    session.flush()
    return fresh


# --- heat -----------------------------------------------------------------------------------


def heat(scores: Mapping[int, float]) -> dict[int, int]:
    """0-100 percentile rank of each raw score within one list (see the module docstring)."""
    n = len(scores)
    if n == 0:
        return {}
    if n == 1:
        ((company_id, value),) = scores.items()
        return {company_id: 100 if value > 0 else 0}
    values = sorted(scores.values())
    out: dict[int, int] = {}
    for company_id, value in scores.items():
        below = sum(1 for v in values if v < value)
        equal_others = sum(1 for v in values if v == value) - 1
        out[company_id] = round(100 * (below + 0.5 * equal_others) / (n - 1))
    return out


def heat_for_watchlist(session: Session, watchlist_id: int, as_of: date) -> dict[int, int]:
    """Heat of every company on a watchlist from the stored scores of its profile."""
    watchlist = session.get(Watchlist, watchlist_id)
    if watchlist is None:
        msg = f"no watchlist with id {watchlist_id}"
        raise KeyError(msg)
    members = select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == watchlist_id)
    stmt = select(Score.company_id, Score.score).where(
        Score.icp_profile == watchlist.icp_profile,
        Score.as_of == as_of,
        Score.company_id.in_(members),
    )
    scores = {int(r[0]): float(r[1]) for r in session.execute(stmt)}
    for (company_id,) in session.execute(members):
        scores.setdefault(int(company_id), 0.0)
    return heat(scores)


# --- account page footer ------------------------------------------------------------------


def why_not_higher(
    session: Session, company_id: int, profile: Profile, as_of: date
) -> list[dict[str, Any]]:
    """Signals whose contribution decayed to zero in the last 30 days (account page footer).

    A signal expires ``4 * half_life`` days after ``starts_at`` (the profile's half-life
    for its type). Those expiring in ``(t - 30d, t]`` are listed newest-expiry first as
    ``{signal_id, signal_type, role_family, url, occurred_at, expired_at, summary}``.
    """
    t = score_instant(as_of)
    window_start = t - _DAY * WHY_NOT_HIGHER_DAYS
    longest = EXPIRY_HALF_LIVES * profile.half_life_days.longest
    views = _load_views(session, [company_id], since=window_start - _DAY * longest, until=t).get(
        company_id, []
    )
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for view in views:
        expired_at = view.starts_at + _DAY * (
            EXPIRY_HALF_LIVES * profile.half_life_days[view.signal_type]
        )
        if window_start < expired_at <= t:
            rows.append(
                (
                    expired_at,
                    {
                        "signal_id": view.id,
                        "signal_type": view.signal_type,
                        "role_family": view.role_family,
                        "url": view.url,
                        "occurred_at": iso_utc(view.occurred_at),
                        "expired_at": iso_utc(expired_at),
                        "summary": summarize(view),
                    },
                )
            )
    rows.sort(key=lambda r: (r[0], r[1]["signal_id"]), reverse=True)
    return [row for _, row in rows]
