"""The weekly page's data (spec section 9 "Weekly page"): ``weekly/{ISO-week}.html`` + ``latest.json``.

One edition covers one ISO week, Monday 00:00 UTC to the next Monday 00:00 UTC
(:class:`IsoWeek`). The weekly workflow runs Monday 06:23 UTC; there, ``--week current``
names the *current edition*: the most recent complete ISO week (:func:`resolve_week`),
because the week that started six hours earlier has nothing to report yet.

Contents
========

``top_hiring``
    Top 25 companies by **sales-hiring velocity** across every monitored company (not
    watchlists): the sum of ``hiring_velocity`` event magnitudes (``min(new7, 10)``) for
    role family ``sales`` whose ``occurred_at`` falls in the week, each with its receipts
    (event URL, summary, counted postings). Only the ``sales`` family counts: ``gtm_ops``
    and ``customer`` are the default profile's other target families but are not
    "sales hiring" (ADR 0023).
``top_funding``
    Top 25 new Form D filings (``form_d_filed`` with ``is_amendment`` false) filed in the
    week by monitored companies, by ``totalOfferingAmount`` (integer USD; undisclosed
    amounts sort last), with the filing URL.
``method_change``
    The most recent ``CHANGELOG.md`` bullet (top of the file first) mentioning scoring,
    velocity or resolution; ``"No method changes this week"`` when there is none.
``coverage``
    ``companies_monitored`` (companies with an active board or a CIK), ``boards_polled``
    (boards with a posting observation in the week or last polled during it) and
    ``filings_read`` (EDGAR events observed in the week).
``repo_url``
    The code repository.

Ties sort by domain, so an edition is deterministic.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import distinct, func, or_, select, union
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.models import AtsBoard, Company, EventRow, Posting, PostingObservation
from tell.signals.scoring import FORM_D_FILED
from tell.signals.velocity import HIRING_VELOCITY

__all__ = [
    "CHANGELOG_PATH",
    "METHOD_KEYWORDS",
    "NO_METHOD_CHANGE",
    "SALES_FAMILY",
    "TOP_N",
    "Coverage",
    "FundingRow",
    "HiringRow",
    "IsoWeek",
    "WeeklyData",
    "build_weekly",
    "coverage",
    "method_change_line",
    "resolve_week",
    "top_funding",
    "top_hiring",
    "velocity_postings",
]

TOP_N = 25
SALES_FAMILY = "sales"
NO_METHOD_CHANGE = "No method changes this week"
METHOD_KEYWORDS: tuple[str, ...] = ("scoring", "velocity", "resolution")
CHANGELOG_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
"""The repository's changelog (``tell`` runs from its checkout; never published to PyPI)."""

_WEEK = timedelta(days=7)
_WEEK_LABEL = re.compile(r"^(\d{4})-W(\d{2})$")
_METHOD_RE = re.compile(r"\b(" + "|".join(METHOD_KEYWORDS) + r")\b", re.IGNORECASE)
_MARKDOWN = re.compile(r"`|\*\*")


@dataclass(frozen=True, order=True)
class IsoWeek:
    """An ISO 8601 week (``2026-W41``)."""

    year: int
    week: int

    def __post_init__(self) -> None:
        date.fromisocalendar(self.year, self.week, 1)  # raises ValueError when invalid

    @classmethod
    def parse(cls, label: str) -> IsoWeek:
        match = _WEEK_LABEL.match(label.strip())
        if match is None:
            msg = f"expected an ISO week like 2026-W41, got {label!r}"
            raise ValueError(msg)
        return cls(int(match.group(1)), int(match.group(2)))

    @classmethod
    def containing(cls, day: date) -> IsoWeek:
        iso = day.isocalendar()
        return cls(iso.year, iso.week)

    @property
    def label(self) -> str:
        return f"{self.year}-W{self.week:02d}"

    @property
    def monday(self) -> date:
        return date.fromisocalendar(self.year, self.week, 1)

    @property
    def start(self) -> datetime:
        return datetime.combine(self.monday, time(0), tzinfo=UTC)

    @property
    def end(self) -> datetime:
        """Exclusive: the next Monday 00:00 UTC."""
        return self.start + _WEEK

    def previous(self) -> IsoWeek:
        return IsoWeek.containing(self.monday - _WEEK)


