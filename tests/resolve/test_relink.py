"""``tell resolve relink-formd`` (ADR 0034): undo production's wrong Form D links.

The main test replays the 41 links production made on 2026-10-14 the way the pre-guard
resolver made them (every link wrote the issuer name and previous names as ``edgar``
aliases, ``legal_name`` and ``cik``), then repairs them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.models import Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve.match import (
    CONFIDENCE,
    add_aliases,
    backfill_aliases,
    link_event,
    resolve_events,
)
from tell.resolve.relink import RELINKED_BY, relink_formd
from tests.resolve.formd_cases import CASES, NOW, add_event, seed_cases

LATER = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _old_link(session: Session, event: EventRow, company_id: int, rule: str) -> None:
    """A link as the pre-ADR 0034 resolver made it: enrichment always, audit record."""
    link_event(session, event, company_id, trusted=True)
    event.payload = {
        **event.payload,
        "match": {
            "rule": rule,
            "ratio": 100.0,
            "candidates": [],
            "confidence": CONFIDENCE.get(rule, 1.0),
            "resolved_at": "2026-10-14T09:16:34+00:00",
        },
    }
    session.flush()


def _replay_production(session: Session) -> dict[str, Company]:
    companies = seed_cases(session)
    for case in sorted(CASES, key=lambda c: (c["payload"]["date_filed"], c["event_id"])):
        event = add_event(session, case)
        _old_link(session, event, companies[case["company"]["domain"]].id, case["rule"])
    return companies


def _edgar_aliases(session: Session, company: Company) -> set[str]:
    return set(
        session.scalars(
            select(CompanyAlias.alias).where(
                CompanyAlias.company_id == company.id, CompanyAlias.source == "edgar"
            )
        )
    )


def _state(session: Session) -> tuple[Any, ...]:
    session.expire_all()
    events = sorted(
        (e.event_id, e.company_id, repr(e.payload)) for e in session.scalars(select(EventRow))
    )
    companies = sorted((c.id, c.legal_name, c.cik) for c in session.scalars(select(Company)))
    aliases = sorted(
        session.execute(
            select(CompanyAlias.company_id, CompanyAlias.alias, CompanyAlias.source)
        ).all()
    )
    items = len(session.scalars(select(ReviewQueueItem)).all())
    return events, companies, aliases, items


WRONG_COMPANIES = {
    "payloadcms.com",
    "overlord.app",
    "ncompass.tech",
    "scale.com",
    "infera.bio",
    "grai.io",
    "hub.xyz",
    "alpaca.markets",
    "vanta.com",
    "modalup.com",
    "sylvian.ai",
    "protocol.ai",
    "jasper.ai",
    "trymanatee.com",
    "sunflowerclinic.com",
    "echothat.ai",
}


def test_relink_repairs_the_production_corruption(db_session: Session) -> None:
    companies = _replay_production(db_session)
    ncompass, scale, exa = (
        companies["ncompass.tech"],
        companies["scale.com"],
        companies["exa.ai"],
    )
    # the corruption, as production had it: a wrong R3 link wrote the alias that made the
    # next filing an R2, and the issuer's legal name and CIK
    assert "compass" in _edgar_aliases(db_session, ncompass)
    assert (ncompass.legal_name, ncompass.cik) == ("Compass, Inc.", "0001563190")
    assert "scaler ai" in _edgar_aliases(db_session, scale)
    assert scale.legal_name == "Scaler AI Labs Inc."
    assert sum(1 for e in db_session.scalars(select(EventRow)) if e.company_id) == 41

    before = _state(db_session)
    dry = relink_formd(db_session, now=NOW)
    assert _state(db_session) == before  # a dry run writes nothing
    assert dry["dry_run"] is True
    assert (dry["scanned"], dry["kept"], dry["unlinked"], dry["moved"]) == (41, 20, 21, 0)
    assert dry["kept_by_rule"] == {"R2": 20}
    assert dry["by_new_rule"] == {"R5": 20, "R6": 1}
    assert dry["review_items_opened"] == 20
    assert dry["by_reason"]["r3_review_only"] == 9  # 5 R3 links + 4 knock-on R2 links
    assert dry["by_reason"]["exact_alias_untrusted"] == 4
    assert dry["by_reason"]["pooled_fund"] == 1
    assert (dry["legal_names_reset"], dry["ciks_reset"]) == (16, 16)
    assert {a["alias"] for a in dry["aliases"]} == {
        "compass",
        "scaler ai",
        "inferra",
        "alpacax",
        "sylvan",
        "polyester",
        "bbot",
    }
    changed = {c["event_id"]: c for c in dry["changes"]}
    for case in CASES:
        assert (case["event_id"] in changed) is (case["verdict"] != "SAME"), case["event_id"]
    compass = [c for c in dry["changes"] if c["company"] == "nCompass Technologies"]
    assert len(compass) == 4
    assert all("over_five_years_for_recent_batch" in c["reason"] for c in compass)

    applied = relink_formd(db_session, now=LATER, apply=True)
    assert {k: v for k, v in applied.items() if k != "dry_run"} == {
        k: v for k, v in dry.items() if k != "dry_run"
    }
    for case in CASES:
        event = db_session.get(EventRow, case["event_id"])
        assert event is not None
        expected = companies[case["company"]["domain"]].id
        if case["verdict"] == "SAME":
            assert event.company_id == expected
            assert event.payload["match"]["rule"] == "R2"
            continue
        assert event.company_id is None
        match = event.payload["match"]
        assert match["relinked_by"] == RELINKED_BY
        assert match["previous"]["rule"] == case["rule"]
        assert match["previous"]["company_id"] == expected
        assert match["resolved_at"] == "2026-10-15T12:00:00+00:00"
    for domain, company in companies.items():
        db_session.refresh(company)
        if domain in WRONG_COMPANIES:
            assert (company.legal_name, company.cik) == (None, None), domain
        else:
            assert company.legal_name is not None, domain
            assert company.cik is not None, domain
    assert _edgar_aliases(db_session, ncompass) == set()
    assert _edgar_aliases(db_session, exa) == {"metaphor systems"}  # a SAME link's history
    items = db_session.scalars(select(ReviewQueueItem)).all()
    assert len(items) == 20
    assert all(item.payload.get("guard_failed") for item in items)

    # idempotent
    snapshot = _state(db_session)
    again = relink_formd(db_session, now=LATER, apply=True)
    assert (again["scanned"], again["kept"], again["unlinked"]) == (20, 20, 0)
    assert (again["aliases_deleted"], again["legal_names_reset"], again["ciks_reset"]) == (0, 0, 0)
    assert again["changes"] == []
    assert _state(db_session) == snapshot

    # the resolver does not bring them back, and keeps the relink audit trail
    stats = resolve_events(db_session, now=LATER, retry=True)
    assert stats.auto_linked == 0
    assert stats.by_rule == {"R5": 20, "R6": 1}
    assert len(db_session.scalars(select(ReviewQueueItem)).all()) == 20
    compass_event = db_session.get(EventRow, compass[0]["event_id"])
    assert compass_event is not None
    assert compass_event.payload["match"]["previous"]["company_id"] == ncompass.id


# --- smaller cases -----------------------------------------------------------------------------


def _company(session: Session, domain: str, name: str, **extra: Any) -> Company:
    company = Company(domain=domain, name=name, **extra)
    session.add(company)
    session.flush()
    return company


def _event(
    session: Session,
    event_id: str,
    name: str,
    *,
    filed: str = "2026-08-01",
    cik: str = "0000000001",
    type_: str = "form_d_filed",
    **extra: Any,
) -> EventRow:
    payload: dict[str, Any] = {
        "accession_number": f"acc-{event_id}",
        "cik": cik,
        "form_type": "D",
        "date_filed": filed,
        "issuer_name": name,
        "issuer_city": "San Francisco",
        "issuer_state": "CA",
        "previous_names": [],
        "jurisdiction_of_inc": "DELAWARE",
        "year_of_inc": 2024,
        "entity_type": "Corporation",
        "industry_group_type": "Other Technology",
        **extra,
    }
    row = EventRow(
        event_id=event_id,
        source="edgar",
        type=type_,
        occurred_at=datetime.fromisoformat(filed).replace(tzinfo=UTC),
        observed_at=NOW,
        magnitude=Decimal(1),
        url=f"https://example.test/{event_id}",
        payload=payload,
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row


def test_renamed_company_keeps_its_filings_through_previous_names(db_session: Session) -> None:
    """An older filing linked through an alias a later trusted filing wrote stays linked."""
    nova = _company(db_session, "nova.example", "Nova Robotics", yc_status="Active")
    backfill_aliases(db_session)
    newer = _event(db_session, "ev-new", "Nova Robotics, Inc.", previous_names=["Old Nova Corp"])
    older = _event(db_session, "ev-old", "Old Nova Corp", filed="2026-06-01")
    _old_link(db_session, newer, nova.id, "R2")
    _old_link(db_session, older, nova.id, "R2")
    # a collector CIK link of the same issuer (no match record)
    cik_linked = _event(db_session, "ev-cik", "Nova Robotics Inc", filed="2026-09-01")
    cik_linked.company_id = nova.id
    db_session.flush()
    assert nova.cik == "0000000001"

    report = relink_formd(db_session, now=NOW, apply=True)
    assert (report["scanned"], report["kept"], report["unlinked"]) == (3, 3, 0)
    assert report["kept_by_rule"] == {"R2": 2, "cik": 1}
    assert "old nova" in _edgar_aliases(db_session, nova)
    assert nova.cik == "0000000001"


def test_cik_links_follow_the_link_that_wrote_the_cik(db_session: Session) -> None:
    zeta = _company(
        db_session,
        "zetta.example",
        "Zetta Analytica",
        hq_state="CA",
        yc_status="Active",
        yc_batch="Winter 2024",
    )
    backfill_aliases(db_session)
    wrong = _event(db_session, "ev-r3", "Zeta Analytica LLC", cik="0000000777")
    _old_link(db_session, wrong, zeta.id, "R3")
    zeta.cik_source = "guarded_r2"  # however it was labelled, the source goes with the CIK
    follower = _event(db_session, "ev-follow", "Zeta Analytica LLC", cik="0000000777")
    follower.company_id = zeta.id  # the collector linked it by the CIK the R3 link wrote
    db_session.flush()
    assert zeta.cik == "0000000777"

    report = relink_formd(db_session, now=NOW, apply=True)
    reasons = {c["event_id"]: c["reason"] for c in report["changes"]}
    assert reasons["ev-r3"] == [R3_REASON]
    assert "cik_untrusted" in reasons["ev-follow"]
    db_session.refresh(zeta)
    assert (zeta.cik, zeta.cik_source, zeta.legal_name) == (None, None, None)
    assert _edgar_aliases(db_session, zeta) == set()
    # both events now wait in review (one item each)
    assert len(db_session.scalars(select(ReviewQueueItem)).all()) == 2


R3_REASON = "r3_review_only"


def test_external_cik_and_human_links_are_kept(db_session: Session) -> None:
    manatee = _company(
        db_session, "trymanatee.com", "Manatee", yc_status="Inactive", yc_batch="Summer 2022"
    )
    seeded = _company(db_session, "seeded.example", "Seeded Co", cik="0000000555")
    backfill_aliases(db_session)
    human = _event(db_session, "ev-human", "Manatee, Inc.", cik="0000000444")
    _old_link(db_session, human, manatee.id, "manual")
    by_cik = _event(db_session, "ev-seeded", "Something Else Entirely", cik="0000000555")
    by_cik.company_id = seeded.id  # CIK set outside the resolver (no linked filing wrote it)
    db_session.flush()
    report = relink_formd(db_session, now=NOW, apply=True)
    assert (report["kept"], report["unlinked"]) == (2, 0)
    assert report["kept_by_rule"] == {"cik": 1, "manual": 1}


def test_a_link_that_now_matches_another_company_moves(db_session: Session) -> None:
    gamma = _company(db_session, "gamma.example", "Gamma", yc_status="Active")
    other = _company(db_session, "gammax.example", "Gammax Robotics", yc_status="Active")
    backfill_aliases(db_session)
    event = _event(db_session, "ev-gamma", "Gamma, Inc.", cik="0000000888")
    event.company_id = other.id
    other.cik = "0000000888"
    other.legal_name = "Gamma, Inc."
    add_aliases(db_session, other.id, ["Gamma, Inc."], source="edgar")
    event.payload = {**event.payload, "match": {"rule": "R2", "ratio": 100.0, "candidates": []}}
    db_session.flush()

    report = relink_formd(db_session, now=NOW, apply=True)
    assert (report["moved"], report["unlinked"]) == (1, 0)
    (change,) = report["changes"]
    assert change["new_company_id"] == gamma.id
    assert change["reason"] == ["matches_other_company"]
    assert event.company_id == gamma.id
    db_session.refresh(other)
    db_session.refresh(gamma)
    assert (other.cik, other.legal_name) == (None, None)
    assert _edgar_aliases(db_session, other) == set()
    assert (gamma.cik, gamma.legal_name) == ("0000000888", "Gamma, Inc.")


def test_aliases_still_supported_by_another_link_are_kept(db_session: Session) -> None:
    """An S-1 still linked under the same name keeps the alias; the Form D link goes."""
    acme = _company(db_session, "acme.example", "Acme Robotics", yc_status="Inactive")
    backfill_aliases(db_session)
    formd = _event(db_session, "ev-d", "Acme Robo Inc", cik="0000000999")
    _old_link(db_session, formd, acme.id, "R4")
    s1 = EventRow(
        event_id="ev-s1",
        company_id=acme.id,
        source="edgar",
        type="form_s1_filed",
        occurred_at=NOW,
        observed_at=NOW,
        magnitude=Decimal(1),
        url="https://example.test/ev-s1",
        payload={"company_name": "Acme Robo Inc", "cik": "0000000998"},
        run_id="test-run",
    )
    db_session.add(s1)
    db_session.flush()
    report = relink_formd(db_session, now=NOW, apply=True)
    assert report["changes"][0]["reason"] == ["yc_status_not_active"]
    assert (report["aliases_deleted"], report["legal_names_reset"]) == (0, 0)
    assert report["ciks"] == [
        {"company_id": acme.id, "company": "Acme Robotics", "cik": "0000000999"}
    ]
    assert _edgar_aliases(db_session, acme) == {"acme robo"}
    db_session.refresh(acme)
    assert (acme.cik, acme.legal_name) == (None, "Acme Robo Inc")


def test_relink_with_nothing_linked(db_session: Session) -> None:
    _event(db_session, "ev-unlinked", "Nobody Inc")
    report = relink_formd(db_session, now=NOW)
    assert (report["scanned"], report["changes"], report["review_items_opened"]) == (0, [], 0)


def test_rejected_pair_is_not_reopened(db_session: Session) -> None:
    acme = _company(db_session, "acme.example", "Acme Robotics", yc_status="Inactive")
    backfill_aliases(db_session)
    event = _event(db_session, "ev-a", "Acme Robotics, Inc.")
    _old_link(db_session, event, acme.id, "R2")
    db_session.add(
        ReviewQueueItem(
            kind="entity_match",
            status="rejected",
            payload={
                "event_id": "ev-older",
                "name": "Acme Robotics Inc",
                "candidates": [{"company_id": acme.id, "alias": "acme robotics", "ratio": 100.0}],
            },
            created_at=NOW,
        )
    )
    db_session.flush()
    dry = relink_formd(db_session, now=NOW)
    assert (dry["unlinked"], dry["review_items_opened"]) == (1, 0)
    applied = relink_formd(db_session, now=NOW, apply=True)
    assert applied["review_items_opened"] == 0
    assert event.payload["match"]["rejected"] is True
    assert event.company_id is None


def test_r0_links_of_an_issuer_first_company_are_kept(db_session: Session) -> None:
    """An R0 link rests on a CIK discovery set (ADR 0035); relink never undoes it, even when
    the issuer's name no longer finds the company."""
    company = Company(
        domain="veined.example",
        name="Veined",
        source="issuer_first",
        cik="0002100001",
        cik_source="issuer_first_a",
    )
    db_session.add(company)
    db_session.flush()
    backfill_aliases(db_session)
    case = {
        "event_id": "ev-r0",
        "type": "form_d_filed",
        "payload": {
            "issuer_name": "Entirely Renamed Holdings Inc",
            "cik": "0002100001",
            "entity_type": "Corporation",
            "jurisdiction_of_inc": "DELAWARE",
            "year_of_inc": 2025,
            "date_filed": "2026-10-01",
            "previous_names": [],
        },
    }
    event = add_event(db_session, case)
    assert resolve_events(db_session, now=NOW).by_rule == {"R0": 1}
    assert event.company_id == company.id
    report = relink_formd(db_session, now=LATER, apply=True)
    assert (report["kept"], report["unlinked"], report["ciks_reset"]) == (1, 0, 0)
    assert event.company_id == company.id
    db_session.refresh(company)
    assert (company.cik, company.cik_source) == ("0002100001", "issuer_first_a")
