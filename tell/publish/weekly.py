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

Rounds filed, not announced
===========================

:func:`rounds_not_announced` (ADR 0042) is the edition's third list. It is published in the
weekly page and on its own (``rounds-not-announced.html``,
``api/v0/rounds-not-announced.json``), not in ``latest.json``: press is read again at every
render, so the list shrinks as funding headlines arrive while ``latest.json`` stays fixed
for the week. One row per original Form D (``form_d_filed`` from EDGAR with
``is_amendment`` false, not a pooled fund by :func:`tell.resolve.guards.fund_signals`)
whose filing date falls in the 14 days that end with the edition's week
(:func:`rounds_window`: the week and the week before) and that

* is linked by a link the speed-vs-press cohort trusts (:func:`trusted_match` and the CIK
  rule of :mod:`tell.backtest.speed_vs_press`: a reviewer's link, rules R0-R4 with no
  guard failure, or a CIK match whose CIK has a trusted ``cik_source`` or was set by
  such a link);
* belongs to a company with a published account page (``published``, the site's size
  budget) whose ``source`` is not ``manual``: intake lists, review verdicts and domains
  added by hand never appear, so no one's private list can be read from the page;
* has no ``news_funding`` event of its company dated from the filing date minus 14 days
  through the publish instant ``now`` (any funding headline counts, including the
  one-word brands the speed-vs-press report discounts: a false "not announced" is the
  worse error on a public page).

Newest filing first, then domain, then URL. The amounts are the payload's own
``total_offering_amount`` (with its ``indefinite`` flag) and ``total_amount_sold``
(``None`` when undisclosed) and the receipt is the filing's EDGAR URL (a row without an ``https://www.sec.gov/`` URL is left
out). No person's name is read: Form D related persons are never stored.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import distinct, func, or_, select, union
from sqlalchemy.orm import Session

from tell.backtest.speed_vs_press import TRUSTED_CIK_SOURCES, TRUSTED_RULES
from tell.collectors.news_classify import event_type
from tell.config import get_settings
from tell.models import AtsBoard, Company, EventRow, Posting, PostingObservation
from tell.resolve.guards import IssuerFacts, fund_signals
from tell.signals.scoring import FORM_D_FILED
from tell.signals.velocity import HIRING_VELOCITY