def resolve_week(value: str, now: datetime) -> IsoWeek:
    """``current`` (the most recent complete ISO week at ``now``) or a ``YYYY-Www`` label."""
    if value.strip().lower() == "current":
        return IsoWeek.containing(now.astimezone(UTC).date()).previous()
    return IsoWeek.parse(value)


# --- shapes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HiringRow:
    domain: str
    name: str
    velocity: int
    """Sum of the week's sales ``hiring_velocity`` magnitudes."""
    receipts: list[dict[str, Any]]
    """``[{occurred_at, url, summary, postings: [{title, posted_at, url}]}]``."""


@dataclass(frozen=True)
class FundingRow:
    domain: str
    name: str
    amount_usd: int | None
    amount_sold_usd: int | None
    date_filed: str
    url: str


@dataclass(frozen=True)
class Coverage:
    companies_monitored: int
    boards_polled: int
    filings_read: int


@dataclass(frozen=True)
class WeeklyData:
    week: IsoWeek
    top_hiring: list[HiringRow]
    top_funding: list[FundingRow]
    method_change: str
    coverage: Coverage
    repo_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "week": self.week.label,
            "start": self.week.monday.isoformat(),
            "end": (self.week.monday + timedelta(days=6)).isoformat(),
            "top_hiring": [
                {
                    "domain": r.domain,
                    "name": r.name,
                    "velocity": r.velocity,
                    "receipts": r.receipts,
                }
                for r in self.top_hiring
            ],
            "top_funding": [
                {
                    "domain": r.domain,
                    "name": r.name,
                    "amount_usd": r.amount_usd,
                    "amount_sold_usd": r.amount_sold_usd,
                    "date_filed": r.date_filed,
                    "url": r.url,
                }
                for r in self.top_funding
            ],
            "method_change": self.method_change,
            "coverage": {
                "companies_monitored": self.coverage.companies_monitored,
                "boards_polled": self.coverage.boards_polled,
                "filings_read": self.coverage.filings_read,
            },
            "repo_url": self.repo_url,
        }


# --- helpers ------------------------------------------------------------------------------------


