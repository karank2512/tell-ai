"""Hiring velocity, stale requisitions and ghost votes (spec section 7).

Definitions (``docs/methods/hiring-velocity.md`` is the prose version):

* **Counted posting.** A ``job_opened`` event of company ``c`` whose payload
  ``role_family`` is ``f``, that is not tagged ``is_intern`` and carries no
  ``repost_of_external_id`` (reposts emit ``job_reposted`` anyway, so they never
  reach this list), and that is *net-new*: no other counted posting with the same
  ``(vendor, board_identifier, fingerprint)`` opened in the 30 days before it
  (spec: "two postings with the same fingerprint on one board within 30 days are
  one requisition"; chained, so a fingerprint re-listed every 25 days stays one
  requisition).
* **Cold start (ADR 0019).** ``job_opened`` events flagged ``first_poll`` were emitted
  by a board's first poll: the postings were already open when tell started watching,
  requisitions that had already closed are invisible, and postings without
  ``posted_at`` are all dated to that poll. They are never counted (not in ``new7``,
  so not in ``base`` either) but they stay in the same-requisition chain, so a
  same-fingerprint posting that follows one of them within 30 days is still one
  requisition.
* ``new7(c, f, t)``: counted postings with ``occurred_at`` in ``(t - 7d, t]``.
* ``base(c, f, t)``: mean of ``new7`` at ``t - 7d``, ``t - 14d``, ``t - 21d`` and
  ``t - 28d`` (the four non-overlapping 7-day windows covering ``(t - 35d, t - 7d]``),
  floored at 0.5.
* ``ratio = new7 / base``. The signal fires when ``new7 >= 3`` and ``ratio >= 1.5``,
  or when ``new7 >= 2`` for ``sales`` / ``gtm_ops`` / ``customer`` at a company with
  fewer than 50 open postings (stale-weighted, see below).
* ``magnitude = min(new7, 10)``.

Stale requisitions: :func:`tell.collectors.base.stale_weight` gives an open posting
weight 1.0 up to 90 days, 0.25 beyond, 0 beyond 180 days. ``job_opened`` events inside
a 7-day window are dated by ``posted_at`` and so are never older than 7 days: the
weighting cannot touch ``new7``. It applies to the open-posting count behind the
small-company rule (:func:`open_postings`) and to :func:`long_open_roles`, the
account page's "long-open roles" list.

Episodes: :func:`emit_velocity_events` writes one ``hiring_velocity`` event per
(company, family) spike episode. An episode is a run of days on which the condition
fires, ending once it has been false for 7 consecutive days; the event's ``version``
is the ISO date of the episode's first firing day, so a rerun on any later day of the
same episode derives the same ``event_id`` and is skipped on conflict. When the
walk-back reaches :data:`EPISODE_LOOKBACK_DAYS` without finding a 7-day gap, the
episode is older than the walk-back can see and any earlier ``hiring_velocity`` event
for the pair marks it (so a company firing continuously never gets a second event).

Ghost votes: anonymous job-seeker "ghost job" votes per posting
(``posting_ghost_votes``, ADR 0017). Inert: nothing here reads them into a weight
until :data:`GHOST_VOTES_INERT_UNTIL` votes exist in total, and even then the
threshold that would use them is a later decision.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    FIRST_POLL_KEY,
    JOB_OPENED,
    STALE_AFTER,
    Event,
    iso_utc,
    new_run_id,
    stale_weight,
    write_events,
)
from tell.models import AtsBoard, EventRow, Posting, PostingGhostVote
from tell.signals.taxonomy import FAMILIES

__all__ = [
    "BASE_FLOOR",
    "BASE_WINDOWS",
    "BOARD_URL_TEMPLATES",
    "EPISODE_GAP_DAYS",
    "EPISODE_LOOKBACK_DAYS",
    "GHOST_VOTES_INERT_UNTIL",
    "HIRING_VELOCITY",
    "MAGNITUDE_CAP",
    "MIN_NEW7",
    "MIN_RATIO",
    "SAME_REQUISITION_WINDOW",
    "SMALL_COMPANY_FAMILIES",
    "SMALL_COMPANY_MIN_NEW7",
    "SMALL_COMPANY_OPEN_POSTINGS",
    "VELOCITY_SOURCE",
    "WINDOW",
    "CountedPosting",
    "LongOpenRole",
    "VelocityResult",
    "base",
    "board_url",
    "emit_velocity_events",
    "ghost_votes",
    "ghost_votes_active",
    "ghost_votes_total",
    "long_open_roles",
    "long_open_roles_many",
    "new7",
    "open_postings",
    "open_postings_many",
    "record_ghost_vote",
    "velocity",
]

HIRING_VELOCITY = "hiring_velocity"
VELOCITY_SOURCE = "tell"
"""``events.source`` of derived ``hiring_velocity`` events."""

WINDOW = timedelta(days=7)
BASE_WINDOWS = 4
BASE_FLOOR = 0.5
MIN_NEW7 = 3
MIN_RATIO = 1.5
SMALL_COMPANY_FAMILIES: frozenset[str] = frozenset({"sales", "gtm_ops", "customer"})
SMALL_COMPANY_MIN_NEW7 = 2
SMALL_COMPANY_OPEN_POSTINGS = 50
MAGNITUDE_CAP = 10
SAME_REQUISITION_WINDOW = timedelta(days=30)
"""Same fingerprint on one board within this span = one requisition (counted once)."""

EPISODE_GAP_DAYS = 7
"""An episode ends once the condition has been false this many consecutive days."""

EPISODE_LOOKBACK_DAYS = 90
"""How far back :func:`emit_velocity_events` walks to find an episode's first day."""

