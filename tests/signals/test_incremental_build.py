"""``build_signals`` reads only what can change, and agrees with a full re-derivation (ADR 0045).

The reference below is the pre-ADR-0045 algorithm, kept verbatim in spirit: load every
eligible event with its whole payload, derive each one, skip keys already present and
rewrite a ``funding`` magnitude its payload no longer supports. At every step of a
multi-day scenario both run on the same database state (the reference inside a
savepoint that is rolled back) and must leave identical ``signals`` rows, in the same
insertion order, and return the same count.

The scenario covers the cases a "last processed" checkpoint would get wrong:

* a **late arrival**: an event first written days after its ``occurred_at``, which is
  older than the previous build;
* a **late link**: a Form D written unlinked and linked to a company by a later
  ``tell resolve``;
* a **future-dated** event that only becomes eligible on a later run;
* **amended filings**: D/A amounts merged into the original payload, up and then back
  down, including the camel-case string amount;
* events that derive nothing (an 8-K without Item 5.02, a side-repo release, a first-poll
  tech baseline), which must stay signal-less.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from tell.collectors.base import (
    FIRST_POLL_KEY,
    TECH_MENTIONED_FIRST_TIME,
    Event,
    update_event_payload,
    write_events,
)
from tell.models import Company, EventRow, Signal
from tell.signals import scoring as sc
from tell.signals.profiles import Profile, load_profiles
from tell.signals.velocity import HIRING_VELOCITY

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 5, 15, 17, tzinfo=UTC)
DAY = timedelta(days=1)
RUN = "tell-test"


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    with freeze_time(T0):
        yield


@pytest.fixture
def profile() -> Profile:
    """``default`` with a tech keyword list, so a ``tech_adoption`` weight reads the payload."""
    base = load_profiles(private_dir=None)["default"].model_dump()
    base["weights"] = {**base["weights"], "tech_adoption": 1.0}
    base["tech_keywords"] = ["rust"]
    return Profile.model_validate(base)


# --- the reference: a full scan of every eligible event's whole payload ----------------------


def _reference_build(session: Session, *, now: datetime, profile: Profile) -> int:
    rows = list(
        session.scalars(
            select(EventRow)
            .where(
                EventRow.type.in_(sc.SIGNAL_EVENT_TYPES),
                EventRow.company_id.is_not(None),
                EventRow.occurred_at <= now,
            )
            .order_by(EventRow.occurred_at, EventRow.event_id)
        )
    )
    existing: dict[tuple[str, str, str | None], tuple[int, Decimal]] = {
        (s.event_id, s.signal_type, s.role_family): (s.id, s.magnitude)
        for s in session.scalars(select(Signal))
    }
    values: list[dict[str, Any]] = []
    amended: list[tuple[int, Decimal]] = []
    for row in rows:
        deriver = sc._DERIVERS.get(row.type)
        payload = row.payload or {}
        derived = deriver(row.magnitude, payload) if deriver is not None else None
        if derived is None or row.company_id is None:
            continue
        signal_type, family, magnitude = derived
        key = (row.event_id, signal_type, family)
        present = existing.get(key)
        if present is not None:
            if signal_type == sc.FUNDING and present[1] != sc._decimal(magnitude):
                amended.append((present[0], sc._decimal(magnitude)))
            continue
        existing[key] = (0, sc._decimal(magnitude))
        view = sc.SignalView(
            id=0,
            company_id=row.company_id,
            event_id=row.event_id,
            signal_type=signal_type,
            role_family=family,
            magnitude=sc._decimal(magnitude),
            half_life_days=profile.half_life_days[signal_type],
            starts_at=row.occurred_at,
            url=row.url,
            occurred_at=row.occurred_at,
            payload=payload,
        )
        values.append(
            {
                "company_id": row.company_id,
                "event_id": row.event_id,
                "signal_type": signal_type,
                "role_family": family,
                "weight": sc._decimal(profile.weight_for(view, keyword=view.keyword)),
                "magnitude": sc._decimal(magnitude),
                "half_life_days": profile.half_life_days[signal_type],
                "starts_at": row.occurred_at,
            }
        )
    for value in values:
        session.add(Signal(**value))
    for signal_id, magnitude in amended:
        session.execute(
            update(Signal)
            .where(Signal.id == signal_id)
            .values(magnitude=magnitude)
            .execution_options(synchronize_session="fetch")
        )
    session.flush()
    return len(values)


# --- helpers ---------------------------------------------------------------------------------


def _company(session: Session, n: int) -> Company:
    company = Company(domain=f"c{n}.example", name=f"Company {n}")
    session.add(company)
    session.flush()
    return company


def _event(
    session: Session,
    company: Company | None,
    kind: str,
    external_id: str,
    *,
    occurred_at: datetime,
    observed_at: datetime,
    payload: dict[str, Any],
    magnitude: float = 1,
    source: str = "synthetic",
) -> str:
    row = Event(
        source=source,
        type=kind,
        external_id=external_id,
        version="1",
        occurred_at=occurred_at,
        observed_at=observed_at,
        url=f"https://example.test/{external_id}",
        payload=payload,
        run_id=RUN,
        magnitude=magnitude,
        company_id=company.id if company is not None else None,
    )
    write_events(session, [row])
    session.flush()
    return row.event_id


def _snapshot(session: Session) -> list[tuple[Any, ...]]:
    """Every ``signals`` row without its id, in insertion (id) order."""
    session.expire_all()
    return [
        (
            s.company_id,
            s.event_id,
            s.signal_type,
            s.role_family,
            s.weight,
            s.magnitude,
            s.half_life_days,
            s.starts_at,
        )
        for s in session.scalars(select(Signal).order_by(Signal.id))
    ]


def _build_both(session: Session, *, now: datetime, profile: Profile) -> list[tuple[Any, ...]]:
    """Run the reference (rolled back) and the real build on the same state; assert they agree."""
    savepoint = session.begin_nested()
    expected_count = _reference_build(session, now=now, profile=profile)
    expected = _snapshot(session)
    savepoint.rollback()
    session.expire_all()
    count = sc.build_signals(session, now=now, profile=profile)
    actual = _snapshot(session)
    assert count == expected_count
    assert actual == expected
    return actual


@contextmanager
def _statements(session: Session) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- the scenario ----------------------------------------------------------------------------


def test_incremental_build_matches_full_rederivation_across_late_and_amended_events(
    db_session: Session, profile: Profile
) -> None:
    acme, beta = _company(db_session, 1), _company(db_session, 2)
    day1, day2, day3 = T0 - 2 * DAY, T0 - DAY, T0

    # --- day 1: the first build ---
    formd = _event(
        db_session,
        acme,
        sc.FORM_D_FILED,
        "0000000001-26-000001",
        occurred_at=day1 - 3 * DAY,
        observed_at=day1,
        payload={
            "is_amendment": False,
            "total_offering_amount": 18_000_000,
            "total_amount_sold": 5_000_000,
            "issuer_state": "DE",
            "amendments": [],
        },
        source="edgar",
    )
    camel = _event(
        db_session,
        beta,
        sc.FORM_D_FILED,
        "0000000002-26-000001",
        occurred_at=day1 - 4 * DAY,
        observed_at=day1,
        payload={"is_amendment": False, "totalOfferingAmount": "2500000", "amendments": []},
        source="edgar",
    )
    _event(
        db_session,
        acme,
        HIRING_VELOCITY,
        f"{acme.id}:sales",
        occurred_at=day1,
        observed_at=day1,
        payload={"role_family": "sales", "new7": 4, "base": 0.5},
        magnitude=4,
        source="tell",
    )
    _event(
        db_session,
        acme,
        sc.FORM_8K_FILED,
        "8k-502",
        occurred_at=day1 - DAY,
        observed_at=day1,
        payload={"items": ["5.02", "9.01"]},
        source="edgar",
    )
    _event(
        db_session,
        acme,
        sc.FORM_8K_FILED,
        "8k-plain",
        occurred_at=day1 - DAY,
        observed_at=day1,
        payload={"items": ["9.01"], "item_5_02": False},
        source="edgar",
    )
    _event(
        db_session,
        beta,
        sc.GITHUB_RELEASE,
        "rel-side",
        occurred_at=day1 - DAY,
        observed_at=day1,
        payload={"repo": "side", "is_flagship": False},
        source="github",
    )
    _event(
        db_session,
        beta,
        TECH_MENTIONED_FIRST_TIME,
        "tech-baseline",
        occurred_at=day1 - DAY,
        observed_at=day1,
        payload={"keyword": "go", FIRST_POLL_KEY: True},
    )
    _event(
        db_session,
        beta,
        TECH_MENTIONED_FIRST_TIME,
        "tech-rust",
        occurred_at=day1 - DAY,
        observed_at=day1,
        payload={"keyword": "rust", FIRST_POLL_KEY: False},
    )
    _event(
        db_session,
        beta,
        sc.NEWS_LAUNCH,
        "launch-tomorrow",
        occurred_at=day2 - timedelta(hours=1),  # in the future on day 1, due on day 2
        observed_at=day1,
        payload={"title": "Beta launches its cloud"},
        source="news",
    )
    unlinked = _event(
        db_session,
        None,
        sc.FORM_D_FILED,
        "0000000003-26-000001",
        occurred_at=day1 - 5 * DAY,
        observed_at=day1,
        payload={"is_amendment": False, "total_offering_amount": 7_000_000, "amendments": []},
        source="edgar",
    )
    # Edge payloads for the SQL pre-filter: each must derive exactly as the Python deriver says.
    edge_cases: list[tuple[str, dict[str, Any]]] = [
        (sc.FORM_8K_FILED, {"items": [5.02]}),  # numeric item: derives leadership
        (sc.FORM_8K_FILED, {"item_5_02": True}),  # flag only: derives
        (sc.FORM_8K_FILED, {"items": "5.02"}),  # not a list: derives nothing
        (sc.FORM_8K_FILED, {"items": [["5.02"]]}),  # nested: derives nothing
        (sc.GITHUB_RELEASE, {"repo": "core"}),  # no flag: derives nothing
        (sc.GITHUB_RELEASE, {"repo": "core", "is_flagship": "true"}),  # string: nothing
        (TECH_MENTIONED_FIRST_TIME, {"keyword": "rust", FIRST_POLL_KEY: "true"}),  # derives
        (TECH_MENTIONED_FIRST_TIME, {"keyword": "rust"}),  # no flag: derives
        (sc.FORM_D_FILED, {"is_amendment": "true", "total_offering_amount": 1_000}),  # derives
        (sc.FORM_D_FILED, {"is_amendment": True, "total_offering_amount": 1_000}),  # nothing
    ]
    for n, (kind, payload) in enumerate(edge_cases):
        _event(
            db_session,
            beta,
            kind,
            f"edge-{n}",
            occurred_at=day1 - 2 * DAY,
            observed_at=day1,
            payload=payload,
        )
    first = _build_both(db_session, now=day1, profile=profile)
    assert {(r[1], r[2]) for r in first} >= {(formd, "funding"), (camel, "funding")}
    assert len(first) == 5 + 5  # 2 funding, velocity, 8-K 5.02, rust tech; 5 edge cases

    # --- day 2: a late arrival, a late link, an amendment, and the future event comes due ---
    late = _event(
        db_session,
        acme,
        sc.NEWS_LEADERSHIP,
        "late-cro",
        occurred_at=day1 - 6 * DAY,  # dated before the day-1 build ran
        observed_at=day2,
        payload={"title": "Acme appoints a chief revenue officer"},
        source="news",
    )
    db_session.execute(
        update(EventRow).where(EventRow.event_id == unlinked).values(company_id=beta.id)
    )
    assert update_event_payload(
        db_session,
        formd,
        {
            "total_offering_amount": 400_000_000,
            "last_amended_on": "2026-10-03",
            "amendments": [{"accession_number": "0000000001-26-000002"}],
        },
    )
    assert update_event_payload(db_session, camel, {"totalOfferingAmount": "90000000"})
    second = _build_both(db_session, now=day2, profile=profile)
    events_with_signals = {r[1] for r in second}
    assert late in events_with_signals
    assert unlinked in events_with_signals
    by_event = {r[1]: r for r in second}
    assert by_event[formd][5] == Decimal("8.0")  # log10(4e8) capped at 8, same row
    assert len(second) == 8 + 5

    # --- day 3: the amendment goes back down; nothing new ---
    assert update_event_payload(db_session, formd, {"total_offering_amount": 30_000_000})
    third = _build_both(db_session, now=day3, profile=profile)
    assert {r[1]: r for r in third}[formd][5] == sc._decimal(sc.funding_magnitude(30_000_000))

    # --- a rerun is a no-op, and a rebuild from scratch reproduces the incremental table ---
    assert _build_both(db_session, now=day3, profile=profile) == third
    db_session.execute(Signal.__table__.delete())
    assert sc.build_signals(db_session, now=day3, profile=profile) == len(third)
    assert sorted(_snapshot(db_session), key=repr) == sorted(third, key=repr)


def test_build_never_selects_a_whole_event_payload(db_session: Session, profile: Profile) -> None:
    """Only single-key projections (``payload -> 'k'`` / ``payload['k']``) reach the wire."""
    acme = _company(db_session, 1)
    _event(
        db_session,
        acme,
        sc.FORM_D_FILED,
        "0000000001-26-000001",
        occurred_at=T0 - DAY,
        observed_at=T0,
        payload={"is_amendment": False, "total_offering_amount": 18_000_000},
        source="edgar",
    )
    with _statements(db_session) as seen:
        assert sc.build_signals(db_session, now=T0, profile=profile) == 1
        assert sc.build_signals(db_session, now=T0, profile=profile) == 0
    selects = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
    assert selects
    bare = re.compile(r"events\.payload(?!\s*(->|\[))")
    assert not [s for s in selects if bare.search(s)]


def test_rerun_reads_no_event_that_already_has_its_signal(
    db_session: Session, profile: Profile
) -> None:
    """The anti-join leaves only signal-less events (which derive nothing) as candidates."""
    acme = _company(db_session, 1)
    for n in range(5):
        _event(
            db_session,
            acme,
            sc.NEWS_LAUNCH,
            f"launch-{n}",
            occurred_at=T0 - (n + 1) * DAY,
            observed_at=T0,
            payload={"title": f"Launch {n}"},
            source="news",
        )
    _event(
        db_session,
        acme,
        sc.FORM_8K_FILED,
        "8k-plain",
        occurred_at=T0 - DAY,
        observed_at=T0,
        payload={"items": ["9.01"]},
        source="edgar",
    )
    assert sc.build_signals(db_session, now=T0, profile=profile) == 5
    eligible = (
        EventRow.type.in_(sc.SIGNAL_EVENT_TYPES),
        EventRow.company_id.is_not(None),
        EventRow.occurred_at <= T0,
    )
    missing = db_session.execute(select(EventRow.type).where(*eligible, sc._signal_missing()))
    assert [str(r[0]) for r in missing] == [sc.FORM_8K_FILED]  # the plain 8-K has no signal
    read = db_session.execute(
        select(EventRow.event_id).where(*eligible, sc._may_derive(), sc._signal_missing())
    )
    assert read.all() == []  # ...and it derives nothing, so a rerun reads no event at all


def test_velocity_event_with_a_non_string_family_is_still_checked_exactly(
    db_session: Session, profile: Profile
) -> None:
    """The anti-join may let extra events through; the Python key check still dedupes them."""
    acme = _company(db_session, 1)
    _event(
        db_session,
        acme,
        HIRING_VELOCITY,
        f"{acme.id}:odd",
        occurred_at=T0 - DAY,
        observed_at=T0,
        payload={"role_family": 7, "new7": 3},
        magnitude=3,
        source="tell",
    )
    assert _build_both(db_session, now=T0, profile=profile)[0][3] == "7"
    assert sc.build_signals(db_session, now=T0, profile=profile) == 0


def test_funding_signal_kept_as_is_when_its_filing_is_later_flagged_an_amendment(
    db_session: Session, profile: Profile
) -> None:
    """As before: the deriver turns the event down, so the existing signal is left untouched."""
    acme = _company(db_session, 1)
    formd = _event(
        db_session,
        acme,
        sc.FORM_D_FILED,
        "0000000001-26-000001",
        occurred_at=T0 - DAY,
        observed_at=T0,
        payload={"is_amendment": False, "total_offering_amount": 18_000_000},
        source="edgar",
    )
    [before] = _build_both(db_session, now=T0, profile=profile)
    assert update_event_payload(
        db_session, formd, {"is_amendment": True, "total_offering_amount": 400_000_000}
    )
    assert _build_both(db_session, now=T0, profile=profile) == [before]
