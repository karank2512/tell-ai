"""Rules R1-R6 (spec section 6): a positive and a negative case per rule, plus the resolver."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import R1_MATCH
from tell.models import AtsBoard, Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve.match import (
    CONFIDENCE,
    GUARD_RATIO,
    AliasIndex,
    Candidate,
    MatchDecision,
    add_aliases,
    backfill_aliases,
    match_name,
    resolve_events,
)
from tell.resolve.review import apply_verdict

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _company(session: Session, domain: str, name: str, **extra: Any) -> Company:
    company = Company(domain=domain, name=name, **extra)
    session.add(company)
    session.flush()
    return company


@pytest.fixture
def companies(db_session: Session) -> dict[str, Company]:
    rows = {
        "acme": _company(
            db_session, "acme.com", "Acme", hq_city="San Francisco", hq_state="CA", hq_country="US"
        ),
        "robotics": _company(
            db_session, "acmerobotics.com", "Acme Robotics", hq_city="New York", hq_state="NY"
        ),
        "zephyr": _company(
            db_session, "zephyr.io", "Zephyr Technologies", hq_city="Austin", hq_state="TX"
        ),
        "beacon": _company(
            db_session, "beaconhealth.com", "Beacon Health", hq_city="Boston", hq_state="MA"
        ),
    }
    backfill_aliases(db_session)
    return rows


def _event(
    session: Session,
    event_id: str,
    payload: dict[str, Any],
    *,
    source: str = "edgar",
    type_: str = "form_d_filed",
    company_id: int | None = None,
) -> EventRow:
    row = EventRow(
        event_id=event_id,
        company_id=company_id,
        source=source,
        type=type_,
        occurred_at=NOW,
        observed_at=NOW,
        magnitude=Decimal(1),
        url=f"https://example.test/{event_id}",
        payload=payload,
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row


def _formd(name: str, state: str | None = "CA", **extra: Any) -> dict[str, Any]:
    """A Form D payload of a Delaware corporation incorporated in 2024 (passes guards b-d)."""
    payload: dict[str, Any] = {
        "accession_number": "0000000000-26-000001",
        "cik": "0000012345",
        "form_type": "D",
        "date_filed": "2026-10-05",
        "issuer_name": name,
        "issuer_city": "San Francisco",
        "issuer_state": state,
        "previous_names": [],
        "jurisdiction_of_inc": "DELAWARE",
        "year_of_inc": 2024,
        "entity_type": "Corporation",
        "industry_group_type": "Other Technology",
        "amendments": [],
    }
    payload.update(extra)
    return payload


def _aliases(session: Session, company_id: int) -> dict[str, str]:
    rows = session.execute(
        select(CompanyAlias.alias, CompanyAlias.source).where(CompanyAlias.company_id == company_id)
    )
    return dict(rows.all())


def _open_items(session: Session) -> list[ReviewQueueItem]:
    stmt = select(ReviewQueueItem).where(
        ReviewQueueItem.kind == "entity_match", ReviewQueueItem.status == "open"
    )
    return list(session.scalars(stmt))


# --- backfill ----------------------------------------------------------------------------------


def test_backfill_adds_names_domain_labels_and_board_slugs(
    db_session: Session, companies: dict[str, Company]
) -> None:
    acme = companies["acme"]
    assert _aliases(db_session, acme.id) == {"acme": "manual"}
    assert _aliases(db_session, companies["zephyr"].id) == {
        "zephyr technologies": "manual",
        "zephyr": "manual",
    }
    db_session.add(
        AtsBoard(
            company_id=acme.id,
            vendor="greenhouse",
            identifier="acme-widgets-inc",
            confidence=Decimal("0.90"),
        )
    )
    db_session.add(
        AtsBoard(
            company_id=acme.id, vendor="workday", identifier="wd5.acme", confidence=Decimal("0.90")
        )
    )
    db_session.flush()
    assert backfill_aliases(db_session) == 1
    assert _aliases(db_session, acme.id) == {"acme": "manual", "acme widgets": "ats_board"}
    # idempotent
    assert backfill_aliases(db_session) == 0


def test_backfill_applies_the_alias_rule(db_session: Session) -> None:
    """ADR 0033: no short / generic / word / platform labels, no failing names or slugs."""
    ofone = _company(db_session, "of.one", "OfOne")
    famegame = _company(db_session, "apple.com", "FameGame")
    ramp = _company(db_session, "ramp.com", "Ramp Business")
    brex = _company(db_session, "brex.com", "Brex Card")
    h1 = _company(db_session, "h1.co", "H1")
    fund = _company(db_session, "the-fund.com", "The Fund")
    db_session.add(
        AtsBoard(company_id=brex.id, vendor="ashby", identifier="ai", confidence=Decimal("0.90"))
    )
    db_session.flush()
    backfill_aliases(db_session)
    assert _aliases(db_session, ofone.id) == {"ofone": "manual"}
    assert _aliases(db_session, famegame.id) == {"famegame": "manual"}
    assert _aliases(db_session, ramp.id) == {"ramp business": "manual"}
    assert _aliases(db_session, brex.id) == {"brex card": "manual", "brex": "manual"}
    assert _aliases(db_session, h1.id) == {}
    assert _aliases(db_session, fund.id) == {}
    # every writer goes through the rule, whatever the source
    assert (
        add_aliases(db_session, ofone.id, ["Of", "AI", "2026", "Capital Partners"], source="edgar")
        == 0
    )


def test_index_ignores_failing_label_aliases_unless_a_review_wrote_them(
    db_session: Session,
) -> None:
    """Legacy ``apple`` / ``ramp`` label rows do not match before ``prune-aliases`` runs."""
    famegame = _company(db_session, "apple.com", "FameGame")
    ramp = _company(db_session, "ramp.com", "Ramp Business")
    for company, alias in ((famegame, "apple"), (ramp, "ramp")):
        db_session.add(CompanyAlias(company_id=company.id, alias=alias, source="manual"))
    db_session.add(CompanyAlias(company_id=ramp.id, alias="rampco", source="ats_board"))
    db_session.flush()
    assert match_name(db_session, "The Apple Company").rule == "R6"
    unlinked = match_name(db_session, "Ramp, Inc.")  # review against "ramp business" only
    assert (unlinked.rule, unlinked.company_id) == ("R5", None)
    assert [c.alias for c in unlinked.candidates if c.ratio >= 85] == ["ramp business"]
    assert match_name(db_session, "Rampco Inc").company_id == ramp.id  # other sources count
    db_session.add(
        ReviewQueueItem(
            kind="entity_match",
            status="resolved",
            payload={"name": "Ramp", "verdict": "link", "company_id": ramp.id},
        )
    )
    db_session.flush()
    decision = match_name(db_session, "Ramp, Inc.")
    assert (decision.rule, decision.company_id) == ("R2", ramp.id)


def test_add_aliases_rejects_unknown_source(
    db_session: Session, companies: dict[str, Company]
) -> None:
    with pytest.raises(ValueError, match="alias source"):
        add_aliases(db_session, companies["acme"].id, ["x"], source="linkedin")
    assert add_aliases(db_session, companies["acme"].id, ["", "..."], source="manual") == 0


# --- rules -----------------------------------------------------------------------------------


def test_r1_domain_wins_whatever_the_name(
    db_session: Session, companies: dict[str, Company]
) -> None:
    acme = companies["acme"]
    decision = match_name(db_session, "Totally Unrelated Board Title", on_domain_company_id=acme.id)
    assert (decision.rule, decision.confidence, decision.action) == ("R1", 0.98, "auto_link")
    assert decision.company_id == acme.id
    # R1 still logs what the name alone would have found.
    assert all(isinstance(c, Candidate) for c in decision.candidates)


def test_r1_negative_without_domain_evidence(
    db_session: Session, companies: dict[str, Company]
) -> None:
    decision = match_name(db_session, "Totally Unrelated Board Title")
    assert decision.rule != "R1"
    assert decision.company_id is None


def test_r2_exact_alias(db_session: Session, companies: dict[str, Company]) -> None:
    decision = match_name(db_session, "Acme, Inc.")
    assert (decision.rule, decision.confidence, decision.action) == ("R2", 0.95, "auto_link")
    assert decision.company_id == companies["acme"].id
    assert decision.ratio == 100.0
    assert decision.candidates[0].alias == "acme"


def test_r2_matches_the_alt_form(db_session: Session, companies: dict[str, Company]) -> None:
    decision = match_name(db_session, "Zephyr Tech Inc")
    assert decision.rule == "R2"
    assert decision.company_id == companies["zephyr"].id
    # 3Scale-style digits: alias stored as "3scale", name spelled out, and vice versa.
    three = _company(db_session, "3scale.net", "3Scale")
    add_aliases(db_session, three.id, ["3Scale"], source="manual")
    assert match_name(db_session, "ThreeScale, Inc.").company_id == three.id


def test_r2_negative_shared_alias_is_ambiguous(
    db_session: Session, companies: dict[str, Company]
) -> None:
    add_aliases(db_session, companies["acme"].id, ["Shared Name"], source="manual")
    add_aliases(db_session, companies["zephyr"].id, ["Shared Name"], source="manual")
    decision = match_name(db_session, "Shared Name Inc")
    assert decision.rule == "R5"
    assert decision.company_id is None
    assert {c.company_id for c in decision.candidates if c.ratio == 100.0} == {
        companies["acme"].id,
        companies["zephyr"].id,
    }
    # geography breaks the tie through R3
    with_state = match_name(db_session, "Shared Name Inc", state="TX")
    assert (with_state.rule, with_state.company_id) == ("R3", companies["zephyr"].id)


def test_r3_name_plus_state(db_session: Session, companies: dict[str, Company]) -> None:
    decision = match_name(db_session, "Beakon Health Inc", state="MA")
    assert (decision.rule, decision.confidence, decision.action) == ("R3", 0.90, "auto_link")
    assert decision.company_id == companies["beacon"].id
    assert decision.ratio is not None
    assert 92 <= decision.ratio < 95
    # full state names fold to the Form D two-letter code
    assert match_name(db_session, "Beakon Health Inc", state="Massachusetts").rule == "R3"


def test_r3_city_when_state_missing(db_session: Session, companies: dict[str, Company]) -> None:
    decision = match_name(db_session, "Beakon Health Inc", state=None, city="Boston")
    assert (decision.rule, decision.company_id) == ("R3", companies["beacon"].id)
    assert match_name(db_session, "Beakon Health Inc", state=None, city="Denver").rule != "R3"


def test_r3_negative_wrong_state_falls_through(
    db_session: Session, companies: dict[str, Company]
) -> None:
    decision = match_name(db_session, "Beakon Health Inc", state="TX")
    assert decision.rule == "R5"  # 92.3 with one candidate and no geography: review, not R3
    assert decision.company_id is None
    # a company without geography never satisfies R3
    nogeo = _company(db_session, "nogeo.example", "Quartz Dynamics")
    add_aliases(db_session, nogeo.id, ["Quartz Dynamics"], source="manual")
    assert match_name(db_session, "Quartz Dynamix", state="CA").rule != "R3"


def test_r4_unique_high_ratio(db_session: Session, companies: dict[str, Company]) -> None:
    decision = match_name(db_session, "Beacon Helth Inc")  # a typo, not a superset
    assert (decision.rule, decision.confidence, decision.action) == ("R4", 0.85, "auto_link")
    assert decision.company_id == companies["beacon"].id
    assert decision.ratio is not None
    assert decision.ratio >= 95
    assert decision.candidates[0].sort_ratio >= GUARD_RATIO


def test_token_superset_guard_sends_supersets_to_review(
    db_session: Session, companies: dict[str, Company]
) -> None:
    """``token_set_ratio`` scores any superset 100; R3 / R4 also need ``token_sort_ratio >= 85``."""
    for name, state in (
        ("Beacon Health Systems LLC", None),  # would be R4 on token_set_ratio alone
        ("Beacon Health Press LLC", "MA"),  # would be R3 on token_set_ratio alone
    ):
        decision = match_name(db_session, name, state=state)
        assert (decision.rule, decision.action) == ("R5", "review"), name
        assert decision.company_id is None
        top = decision.candidates[0]
        assert top.ratio == 100.0
        assert top.sort_ratio < GUARD_RATIO
        assert not top.guarded
    # the guard never touches R2: an exact alias is an exact alias
    assert match_name(db_session, "Beacon Health, Inc.").rule == "R2"


def test_single_token_alias_never_scores_against_a_longer_name(
    db_session: Session, companies: dict[str, Company]
) -> None:
    """ADR 0033: a one-word alias is no candidate for a token superset (not even review)."""
    acme = companies["acme"].id
    for name, state in (("Blue Acme Bakery", None), ("Anti Acme Coalition Inc", "CA")):
        decision = match_name(db_session, name, state=state)
        assert (decision.rule, decision.action) == ("R6", "unlinked"), name
        assert acme not in {c.company_id for c in decision.candidates}
    # a single-token name is still scored against single-token aliases (typos reach review)
    typo = match_name(db_session, "Acmee Inc")
    assert (typo.rule, typo.candidates[0].company_id) == ("R5", acme)
    # and the exact one-word name is R2
    assert match_name(db_session, "ACME, Inc.").company_id == acme


def test_aliases_failing_the_rule_are_ignored_even_when_present(db_session: Session) -> None:
    """Rows written before the rule existed never become candidates (ADR 0033)."""
    ofone = _company(db_session, "of.one", "OfOne")
    db_session.add_all(
        [
            CompanyAlias(company_id=ofone.id, alias=alias, source="manual")
            for alias in ("of", "ai", "the fund", "2026")
        ]
    )
    db_session.flush()
    index = AliasIndex.from_session(db_session)
    assert len(index) == 1  # only "ofone"
    for name in ("Arboretum Fund I, a series of CGF2026 LLC", "Of Inc", "AI Inc", "The Fund LP"):
        decision = match_name(db_session, name, index=index)
        assert decision.rule == "R6", name
        assert all(c.alias == "ofone" and c.ratio < 85 for c in decision.candidates), name
    assert match_name(db_session, "OfOne, Inc.", index=index).rule == "R2"


def test_r4_negative_two_candidates_go_to_review(
    db_session: Session, companies: dict[str, Company]
) -> None:
    robotix = _company(db_session, "acmerobotix.com", "Acme Robotix", hq_state="TX")
    add_aliases(db_session, robotix.id, ["Acme Robotix"], source="manual")
    decision = match_name(db_session, "Acme Robotic")  # 96 and 91.7: two candidates
    assert (decision.rule, decision.confidence, decision.action) == ("R5", 0.60, "review")
    assert decision.company_id is None
    assert {c.company_id for c in decision.candidates[:2]} == {
        companies["robotics"].id,
        robotix.id,
    }


def test_r4_uniqueness_is_evaluated_over_the_whole_table(db_session: Session) -> None:
    """A company holding many aliases cannot hide a second candidate company from R4."""
    hoarder = _company(db_session, "acme-i.example", "Acme Corp I")
    add_aliases(db_session, hoarder.id, [f"Acme Corp {n}" for n in range(1, 26)], source="manual")
    rival = _company(db_session, "acmee.example", "Acmee")
    add_aliases(db_session, rival.id, ["Acmee"], source="manual")
    decision = match_name(db_session, "Acme Inc")
    assert (decision.rule, decision.action) == ("R5", "review")
    strong = {c.company_id for c in decision.candidates if c.ratio >= 85}
    assert strong == {hoarder.id, rival.id}
    # a name only the hoarder resembles is still linked
    alone = match_name(db_session, "Acme Corp 3 Inc")
    assert (alone.rule, alone.company_id) == ("R2", hoarder.id)


def test_r3_geography_tie_is_not_auto_linked(db_session: Session) -> None:
    """Two companies sharing an alias in the issuer's state: review, both logged."""
    first = _company(db_session, "method.example", "Method", hq_state="NY", hq_city="New York")
    second = _company(db_session, "method.io", "Method Financial", hq_state="NY", hq_city="Albany")
    add_aliases(db_session, first.id, ["Method"], source="manual")
    add_aliases(db_session, second.id, ["Method"], source="manual")
    for name in ("Method Inc", "Methods Inc"):  # exact shared alias, and 92.3 against it
        decision = match_name(db_session, name, state="NY")
        assert (decision.rule, decision.action) == ("R5", "review"), name
        assert decision.company_id is None
        assert {c.company_id for c in decision.candidates if c.ratio >= 92} == {
            first.id,
            second.id,
        }
    # geography that singles one out still links (R3), and a city breaks the tie when the
    # issuer state is missing
    third = _company(db_session, "method.co", "Method Labs", hq_state="TX", hq_city="Austin")
    add_aliases(db_session, third.id, ["Methodd"], source="manual")
    assert match_name(db_session, "Methodd Inc", state="TX").company_id == third.id
    by_city = match_name(db_session, "Method Inc", state=None, city="Albany")
    assert (by_city.rule, by_city.company_id) == ("R3", second.id)


