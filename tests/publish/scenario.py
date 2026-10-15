"""A small but real publishing scenario, built through the production write paths.

Fixtures are replayed through each collector's ``COLLECTOR.run_fixture`` (greenhouse,
edgar, news), Form D events are linked by the resolver, a sales hiring spike is added as
``job_opened`` events on the greenhouse fixture board and turned into a ``hiring_velocity``
event by the velocity module, then signals are built and the ``default`` profile scored.
Every company, URL and title is synthetic or from the scrubbed fixtures; nothing names a
person.

Timeline (UTC): the board's first poll on 2026-09-25, the spike on 2026-09-30 to
2026-10-02, velocity evaluated 2026-10-02 18:00, the Form D filings dated 2026-10-01, news
from 2026-09-28 to 2026-10-04, and the publish instant :data:`NOW` on Tuesday 2026-10-06
12:00 (ISO week 2026-W41, so the weekly edition is 2026-W40).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors import edgar, greenhouse, news
from tell.collectors.base import CollectorSpec, Event, finish_run, start_run, write_events
from tell.models import AtsBoard, Company, Watchlist, WatchlistItem
from tell.resolve.match import backfill_aliases, resolve_events
from tell.signals.profiles import Profile, load_profiles
from tell.signals.scoring import build_signals, score_profile
from tell.signals.velocity import emit_velocity_events

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
AS_OF: date = NOW.date()
DAY = timedelta(days=1)
CROSSED_AT = NOW - timedelta(hours=1)
"""When the day's scoring run emits the ``score_crossed_threshold`` events."""

ROBOTICS = "examplerobotics.example"
BLUEFIN = "bluefin.example"
GREENHOUSE = greenhouse.FIXTURE_DOMAIN
QUILLSTONE = "quillstone.example"
OLDCO = "oldco.example"

SPIKE_TITLES = (
    "Account Executive, Mid-Market",
    "Enterprise Account Executive",
    "Sales Development Representative",
)


@dataclass
class Scenario:
    profiles: dict[str, Profile]
    companies: dict[str, int]
    public: Watchlist
    private: Watchlist

    @property
    def default(self) -> Profile:
        return self.profiles["default"]


def profiles() -> dict[str, Profile]:
    """Shipped profiles only (no ``tell-private`` leaks into the tests)."""
    return load_profiles(private_dir=None)


def replay(session: Session, spec: CollectorSpec, case: str, now: datetime) -> None:
    """One fixture replay wrapped in a ``runs`` row, as a scheduled run would be."""
    run_id = start_run(session, spec.source, now=now)
    result = spec.run_fixture(session, spec.fixture_dir / case, run_id=run_id, now=now)
    finish_run(
        session,
        run_id,
        status="ok",
        fetched=result.fetched,
        inserted=result.inserted,
        skipped=result.skipped,
        now=now + timedelta(minutes=2),
    )


def _company(session: Session, domain: str, name: str) -> Company:
    company = session.scalars(select(Company).where(Company.domain == domain)).one_or_none()
    if company is None:
        # A YC directory company (ADR 0009): the Form D guards' YC facts are present, so
        # guard (f) for companies without them (ADR 0035) does not apply.
        company = Company(domain=domain, name=name, yc_status="Active")
        session.add(company)
        session.flush()
    return company


def spike_events(session: Session, company_id: int) -> list[Event]:
    """Three new sales postings on the greenhouse fixture board, 2026-09-30 to 2026-10-02."""
    events = []
    for n, title in enumerate(SPIKE_TITLES):
        occurred = datetime(2026, 9, 30, 15, tzinfo=UTC) + DAY * n
        external = f"spike-{n}"
        payload: dict[str, Any] = {
            "vendor": "greenhouse",
            "board_identifier": greenhouse.FIXTURE_BOARD,
            "posting_external_id": external,
            "title": title,
            "role_family": "sales",
            "posted_at": occurred.isoformat(),
            "fingerprint": f"fp-{n}",
            "is_intern": False,
            "repost_of_external_id": None,
        }
        events.append(
            Event(
                source="greenhouse",
                type="job_opened",
                external_id=f"greenhouse:{greenhouse.FIXTURE_BOARD}:{external}",
                version=occurred.isoformat(),
                occurred_at=occurred,
                observed_at=occurred + timedelta(hours=3),
                url=f"https://boards.greenhouse.io/{greenhouse.FIXTURE_BOARD}/jobs/{external}",
                payload=payload,
                run_id="greenhouse-scenario",
                company_id=company_id,
            )
        )
    write_events(session, events)
    session.flush()
    return events


def old_signal(session: Session, company_id: int) -> None:
    """A launch headline 200 days before :data:`NOW`: outside the 180-day size budget."""
    occurred = NOW - DAY * 200
    write_events(
        session,
        [
            Event(
                source="news",
                type="news_launch",
                external_id="oldco-launch",
                version="1",
                occurred_at=occurred,
                observed_at=occurred,
                url="https://news.example/oldco-launch",
                payload={"title": "Oldco launches a product"},
                run_id="news-scenario",
                company_id=company_id,
            )
        ],
    )


def build(session: Session) -> Scenario:
    """Build the whole scenario and score ``default`` for :data:`AS_OF` at :data:`CROSSED_AT`."""
    profs = profiles()
    robotics = _company(session, ROBOTICS, "Example Robotics")
    bluefin = _company(session, BLUEFIN, "Bluefin Data Systems")
    oldco = _company(session, OLDCO, "Oldco")

    replay(session, greenhouse.COLLECTOR, "normal.json", datetime(2026, 9, 25, 2, 17, tzinfo=UTC))
    board = session.scalars(
        select(AtsBoard).where(AtsBoard.identifier == greenhouse.FIXTURE_BOARD)
    ).one()
    assert board.company_id is not None
    spike_events(session, board.company_id)
    emit_velocity_events(session, now=datetime(2026, 10, 2, 18, 0, tzinfo=UTC))

    replay(session, edgar.COLLECTOR, "normal.json", datetime(2026, 10, 2, 2, 17, tzinfo=UTC))
    replay(session, news.COLLECTOR, "normal.json", datetime(2026, 10, 5, 20, 17, tzinfo=UTC))
    backfill_aliases(session)
    resolve_events(session, now=NOW - DAY)
    old_signal(session, oldco.id)

    build_signals(session, now=NOW, profile=profs["default"])
    score_profile(session, profs["default"], AS_OF, now=CROSSED_AT)

    quill = session.scalars(select(Company).where(Company.domain == QUILLSTONE)).one()
    public = Watchlist(owner="team-a", name="Pilot list", slug="pilot", is_public=True)
    private = Watchlist(owner="team-b", name="Private list")
    session.add_all([public, private])
    session.flush()
    for watchlist, members in (
        (public, [robotics.id, board.company_id, quill.id]),
        (private, [robotics.id, bluefin.id]),
    ):
        session.add_all(WatchlistItem(watchlist_id=watchlist.id, company_id=m) for m in members)
    session.flush()
    companies = {c.domain: c.id for c in session.scalars(select(Company).order_by(Company.domain))}
    return Scenario(profiles=profs, companies=companies, public=public, private=private)
