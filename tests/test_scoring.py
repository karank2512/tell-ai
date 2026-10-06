"""Scoring acceptance (spec section 8, test family "Scoring" in section 12).

The six explainability tests, literally, plus the derivation rules (funding magnitude,
amendments, idempotency), size band, heat, ``why_not_higher`` and the ``tell score``
sub-app. Everything runs under a frozen clock; the scoring instant is always passed
explicitly, so the clock only matters where ``utcnow()`` is read.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.collectors.base import (
    FIRST_POLL_KEY,
    TECH_MENTIONED_FIRST_TIME,
    Event,
    write_events,
)
from tell.db import SessionLocal
from tell.models import (
    SIGNAL_TYPES,
    AtsBoard,
    Company,
    EventRow,
    Posting,
    Score,
    Signal,
    Watchlist,
    WatchlistItem,
)
from tell.signals import cli
from tell.signals import scoring as sc
from tell.signals.profiles import Profile, load_profiles
from tell.signals.scoring import (
    FORM_8K_FILED,
    FORM_D_FILED,
    GITHUB_RELEASE,
    NEWS_EXPANSION,
    NEWS_LAUNCH,
    NEWS_LEADERSHIP,
    SCORE_CROSSED_THRESHOLD,
    ScoreResult,
    SignalView,
    active_signals,
    build_signals,
    contribution,
    emit_crossings,
    funding_magnitude,
    heat,
    heat_for_watchlist,
    in_size_band,
    score,
    score_all,
    score_companies,
    score_instant,
    score_profile,
    summarize,
    why_not_higher,
)
from tell.signals.velocity import HIRING_VELOCITY

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
D0 = T0.date()
DAY = timedelta(days=1)
RUN = "tell-test"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    with freeze_time(T0):
        yield


@pytest.fixture
def profiles() -> dict[str, Profile]:
    """Shipped profiles only (no ``tell-private`` leaks into the tests)."""
    return load_profiles(private_dir=None)


@pytest.fixture
def default(profiles: dict[str, Profile]) -> Profile:
    return profiles["default"]


# --- helpers ---------------------------------------------------------------------------------


def _company(session: Session, n: int) -> Company:
    company = Company(domain=f"c{n}.example", name=f"Company {n}")
    session.add(company)
    session.flush()
    return company


def _event(
    session: Session,
    company: Company,
    kind: str,
    *,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
    magnitude: float = 1,
    external_id: str | None = None,
    url: str | None = None,
    source: str = "synthetic",
) -> Event:
    ext = external_id or f"{company.id}:{kind}:{occurred_at.isoformat()}"
    event = Event(
        source=source,
        type=kind,
        external_id=ext,
        version="1",
        occurred_at=occurred_at,
        observed_at=T0,
        url=url or f"https://example.test/{company.id}/{kind}/{abs(hash(ext))}",
        payload=payload or {},
        run_id=RUN,
        magnitude=magnitude,
        company_id=company.id,
    )
    write_events(session, [event])
    session.flush()
    return event


def _signal(
    session: Session,
    company: Company,
    signal_type: str,
    *,
    starts_at: datetime,
    magnitude: float = 1.0,
    role_family: str | None = None,
    profile: Profile,
) -> Signal:
    """A synthetic ``signals`` row (with its own event) stamped with ``profile``'s values."""
    event = _event(
        session,
        company,
        f"synthetic_{signal_type}",
        occurred_at=starts_at,
        external_id=f"{company.id}:{signal_type}:{role_family}:{starts_at.isoformat()}",
        payload={"role_family": role_family} if role_family else {},
    )
    row = Signal(
        company_id=company.id,
        event_id=event.event_id,
        signal_type=signal_type,
        role_family=role_family,
        weight=Decimal(str(profile.weights[signal_type] if signal_type != HIRING_VELOCITY else 1)),
        magnitude=Decimal(str(magnitude)),
        half_life_days=profile.half_life_days[signal_type],
        starts_at=starts_at,
    )
    session.add(row)
    session.flush()
    return row


def _view(
    signal_type: str,
    *,
    starts_at: datetime,
    magnitude: float = 1.0,
    role_family: str | None = None,
    payload: dict[str, Any] | None = None,
    signal_id: int = 1,
) -> SignalView:
    return SignalView(
        id=signal_id,
        company_id=1,
        event_id="e",
        signal_type=signal_type,
        role_family=role_family,
        magnitude=Decimal(str(magnitude)),
        half_life_days=30,
        starts_at=starts_at,
        url="https://example.test/receipt",
        occurred_at=starts_at,
        payload=payload or {},
    )


def _store(session: Session, result: ScoreResult) -> None:
    session.add(
        Score(
            company_id=result.company_id,
            icp_profile=result.profile,
            as_of=result.as_of,
            score=Decimal(repr(result.score)),
            contributions=result.contributions,
        )
    )
    session.flush()