def test_r5_single_candidate_between_85_and_91(
    db_session: Session, companies: dict[str, Company]
) -> None:
    decision = match_name(db_session, "Acmee Inc")
    assert (decision.rule, decision.action) == ("R5", "review")
    assert decision.ratio is not None
    assert 85 <= decision.ratio <= 91
    assert len([c for c in decision.candidates if c.ratio >= 85]) == 1


def test_r5_negative_below_85_is_unlinked(
    db_session: Session, companies: dict[str, Company]
) -> None:
    decision = match_name(db_session, "Acq Inc")
    assert decision.rule == "R6"
    assert all(c.ratio < 85 for c in decision.candidates)


def test_r6_nothing_close(db_session: Session, companies: dict[str, Company]) -> None:
    decision = match_name(db_session, "Completely Different Enterprises")
    assert (decision.rule, decision.confidence, decision.action) == ("R6", 0.0, "unlinked")
    assert decision.company_id is None
    assert all(c.ratio < 85 for c in decision.candidates)
    assert len(decision.candidates) <= 5


def test_r6_negative_a_close_name_does_not_land_in_r6(
    db_session: Session, companies: dict[str, Company]
) -> None:
    assert match_name(db_session, "Acme Inc").rule != "R6"
    assert match_name(db_session, "Acmee Inc").rule != "R6"


