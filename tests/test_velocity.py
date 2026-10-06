"""Phase 6 acceptance: taxonomy, fingerprints, reposts, stale handling, velocity (spec section 7).

Every test runs under a frozen clock (``freezegun``); velocity functions also take the
instant explicitly, so the clock only matters where ``utcnow()`` is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    JOB_OPENED,
    JOB_REPOSTED,
    Event,
    ObservedPosting,
    iso_utc,
    stale_weight,
    sync_board_postings,
    utcnow,
    write_events,
)
from tell.models import AtsBoard, Company, EventRow, Posting
from tell.signals import velocity as vel
from tell.signals.taxonomy import (
    FAMILIES,
    OTHER,
    TARGET_FAMILIES_DEFAULT,
    fingerprint,
    is_intern,
    normalize_title,
    role_family,
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    with freeze_time(T0):
        yield


# --- 1. the 60 labelled titles ------------------------------------------------------------------

LABELLED_TITLES: list[tuple[str, str | None, str]] = [
    # sales (incl. the sales-engineer rule and exec-last)
    ("Account Executive", None, "sales"),
    ("Senior Account Executive, Mid-Market", None, "sales"),
    ("Business Development Representative", None, "sales"),
    ("Strategic Account Manager", None, "sales"),
    ("Sales Engineer", None, "sales"),
    ("Senior Solutions Engineer", "Engineering", "sales"),
    ("Chief Revenue Officer", None, "sales"),
    ("Sales Development Representative (Intern)", None, "sales"),
    # gtm_ops
    ("Revenue Operations Manager", None, "gtm_ops"),
    ("RevOps Analyst", None, "gtm_ops"),
    ("Sales Operations Analyst", None, "gtm_ops"),
    ("GTM Engineer", None, "gtm_ops"),
    ("Growth Engineer", None, "gtm_ops"),
    ("Demand Generation Manager", None, "gtm_ops"),
    # marketing
    ("Product Marketing Manager", None, "marketing"),
    ("Content Writer", None, "marketing"),
    ("Brand Designer", None, "marketing"),
    ("SEO Specialist", None, "marketing"),
    ("Events Coordinator", None, "marketing"),
    # customer
    ("Customer Success Manager", None, "customer"),
    ("Senior CSM", None, "customer"),
    ("Technical Support Engineer", None, "customer"),
    ("Implementation Consultant", None, "customer"),
    ("Solutions Architect", None, "customer"),
    ("Forward Deployed Engineer", None, "customer"),
    # engineering
    ("Senior Software Engineer, Backend", None, "engineering"),
    ("Frontend Engineer", None, "engineering"),
    ("Full-Stack Developer", None, "engineering"),
    ("Platform Engineer", None, "engineering"),
    ("Site Reliability Engineer (SRE)", None, "engineering"),
    ("iOS Engineer, Mobile", None, "engineering"),
    ("Security Engineer", None, "engineering"),
    # ml_ai
    ("Machine Learning Engineer", None, "ml_ai"),
    ("Research Scientist, LLMs", None, "ml_ai"),
    ("Applied Scientist", None, "ml_ai"),
    ("Data Scientist", None, "ml_ai"),
    ("AI Engineer", None, "ml_ai"),
    # data
    ("Data Engineer", None, "data"),
    ("Analytics Engineer", None, "data"),
    ("Senior Data Analyst", None, "data"),
    ("BI Developer", None, "data"),
    # product_design
    ("Product Manager, Payments", None, "product_design"),
    ("Senior Product Designer", None, "product_design"),
    ("UX Researcher", None, "product_design"),
    ("Design Lead", None, "product_design"),
    # finance_ops (incl. exec-last: "Head of Talent", "Chief of Staff")
    ("Financial Controller", None, "finance_ops"),
    ("Accounting Manager", None, "finance_ops"),
    ("Technical Recruiter", None, "finance_ops"),
    ("Head of Talent", None, "finance_ops"),
    ("Chief of Staff", None, "finance_ops"),
    ("HR Generalist", None, "finance_ops"),
    # exec: only when nothing else matched
    ("VP Engineering", None, "exec"),
    ("Vice President, Sales", "Sales", "exec"),
    ("Head of Product", None, "exec"),
    ("Chief Executive Officer", None, "exec"),
    # department hint as tie-breaker; non-English falls to other
    ("Associate", "Sales", "sales"),
    ("Specialist", "Customer Experience", "customer"),
    ("Analyst", "Data & Analytics", "data"),
    ("営業マネージャー", "Sales", OTHER),
    ("Менеджер по продажам", None, OTHER),
]


def test_sixty_labelled_titles_all_land_in_their_family() -> None:
    assert len(LABELLED_TITLES) == 60
    assert {family for _, _, family in LABELLED_TITLES} == set(FAMILIES) | {OTHER}
    wrong = [
        (title, department, expected, role_family(title, department))
        for title, department, expected in LABELLED_TITLES
        if role_family(title, department) != expected
    ]
    assert wrong == []
    correct = len(LABELLED_TITLES) - len(wrong)
    assert f"{correct}/{len(LABELLED_TITLES)}" == "60/60"


def test_family_constants() -> None:
    assert FAMILIES == (
        "sales",
        "gtm_ops",
        "marketing",
        "customer",
        "engineering",
        "ml_ai",
        "data",
        "product_design",
        "finance_ops",
        "exec",
    )
    assert TARGET_FAMILIES_DEFAULT == ("sales", "gtm_ops", "customer")


def test_intern_and_contract_tagging() -> None:
    assert is_intern("Sales Development Representative (Intern)")
    assert is_intern("Software Engineering Internship, Summer 2027")
    assert is_intern("Data Analyst - Contractor")
    assert is_intern("Account Executive", "Contract")
    assert is_intern("Account Executive", "Intern")
    assert not is_intern("Account Executive", "FullTime")
    assert not is_intern("Account Executive", "Full-time")
    assert not is_intern("Internal Audit Lead")
    assert not is_intern("International Sales Manager")
    assert not is_intern("Contract Manager")  # a legal role, not a contract job


# --- 2. fingerprints -----------------------------------------------------------------------------


def _fp(title: str, department: str | None, location: str | None, remote: bool | None) -> str:
    return fingerprint(normalize_title(title), department, location, remote)


def test_fingerprint_is_equal_across_seniority_variants() -> None:
    reference = _fp("Account Executive", "Sales", "New York, NY", False)
    for title in (
        "Senior Account Executive",
        "Sr. Account Executive",
        "Account Executive II",
        "Account Executive III",
        "Principal Account Executive",
        "Lead Account Executive",
        "Staff Account Executive (L5)",
        "account   executive",
    ):
        assert _fp(title, "Sales", "New York, NY", False) == reference, title
    # Department case and location spelling do not split a requisition either.
    assert _fp("Account Executive", " sales ", "New York, NY, United States", False) == reference
    assert _fp("Account Executive", "Sales", "New York, NY, USA", False) == reference


def test_fingerprint_differs_across_locations_and_fields() -> None:
    reference = _fp("Account Executive", "Sales", "New York, NY", False)
    assert _fp("Account Executive", "Sales", "San Francisco, CA", False) != reference
    assert _fp("Account Executive", "Sales", "London, UK", False) != reference
    assert _fp("Account Executive", "Sales", "New York, NY", True) != reference
    assert _fp("Account Executive", "Marketing", "New York, NY", False) != reference
    assert _fp("Solutions Engineer", "Sales", "New York, NY", False) != reference
    remote_us = _fp("Account Executive", "Sales", "Remote - US", True)
    assert remote_us == _fp("Account Executive", "Sales", "US (Remote)", True)
    assert remote_us != _fp("Account Executive", "Sales", "Remote - Canada", True)


# --- helpers for the database tests --------------------------------------------------------------


@pytest.fixture
def company(db_session: Session) -> Company:
    c = Company(domain="acme.test", name="Acme")
    db_session.add(c)
    db_session.flush()
    return c


@pytest.fixture
def board(db_session: Session, company: Company) -> AtsBoard:
    b = AtsBoard(
        vendor="greenhouse", identifier="acme", confidence=Decimal("0.95"), company_id=company.id
    )
    db_session.add(b)
    db_session.flush()
    return b


def obs(
    external_id: str,
    *,
    title: str = "Account Executive",
    department: str | None = "Sales",
    location: str | None = "New York, NY",
    remote: bool | None = False,
    posted_at: datetime | None = None,
    description: str | None = "Sell the product to mid-market teams.",
    extra: dict[str, Any] | None = None,
) -> ObservedPosting:
    return ObservedPosting(
        external_id=external_id,
        title=title,
        url=f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        version="v1",
        department=department,
        location=location,
        remote=remote,
        posted_at=posted_at,
        description_plain=description,
        extra=extra or {},
    )


def poll(
    session: Session, board: AtsBoard, observed: list[ObservedPosting], now: datetime
) -> list[Event]:
    events = sync_board_postings(session, board, observed, run_id="run-test", now=now)
    write_events(session, events)
    return events


def warm(session: Session, board: AtsBoard, at: datetime) -> None:
    """An empty first poll, so the postings of the next poll are not cold-start ones."""
    assert poll(session, board, [], at) == []


def close(session: Session, board: AtsBoard, keep: list[ObservedPosting], at: datetime) -> None:
    """Two absent polls 12h apart: everything not in ``keep`` is closed at ``at``."""
    poll(session, board, keep, at - 12 * H)
    poll(session, board, keep, at)


def posting(session: Session, board: AtsBoard, external_id: str) -> Posting:
    stmt = select(Posting).where(Posting.board_id == board.id, Posting.external_id == external_id)
    return session.scalars(stmt).one()


def opened_event(
    company_id: int,
    external_id: str,
    occurred_at: datetime,
    *,
    family: str = "sales",
    fp: str | None = None,
    intern: bool = False,
    repost_of: str | None = None,
    identifier: str = "acme",
) -> Event:
    return Event(
        source="greenhouse",
        type=JOB_OPENED,
        external_id=f"greenhouse:{identifier}:{external_id}",
        version=iso_utc(occurred_at),
        occurred_at=occurred_at,
        observed_at=occurred_at,
        url=f"https://boards.greenhouse.io/{identifier}/jobs/{external_id}",
        payload={
            "vendor": "greenhouse",
            "board_identifier": identifier,
            "posting_external_id": external_id,
            "title": f"Role {external_id}",
            "role_family": family,
            "posted_at": iso_utc(occurred_at),
            "fingerprint": fp or f"fp-{external_id}",
            "is_intern": intern,
            "repost_of_external_id": repost_of,
        },
        company_id=company_id,
        run_id="run-test",
    )


def spikes(
    company_id: int, count: int, at: datetime, *, prefix: str = "s", **kw: Any
) -> list[Event]:
    return [opened_event(company_id, f"{prefix}{i}", at - i * H, **kw) for i in range(count)]


# --- 3. reposts ----------------------------------------------------------------------------------


def test_repost_within_30_days_of_close_is_job_reposted_and_not_counted(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    poll(db_session, board, [obs("j1", posted_at=T0 - 40 * DAY)], T0 - 40 * DAY)
    close(db_session, board, [], T0 - 20 * DAY)
    original = posting(db_session, board, "j1")
    assert original.closed_at == T0 - 20 * DAY

    events = poll(db_session, board, [obs("j2", posted_at=T0 - H, description="New words.")], T0)
    assert [e.type for e in events] == [JOB_REPOSTED]
    repost = posting(db_session, board, "j2")
    assert repost.repost_of == original.id
    assert events[0].payload["repost_of_external_id"] == "j1"
    assert events[0].payload["repost_reason"] == "closed_within_30d"
    assert vel.new7(db_session, company.id, "sales", T0) == 0


def test_close_more_than_30_days_ago_is_a_new_requisition(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    poll(db_session, board, [obs("j1", posted_at=T0 - 60 * DAY)], T0 - 60 * DAY)
    close(db_session, board, [], T0 - 31 * DAY)
    events = poll(db_session, board, [obs("j2", posted_at=T0 - H)], T0)
    assert [e.type for e in events] == [JOB_OPENED]
    assert posting(db_session, board, "j2").repost_of is None
    assert vel.new7(db_session, company.id, "sales", T0) == 1


def test_external_id_change_with_same_fingerprint_and_content_within_7_days_is_a_repost(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    warm(db_session, board, T0 - 4 * DAY)
    poll(db_session, board, [obs("j1", posted_at=T0 - 3 * DAY)], T0 - 3 * DAY)
    # j1 vanishes (not closed yet: one absence) and j2 appears with identical content.
    events = poll(db_session, board, [obs("j2", posted_at=T0 - H)], T0)
    assert [e.type for e in events] == [JOB_REPOSTED]
    assert events[0].payload["repost_reason"] == "same_content_within_7d"
    assert posting(db_session, board, "j2").repost_of == posting(db_session, board, "j1").id
    assert vel.new7(db_session, company.id, "sales", T0) == 1  # j1 only


def test_external_id_change_needs_same_content_and_7_days(
    db_session: Session, board: AtsBoard
) -> None:
    # The old listing was last seen 35 days ago: no rule applies, j2 is a new requisition.
    poll(db_session, board, [obs("j1", posted_at=T0 - 40 * DAY)], T0 - 40 * DAY)
    poll(db_session, board, [obs("j1")], T0 - 35 * DAY)
    events = poll(db_session, board, [obs("j2", posted_at=T0 - H)], T0)
    assert [e.type for e in events] == [JOB_OPENED]

    # Same fingerprint, different content, the old listing gone for 8 days: not rule 2
    # (which needs identical content within 7 days) but rule 3, vanished within 30 days.
    poll(
        db_session,
        board,
        [obs("j2"), obs("j3", title="Data Engineer", department="Data")],
        T0 + DAY,
    )
    poll(db_session, board, [obs("j2")], T0 + 5 * DAY)
    events = poll(
        db_session,
        board,
        [obs("j2"), obs("j4", title="Data Engineer", department="Data", description="Other.")],
        T0 + 9 * DAY,
    )
    assert [(e.type, e.payload["repost_reason"]) for e in events] == [
        (JOB_REPOSTED, "vanished_within_30d"),
        ("job_closed", None),
    ]
    assert posting(db_session, board, "j4").repost_of == posting(db_session, board, "j3").id


def test_reworded_twin_replacing_an_old_posting_in_one_poll_is_a_repost(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    """A (opened 40 days ago) disappears in the poll where its reworded twin B appears."""
    warm(db_session, board, T0 - 41 * DAY)
    poll(db_session, board, [obs("a", posted_at=T0 - 40 * DAY)], T0 - 40 * DAY)
    poll(db_session, board, [obs("a")], T0 - DAY)
    events = poll(db_session, board, [obs("b", posted_at=T0 - H, description="Reworded.")], T0)
    assert [(e.type, e.payload["repost_reason"]) for e in events] == [
        (JOB_REPOSTED, "vanished_within_30d")
    ]
    assert events[0].payload["repost_of_external_id"] == "a"
    assert vel.new7(db_session, company.id, "sales", T0) == 0


def test_side_by_side_duplicates_are_one_requisition_in_new7(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    warm(db_session, board, T0 - DAY)
    events = poll(
        db_session, board, [obs("j1", posted_at=T0 - H), obs("j2", posted_at=T0 - 2 * H)], T0
    )
    assert [e.type for e in events] == [JOB_OPENED, JOB_OPENED]
    assert vel.new7(db_session, company.id, "sales", T0) == 1


# --- 3b. cold start (ADR 0019) ----------------------------------------------------------------


def test_first_poll_postings_are_flagged_and_never_counted(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    """A board's first poll sees only what survived: no spike, no inflated baseline."""
    first = poll(
        db_session,
        board,
        [obs(f"c{i}", title=f"Account Executive {i}", posted_at=None) for i in range(5)],
        T0 - DAY,
    )
    assert [e.type for e in first] == [JOB_OPENED] * 5
    assert all(e.payload["first_poll"] is True for e in first)
    assert all(
        e.payload["match"]
        == {
            "rule": "R1",
            "ratio": None,
            "candidates": [],
            "confidence": 0.98,
            "via": "ats_board",
        }
        for e in first
    )
    cold = vel.velocity(db_session, company.id, "sales", T0 - DAY)
    assert (cold.new7, cold.base, cold.fires) == (0, 0.5, False)
    assert vel.emit_velocity_events(db_session, now=T0 - DAY) == []

    # The next poll's new postings are real news and count against the 0.5 floor.
    second = poll(
        db_session,
        board,
        [obs(f"c{i}", title=f"Account Executive {i}", posted_at=None) for i in range(5)]
        + [
            obs(f"n{i}", title=title, posted_at=T0 - H)
            for i, title in enumerate(("Enterprise AE", "Mid-Market AE", "Strategic AE"))
        ],
        T0,
    )
    assert [e.type for e in second] == [JOB_OPENED] * 3
    assert all("first_poll" not in e.payload for e in second)
    warm_result = vel.velocity(db_session, company.id, "sales", T0)
    assert (warm_result.new7, warm_result.base, warm_result.fires) == (3, 0.5, True)
    assert [p["external_id"] for p in warm_result.postings] == ["n0", "n1", "n2"]


