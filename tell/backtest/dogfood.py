"""Spec 13.5 dogfood: how fast tell surfaces postings for Karan's own search. PRIVATE.

The only report that mixes personal outcomes with system output, so it is written to
``tell-private/reports/dogfood-<date>.md`` (:data:`DEFAULT_OUT`) and nowhere else:
:func:`check_private_out` refuses any directory inside the public checkout, and no
``-latest`` copy or index entry is written.

Definitions:

* **Targets.** The watchlist :data:`WATCHLIST` (``karan-targets``, matched by slug or name)
  and the ICP profile :data:`PROFILE` (``job-seeker``), whose ``target_families`` are the
  roles applied to.
* **Surfaced posting.** A ``job_opened`` event of a watchlist company whose role family is a
  target family, not an intern role and not a repost, opened in the trailing
  :data:`PERIOD`. First-poll baselines (ADR 0019) are left out: those postings were open
  before tell watched the board, so their delay measures the posting's age.
* ``surfaced_at`` is the event's ``observed_at`` (the run that first saw the posting; the
  digest and site are rebuilt in the same run); ``posted_at`` is the provider's date
  (``payload.posted_at``, else ``occurred_at``). Hours to surface is the difference,
  floored at zero.
* **Applied / interview.** tell records no applications, so the ``feedback`` verdicts of the
  ``karan-targets`` watchlist stand in: ``worked`` means "applied", ``booked`` (or ``won``)
  means "interview" (and implies applied). A verdict counts for a posting when its signal's
  event is the posting's ``job_opened`` event or a ``hiring_velocity`` event whose
  ``counted_postings`` lists the posting.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tell.backtest.common import PRIVATE_ROOT, REPO_ROOT, Metric, Report, Section, Table, median
from tell.collectors.base import FIRST_POLL_KEY, JOB_OPENED, parse_iso
from tell.models import Company, EventRow, Feedback, Signal, Watchlist, WatchlistItem
from tell.signals.profiles import Profile, ProfileNotFoundError, get_profile

__all__ = [
    "APPLIED_VERDICTS",
    "DEFAULT_OUT",
    "INTERVIEW_VERDICTS",
    "NAME",
    "PERIOD",
    "PROFILE",
    "WATCHLIST",
    "PrivateOutputError",
    "Surfaced",
    "build_report",
    "check_private_out",
    "find_watchlist",
    "surfaced_postings",
]

NAME = "dogfood"
SCRIPT = "tell/backtest/dogfood.py"
WATCHLIST = "karan-targets"
PROFILE = "job-seeker"
PERIOD = timedelta(days=90)
DEFAULT_OUT = PRIVATE_ROOT / "reports"
APPLIED_VERDICTS: frozenset[str] = frozenset({"worked", "booked", "won"})
INTERVIEW_VERDICTS: frozenset[str] = frozenset({"booked", "won"})
MAX_ROWS = 200
"""Posting rows listed in the report (newest first); the metrics use every posting."""


class PrivateOutputError(ValueError):
    """The dogfood report was pointed at a directory inside the public checkout."""


def check_private_out(out_dir: Path, *, repo_root: Path = REPO_ROOT) -> Path:
    """``out_dir`` resolved; raises :class:`PrivateOutputError` when it is in the checkout."""
    resolved = out_dir.expanduser().resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        msg = (
            f"dogfood is private and is never written inside the public checkout ({root}); "
            f"got {resolved}"
        )
        raise PrivateOutputError(msg)
    return resolved


@dataclass(frozen=True)
class Surfaced:
    """One surfaced posting and the outcome proxies."""

    domain: str
    title: str
    family: str
    external_id: str
    posted_at: datetime
    surfaced_at: datetime
    applied: bool = False
    interview: bool = False

    @property
    def hours(self) -> float:
        return max((self.surfaced_at - self.posted_at).total_seconds(), 0.0) / 3600.0


def find_watchlist(session: Session, name: str = WATCHLIST) -> Watchlist | None:
    """The watchlist whose slug or name is ``name`` (lowest id when several)."""
    stmt = (
        select(Watchlist)
        .where(or_(Watchlist.slug == name, Watchlist.name == name))
        .order_by(Watchlist.id)
    )
    return session.scalars(stmt).first()


def _posted_at(payload: dict[str, Any], occurred_at: datetime) -> datetime:
    raw = payload.get("posted_at")
    if isinstance(raw, str) and raw:
        try:
            return parse_iso(raw)
        except ValueError:
            return occurred_at
    return occurred_at


def _verdicts(session: Session, watchlist_id: int) -> dict[tuple[str, str], set[str]]:
    """``{(event_id or posting key): verdicts}``: which postings each verdict touches."""
    stmt = (
        select(Feedback.verdict, EventRow.event_id, EventRow.type, EventRow.payload)
        .join(Signal, Signal.id == Feedback.signal_id)
        .join(EventRow, EventRow.event_id == Signal.event_id)
        .where(Feedback.watchlist_id == watchlist_id)
    )
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for verdict, event_id, type_, payload in session.execute(stmt):
        out[("event", str(event_id))].add(str(verdict))
        if type_ == "hiring_velocity":
            for posting in payload.get("counted_postings") or ():
                if isinstance(posting, dict) and posting.get("external_id"):
                    out[("posting", str(posting["external_id"]))].add(str(verdict))
    return out


def surfaced_postings(
    session: Session, watchlist: Watchlist, profile: Profile, now: datetime
) -> list[Surfaced]:
    """Every surfaced posting of the trailing :data:`PERIOD` (see the module docstring)."""
    members = select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == watchlist.id)
    stmt = (
        select(
            EventRow.event_id,
            EventRow.occurred_at,
            EventRow.observed_at,
            EventRow.payload,
            Company.domain,
        )
        .join(Company, Company.id == EventRow.company_id)
        .where(
            EventRow.type == JOB_OPENED,
            EventRow.company_id.in_(members),
            EventRow.observed_at > now - PERIOD,
            EventRow.observed_at <= now,
        )
        .order_by(EventRow.observed_at.desc(), EventRow.event_id)
    )
    families = set(profile.target_families)
    verdicts = _verdicts(session, watchlist.id)
    out: list[Surfaced] = []
    for event_id, occurred_at, observed_at, payload, domain in session.execute(stmt):
        if (
            payload.get(FIRST_POLL_KEY) is True
            or payload.get("is_intern") is True
            or payload.get("repost_of_external_id")
            or payload.get("role_family") not in families
        ):
            continue
        external = str(payload.get("posting_external_id") or "")
        seen = verdicts.get(("event", str(event_id)), set()) | verdicts.get(
            ("posting", external), set()
        )
        out.append(
            Surfaced(
                domain=str(domain),
                title=str(payload.get("title") or ""),
                family=str(payload.get("role_family")),
                external_id=external,
                posted_at=_posted_at(payload, occurred_at).astimezone(UTC),
                surfaced_at=observed_at.astimezone(UTC),
                applied=bool(seen & APPLIED_VERDICTS),
                interview=bool(seen & INTERVIEW_VERDICTS),
            )
        )
    return out


def build_report(session: Session, now: datetime, *, profile: Profile | None = None) -> Report:
    """The private 13.5 report at ``now``."""
    watchlist = find_watchlist(session)
    notices: list[str] = []
    try:
        chosen = profile or get_profile(PROFILE)
    except ProfileNotFoundError:  # pragma: no cover - job-seeker ships with tell
        chosen = None
        notices.append(f"Profile {PROFILE} not found.")
    rows: list[Surfaced] = []
    if watchlist is None:
        notices.append(
            f"Watchlist {WATCHLIST} not found: create it before this report means anything."
        )
    elif chosen is not None:
        rows = surfaced_postings(session, watchlist, chosen, now)
    families = ", ".join(chosen.target_families) if chosen is not None else "none"
    applied = sum(1 for r in rows if r.applied)
    interviews = sum(1 for r in rows if r.interview)
    report = Report(
        name=NAME,
        title="Dogfood: postings surfaced for the job-seeker profile",
        question=(
            "How quickly does tell surface new postings at the target companies, and what did "
            "they lead to?"
        ),
        cohort=(
            f"New postings (no first-poll baselines, interns or reposts) in the target families "
            f"({families}) of the {PROFILE} profile at companies on the {WATCHLIST} watchlist, "
            f"first seen in the {PERIOD.days} days before the evaluation instant."
        ),
        n=len(rows),
        as_of=now.date(),
        generated_at=now,
        script=SCRIPT,
        notices=notices,
        blockers=["private report, never quoted publicly"],
        private=True,
    )
    report.sections = [
        Section(
            title="Speed and outcomes",
            metrics=[
                Metric(
                    "Median hours to surface (surfaced_at - posted_at)",
                    median(r.hours for r in rows),
                    len(rows),
                    "hours",
                ),
                Metric(
                    "Applications generated (verdict worked, booked or won)", applied, kind="count"
                ),
                Metric("Interviews (verdict booked or won)", interviews, kind="count"),
            ],
            table=Table(
                headers=[
                    "company",
                    "title",
                    "family",
                    "posted",
                    "surfaced",
                    "hours",
                    "applied",
                    "interview",
                ],
                rows=[
                    [
                        r.domain,
                        r.title,
                        r.family,
                        r.posted_at.strftime("%Y-%m-%d %H:%M"),
                        r.surfaced_at.strftime("%Y-%m-%d %H:%M"),
                        f"{r.hours:.1f}",
                        "yes" if r.applied else "no",
                        "yes" if r.interview else "no",
                    ]
                    for r in rows[:MAX_ROWS]
                ],
            ),
        )
    ]
    report.caveats = [
        (
            "Applied and interview are proxies: feedback verdict worked stands for applied, booked "
            "or won for an interview; anything not recorded as feedback is invisible."
        ),
        (
            "surfaced_at is when tell first saw the posting; the digest only reaches the inbox at "
            "the next digest hour."
        ),
        "posted_at is the provider's date; some vendors re-date postings when they are edited.",
        "Private: personal outcomes stay in tell-private and are never published.",
    ]
    return report