def test_empty_table_and_empty_name(db_session: Session) -> None:
    decision = match_name(db_session, "Anyone")
    assert decision.rule == "R6"
    assert decision.candidates == []
    _company(db_session, "acme.com", "Acme")
    assert match_name(db_session, "...").rule == "R6"


def test_candidates_capped_at_five_and_ranked(db_session: Session) -> None:
    for n in range(8):
        company = _company(db_session, f"acme{n}.example", f"Acme Group {n}")
        add_aliases(db_session, company.id, [f"Acme Group {n}"], source="manual")
    decision = match_name(db_session, "Acme Group Holdings")
    assert len(decision.candidates) == 5
    ratios = [c.ratio for c in decision.candidates]
    assert ratios == sorted(ratios, reverse=True)
    assert decision.rule == "R5"


def test_confidence_table_is_the_spec_table() -> None:
    assert CONFIDENCE == {"R1": 0.98, "R2": 0.95, "R3": 0.90, "R4": 0.85, "R5": 0.60, "R6": 0.0}


def test_alias_index_reused_across_calls(
    db_session: Session, companies: dict[str, Company]
) -> None:
    index = AliasIndex.from_session(db_session)
    size = len(index)
    assert size >= 4
    first = match_name(db_session, "Acme Inc", index=index)
    index.add("brand new alias", companies["beacon"].id)
    second = match_name(db_session, "Brand New Alias Inc", index=index)
    assert (first.company_id, second.company_id) == (companies["acme"].id, companies["beacon"].id)
    assert len(index) == size + 1
    index.add("", companies["beacon"].id)
    assert len(index) == size + 1