def _result(company: Company, day: date, value: float, profile: str = "default") -> ScoreResult:
    return ScoreResult(
        company_id=company.id,
        profile=profile,
        as_of=day,
        score=value,
        contributions=[
            {
                "signal_id": 1,
                "signal_type": "funding",
                "role_family": None,
                "contribution": value,
                "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/",
                "occurred_at": "2026-10-01T00:00:00Z",
                "summary": "Form D: $18,000,000 offering",
            }
        ],
    )


def _crossings(session: Session) -> list[EventRow]:
    stmt = (
        select(EventRow)
        .where(EventRow.type == SCORE_CROSSED_THRESHOLD)
        .order_by(EventRow.occurred_at)
    )
    return list(session.scalars(stmt))


def _realistic_events(session: Session, company: Company) -> None:
    """One event of every signal-bearing type, dated inside their active windows."""
    _event(
        session,
        company,
        HIRING_VELOCITY,
        occurred_at=T0 - 3 * DAY,
        magnitude=4,
        payload={"role_family": "sales", "new7": 4, "base": 0.5, "ratio": 8.0},
        url="https://boards.greenhouse.io/acme",
        source="tell",
    )
    _event(
        session,
        company,
        FORM_D_FILED,
        occurred_at=T0 - 10 * DAY,
        magnitude=18_000_000,
        payload={
            "is_amendment": False,
            "total_offering_amount": 18_000_000,
            "total_amount_sold": 5_000_000,
        },
        url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/",
        source="edgar",
    )
    _event(
        session,
        company,
        NEWS_LEADERSHIP,
        occurred_at=T0 - 5 * DAY,
        payload={"title": "Acme appoints [person] as chief revenue officer"},
        url="https://news.example.test/acme-cro",
        source="news",
    )
    _event(
        session,
        company,
        FORM_8K_FILED,
        occurred_at=T0 - 6 * DAY,
        payload={"items": ["5.02", "9.01"], "item_5_02": True, "item_1_01": False},
        url="https://www.sec.gov/Archives/edgar/data/1/000000000126000002/",
        source="edgar",
    )
    _event(
        session,
        company,
        NEWS_LAUNCH,
        occurred_at=T0 - 2 * DAY,
        payload={"title": "Acme launches Acme Cloud"},
        url="https://news.example.test/acme-cloud",
        source="news",
    )
    _event(
        session,
        company,
        GITHUB_RELEASE,
        occurred_at=T0 - 1 * DAY,
        payload={"repo": "acme", "tag_name": "v2.0.0", "name": "v2.0.0", "is_flagship": True},
        url="https://github.com/acme/acme/releases/tag/v2.0.0",
        source="github",
    )
    _event(
        session,
        company,
        NEWS_EXPANSION,
        occurred_at=T0 - 4 * DAY,
        payload={"title": "Acme opens London office"},
        url="https://news.example.test/acme-london",
        source="news",
    )
    _event(
        session,
        company,
        TECH_MENTIONED_FIRST_TIME,
        occurred_at=T0 - 8 * DAY,
        payload={"keyword": "kubernetes", "scope": str(company.id)},
        url="https://boards.greenhouse.io/acme/jobs/1",
        source="greenhouse",
    )


# --- 1. sum test ----------------------------------------------------------------------------------


def test_1_sum_of_contributions_equals_score(db_session: Session, default: Profile) -> None:
    rng = random.Random(8)
    companies = [_company(db_session, n) for n in range(200)]
    for company in companies:
        for _ in range(rng.randint(1, 6)):
            signal_type = rng.choice(SIGNAL_TYPES)
            family = rng.choice(["sales", "engineering", "marketing"])
            _signal(
                db_session,
                company,
                signal_type,
                starts_at=T0 - timedelta(days=rng.uniform(0, 400), hours=rng.uniform(0, 24)),
                magnitude=rng.choice([1, 2, 3, 6, 7.25, 8, 10]),
                role_family=family if signal_type == HIRING_VELOCITY else None,
                profile=default,
            )
    ids = [c.id for c in companies]
    checked = 0
    with_parts = 0
    for _ in range(5):
        day = D0 - timedelta(days=rng.randint(0, 120))
        score_companies(db_session, default, day, ids)
        rows = db_session.scalars(select(Score).where(Score.as_of == day)).all()
        assert len(rows) == 200
        for row in rows:
            total = sum(float(c["contribution"]) for c in row.contributions)
            assert abs(float(row.score) - total) < 1e-9
            assert all(c["contribution"] > 0 for c in row.contributions)
            checked += 1
            with_parts += bool(row.contributions)
    assert checked == 1000
    assert with_parts > 300