GHOST_VOTES_INERT_UNTIL = 500
"""Ghost votes change no weight until at least this many exist (spec section 7)."""

_DAY = timedelta(days=1)
_HISTORY = BASE_WINDOWS * WINDOW + WINDOW + SAME_REQUISITION_WINDOW
"""Event history one evaluation needs: the base windows, the current window and 30 days
before them for the same-requisition rule."""

BOARD_URL_TEMPLATES: dict[str, str] = {
    "greenhouse": "https://boards.greenhouse.io/{identifier}",
    "lever": "https://jobs.lever.co/{identifier}",
    "ashby": "https://jobs.ashbyhq.com/{identifier}",
    "smartrecruiters": "https://careers.smartrecruiters.com/{identifier}",
}
"""Public, human-facing board pages; the receipt links here, not to the JSON API."""


# --- result shapes --------------------------------------------------------------------------


@dataclass(frozen=True)
class CountedPosting:
    """One posting counted in ``new7``: the receipt line on the account page."""

    title: str
    posted_at: str
    url: str
    external_id: str
    vendor: str
    board_identifier: str
    fingerprint: str
    occurred_at: datetime
    first_poll: bool = False
    """Emitted by the board's first poll: anchors the same-requisition chain, never counted."""

    def receipt(self) -> dict[str, Any]:
        """``{title, posted_at, url, external_id}`` as the event payload lists it."""
        return {
            "title": self.title,
            "posted_at": self.posted_at,
            "url": self.url,
            "external_id": self.external_id,
        }


@dataclass(frozen=True)
class VelocityResult:
    """Velocity of one company and family at one instant."""

    company_id: int
    family: str
    as_of: datetime
    new7: int
    base: float
    ratio: float
    fires: bool
    magnitude: int
    open_postings: float
    board_url: str | None
    postings: list[dict[str, Any]] = field(default_factory=list)
    """``[{title, posted_at, url, external_id}]`` for each counted posting."""


@dataclass(frozen=True)
class LongOpenRole:
    """An open posting older than 90 days, for the account page's "long-open roles"."""

    title: str
    url: str
    external_id: str
    posted_at: datetime | None
    days_open: int
    weight: float
    """0.25 (stale) or 0.0 (over 180 days, excluded from every magnitude)."""