def test_first_poll_postings_anchor_the_same_requisition_chain(
    db_session: Session, company: Company
) -> None:
    anchor = opened_event(company.id, "old", T0 - 10 * DAY, fp="fp-same")
    anchor.payload["first_poll"] = True
    write_events(db_session, [anchor, opened_event(company.id, "new", T0, fp="fp-same")])
    assert vel.new7(db_session, company.id, "sales", T0) == 0  # same requisition as the anchor
    assert vel.new7(db_session, company.id, "sales", T0 - 10 * DAY) == 0  # anchor never counted
    write_events(db_session, [opened_event(company.id, "other", T0, fp="fp-other")])
    assert vel.new7(db_session, company.id, "sales", T0) == 1


# --- 4. stale handling ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_days", "weight"), [(89, 1.0), (90, 1.0), (91, 0.25), (180, 0.25), (181, 0.0)]
)
def test_stale_weight_by_age(age_days: int, weight: float) -> None:
    now = utcnow()
    p = Posting(posted_at=now - age_days * DAY, first_seen_at=now)
    assert stale_weight(p, now) == weight
    # Without posted_at the first sighting dates the posting.
    q = Posting(posted_at=None, first_seen_at=now - age_days * DAY)
    assert stale_weight(q, now) == weight
    with pytest.raises(ValueError, match="timezone-aware"):
        stale_weight(p, datetime(2026, 1, 1))  # deliberately naive