def test_decision_audit_shape() -> None:
    decision = MatchDecision(
        rule="R4",
        confidence=0.85,
        company_id=7,
        ratio=96.5,
        candidates=[Candidate(7, "acme", 96.5, 96.5)],
        action="auto_link",
    )
    audit = decision.audit(now=NOW)
    assert audit == {
        "rule": "R4",
        "ratio": 96.5,
        "candidates": [{"company_id": 7, "alias": "acme", "ratio": 96.5, "sort_ratio": 96.5}],
        "confidence": 0.85,
        "resolved_at": "2026-10-05T12:00:00+00:00",
    }


def test_r1_audit_record_matches_the_spec_confidence() -> None:
    """Collectors stamp ``R1_MATCH`` when they copy ``board.company_id``; same shape and confidence."""
    assert R1_MATCH["rule"] == "R1"
    assert R1_MATCH["confidence"] == CONFIDENCE["R1"]
    assert R1_MATCH["via"] == "ats_board"
    assert set(R1_MATCH) >= {"rule", "ratio", "candidates"}


# --- resolve_events --------------------------------------------------------------------------


def test_resolve_links_form_d_and_enriches_company(
    db_session: Session, companies: dict[str, Company]
) -> None:
    acme = companies["acme"]
    _event(
        db_session,
        "ev-acme",
        _formd("Acme, Inc.", previous_names=["Acme Widgets LLC", "Old Acme Holdings Inc"]),
    )
    stats = resolve_events(db_session, now=NOW)
    assert stats.to_dict() == {
        "scanned": 1,
        "auto_linked": 1,
        "review": 0,
        "unlinked": 0,
        "no_name": 0,
        "rejected_skipped": 0,
        "aliases_added": 2,
        "guard_demoted": 0,
        "pooled_funds": 0,
        "by_rule": {"R2": 1},
    }
    event = db_session.get(EventRow, "ev-acme")
    assert event is not None
    assert event.company_id == acme.id
    match = event.payload["match"]
    assert match["rule"] == "R2"
    assert match["confidence"] == 0.95
    assert match["ratio"] == 100.0
    assert match["resolved_at"] == "2026-10-05T12:00:00+00:00"
    assert match["candidates"][0] == {
        "company_id": acme.id,
        "alias": "acme",
        "ratio": 100.0,
        "sort_ratio": 100.0,
    }
    assert len(match["candidates"]) <= 5
    # the original payload keys survive the merge
    assert event.payload["issuer_name"] == "Acme, Inc."
    db_session.refresh(acme)
    assert acme.cik == "0000012345"
    assert acme.legal_name == "Acme, Inc."
    assert _aliases(db_session, acme.id) == {
        "acme": "manual",
        "acme widgets": "edgar",
        "old acme": "edgar",
    }


