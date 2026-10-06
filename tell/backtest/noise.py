"""Spec 13.3 noise handling: reposts, posting lifetimes, long-open roles.

* **Repost share.** ``count(repost_of is not null) / count(*)`` over postings first seen in
  the 90 days before the evaluation instant, overall, by vendor (``ats_boards.vendor``)
  and by role family.
* **Posting lifetime.** Median ``closed_at - posted_at`` in days per family, over postings
  closed by the evaluation instant that carry a provider ``posted_at`` (``closed_at`` before
  ``posted_at`` is a provider error and is left out).
* **Long-open boards.** Share of ``active`` boards with at least one posting open at the
  evaluation instant whose age (``posted_at``, else ``first_seen_at``) exceeds 180 days.
* **Rated usefulness, filtered vs unfiltered.** The design-partner CSV pair has no table
  in tell; the section is a placeholder until both lists have
  :data:`MIN_RATED_ROWS` rated rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from tell.backtest.common import Metric, Query, Report, Section, Table, share

__all__ = [
    "BOARDS_SQL",
    "LIFETIME_SQL",
    "MIN_RATED_ROWS",
    "NAME",
    "REPOST_SQL",
    "Group",
    "build_report",
    "lifetimes",
    "long_open_boards",
    "repost_shares",
]

NAME = "noise"
SCRIPT = "tell/backtest/noise.py"
MIN_RATED_ROWS = 50
"""Spec 13.3: quote the filtered-vs-unfiltered precision only with this many rated rows per list."""

REPOST_SQL = """\
select b.vendor, p.role_family, count(*) as n, count(p.repost_of) as reposts
from postings p join ats_boards b on b.id = p.board_id
where p.first_seen_at > cast(:as_of as timestamptz) - interval '90 days'
  and p.first_seen_at <= cast(:as_of as timestamptz)
group by b.vendor, p.role_family
order by b.vendor, p.role_family;"""

LIFETIME_SQL = """\
select p.role_family, count(*) as n,
       percentile_cont(0.5) within group (
         order by extract(epoch from p.closed_at - p.posted_at) / 86400.0) as median_days
from postings p
where p.closed_at is not null and p.posted_at is not null
  and p.closed_at <= cast(:as_of as timestamptz) and p.closed_at >= p.posted_at
group by rollup (p.role_family)
order by p.role_family nulls last;"""

BOARDS_SQL = """\
select count(*) as boards,
       count(*) filter (where exists (
         select 1 from postings p
         where p.board_id = b.id
           and p.first_seen_at <= cast(:as_of as timestamptz)
           and (p.closed_at is null or p.closed_at > cast(:as_of as timestamptz))
           and coalesce(p.posted_at, p.first_seen_at)
               < cast(:as_of as timestamptz) - interval '180 days')) as long_open