def _usd(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return round(value)
    if isinstance(value, str) and value.strip():
        try:
            return round(float(value))
        except ValueError:
            return None
    return None


def _day(value: Any, fallback: datetime) -> str:
    if isinstance(value, str) and len(value) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return fallback.astimezone(UTC).date().isoformat()


def velocity_postings(payload: dict[str, Any], fallback: datetime) -> list[dict[str, str]]:
    """``[{title, posted_at (date), url}]`` from a ``hiring_velocity`` payload's receipt."""
    out: list[dict[str, str]] = []
    for item in payload.get("counted_postings") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        out.append(
            {
                "title": str(item.get("title") or ""),
                "posted_at": _day(item.get("posted_at"), fallback),
                "url": str(item["url"]),
            }
        )
    return out


def _velocity_summary(payload: dict[str, Any], magnitude: int) -> str:
    new7 = payload.get("new7", magnitude)
    base = payload.get("base")
    tail = f" vs {base} baseline" if base is not None else ""
    return f"{new7} new sales roles in 7 days{tail}"


def _monitored() -> Any:
    active_board = (
        select(AtsBoard.id)
        .where(AtsBoard.company_id == Company.id, AtsBoard.status == "active")
        .exists()
    )
    return or_(Company.cik.is_not(None), active_board)


# --- the lists -------------------------------------------------------------------------------


def top_hiring(session: Session, week: IsoWeek, limit: int = TOP_N) -> list[HiringRow]:
    """Top companies by summed sales ``hiring_velocity`` magnitude in ``week``."""
    stmt = (
        select(EventRow, Company.domain, Company.name)
        .join(Company, Company.id == EventRow.company_id)
        .where(
            EventRow.type == HIRING_VELOCITY,
            EventRow.payload.contains({"role_family": SALES_FAMILY}),
            EventRow.occurred_at >= week.start,
            EventRow.occurred_at < week.end,
        )
        .order_by(EventRow.occurred_at, EventRow.event_id)
    )
    totals: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    receipts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row, domain, name in session.execute(stmt):
        magnitude = int(row.magnitude)
        totals[domain] += magnitude
        names[domain] = name
        receipts[domain].append(
            {
                "occurred_at": row.occurred_at.astimezone(UTC).date().isoformat(),
                "url": row.url,
                "summary": _velocity_summary(row.payload or {}, magnitude),
                "postings": velocity_postings(row.payload or {}, row.occurred_at),
            }
        )
    ranked = sorted(totals, key=lambda d: (-totals[d], d))[:limit]
    return [HiringRow(d, names[d], totals[d], receipts[d]) for d in ranked]


def top_funding(session: Session, week: IsoWeek, limit: int = TOP_N) -> list[FundingRow]:
    """Top new Form D filings of monitored companies in ``week`` by offering amount."""
    stmt = (
        select(EventRow, Company.domain, Company.name)
        .join(Company, Company.id == EventRow.company_id)
        .where(
            EventRow.type == FORM_D_FILED,
            EventRow.occurred_at >= week.start,
            EventRow.occurred_at < week.end,
            _monitored(),
        )
        .order_by(EventRow.event_id)
    )
    rows: list[FundingRow] = []
    for row, domain, name in session.execute(stmt):
        payload = row.payload or {}
        if payload.get("is_amendment") is True:
            continue
        rows.append(
            FundingRow(
                domain=domain,
                name=name,
                amount_usd=_usd(payload.get("total_offering_amount")),
                amount_sold_usd=_usd(payload.get("total_amount_sold")),
                date_filed=_day(payload.get("date_filed"), row.occurred_at),
                url=row.url,
            )
        )
    rows.sort(key=lambda r: (r.amount_usd is None, -(r.amount_usd or 0), r.domain, r.url))
    return rows[:limit]


def coverage(session: Session, week: IsoWeek) -> Coverage:
    """Companies monitored (now), boards polled and filings read in ``week``."""
    monitored = session.execute(
        select(func.count()).select_from(Company).where(_monitored())
    ).scalar_one()
    observed = (
        select(Posting.board_id.label("board_id"))
        .join(PostingObservation, PostingObservation.posting_id == Posting.id)
        .where(
            PostingObservation.observed_at >= week.start,
            PostingObservation.observed_at < week.end,
        )
    )
    polled = select(AtsBoard.id.label("board_id")).where(
        AtsBoard.last_polled_at >= week.start, AtsBoard.last_polled_at < week.end
    )
    boards = union(observed, polled).subquery()
    boards_polled = session.execute(select(func.count(distinct(boards.c.board_id)))).scalar_one()
    filings = session.execute(
        select(func.count())
        .select_from(EventRow)
        .where(
            EventRow.source == "edgar",
            EventRow.observed_at >= week.start,
            EventRow.observed_at < week.end,
        )
    ).scalar_one()
    return Coverage(int(monitored), int(boards_polled), int(filings))


def method_change_line(changelog: Path | None = None) -> str:
    """The first ``CHANGELOG.md`` bullet mentioning scoring, velocity or resolution."""
    path = changelog or CHANGELOG_PATH
    if not path.is_file():
        return NO_METHOD_CHANGE
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith(("- ", "* ")):
            continue
        text = _MARKDOWN.sub("", line[2:]).strip()
        if _METHOD_RE.search(text):
            return text if len(text) <= 280 else text[:277].rstrip() + "..."
    return NO_METHOD_CHANGE


def build_weekly(
    session: Session, week: IsoWeek, *, changelog: Path | None = None, limit: int = TOP_N
) -> WeeklyData:
    """Everything the weekly page and ``api/v0/weekly/latest.json`` show for ``week``."""
    return WeeklyData(
        week=week,
        top_hiring=top_hiring(session, week, limit),
        top_funding=top_funding(session, week, limit),
        method_change=method_change_line(changelog),
        coverage=coverage(session, week),
        repo_url=get_settings().tell_repo_url,
    )