def test_resolve_keeps_existing_cik_and_legal_name(
    db_session: Session, companies: dict[str, Company]
) -> None:
    beacon = companies["beacon"]
    beacon.cik = "0000099999"
    beacon.legal_name = "Beacon Health Corporation"
    other = companies["zephyr"]
    other.cik = "0000012345"
    db_session.flush()
    _event(db_session, "ev-beacon", _formd("Beacon Health Systems LLC", state="MA"))
    _event(db_session, "ev-robotics", _formd("Acme Robotics Inc", state="NY", cik="0000012345"))
    resolve_events(db_session, now=NOW)
    db_session.refresh(beacon)
    assert (beacon.cik, beacon.legal_name) == ("0000099999", "Beacon Health Corporation")
    robotics = companies["robotics"]
    db_session.refresh(robotics)
    # CIK already belongs to another company: never duplicated (unique column)
    assert robotics.cik is None
    assert robotics.legal_name == "Acme Robotics Inc"


def test_resolve_r5_opens_one_review_item_and_is_idempotent(
    db_session: Session, companies: dict[str, Company]
) -> None:
    _event(db_session, "ev-ambiguous", _formd("Acme Robotics Group", state="WA"))
    _event(db_session, "ev-nothing", _formd("Completely Different Enterprises", state="WA"))
    first = resolve_events(db_session, now=NOW)
    assert (first.scanned, first.review, first.unlinked) == (2, 1, 1)
    assert first.by_rule == {"R5": 1, "R6": 1}

    items = _open_items(db_session)
    assert len(items) == 1
    payload = items[0].payload
    assert payload["event_id"] == "ev-ambiguous"
    assert payload["name"] == "Acme Robotics Group"
    assert payload["state"] == "WA"
    assert payload["city"] == "San Francisco"
    # "acme" is a single-token alias, so only "acme robotics" is a candidate (ADR 0033)
    assert payload["candidates"][0]["company_id"] == companies["robotics"].id
    assert companies["acme"].id not in {c["company_id"] for c in payload["candidates"]}
    assert set(payload) == {"event_id", "name", "state", "city", "candidates"}

    before = {row.event_id: row.payload for row in db_session.scalars(select(EventRow))}
    alias_count = db_session.scalar(select(func.count()).select_from(CompanyAlias))

    second = resolve_events(db_session, now=LATER)
    assert second.scanned == 0
    assert len(_open_items(db_session)) == 1
    assert {row.event_id: row.payload for row in db_session.scalars(select(EventRow))} == before
    assert db_session.scalar(select(func.count()).select_from(CompanyAlias)) == alias_count

    # --retry re-evaluates but still never duplicates the open item
    third = resolve_events(db_session, now=LATER, retry=True)
    assert third.scanned == 2
    assert len(_open_items(db_session)) == 1
    unlinked = db_session.get(EventRow, "ev-nothing")
    assert unlinked is not None
    assert unlinked.company_id is None
    assert unlinked.payload["match"]["rule"] == "R6"
    assert unlinked.payload["match"]["resolved_at"] == "2026-10-06T12:00:00+00:00"