@dataclass(frozen=True, slots=True)
class _OpenPosting:
    """The ``postings`` columns :func:`_open_weight` reads, and nothing else.

    The loaders below select these columns instead of whole :class:`Posting` rows
    (title, description hashes, markers and the rest never reach Python): less read
    transfer per run, and no ORM instance that could lazy-load a column later.
    """

    posted_at: datetime | None
    first_seen_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _OpenRole(_OpenPosting):
    """:class:`_OpenPosting` plus the columns :func:`_long_open` copies onto a
    :class:`LongOpenRole`."""

    title: str
    url: str
    external_id: str


# --- helpers ----------------------------------------------------------------------------------


def board_url(vendor: str, identifier: str) -> str | None:
    """The public board page for ``vendor`` / ``identifier``; ``None`` for unknown vendors."""
    template = BOARD_URL_TEMPLATES.get(vendor)
    if template is None:
        return None
    if vendor == "lever" and identifier.lower().startswith("eu:"):
        return f"https://jobs.eu.lever.co/{identifier[3:]}"
    return template.format(identifier=identifier)


def _counted(row_occurred_at: datetime, url: str, payload: dict[str, Any]) -> CountedPosting | None:
    if payload.get("is_intern") is True or payload.get("repost_of_external_id"):
        return None
    return CountedPosting(
        title=str(payload.get("title") or ""),
        posted_at=str(payload.get("posted_at") or iso_utc(row_occurred_at)),
        url=url,
        external_id=str(payload.get("posting_external_id") or ""),
        vendor=str(payload.get("vendor") or ""),
        board_identifier=str(payload.get("board_identifier") or ""),
        fingerprint=str(payload.get("fingerprint") or ""),
        occurred_at=row_occurred_at,
        first_poll=payload.get(FIRST_POLL_KEY) is True,
    )


def _net_new(items: Iterable[CountedPosting]) -> list[CountedPosting]:
    """Drop items whose fingerprint opened on the same board within the 30 days before."""
    last: dict[tuple[str, str, str], datetime] = {}
    kept: list[CountedPosting] = []
    for item in sorted(items, key=lambda i: (i.occurred_at, i.external_id)):
        key = (item.vendor, item.board_identifier, item.fingerprint)
        previous = last.get(key)
        last[key] = item.occurred_at
        if previous is not None and item.occurred_at - previous <= SAME_REQUISITION_WINDOW:
            continue
        kept.append(item)
    return kept


def _in_window(items: Sequence[CountedPosting], t: datetime) -> list[CountedPosting]:
    return [item for item in items if not item.first_poll and t - WINDOW < item.occurred_at <= t]


def _fires(family: str, new: int, ratio: float, open_count: float) -> bool:
    if new >= MIN_NEW7 and ratio >= MIN_RATIO:
        return True
    return (
        family in SMALL_COMPANY_FAMILIES
        and open_count < SMALL_COMPANY_OPEN_POSTINGS
        and new >= SMALL_COMPANY_MIN_NEW7
    )


def _pick_board_url(counted: Sequence[CountedPosting]) -> str | None:
    """The board with the most counted postings (ties: alphabetical); else a posting URL."""
    if not counted:
        return None
    boards = Counter((item.vendor, item.board_identifier) for item in counted)
    (vendor, identifier), _ = min(boards.items(), key=lambda kv: (-kv[1], kv[0]))
    return board_url(vendor, identifier) or counted[0].url