# --- 2. monotone decay -------------------------------------------------------------------------


def test_2_contribution_never_grows_from_t_to_t_plus_one_day(default: Profile) -> None:
    rng = random.Random(2)
    for _ in range(500):
        signal_type = rng.choice(SIGNAL_TYPES)
        view = _view(
            signal_type,
            starts_at=T0 - timedelta(days=rng.uniform(0, 400)),
            magnitude=rng.uniform(0.5, 10),
            role_family="sales" if signal_type == HIRING_VELOCITY else None,
            payload={"keyword": "rust"},
        )
        for k in range(0, 400, 7):
            t = view.starts_at + timedelta(days=k, hours=rng.uniform(0, 24))
            assert contribution(view, default, t + DAY) <= contribution(view, default, t)


def test_contribution_is_zero_before_the_signal_starts(default: Profile) -> None:
    view = _view("funding", starts_at=T0 + DAY, magnitude=7)
    assert contribution(view, default, T0) == 0.0
    assert contribution(view, default, T0 + 2 * DAY) > 0


def test_contribution_is_zero_and_dropped_beyond_four_half_lives(default: Profile) -> None:
    view = _view("launch", starts_at=T0 - 181 * DAY)  # launch half-life 45 -> 180 days
    assert contribution(view, default, T0) == 0.0
    inside = _view("launch", starts_at=T0 - 179 * DAY)
    assert contribution(inside, default, T0) > 0


# --- 3. half-life ------------------------------------------------------------------------------------


@pytest.mark.parametrize("signal_type", SIGNAL_TYPES)
def test_3_contribution_halves_after_one_half_life(
    signal_type: str, profiles: dict[str, Profile]
) -> None:
    profile = profiles["job-seeker"]  # every weight is positive here, tech_adoption included
    hl = profile.half_life_days[signal_type]
    view = _view(
        signal_type,
        starts_at=T0,
        magnitude=3,
        role_family="engineering" if signal_type == HIRING_VELOCITY else None,
        payload={"keyword": "rust"},
    )
    for start_days in (0, 0.5, 1, hl / 2, hl, 2 * hl):
        t = T0 + timedelta(days=start_days)
        at_t = contribution(view, profile, t)
        assert at_t > 0
        assert abs(contribution(view, profile, t + timedelta(days=hl)) - 0.5 * at_t) < 1e-9


# --- 4. no orphan contributions ---------------------------------------------------------------


def test_4_every_contribution_traces_to_an_event_with_a_url(
    db_session: Session, default: Profile, profiles: dict[str, Profile]
) -> None:
    companies = [_company(db_session, n) for n in range(3)]
    for company in companies:
        _realistic_events(db_session, company)
    assert build_signals(db_session, now=T0, profile=default) == 3 * 8
    for profile in profiles.values():
        score_companies(db_session, profile, D0, [c.id for c in companies])
    rows = db_session.scalars(select(Score)).all()
    assert len(rows) == 3 * len(profiles)
    seen = 0
    for row in rows:
        assert row.contributions, row
        for part in row.contributions:
            assert set(part) == {
                "signal_id",
                "signal_type",
                "role_family",
                "contribution",
                "url",
                "occurred_at",
                "summary",
            }
            signal = db_session.get(Signal, part["signal_id"])
            assert signal is not None
            event = db_session.get(EventRow, signal.event_id)
            assert event is not None
            assert event.url
            assert part["url"] == event.url
            assert part["signal_type"] == signal.signal_type
            assert part["summary"]
            seen += 1
    assert seen > 0


# --- 5. profile isolation -----------------------------------------------------------------------


def test_5_changing_profile_a_leaves_profile_b_unchanged(
    db_session: Session, profiles: dict[str, Profile]
) -> None:
    a = profiles["job-seeker"]
    b = profiles["default"]
    companies = [_company(db_session, n) for n in range(4)]
    for company in companies:
        _realistic_events(db_session, company)
    build_signals(db_session, now=T0, profile=b)
    ids = [c.id for c in companies]
    score_companies(db_session, a, D0, ids)
    score_companies(db_session, b, D0, ids)
    before = {
        row.company_id: (row.score, json.dumps(row.contributions, sort_keys=True))
        for row in db_session.scalars(select(Score).where(Score.icp_profile == b.name))
    }
    a_before = {
        row.company_id: row.score
        for row in db_session.scalars(select(Score).where(Score.icp_profile == a.name))
    }
    changed = a.model_copy(
        update={"weights": a.weights.model_copy(update={"funding": 9.0, "launch": 0.0})}
    )
    score_companies(db_session, changed, D0, ids)
    db_session.expire_all()
    after = {
        row.company_id: (row.score, json.dumps(row.contributions, sort_keys=True))
        for row in db_session.scalars(select(Score).where(Score.icp_profile == b.name))
    }
    a_after = {
        row.company_id: row.score
        for row in db_session.scalars(select(Score).where(Score.icp_profile == a.name))
    }
    assert after == before
    assert all(a_after[c] != a_before[c] for c in a_after)