def test_resolve_retry_picks_up_new_aliases(
    db_session: Session, companies: dict[str, Company]
) -> None:
    _event(db_session, "ev-later", _formd("Nimbus Analytics Inc", state="WA"))
    assert resolve_events(db_session, now=NOW).by_rule == {"R6": 1}
    add_aliases(db_session, companies["zephyr"].id, ["Nimbus Analytics"], source="manual")
    assert resolve_events(db_session, now=LATER).scanned == 0
    stats = resolve_events(db_session, now=LATER, retry=True)
    assert stats.by_rule == {"R2": 1}
    event = db_session.get(EventRow, "ev-later")
    assert event is not None
    assert event.company_id == companies["zephyr"].id


def test_rejected_pair_does_not_come_back(
    db_session: Session, companies: dict[str, Company]
) -> None:
    _event(db_session, "ev-first", _formd("Acme Robotics Group", state="WA"))
    resolve_events(db_session, now=NOW)
    item = _open_items(db_session)[0]
    apply_verdict(db_session, item.id, "reject", now=NOW)
    assert _open_items(db_session) == []

    _event(db_session, "ev-second", _formd("ACME ROBOTICS GROUP, INC.", state="WA"))
    stats = resolve_events(db_session, now=LATER)
    assert (stats.scanned, stats.rejected_skipped, stats.review, stats.unlinked) == (1, 1, 0, 1)
    assert _open_items(db_session) == []
    event = db_session.get(EventRow, "ev-second")
    assert event is not None
    assert event.company_id is None
    assert event.payload["match"]["rule"] == "R5"
    assert event.payload["match"]["rejected"] is True

    # a different ambiguous name is still queued
    _event(db_session, "ev-third", _formd("Acme Robotics Partners", state="WA"))
    assert resolve_events(db_session, now=LATER).review == 1