@dataclass
class _Series:
    """Everything needed to evaluate one company and family at any ``t`` in a range."""

    company_id: int
    family: str
    counted: list[CountedPosting]
    postings: list[_OpenPosting]

    @classmethod
    def load(
        cls, session: Session, company_id: int, family: str, start: datetime, end: datetime
    ) -> _Series:
        """Load events in ``(start - history, end]`` and postings open at any time since."""
        since = start - _HISTORY
        stmt = (
            select(EventRow.occurred_at, EventRow.url, EventRow.payload)
            .where(
                EventRow.type == JOB_OPENED,
                EventRow.company_id == company_id,
                EventRow.payload.contains({"role_family": family}),
                EventRow.occurred_at > since,
                EventRow.occurred_at <= end,
            )
            .order_by(EventRow.occurred_at, EventRow.event_id)
        )
        items = [_counted(row[0], row[1], row[2]) for row in session.execute(stmt)]
        counted = _net_new(item for item in items if item is not None)
        postings_stmt = (
            select(Posting.posted_at, Posting.first_seen_at, Posting.closed_at)
            .join(AtsBoard, AtsBoard.id == Posting.board_id)
            .where(
                AtsBoard.company_id == company_id,
                (Posting.closed_at.is_(None)) | (Posting.closed_at > start),
            )
        )
        postings = [_OpenPosting(*row) for row in session.execute(postings_stmt)]
        return cls(company_id, family, counted, postings)

    def new7(self, t: datetime) -> int:
        return len(_in_window(self.counted, t))

    def base(self, t: datetime) -> float:
        windows = [self.new7(t - WINDOW * k) for k in range(1, BASE_WINDOWS + 1)]
        return max(sum(windows) / BASE_WINDOWS, BASE_FLOOR)

    def open_postings(self, t: datetime) -> float:
        return _open_weight(self.postings, t)

    def evaluate(self, t: datetime) -> VelocityResult:
        counted = _in_window(self.counted, t)
        new = len(counted)
        floor_base = self.base(t)
        ratio = new / floor_base
        open_count = self.open_postings(t)
        return VelocityResult(
            company_id=self.company_id,
            family=self.family,
            as_of=t,
            new7=new,
            base=floor_base,
            ratio=ratio,
            fires=_fires(self.family, new, ratio, open_count),
            magnitude=min(new, MAGNITUDE_CAP),
            open_postings=open_count,
            board_url=_pick_board_url(counted),
            postings=[item.receipt() for item in counted],
        )

    def episode_start(self, now: datetime) -> tuple[datetime, bool]:
        """First firing instant of the episode ``now`` belongs to (``now`` must fire).

        Returns ``(start, capped)``; ``capped`` is true when the walk-back reached
        :data:`EPISODE_LOOKBACK_DAYS` without a 7-day false run, so the episode may have
        begun earlier than ``start``.
        """
        start = now
        false_run = 0
        for k in range(1, EPISODE_LOOKBACK_DAYS + 1):
            t = now - _DAY * k
            if self.evaluate(t).fires:
                start = t
                false_run = 0
                continue
            false_run += 1
            if false_run >= EPISODE_GAP_DAYS:
                return start, False
        return start, True


def _open_weight(postings: Iterable[_OpenPosting], t: datetime) -> float:
    return sum(
        stale_weight(p, t)
        for p in postings
        if p.first_seen_at <= t and (p.closed_at is None or p.closed_at > t)
    )


# --- public velocity API ------------------------------------------------------------------


def velocity(session: Session, company_id: int, family: str, t: datetime) -> VelocityResult:
    """Velocity of ``company_id`` / ``family`` at ``t`` (see the module docstring)."""
    return _Series.load(session, company_id, family, t, t).evaluate(t)


def new7(session: Session, company_id: int, family: str, t: datetime) -> int:
    """Counted postings of ``family`` at ``company_id`` opened in ``(t - 7d, t]``."""
    return velocity(session, company_id, family, t).new7


def base(session: Session, company_id: int, family: str, t: datetime) -> float:
    """Mean ``new7`` over the four 7-day windows in ``(t - 35d, t - 7d]``, floored at 0.5."""
    return velocity(session, company_id, family, t).base


_IN_CHUNK = 500
"""Company ids per ``IN (...)`` list of the batch helpers below."""