from ats_boards b
where b.status = 'active';"""


@dataclass(frozen=True)
class Group:
    """One breakdown row: ``key`` with ``part`` of ``n`` (or a median over ``n``)."""

    key: str
    n: int
    part: int = 0
    value: float | None = None

    @property
    def share(self) -> float | None:
        return share(self.part, self.n)


def repost_shares(session: Session, now: datetime) -> tuple[Group, list[Group], list[Group]]:
    """``(overall, by_vendor, by_family)`` repost shares (see the module docstring)."""
    vendors: dict[str, list[int]] = {}
    families: dict[str, list[int]] = {}
    total = [0, 0]
    for vendor, family, n, reposts in session.execute(text(REPOST_SQL), {"as_of": now}):
        for bucket, key in ((vendors, str(vendor)), (families, str(family))):
            counts = bucket.setdefault(key, [0, 0])
            counts[0] += int(n)
            counts[1] += int(reposts)
        total[0] += int(n)
        total[1] += int(reposts)

    def rows(bucket: dict[str, list[int]]) -> list[Group]:
        return [Group(key, n=c[0], part=c[1]) for key, c in sorted(bucket.items())]

    return Group("all", n=total[0], part=total[1]), rows(vendors), rows(families)


def lifetimes(session: Session, now: datetime) -> tuple[Group, list[Group]]:
    """``(overall, by_family)`` median lifetime in days."""
    overall = Group("all", n=0)
    by_family: list[Group] = []
    for family, n, median_days in session.execute(text(LIFETIME_SQL), {"as_of": now}):
        value = None if median_days is None else float(median_days)
        if family is None:
            overall = Group("all", n=int(n), value=value)
        else:
            by_family.append(Group(str(family), n=int(n), value=value))
    return overall, by_family


def long_open_boards(session: Session, now: datetime) -> Group:
    """Active boards with a posting open more than 180 days, of all active boards."""
    row = session.execute(text(BOARDS_SQL), {"as_of": now}).one()
    return Group("boards", n=int(row.boards), part=int(row.long_open))


def _share_table(title: str, groups: list[Group]) -> Table:
    return Table(
        headers=[title, "postings", "reposts", "repost share"],
        rows=[
            [
                g.key,
                f"{g.n:,}",
                f"{g.part:,}",
                Metric(f"repost share {g.key}", g.share, g.n, "share", headline=False),
            ]
            for g in groups
        ],
    )


def build_report(session: Session, now: datetime) -> Report:
    """The 13.3 report at ``now``."""
    overall, by_vendor, by_family = repost_shares(session, now)
    life_all, life_family = lifetimes(session, now)
    boards = long_open_boards(session, now)
    report = Report(
        name=NAME,
        title="Noise handling: reposts, lifetimes and long-open roles",
        question=(
            "How much of what job boards show is noise (reposted and long-open roles), and "
            "how long does a posting live?"
        ),
        cohort=(
            "Repost share: postings first seen in the 90 days before the evaluation instant. "
            "Lifetime: postings closed by then that carry the provider's posted_at. Long-open "
            "share: boards with status active."
        ),
        n=overall.n,
        as_of=now.date(),
        generated_at=now,
        script=SCRIPT,
    )
    report.sections = [
        Section(
            title="Reposts",
            metrics=[
                Metric(
                    "Share of postings that are reposts (first seen in the last 90 days), percent",
                    overall.share,
                    overall.n,
                    "share",
                )
            ],
            table=_share_table("vendor", by_vendor),
        ),
        Section(title="Reposts by role family", table=_share_table("family", by_family)),
        Section(
            title="Posting lifetime",
            metrics=[
                Metric(
                    "Median posting lifetime, closed_at - posted_at, days (all families)",
                    life_all.value,
                    life_all.n,
                    "days",
                )
            ],
            table=Table(
                headers=["family", "median lifetime"],
                rows=[
                    [g.key, Metric(f"lifetime {g.key}", g.value, g.n, "days", headline=False)]
                    for g in life_family
                ],
            ),
        ),
        Section(
            title="Long-open roles",
            metrics=[
                Metric(
                    "Share of boards with at least one posting open more than 180 days, percent",
                    boards.share,
                    boards.n,
                    "share",
                )
            ],
        ),
        Section(
            title="Rated usefulness, filtered vs unfiltered (placeholder)",
            text=(
                "Not run yet. For the first three design partners, two CSVs go out on the same "
                "day, one with reposts and stale roles included and one without, and each row "
                "is rated. The two precision figures are quoted only when both lists have at "
                f"least {MIN_RATED_ROWS} rated rows; until then nothing is reported here."
            ),
            metrics=[
                Metric("Rated rows, filtered list", 0, kind="count", headline=False),
                Metric("Rated rows, unfiltered list", 0, kind="count", headline=False),
            ],
        ),
    ]
    report.queries = [
        Query("Repost share (bind :as_of = " + now.isoformat() + ")", REPOST_SQL),
        Query("Posting lifetime", LIFETIME_SQL),
        Query("Boards with a posting open more than 180 days", BOARDS_SQL),
    ]
    report.caveats = [
        (
            "A repost is what tell's repost rule says it is (same fingerprint on the same board, "
            "closed within 30 days or same content within 7 days); a role re-listed after a longer "
            "gap counts as new."
        ),
        (
            "Lifetime is right-censored: postings still open are left out, so long-lived roles "
            "pull the median less than they should, and roles that closed before tell started "
            "watching are invisible."
        ),
        (
            "Lifetime needs the provider's posted_at; postings without one are left out of that "
            "section."
        ),
        (
            "Age for the 180-day rule is posted_at when the provider gives it, else the first time "
            "tell saw the posting, which understates the age of old roles on boards added late."
        ),
    ]
    return report