def test_long_open_roles_and_weighted_open_postings(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    observed = [
        obs("fresh", title="Data Engineer", department="Data", posted_at=T0 - 89 * DAY),
        obs("stale", title="Platform Engineer", department=None, posted_at=T0 - 91 * DAY),
        obs("ghost", title="Product Designer", department=None, posted_at=T0 - 181 * DAY),
    ]
    poll(db_session, board, observed, T0)
    roles = vel.long_open_roles(db_session, company.id, T0)
    assert [(r.external_id, r.days_open, r.weight) for r in roles] == [
        ("ghost", 181, 0.0),
        ("stale", 91, 0.25),
    ]
    assert roles[1].url == "https://boards.greenhouse.io/acme/jobs/stale"
    assert vel.open_postings(db_session, company.id, T0) == 1.25


# --- 5. velocity ------------------------------------------------------------------------------------


def test_new7_window_bounds_and_exclusions(db_session: Session, company: Company) -> None:
    events = [
        opened_event(company.id, "in-1", T0),  # t itself is inside
        opened_event(company.id, "in-2", T0 - 7 * DAY + H),
        opened_event(company.id, "edge", T0 - 7 * DAY),  # (t-7d, t]: excluded
        opened_event(company.id, "future", T0 + H),
        opened_event(company.id, "intern", T0 - H, intern=True),
        opened_event(company.id, "repost", T0 - H, repost_of="in-1"),
        opened_event(company.id, "eng", T0 - H, family="engineering"),
    ]
    write_events(db_session, events)
    assert vel.new7(db_session, company.id, "sales", T0) == 2
    assert vel.new7(db_session, company.id, "engineering", T0) == 1
    assert vel.new7(db_session, company.id, "marketing", T0) == 0


def test_same_fingerprint_on_one_board_within_30_days_is_one_requisition(
    db_session: Session, company: Company
) -> None:
    write_events(
        db_session,
        [
            opened_event(company.id, "a", T0 - 20 * DAY, fp="same"),
            opened_event(company.id, "b", T0 - H, fp="same"),  # 20 days later: same requisition
            opened_event(company.id, "c", T0 - 2 * H, fp="same", identifier="other"),
        ],
    )
    assert vel.new7(db_session, company.id, "sales", T0) == 1  # only the other board's
    write_events(db_session, [opened_event(company.id, "d", T0 + 40 * DAY, fp="same")])
    assert vel.new7(db_session, company.id, "sales", T0 + 41 * DAY) == 1  # 41 days later: new


def test_baseline_is_mean_of_four_windows_with_floor(db_session: Session, company: Company) -> None:
    assert vel.base(db_session, company.id, "sales", T0) == 0.5  # no history: floor
    # Windows (t-14,t-7], (t-21,t-14], (t-28,t-21], (t-35,t-28] get 2, 0, 1, 1.
    write_events(
        db_session,
        [
            opened_event(company.id, "w1a", T0 - 8 * DAY),
            opened_event(company.id, "w1b", T0 - 9 * DAY),
            opened_event(company.id, "w3", T0 - 22 * DAY),
            opened_event(company.id, "w4", T0 - 34 * DAY),
            opened_event(company.id, "too-old", T0 - 36 * DAY),
        ],
    )
    assert vel.base(db_session, company.id, "sales", T0) == 1.0
    assert vel.base(db_session, company.id, "engineering", T0) == 0.5
    # One posting in four windows: mean 0.25, floored to 0.5.
    assert vel.base(db_session, company.id, "sales", T0 + 26 * DAY) == 0.5


def test_fires_on_three_new_with_ratio(db_session: Session, company: Company) -> None:
    write_events(db_session, spikes(company.id, 3, T0, family="engineering"))
    result = vel.velocity(db_session, company.id, "engineering", T0)
    assert (result.new7, result.base, result.ratio, result.fires) == (3, 0.5, 6.0, True)
    assert result.magnitude == 3
    assert result.board_url == "https://boards.greenhouse.io/acme"
    assert [p["external_id"] for p in result.postings] == ["s2", "s1", "s0"]
    assert set(result.postings[0]) == {"title", "posted_at", "url", "external_id"}


def test_does_not_fire_when_ratio_is_low(db_session: Session, company: Company) -> None:
    history = [
        opened_event(company.id, f"h{w}{i}", T0 - (7 * w + 1) * DAY - i * H, family="engineering")
        for w in range(1, 5)
        for i in range(3)
    ]  # three per baseline window: base 3.0
    write_events(db_session, history + spikes(company.id, 4, T0, family="engineering"))
    result = vel.velocity(db_session, company.id, "engineering", T0)
    assert (result.new7, result.base) == (4, 3.0)
    assert result.ratio == pytest.approx(4 / 3)
    assert not result.fires


def test_two_new_fire_only_for_target_families_at_small_companies(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    write_events(db_session, spikes(company.id, 2, T0, family="sales"))
    write_events(db_session, spikes(company.id, 2, T0, family="engineering", prefix="e"))
    assert vel.velocity(db_session, company.id, "sales", T0).fires
    assert not vel.velocity(db_session, company.id, "engineering", T0).fires
    assert vel.velocity(db_session, company.id, "sales", T0).ratio == 4.0

    # 50 fresh open postings: no longer a small company.
    many = [
        obs(f"o{i}", title=f"Role {i}", department="Other", posted_at=T0 - 10 * DAY)
        for i in range(50)
    ]
    poll(db_session, board, many, T0 - DAY)
    assert vel.open_postings(db_session, company.id, T0) == 50.0
    assert not vel.velocity(db_session, company.id, "sales", T0).fires


def test_stale_open_postings_do_not_make_a_company_large(
    db_session: Session, board: AtsBoard, company: Company
) -> None:
    write_events(db_session, spikes(company.id, 2, T0, family="customer"))
    old = [
        obs(f"o{i}", title=f"Role {i}", department="Other", posted_at=T0 - 200 * DAY)
        for i in range(60)
    ]
    poll(db_session, board, old, T0 - DAY)
    assert vel.open_postings(db_session, company.id, T0) == 0.0  # all over 180 days
    assert vel.velocity(db_session, company.id, "customer", T0).fires


def test_magnitude_is_capped_at_ten(db_session: Session, company: Company) -> None:
    write_events(db_session, spikes(company.id, 12, T0, family="data"))
    result = vel.velocity(db_session, company.id, "data", T0)
    assert (result.new7, result.magnitude, result.fires) == (12, 10, True)


# --- 6. hiring_velocity events: one per episode ---------------------------------------------------------


def velocity_events(session: Session) -> list[EventRow]:
    stmt = select(EventRow).where(EventRow.type == "hiring_velocity").order_by(EventRow.occurred_at)
    return list(session.scalars(stmt))


def test_one_event_per_episode(db_session: Session, board: AtsBoard, company: Company) -> None:
    write_events(db_session, spikes(company.id, 3, T0, family="sales"))

    emitted = vel.emit_velocity_events(db_session, now=utcnow())
    assert len(emitted) == 1
    (row,) = velocity_events(db_session)
    assert row.source == "tell"
    assert row.company_id == company.id
    assert row.occurred_at == T0
    assert row.url == "https://boards.greenhouse.io/acme"
    assert row.magnitude == 3
    assert row.payload["role_family"] == "sales"
    assert row.payload["new7"] == 3
    assert row.payload["base"] == 0.5
    assert row.payload["ratio"] == 6.0
    assert row.payload["episode_start"] == "2026-10-05"
    assert [p["external_id"] for p in row.payload["counted_postings"]] == ["s2", "s1", "s0"]
    assert emitted[0].external_id == f"{company.id}:sales"
    assert emitted[0].version == "2026-10-05"

    # Same day, next days of the same episode: nothing new.
    assert vel.emit_velocity_events(db_session, now=T0) == []
    for day in (1, 3, 6):
        with freeze_time(T0 + day * DAY):
            assert vel.emit_velocity_events(db_session, now=utcnow()) == []
    assert len(velocity_events(db_session)) == 1

    # The condition stops (the spike leaves the window), stays false for a week, then a
    # second spike starts a new episode: exactly one more event.
    with freeze_time(T0 + 20 * DAY):
        assert not vel.velocity(db_session, company.id, "sales", T0 + 12 * DAY).fires
        write_events(db_session, spikes(company.id, 3, T0 + 20 * DAY, prefix="n"))
        second = vel.emit_velocity_events(db_session, now=utcnow())
    assert len(second) == 1
    assert second[0].version == "2026-10-25"
    assert len(velocity_events(db_session)) == 2


def test_episode_survives_a_short_gap(db_session: Session, company: Company) -> None:
    write_events(db_session, spikes(company.id, 3, T0, family="sales"))
    assert len(vel.emit_velocity_events(db_session, now=T0)) == 1
    # False from T0+7d to T0+11d (5 days), firing again at T0+12d: same episode.
    write_events(db_session, spikes(company.id, 3, T0 + 12 * DAY, prefix="n"))
    assert not vel.velocity(db_session, company.id, "sales", T0 + 9 * DAY).fires
    assert vel.velocity(db_session, company.id, "sales", T0 + 12 * DAY).fires
    assert vel.emit_velocity_events(db_session, now=T0 + 12 * DAY) == []
    assert len(velocity_events(db_session)) == 1


def test_long_episode_never_re_emits_after_the_lookback_cap(
    db_session: Session, company: Company
) -> None:
    """A small company opening 2 sales roles every week fires continuously (no ratio rule);
    the 90-day walk-back cap must not start a second episode."""
    start = T0 - 120 * DAY
    for week in range(18):
        at = start + 7 * week * DAY
        write_events(db_session, spikes(company.id, 2, at, prefix=f"w{week}-"))
    assert vel.velocity(db_session, company.id, "sales", start).fires
    assert len(vel.emit_velocity_events(db_session, now=start)) == 1
    for day in (7, 30, 98, 105, 119):
        at = start + day * DAY
        assert vel.velocity(db_session, company.id, "sales", at).fires
        assert vel.emit_velocity_events(db_session, now=at) == [], day
    assert len(velocity_events(db_session)) == 1


def test_no_event_without_a_spike_or_a_company(db_session: Session, company: Company) -> None:
    write_events(db_session, spikes(company.id, 1, T0, family="sales"))
    unresolved = opened_event(company.id, "x", T0)
    write_events(
        db_session,
        [
            Event(
                source=unresolved.source,
                type=JOB_OPENED,
                external_id="greenhouse:nobody:x",
                version=unresolved.version,
                occurred_at=T0,
                observed_at=T0,
                url=unresolved.url,
                payload=unresolved.payload,
                run_id="run-test",
            )
        ],
    )
    assert vel.emit_velocity_events(db_session, now=T0, run_id="velocity-run") == []
    count = db_session.execute(select(func.count()).select_from(EventRow)).scalar_one()
    assert count == 2


def test_board_url_templates() -> None:
    assert vel.board_url("greenhouse", "acme") == "https://boards.greenhouse.io/acme"
    assert vel.board_url("lever", "acme") == "https://jobs.lever.co/acme"
    assert vel.board_url("lever", "eu:acme") == "https://jobs.eu.lever.co/acme"
    assert vel.board_url("ashby", "acme") == "https://jobs.ashbyhq.com/acme"
    assert vel.board_url("smartrecruiters", "Acme") == "https://careers.smartrecruiters.com/Acme"
    assert vel.board_url("synthetic", "acme") is None


def test_unknown_vendor_falls_back_to_a_posting_url(db_session: Session, company: Company) -> None:
    events = [
        Event(
            source="synthetic",
            type=JOB_OPENED,
            external_id=f"synthetic:acme:{e.payload['posting_external_id']}",
            version=e.version,
            occurred_at=e.occurred_at,
            observed_at=e.observed_at,
            url=e.url,
            payload={**e.payload, "vendor": "synthetic"},
            company_id=company.id,
            run_id="run-test",
        )
        for e in spikes(company.id, 3, T0)
    ]
    write_events(db_session, events)
    (event,) = vel.emit_velocity_events(db_session, now=T0)
    assert event.url == "https://boards.greenhouse.io/acme/jobs/s2"


# --- 7. ghost votes (ADR 0017) ---------------------------------------------------------------------------


def test_ghost_votes_are_counted_and_inert(
    db_session: Session, board: AtsBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    poll(db_session, board, [obs("j1"), obs("j2", title="Data Engineer")], T0)
    p1, p2 = posting(db_session, board, "j1"), posting(db_session, board, "j2")
    assert vel.ghost_votes(db_session, p1.id) == 0
    for _ in range(3):
        vel.record_ghost_vote(db_session, p1.id, now=T0)
    vel.record_ghost_vote(db_session, p2.id, now=T0, source="api")
    assert vel.ghost_votes(db_session, p1.id) == 3
    assert vel.ghost_votes_total(db_session) == 4
    assert vel.GHOST_VOTES_INERT_UNTIL == 500
    assert not vel.ghost_votes_active(db_session)
    monkeypatch.setattr(vel, "GHOST_VOTES_INERT_UNTIL", 4)
    assert vel.ghost_votes_active(db_session)
    # Votes change no weight.
    assert stale_weight(p1, T0) == 1.0
    with pytest.raises(ValueError, match="timezone-aware"):
        vel.record_ghost_vote(db_session, p1.id, now=datetime(2026, 1, 1))  # deliberately naive
    with pytest.raises(ValueError, match="non-empty"):
        vel.record_ghost_vote(db_session, p1.id, now=T0, source="")