def _postings_by_company(
    session: Session, company_ids: Iterable[int], *criteria: Any
) -> tuple[list[int], dict[int, list[_OpenRole]]]:
    """``(sorted ids, {company_id: [_OpenRole]})`` for every board of ``company_ids``.

    One query per :data:`_IN_CHUNK` companies instead of one per company, selecting
    only the six columns :func:`_open_weight` and :func:`_long_open` read.
    """
    ids = sorted(set(company_ids))
    found: dict[int, list[_OpenRole]] = {}
    for start in range(0, len(ids), _IN_CHUNK):
        stmt = (
            select(
                Posting.posted_at,
                Posting.first_seen_at,
                Posting.closed_at,
                Posting.title,
                Posting.url,
                Posting.external_id,
                AtsBoard.company_id,
            )
            .join(AtsBoard, AtsBoard.id == Posting.board_id)
            .where(AtsBoard.company_id.in_(ids[start : start + _IN_CHUNK]), *criteria)
            .order_by(Posting.id)
        )
        for posted_at, first_seen_at, closed_at, title, url, external_id, cid in session.execute(
            stmt
        ):
            # Never NULL here: the IN filter only matches boards that have a company.
            found.setdefault(cast("int", cid), []).append(
                _OpenRole(posted_at, first_seen_at, closed_at, title, url, external_id)
            )
    return ids, found


def open_postings_many(
    session: Session, company_ids: Iterable[int], t: datetime
) -> dict[int, float]:
    """:func:`open_postings` of every company in ``company_ids``, batched."""
    ids, found = _postings_by_company(
        session, company_ids, (Posting.closed_at.is_(None)) | (Posting.closed_at > t)
    )
    return {cid: _open_weight(found.get(cid, ()), t) for cid in ids}


def open_postings(session: Session, company_id: int, t: datetime) -> float:
    """Stale-weighted count of the company's postings open at ``t`` (all families)."""
    return open_postings_many(session, [company_id], t)[company_id]


def _long_open(postings: Iterable[_OpenRole], now: datetime) -> list[LongOpenRole]:
    roles = []
    for posting in postings:
        age = now - (posting.posted_at or posting.first_seen_at)
        if age <= STALE_AFTER:
            continue
        roles.append(
            LongOpenRole(
                title=posting.title,
                url=posting.url,
                external_id=posting.external_id,
                posted_at=posting.posted_at,
                days_open=age.days,
                weight=stale_weight(posting, now),
            )
        )
    return sorted(roles, key=lambda r: (-r.days_open, r.title, r.external_id))


def long_open_roles_many(
    session: Session, company_ids: Iterable[int], now: datetime
) -> dict[int, list[LongOpenRole]]:
    """:func:`long_open_roles` of every company in ``company_ids``, batched."""
    ids, found = _postings_by_company(session, company_ids, Posting.closed_at.is_(None))
    return {cid: _long_open(found.get(cid, ()), now) for cid in ids}


def long_open_roles(session: Session, company_id: int, now: datetime) -> list[LongOpenRole]:
    """Open postings older than 90 days, oldest first (weight 0.25, or 0.0 past 180 days)."""
    return long_open_roles_many(session, [company_id], now)[company_id]


def _candidates(session: Session, now: datetime) -> list[tuple[int, str]]:
    family = EventRow.payload["role_family"].astext
    stmt = (
        select(EventRow.company_id, family)
        .where(
            EventRow.type == JOB_OPENED,
            EventRow.company_id.is_not(None),
            EventRow.occurred_at > now - WINDOW,
            EventRow.occurred_at <= now,
            family.in_(FAMILIES),
        )
        .distinct()
    )
    return sorted((int(row[0]), str(row[1])) for row in session.execute(stmt))


def _already_emitted(
    session: Session, company_id: int, family: str, episode_start: datetime, *, capped: bool
) -> bool:
    """A ``hiring_velocity`` event for this company and family already marks this episode.

    Any such event at or after ``episode_start - 7d`` belongs to this episode: an
    earlier episode's last firing day is more than 7 days before this one's first. When
    the walk-back was ``capped`` (no 7-day gap within :data:`EPISODE_LOOKBACK_DAYS`) the
    episode may be older than ``episode_start``, so any earlier event counts too.
    """
    stmt = (
        select(func.count())
        .select_from(EventRow)
        .where(
            EventRow.type == HIRING_VELOCITY,
            EventRow.company_id == company_id,
            EventRow.payload.contains({"role_family": family}),
        )
    )
    if not capped:
        stmt = stmt.where(EventRow.occurred_at >= episode_start - _DAY * EPISODE_GAP_DAYS)
    return session.execute(stmt).scalar_one() > 0