def test_resolve_limit_and_order(db_session: Session, companies: dict[str, Company]) -> None:
    _event(db_session, "ev-b", _formd("Acme Inc"))
    _event(db_session, "ev-a", _formd("Zephyr Technologies Inc", state="TX"))
    stats = resolve_events(db_session, now=NOW, limit=1)
    assert stats.scanned == 1
    linked = [row.event_id for row in db_session.scalars(select(EventRow)) if row.company_id]
    assert linked == ["ev-a"]
    assert resolve_events(db_session, now=NOW).scanned == 1


def test_resolve_skips_events_without_a_name(
    db_session: Session, companies: dict[str, Company]
) -> None:
    _event(
        db_session, "ev-noname", {"accession_number": "x", "amendments": []}, type_="form_s1_filed"
    )
    _event(
        db_session,
        "ev-news-noq",
        {"query": "https://news.example/rss", "match": {"rule": "news_query"}},
        source="news",
        type_="news_launch",
    )
    stats = resolve_events(db_session, now=NOW)
    assert (stats.scanned, stats.no_name) == (2, 2)
    event = db_session.get(EventRow, "ev-noname")
    assert event is not None
    assert "match" not in event.payload
    # an unresolved event stays eligible for the next run
    assert resolve_events(db_session, now=LATER).scanned == 2