# --- 6. threshold hysteresis -------------------------------------------------------------------


def test_6_oscillation_under_half_a_point_alerts_at_most_once_per_week(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    emitted: list[tuple[date, int]] = []
    for k in range(14):
        day = D0 - 13 * DAY + k * DAY
        value = 6.2 if k % 2 else 5.8  # oscillates by 0.4 around 6.0
        result = _result(company, day, value)
        _store(db_session, result)
        events = emit_crossings(db_session, default, day, [result])
        emitted.append((day, len(events)))
    total = sum(n for _, n in emitted)
    assert total <= 1
    assert total == 1  # the first upward crossing does fire
    for start in range(14):
        window = [n for d, n in emitted if day_in(d, D0 - 13 * DAY + start * DAY, 7)]
        assert sum(window) <= 1
    assert len(_crossings(db_session)) == 1


def day_in(d: date, start: date, days: int) -> bool:
    return start <= d < start + timedelta(days=days)


def test_crossing_rearms_after_dropping_half_a_point_and_seven_days(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    sequence = [5.0, 6.2, 5.0, 6.2, 5.9, 5.9, 5.9, 5.9, 5.9, 6.2]
    fired: list[int] = []
    for k, value in enumerate(sequence):
        day = D0 - (len(sequence) - 1 - k) * DAY
        result = _result(company, day, value)
        _store(db_session, result)
        fired.append(len(emit_crossings(db_session, default, day, [result])))
    # day 1 fires; day 3 is inside the 7-day cooldown although re-armed (dropped to 5.0);
    # day 9 is 8 days after the crossing and the score was below 5.5 since: fires again.
    assert fired == [0, 1, 0, 0, 0, 0, 0, 0, 0, 1]


def test_crossing_does_not_rearm_without_a_half_point_drop(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    sequence = [5.0, 6.2, 5.8, 5.8, 5.8, 5.8, 5.8, 5.8, 5.8, 5.8, 6.4]
    fired: list[int] = []
    for k, value in enumerate(sequence):
        day = D0 - (len(sequence) - 1 - k) * DAY
        result = _result(company, day, value)
        _store(db_session, result)
        fired.append(len(emit_crossings(db_session, default, day, [result])))
    assert fired == [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]


def test_crossing_event_shape_and_idempotency(db_session: Session, default: Profile) -> None:
    company = _company(db_session, 1)
    _realistic_events(db_session, company)
    build_signals(db_session, now=T0, profile=default)
    results, events = score_profile(db_session, default, D0, now=T0, run_id=RUN)
    assert len(results) == 1
    assert results[0].score >= default.alert_threshold
    assert len(events) == 1
    event = events[0]
    assert event.source == "tell"
    assert event.type == SCORE_CROSSED_THRESHOLD
    assert event.external_id == f"{company.id}:default"
    assert event.version == D0.isoformat()
    assert event.url == results[0].contributions[0]["url"]
    assert event.payload["score"] == results[0].score
    assert event.payload["previous_score"] == 0.0
    assert event.payload["threshold"] == 6.0
    assert len(event.payload["top_contributions"]) == 2
    assert event.payload["top_contributions"][0]["signal_type"] == "funding"
    # same day again: same event_id, nothing new; next day: already above, nothing new
    _, again = score_profile(db_session, default, D0, now=T0, run_id=RUN)
    assert again == []
    assert len(_crossings(db_session)) == 1
    _, next_day = score_profile(db_session, default, D0 + DAY, now=T0 + DAY, run_id=RUN)
    assert next_day == []


def test_crossing_uses_the_most_recent_stored_score_before_as_of(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    _store(db_session, _result(company, D0 - 3 * DAY, 7.0))  # skipped days in between
    assert emit_crossings(db_session, default, D0, [_result(company, D0, 6.5)]) == []
    _store(db_session, _result(company, D0 - 2 * DAY, 2.0))
    assert len(emit_crossings(db_session, default, D0, [_result(company, D0, 6.5)])) == 1


def test_emit_crossings_rejects_naive_now(db_session: Session, default: Profile) -> None:
    company = _company(db_session, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        emit_crossings(
            db_session, default, D0, [_result(company, D0, 6.5)], now=datetime(2026, 10, 5)
        )


# --- derivation rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (18_000_000, math.log10(18_000_000)),
        (1_000_000_000, 8.0),
        (10**12, 8.0),
        (None, 6.0),
        (0, 6.0),
        (-5, 6.0),
        (float("nan"), 6.0),
        (1_000_000, 6.0),
        (500_000, math.log10(500_000)),
    ],
)
def test_funding_magnitude(amount: float | None, expected: float) -> None:
    assert abs(funding_magnitude(amount) - expected) < 1e-12


def test_funding_magnitude_18m_is_seven_point_two_five() -> None:
    assert round(funding_magnitude(18_000_000), 4) == 7.2553


def test_build_signals_funding_cases_and_amendments(db_session: Session, default: Profile) -> None:
    company = _company(db_session, 1)
    cases: dict[str, dict[str, Any]] = {
        "a": {"is_amendment": False, "total_offering_amount": 18_000_000},
        "b": {"is_amendment": False, "total_offering_amount": 1_000_000_000},
        "c": {"is_amendment": False, "total_offering_amount": None},
        "d": {"is_amendment": False, "totalOfferingAmount": "2500000"},
    }
    for key, payload in cases.items():
        _event(
            db_session,
            company,
            FORM_D_FILED,
            occurred_at=T0 - DAY,
            payload=payload,
            external_id=f"formd-{key}",
            source="edgar",
        )
    _event(
        db_session,
        company,
        FORM_D_FILED,
        occurred_at=T0 - DAY,
        payload={"is_amendment": True, "total_offering_amount": 99_000_000},
        external_id="formd-amended-flag",
        source="edgar",
    )
    _event(
        db_session,
        company,
        "form_d_amendment",
        occurred_at=T0 - DAY,
        payload={"is_amendment": True, "total_offering_amount": 99_000_000},
        external_id="formd-da",
        source="edgar",
    )
    assert build_signals(db_session, now=T0, profile=default) == 4
    rows = db_session.scalars(select(Signal).order_by(Signal.id)).all()
    assert [r.signal_type for r in rows] == ["funding"] * 4
    magnitudes = sorted(float(r.magnitude) for r in rows)
    assert magnitudes == sorted([math.log10(18_000_000), 8.0, 6.0, math.log10(2_500_000)])
    assert all(float(r.weight) == 4.0 and r.half_life_days == 90 for r in rows)
    assert all(r.role_family is None for r in rows)


def test_build_signals_is_idempotent_including_null_role_family(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    _realistic_events(db_session, company)
    first = build_signals(db_session, now=T0, profile=default)
    assert first == 8
    count = db_session.execute(select(func.count()).select_from(Signal)).scalar_one()
    assert build_signals(db_session, now=T0, profile=default) == 0
    assert db_session.execute(select(func.count()).select_from(Signal)).scalar_one() == count
    tech = db_session.scalars(select(Signal).where(Signal.signal_type == "tech_adoption")).all()
    assert len(tech) == 1
    assert tech[0].role_family is None
    assert float(tech[0].weight) == 0.0  # default profile: tech_adoption weighs 0
    velocity = db_session.scalars(select(Signal).where(Signal.signal_type == HIRING_VELOCITY)).one()
    assert velocity.role_family == "sales"
    assert float(velocity.weight) == 1.0
    assert float(velocity.magnitude) == 4.0
    by_type = {r.signal_type: r for r in db_session.scalars(select(Signal))}
    assert set(by_type) == set(SIGNAL_TYPES)
    assert float(by_type["funding"].magnitude) == pytest.approx(math.log10(18_000_000))


def test_build_signals_skips_unflagged_and_unlinked_events(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    _event(
        db_session,
        company,
        FORM_8K_FILED,
        occurred_at=T0 - DAY,
        payload={"items": ["9.01"], "item_5_02": False},
        external_id="8k-plain",
    )
    _event(
        db_session,
        company,
        GITHUB_RELEASE,
        occurred_at=T0 - DAY,
        payload={"repo": "side", "is_flagship": False},
        external_id="rel-side",
    )
    _event(
        db_session,
        company,
        TECH_MENTIONED_FIRST_TIME,
        occurred_at=T0 - DAY,
        payload={"keyword": "go", FIRST_POLL_KEY: True},
        external_id="tech-baseline",
    )
    _event(
        db_session,
        company,
        HIRING_VELOCITY,
        occurred_at=T0 - DAY,
        payload={"new7": 3},  # no role family
        external_id="vel-nofamily",
    )
    _event(
        db_session,
        company,
        "news_funding",
        occurred_at=T0 - DAY,
        payload={"title": "Acme raises $10M"},
        external_id="news-funding",
    )
    _event(
        db_session,
        company,
        NEWS_LAUNCH,
        occurred_at=T0 + DAY,  # in the future relative to now
        payload={"title": "Acme launches tomorrow"},
        external_id="future",
    )
    unlinked = Event(
        source="news",
        type=NEWS_LAUNCH,
        external_id="unlinked",
        version="1",
        occurred_at=T0 - DAY,
        observed_at=T0,
        url="https://news.example.test/unlinked",
        payload={"title": "Somebody launches something"},
        run_id=RUN,
    )
    write_events(db_session, [unlinked])
    assert build_signals(db_session, now=T0, profile=default) == 0
    with pytest.raises(ValueError, match="timezone-aware"):
        build_signals(db_session, now=datetime(2026, 10, 5), profile=default)


def test_build_signals_with_no_events(db_session: Session, default: Profile) -> None:
    assert build_signals(db_session, now=T0, profile=default) == 0


# --- the spec functions on real rows ----------------------------------------------------------


def test_score_and_active_signals_match_the_stored_result(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    _realistic_events(db_session, company)
    _signal(db_session, company, "launch", starts_at=T0 - 200 * DAY, profile=default)  # expired
    build_signals(db_session, now=T0, profile=default)
    t = score_instant(D0)
    active = active_signals(db_session, company.id, t, profile=default)
    assert len(active) == 9  # within four of the longest half-life; the old launch is just 0
    total, parts = score(db_session, company.id, default, t)
    assert abs(total - sum(c for _, c in parts)) < 1e-9
    assert all(c > 0 for _, c in parts)
    assert len(parts) == 7  # tech_adoption weighs 0 under default and is dropped
    [stored] = score_companies(db_session, default, D0, [company.id])
    assert abs(stored.score - total) < 1e-9
    assert [p["signal_id"] for p in stored.contributions] == [
        s.id for s, _ in sorted(parts, key=lambda sc_: (-sc_[1], sc_[0].id))
    ]
    row = db_session.get(Signal, stored.contributions[0]["signal_id"])
    assert row is not None
    assert contribution(row, default, t) == pytest.approx(stored.contributions[0]["contribution"])
    assert active_signals(db_session, company.id, t)  # default profile when none given


def test_score_companies_handles_empty_input_and_zero_rows(
    db_session: Session, default: Profile
) -> None:
    assert score_companies(db_session, default, D0, []) == []
    company = _company(db_session, 1)
    [result] = score_companies(db_session, default, D0, [company.id])
    assert result.score == 0.0
    assert result.contributions == []
    assert result.top_url is None
    row = db_session.get(Score, (company.id, "default", D0))
    assert row is not None
    assert row.score == 0
    assert score_companies(db_session, default, D0, [company.id], write=False)[0].score == 0.0


def test_score_companies_upserts_the_same_day(db_session: Session, default: Profile) -> None:
    company = _company(db_session, 1)
    score_companies(db_session, default, D0, [company.id])
    _realistic_events(db_session, company)
    build_signals(db_session, now=T0, profile=default)
    [result] = score_companies(db_session, default, D0, [company.id])
    db_session.expire_all()
    row = db_session.get(Score, (company.id, "default", D0))
    assert row is not None
    assert float(row.score) == pytest.approx(result.score)
    assert db_session.execute(select(func.count()).select_from(Score)).scalar_one() == 1


# --- daily job ----------------------------------------------------------------------------------


def test_score_all_default_scores_active_companies_and_others_score_watchlists(
    db_session: Session, profiles: dict[str, Profile]
) -> None:
    active = _company(db_session, 1)
    quiet = _company(db_session, 2)
    listed = _company(db_session, 3)
    _realistic_events(db_session, active)
    build_signals(db_session, now=T0, profile=profiles["default"])
    watchlist = Watchlist(owner="team-a", name="pilot", icp_profile="job-seeker")
    db_session.add(watchlist)
    db_session.flush()
    db_session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=listed.id))
    db_session.flush()
    stats = score_all(db_session, as_of=D0, profiles=profiles, now=T0, run_id=RUN)
    assert stats == {
        "default": {"companies": 1, "crossings": 1},
        "job-seeker": {"companies": 1, "crossings": 0},
    }
    rows = {(r.company_id, r.icp_profile) for r in db_session.scalars(select(Score))}
    assert rows == {(active.id, "default"), (listed.id, "job-seeker")}
    assert (quiet.id, "default") not in rows


def test_score_profile_without_emitting(db_session: Session, default: Profile) -> None:
    company = _company(db_session, 1)
    _realistic_events(db_session, company)
    build_signals(db_session, now=T0, profile=default)
    results, events = score_profile(db_session, default, D0, emit=False)
    assert len(results) == 1
    assert events == []
    assert _crossings(db_session) == []


# --- size band -----------------------------------------------------------------------------------


def _board_with_postings(session: Session, company: Company, n: int) -> None:
    board = AtsBoard(
        company_id=company.id, vendor="greenhouse", identifier=company.domain, confidence=Decimal(1)
    )
    session.add(board)
    session.flush()
    for k in range(n):
        session.add(
            Posting(
                board_id=board.id,
                external_id=str(k),
                fingerprint=f"fp{k}",
                title=f"Account Executive {k}",
                title_norm=f"account executive {k}",
                role_family="sales",
                posted_at=T0 - 10 * DAY,
                first_seen_at=T0 - 10 * DAY,
                last_seen_at=T0,
                url=f"https://boards.greenhouse.io/{company.domain}/jobs/{k}",
            )
        )
    session.flush()


def test_size_band_gates_alerts_but_the_raw_score_is_stored(
    db_session: Session, default: Profile
) -> None:
    tiny = _company(db_session, 1)
    sized = _company(db_session, 2)
    unknown = _company(db_session, 3)
    _board_with_postings(db_session, tiny, 1)  # below min_open_postings 3
    _board_with_postings(db_session, sized, 5)
    for company in (tiny, sized, unknown):
        _realistic_events(db_session, company)
    build_signals(db_session, now=T0, profile=default)
    results = {
        r.company_id: r
        for r in score_companies(db_session, default, D0, [tiny.id, sized.id, unknown.id])
    }
    assert results[tiny.id].in_band is False
    assert results[sized.id].in_band is True
    assert results[unknown.id].in_band is True  # no board: size unknown, not filtered
    assert results[tiny.id].score == pytest.approx(results[sized.id].score)
    stored = db_session.get(Score, (tiny.id, "default", D0))
    assert stored is not None
    assert float(stored.score) == pytest.approx(results[tiny.id].score)
    events = emit_crossings(db_session, default, D0, list(results.values()), run_id=RUN)
    assert sorted(e.company_id or 0 for e in events) == [sized.id, unknown.id]
    t = score_instant(D0)
    assert in_size_band(db_session, default, tiny.id, t) is False
    assert in_size_band(db_session, default, sized.id, t) is True
    assert in_size_band(db_session, default, unknown.id, t) is True
    wide = default.model_copy(
        update={"size_band": default.size_band.model_copy(update={"max_open_postings": 4})}
    )
    assert in_size_band(db_session, wide, sized.id, t) is False


# --- heat -------------------------------------------------------------------------------------


def test_heat_is_a_percentile_rank_within_the_list() -> None:
    assert heat({}) == {}
    assert heat({7: 3.0}) == {7: 100}
    assert heat({7: 0.0}) == {7: 0}
    assert heat({1: 10.0, 2: 5.0, 3: 5.0, 4: 0.0}) == {1: 100, 2: 50, 3: 50, 4: 0}
    assert heat({1: 2.0, 2: 2.0}) == {1: 50, 2: 50}
    ranked = heat({i: float(i) for i in range(11)})
    assert [ranked[i] for i in range(11)] == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


def test_heat_for_watchlist_reads_stored_scores(db_session: Session) -> None:
    companies = [_company(db_session, n) for n in range(4)]
    watchlist = Watchlist(owner="team-a", name="pilot", icp_profile="default")
    db_session.add(watchlist)
    db_session.flush()
    for company in companies[:3]:
        db_session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=company.id))
    db_session.flush()
    _store(db_session, _result(companies[0], D0, 9.0))
    _store(db_session, _result(companies[1], D0, 3.0))
    _store(db_session, _result(companies[3], D0, 100.0))  # not on the list
    _store(db_session, _result(companies[0], D0 - DAY, 0.0))  # another day
    ranks = heat_for_watchlist(db_session, watchlist.id, D0)
    assert ranks == {companies[0].id: 100, companies[1].id: 50, companies[2].id: 0}
    with pytest.raises(KeyError, match="no watchlist"):
        heat_for_watchlist(db_session, watchlist.id + 99, D0)


# --- why not higher ----------------------------------------------------------------------------


def test_why_not_higher_lists_signals_that_expired_in_the_last_30_days(
    db_session: Session, default: Profile
) -> None:
    company = _company(db_session, 1)
    recent = _signal(
        db_session,
        company,
        HIRING_VELOCITY,
        starts_at=T0 - 125 * DAY,
        role_family="sales",
        magnitude=4,
        profile=default,
    )  # expired 5 days ago (4 x 30)
    edge = _signal(
        db_session, company, "launch", starts_at=T0 - 209 * DAY, profile=default
    )  # 4 x 45 = 180 -> expired 29 days ago
    _signal(db_session, company, "funding", starts_at=T0 - 400 * DAY, profile=default)  # 40d ago
    _signal(db_session, company, "funding", starts_at=T0 - 10 * DAY, profile=default)  # active
    rows = why_not_higher(db_session, company.id, default, D0)
    assert [r["signal_id"] for r in rows] == [recent.id, edge.id]
    assert rows[0]["signal_type"] == HIRING_VELOCITY
    assert rows[0]["summary"] == "4 new sales roles in 7 days"
    assert rows[0]["url"].startswith("https://example.test/")
    assert rows[0]["expired_at"] > rows[1]["expired_at"]
    assert set(rows[0]) == {
        "signal_id",
        "signal_type",
        "role_family",
        "url",
        "occurred_at",
        "expired_at",
        "summary",
    }


# --- receipts ----------------------------------------------------------------------------------


def test_summaries_read_like_receipts() -> None:
    assert (
        summarize(
            _view(
                HIRING_VELOCITY,
                starts_at=T0,
                magnitude=4,
                role_family="sales",
                payload={"new7": 4, "base": 0.5},
            )
        )
        == "4 new sales roles in 7 days vs 0.5 baseline"
    )
    assert (
        summarize(_view(HIRING_VELOCITY, starts_at=T0, magnitude=2, role_family="gtm_ops"))
        == "2 new gtm ops roles in 7 days"
    )
    assert (
        summarize(
            _view(
                "funding",
                starts_at=T0,
                payload={"total_offering_amount": 18_000_000, "total_amount_sold": 5_000_000},
            )
        )
        == "Form D: $18,000,000 offering, $5,000,000 sold"
    )
    assert (
        summarize(_view("funding", starts_at=T0, payload={"total_offering_amount": None}))
        == "Form D: undisclosed offering"
    )
    assert (
        summarize(
            _view(
                "funding",
                starts_at=T0,
                payload={"total_offering_amount": None, "total_offering_amount_indefinite": True},
            )
        )
        == "Form D: indefinite offering"
    )
    assert (
        summarize(_view("leadership", starts_at=T0, payload={"title": " Acme names [person] CEO "}))
        == "Acme names [person] CEO"
    )
    assert (
        summarize(_view("leadership", starts_at=T0, payload={"items": ["5.02"]}))
        == "8-K Item 5.02 (departure or appointment of officers)"
    )
    assert (
        summarize(_view("launch", starts_at=T0, payload={"repo": "acme", "tag_name": "v2"}))
        == "Release v2 of acme"
    )
    assert summarize(_view("expansion", starts_at=T0)) == "expansion"
    assert (
        summarize(_view("tech_adoption", starts_at=T0, payload={"keyword": "kubernetes"}))
        == "First posting mentioning kubernetes"
    )
    assert summarize(_view("tech_adoption", starts_at=T0)) == "First posting mentioning a new tool"


# --- CLI ----------------------------------------------------------------------------------------


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Point the sub-app's ``session_scope`` at the temporary test database."""

    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    monkeypatch.setattr(sc, "load_profiles", lambda: load_profiles(private_dir=None))
    return engine


def test_cli_build_run_all_and_report(bound: Engine) -> None:
    with SessionLocal(bind=bound) as session:
        company = _company(session, 1)
        _realistic_events(session, company)
        watchlist = Watchlist(owner="team-a", name="pilot", icp_profile="job-seeker")
        session.add(watchlist)
        session.flush()
        session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=company.id))
        session.commit()
        company_id = company.id

    result = runner.invoke(cli.app, ["build-signals"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["inserted"] == 8

    result = runner.invoke(cli.app, ["run", "--profile", "default", "--as-of", D0.isoformat()])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record == {
        "above_threshold": 1,
        "as_of": D0.isoformat(),
        "companies": 1,
        "crossings": 1,
        "profile": "default",
    }

    result = runner.invoke(cli.app, ["run"])  # defaults: profile default, today
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["crossings"] == 0  # already emitted today

    result = runner.invoke(cli.app, ["all", "--as-of", (D0 + DAY).isoformat()])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["as_of"] == (D0 + DAY).isoformat()
    assert record["profiles"]["default"] == {"companies": 1, "crossings": 0}
    assert record["profiles"]["job-seeker"]["companies"] == 1

    result = runner.invoke(cli.app, ["all"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli.app, ["feedback-report", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("## Precision by signal type: profile `default`")
    assert "weights stay hand-tuned" in result.output

    with SessionLocal(bind=bound) as session:
        rows = session.scalars(select(Score).where(Score.company_id == company_id)).all()
        assert {(r.icp_profile, r.as_of) for r in rows} == {
            ("default", D0),
            ("default", D0 + DAY),
            ("job-seeker", D0),
            ("job-seeker", D0 + DAY),
        }


def test_cli_rejects_unknown_profile_and_bad_date(bound: Engine) -> None:
    result = runner.invoke(cli.app, ["run", "--profile", "nope"])
    assert result.exit_code != 0
    assert "no ICP profile named 'nope'" in result.output
    result = runner.invoke(cli.app, ["run", "--as-of", "yesterday"])
    assert result.exit_code != 0
    assert "ISO date" in result.output
    result = runner.invoke(cli.app, [])
    assert "build-signals" in result.output