def emit_velocity_events(
    session: Session, *, now: datetime, run_id: str | None = None
) -> list[Event]:
    """Write one ``hiring_velocity`` event per (company, family) spike episode firing at ``now``.

    Candidates are the (company, family) pairs with a ``job_opened`` in the last 7
    days (companies only: events of boards not yet resolved to a company carry no
    ``company_id``). For each one that fires, the episode's first day is found by
    walking back day by day (:data:`EPISODE_LOOKBACK_DAYS` at most) until the
    condition has been false for 7 consecutive days. The event:

    ``source="tell"``, ``type="hiring_velocity"``, ``external_id=f"{company_id}:{family}"``,
    ``version`` = ISO date of the episode's first day, ``occurred_at = observed_at = now``,
    ``url`` = the board URL, ``magnitude = min(new7, 10)``, and payload ``{role_family,
    new7, base, ratio, open_postings, episode_start, counted_postings: [{title,
    posted_at, url, external_id}]}`` (the receipt).

    Nothing is written when an event for the same company and family already marks
    the episode (:func:`_already_emitted`; any earlier event when the walk-back hit its
    cap without finding a gap); the deterministic ``event_id`` makes a same-day rerun a
    no-op as well. Returns the events handed to ``write_events``.
    Flushes; never commits.
    """
    run = run_id or new_run_id(VELOCITY_SOURCE, now)
    events: list[Event] = []
    for company_id, family in _candidates(session, now):
        series = _Series.load(session, company_id, family, now - _DAY * EPISODE_LOOKBACK_DAYS, now)
        result = series.evaluate(now)
        if not result.fires:
            continue
        start, capped = series.episode_start(now)
        if _already_emitted(session, company_id, family, start, capped=capped):
            continue
        events.append(
            Event(
                source=VELOCITY_SOURCE,
                type=HIRING_VELOCITY,
                external_id=f"{company_id}:{family}",
                version=start.date().isoformat(),
                occurred_at=now,
                observed_at=now,
                url=result.board_url or result.postings[0]["url"],
                magnitude=result.magnitude,
                payload={
                    "role_family": family,
                    "new7": result.new7,
                    "base": round(result.base, 4),
                    "ratio": round(result.ratio, 4),
                    "open_postings": round(result.open_postings, 2),
                    "episode_start": start.date().isoformat(),
                    "counted_postings": result.postings,
                },
                company_id=company_id,
                run_id=run,
            )
        )
    write_events(session, events)
    session.flush()
    return events


# --- ghost votes (ADR 0017) -------------------------------------------------------------------


def record_ghost_vote(
    session: Session, posting_id: int, *, now: datetime, source: str = "site"
) -> None:
    """Store one anonymous "this looks like a ghost job" vote. Flushes; never commits."""
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be a timezone-aware datetime, got naive {now!r}"
        raise ValueError(msg)
    if not source:
        msg = "source must be a non-empty string"
        raise ValueError(msg)
    session.add(PostingGhostVote(posting_id=posting_id, voted_at=now, source=source))
    session.flush()


def ghost_votes(session: Session, posting_id: int) -> int:
    """Votes recorded for one posting."""
    stmt = (
        select(func.count())
        .select_from(PostingGhostVote)
        .where(PostingGhostVote.posting_id == posting_id)
    )
    return int(session.execute(stmt).scalar_one())


def ghost_votes_total(session: Session) -> int:
    """Votes recorded across every posting."""
    return int(session.execute(select(func.count()).select_from(PostingGhostVote)).scalar_one())


def ghost_votes_active(session: Session) -> bool:
    """Whether the vote count may start informing weights (>= 500 votes in total).

    Even when this turns true nothing changes automatically: the threshold that would
    use the votes is set later through ``docs/decisions/``.
    """
    return ghost_votes_total(session) >= GHOST_VOTES_INERT_UNTIL