def test_resolve_news_event_by_query_name(
    db_session: Session, companies: dict[str, Company]
) -> None:
    payload = {
        "title": "Acme launches a thing",
        "query": "https://news.google.com/rss/search?q=%22Acme%22&hl=en-US",
        "match": {"rule": "news_query", "note": "by name"},
        "key": "news:abc",
    }
    _event(db_session, "ev-news", payload, source="news", type_="news_launch")
    stats = resolve_events(db_session, now=NOW)
    assert stats.by_rule == {"R2": 1}
    event = db_session.get(EventRow, "ev-news")
    assert event is not None
    assert event.company_id == companies["acme"].id
    assert event.payload["match"]["rule"] == "R2"
    assert event.payload["match"]["source_rule"] == "news_query"
    assert event.payload["title"] == "Acme launches a thing"
    assert _aliases(db_session, companies["acme"].id) == {"acme": "manual"}


def test_resolve_other_edgar_forms_use_company_name(
    db_session: Session, companies: dict[str, Company]
) -> None:
    """Other forms match on ``company_name``; an R4 link writes no enrichment (ADR 0034)."""
    _event(
        db_session,
        "ev-s1",
        {"accession_number": "y", "cik": "0000055555", "company_name": "Beacon Helth"},
        type_="form_s1_filed",
    )
    stats = resolve_events(db_session, now=NOW)
    assert stats.by_rule == {"R4": 1}
    assert stats.aliases_added == 0
    beacon = companies["beacon"]
    event = db_session.get(EventRow, "ev-s1")
    assert event is not None
    assert event.company_id == beacon.id
    db_session.refresh(beacon)
    assert (beacon.cik, beacon.legal_name) == (None, None)
    assert "beacon helth" not in _aliases(db_session, beacon.id)

    # an exact name (R2) is trusted and enriches
    _event(
        db_session,
        "ev-s1-exact",
        {"accession_number": "z", "cik": "0000055555", "company_name": "Beacon Health Inc"},
        type_="form_s1_filed",
    )
    assert resolve_events(db_session, now=NOW).by_rule == {"R2": 1}
    db_session.refresh(beacon)
    assert (beacon.cik, beacon.legal_name) == ("0000055555", "Beacon Health Inc")


def test_resolve_skips_sources_without_company_names(
    db_session: Session, companies: dict[str, Company]
) -> None:
    """A GitHub repo or a job title named like a company is not a company name."""
    _event(
        db_session,
        "ev-repo",
        {"name": "acme", "full_name": "someone/acme", "language": "Rust"},
        source="github",
        type_="repo_created",
    )
    _event(
        db_session,
        "ev-job",
        {"title": "Acme", "vendor": "greenhouse", "board_identifier": "nobody"},
        source="greenhouse",
        type_="job_opened",
    )
    stats = resolve_events(db_session, now=NOW)
    assert (stats.scanned, stats.no_name, stats.auto_linked) == (2, 2, 0)
    for event_id in ("ev-repo", "ev-job"):
        event = db_session.get(EventRow, event_id)
        assert event is not None
        assert event.company_id is None
        assert "match" not in event.payload


def test_resolve_leaves_linked_events_alone(
    db_session: Session, companies: dict[str, Company]
) -> None:
    _event(db_session, "ev-linked", _formd("Acme Inc"), company_id=companies["zephyr"].id)
    stats = resolve_events(db_session, now=NOW)
    assert stats.scanned == 0
    event = db_session.get(EventRow, "ev-linked")
    assert event is not None
    assert event.company_id == companies["zephyr"].id