__all__ = [
    "CHANGELOG_PATH",
    "EDGAR_RECEIPT_PREFIX",
    "MANUAL_SOURCE",
    "METHOD_KEYWORDS",
    "METHOD_LINE_MAX",
    "NEWS_FUNDING",
    "NO_METHOD_CHANGE",
    "PRESS_LOOKBACK_DAYS",
    "ROUNDS_WINDOW_DAYS",
    "SALES_FAMILY",
    "TOP_N",
    "Coverage",
    "FundingRow",
    "HiringRow",
    "IsoWeek",
    "RoundRow",
    "RoundsData",
    "WeeklyData",
    "build_weekly",
    "coverage",
    "method_change_line",
    "resolve_week",
    "rounds_not_announced",
    "rounds_window",
    "top_funding",
    "top_hiring",
    "trusted_match",
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
_METHOD_LABEL_RE = re.compile(r"^(" + "|".join(METHOD_KEYWORDS) + r")( rule)?:", re.IGNORECASE)
"""A bullet that opens with a method label, e.g. ``Scoring rule:`` or ``Velocity:``."""
_MARKDOWN = re.compile(r"`|\*\*")
METHOD_LINE_MAX = 280
"""Length cap of the method-change line on the weekly page and in ``latest.json``."""

NEWS_FUNDING = event_type("funding")
"""``news_funding``: a funding headline the news collector kept and linked to a company."""
ROUNDS_WINDOW_DAYS = 14
"""Filing dates the rounds list covers: the edition's week and the week before."""
PRESS_LOOKBACK_DAYS = 14
"""Press counts from the filing date minus this many days (the speed-vs-press window start)."""
MANUAL_SOURCE = "manual"
"""``companies.source`` of rows added by hand (an intake list, a review verdict, a domain
passed to ``tell discover``): never in the rounds list."""
EDGAR_RECEIPT_PREFIX = "https://www.sec.gov/"
"""Every rounds row links its EDGAR filing."""


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
class RoundRow:
    domain: str
    name: str
    date_filed: str
    amount_usd: int | None
    """``total_offering_amount`` as stored; ``None`` when undisclosed or indefinite."""
    amount_indefinite: bool
    """The filing states an indefinite offering amount (``total_offering_amount_indefinite``)."""
    amount_sold_usd: int | None
    """``total_amount_sold`` as stored; ``None`` when undisclosed."""
    url: str
    """The filing's EDGAR folder: the receipt."""


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


def rounds_window(week: IsoWeek) -> tuple[datetime, datetime]:
    """``[start, end)`` of the filing dates the rounds list covers: 14 days ending with ``week``."""
    return week.end - timedelta(days=ROUNDS_WINDOW_DAYS), week.end


@dataclass(frozen=True)
class RoundsData:
    """Rounds filed, not announced: one edition's list as read at ``now`` (ADR 0042)."""

    week: IsoWeek
    now: datetime
    """The publish instant: funding headlines are read through it."""
    rounds: list[RoundRow]

    @property
    def filed_from(self) -> date:
        return rounds_window(self.week)[0].date()

    @property
    def filed_to(self) -> date:
        """Inclusive: the last day of the edition's week."""
        return rounds_window(self.week)[1].date() - timedelta(days=1)

    @property
    def as_of(self) -> date:
        """The UTC day of the publish instant."""
        return self.now.astimezone(UTC).date()

    @property
    def method(self) -> str:
        """The one-sentence method printed under the table: window, cohort, freshness."""
        return (
            f"Every original Form D (no amendments, no pooled funds) filed {self.filed_from} "
            f"to {self.filed_to} by a company with a tell account page and a trusted link to "
            "the issuer (an exact CIK, a name match that passed the Form D guards, or a "
            "reviewer's link), with no funding headline in tell's news data from "
            f"{PRESS_LOOKBACK_DAYS} days before the filing through {self.as_of}; the list "
            "changes as news arrives."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "week": self.week.label,
            "filed_from": self.filed_from.isoformat(),
            "filed_to": self.filed_to.isoformat(),
            "as_of": self.as_of.isoformat(),
            "rounds": [
                {
                    "domain": r.domain,
                    "name": r.name,
                    "date_filed": r.date_filed,
                    "amount_usd": r.amount_usd,
                    "amount_indefinite": r.amount_indefinite,
                    "amount_sold_usd": r.amount_sold_usd,
                    "url": r.url,
                }
                for r in self.rounds
            ],
            "method": self.method,
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


def trusted_match(match: Any) -> bool:
    """Whether a ``payload.match`` record is a link the speed-vs-press cohort trusts.

    A reviewer's link (``rule = 'manual'``, which wins over a guard failure left from the R5
    candidate it resolved) or rules R0-R4 with no guard failure. ``guard_failed`` counts as
    failed when it is ``true``, a non-empty list, object or string, or a non-zero number,
    exactly as :data:`tell.backtest.speed_vs_press.SQL` reads it; a record that is not an
    object is not trusted.
    """
    if not isinstance(match, Mapping):
        return False
    rule = match.get("rule")
    if rule == "manual":
        return True
    return rule in TRUSTED_RULES and not match.get("guard_failed")


def _vouched_ciks(session: Session, company_ids: Collection[int]) -> dict[int, set[str]]:
    """``{company_id: CIKs}`` carried by the companies' EDGAR events whose link is trusted.

    A CIK one of these links carries vouches for the company's filings linked by CIK alone
    (the speed-vs-press ``trusted`` CTE): the CIK was set by a trusted link.
    """
    if not company_ids:
        return {}
    stmt = select(
        EventRow.company_id, EventRow.payload["match"], EventRow.payload["cik"].astext
    ).where(
        EventRow.source == "edgar",
        EventRow.company_id.in_(sorted(company_ids)),
        EventRow.payload["match"].is_not(None),
    )
    vouched: dict[int, set[str]] = defaultdict(set)
    for company_id, match, cik in session.execute(stmt):
        if cik is not None and trusted_match(match):
            vouched[int(company_id)].add(str(cik))
    return vouched


def _press(
    session: Session, company_ids: Collection[int], since: datetime, until: datetime
) -> dict[int, list[datetime]]:
    """``{company_id: [occurred_at]}`` of the companies' ``news_funding`` events in the span."""
    if not company_ids:
        return {}
    stmt = select(EventRow.company_id, EventRow.occurred_at).where(
        EventRow.type == NEWS_FUNDING,
        EventRow.company_id.in_(sorted(company_ids)),
        EventRow.occurred_at >= since,
        EventRow.occurred_at <= until,
    )
    press: dict[int, list[datetime]] = defaultdict(list)
    for company_id, occurred_at in session.execute(stmt):
        if company_id is not None:  # always: the query selects by company id
            press[company_id].append(occurred_at)
    return press


def _original_operating_filing(row: EventRow) -> bool:
    """An original Form D (not an amendment) of an operating company with an EDGAR receipt."""
    payload = row.payload or {}
    if payload.get("is_amendment") is not False:
        return False
    if not row.url.startswith(EDGAR_RECEIPT_PREFIX):
        return False
    issuer = IssuerFacts.from_payload(str(payload.get("issuer_name") or ""), payload)
    return not fund_signals(issuer)


def rounds_not_announced(
    session: Session, week: IsoWeek, *, now: datetime, published: Collection[int]
) -> RoundsData:
    """Original Form D filings of ``week``'s 14-day window with no funding headline by ``now``.

    ``published`` holds the ids of the companies that have an account page (the site's
    :func:`tell.publish.site.published_company_ids`); ``now`` is the publish instant and
    must be timezone-aware. See the module docstring for every rule. Reads only.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be a timezone-aware datetime, got naive {now!r}"
        raise ValueError(msg)
    pages = set(published)
    start, end = rounds_window(week)
    stmt = (
        select(EventRow, Company, EventRow.payload["cik"].astext)
        .join(Company, Company.id == EventRow.company_id)
        .where(
            EventRow.source == "edgar",
            EventRow.type == FORM_D_FILED,
            EventRow.occurred_at >= start,
            EventRow.occurred_at < end,
            Company.source != MANUAL_SOURCE,
        )
        .order_by(EventRow.event_id)
    )
    candidates = [
        (row, company, cik)
        for row, company, cik in session.execute(stmt)
        if company.id in pages and _original_operating_filing(row)
    ]
    by_cik = {company.id for row, company, _ in candidates if "match" not in (row.payload or {})}
    vouched = _vouched_ciks(session, by_cik)

    def trusted(row: EventRow, company: Company, cik: str | None) -> bool:
        payload = row.payload or {}
        if "match" in payload:
            return trusted_match(payload["match"])
        return (
            company.cik is not None
            and cik == company.cik
            and (company.cik_source in TRUSTED_CIK_SOURCES or cik in vouched.get(company.id, ()))
        )

    linked = [(row, company) for row, company, cik in candidates if trusted(row, company, cik)]
    press = _press(
        session,
        {company.id for _, company in linked},
        start - timedelta(days=PRESS_LOOKBACK_DAYS),
        now,
    )
    rounds: list[RoundRow] = []
    for row, company in linked:
        filed_on = row.occurred_at.astimezone(UTC).date()
        floor = datetime.combine(filed_on, time(0), tzinfo=UTC) - timedelta(
            days=PRESS_LOOKBACK_DAYS
        )
        if any(at >= floor for at in press.get(company.id, ())):
            continue
        payload = row.payload or {}
        rounds.append(
            RoundRow(
                domain=company.domain,
                name=company.name or company.domain,
                date_filed=_day(payload.get("date_filed"), row.occurred_at),
                amount_usd=_usd(payload.get("total_offering_amount")),
                amount_indefinite=payload.get("total_offering_amount_indefinite") is True,
                amount_sold_usd=_usd(payload.get("total_amount_sold")),
                url=row.url,
            )
        )
    rounds.sort(key=lambda r: (r.domain, r.url))
    rounds.sort(key=lambda r: r.date_filed, reverse=True)
    return RoundsData(week=week, now=now, rounds=rounds)


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


def _shorten(text: str, limit: int = METHOD_LINE_MAX) -> str:
    """``text`` cut to ``limit`` characters at the last sentence or clause boundary.

    A bullet that is one long sentence (the release bullets) is cut mid-text with an
    ellipsis; otherwise the cut lands after the last ``. `` or ``; `` in the first half or
    later, so the public weekly page never ends on a half-word.
    """
    if len(text) <= limit:
        return text
    head = text[: limit - 3]
    boundary = max(head.rfind(". "), head.rfind("; "))
    if boundary >= limit // 2:
        return head[: boundary + 1].rstrip(";").rstrip()
    return head.rstrip() + "..."


def method_change_line(changelog: Path | None = None) -> str:
    """The latest ``CHANGELOG.md`` bullet describing a method change.

    A bullet that *starts* with a method label (``Scoring rule:``, ``Scoring:``,
    ``Velocity:``, ``Resolution:``) wins over one that merely mentions the word, so a
    phase release bullet listing every module never displaces the one-line rule change
    beneath it. The first such bullet in file order (newest first) is used; without one,
    the first bullet mentioning scoring, velocity or resolution. Cut to
    :data:`METHOD_LINE_MAX` characters at a sentence boundary.
    """
    path = changelog or CHANGELOG_PATH
    if not path.is_file():
        return NO_METHOD_CHANGE
    bullets: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(("- ", "* ")):
            bullets.append(_MARKDOWN.sub("", line[2:]).strip())
    for text in bullets:
        if _METHOD_LABEL_RE.match(text):
            return _shorten(text)
    for text in bullets:
        if _METHOD_RE.search(text):
            return _shorten(text)
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
