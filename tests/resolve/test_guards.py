"""Form D linking guards and the distinctive-token floor (ADR 0034), guard (f) (ADR 0035).

The fixture ``formd_links_20261014.json`` holds the 41 Form D links production made to YC
companies on 2026-10-14, each hand-verified against the SEC filing and the company's site
(20 SAME, 20 DIFFERENT, 1 UNCLEAR): the issuer fields of the payload, the linked company's
directory fields and the verdict. No person appears in it.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.models import Company, CompanyAlias, EventRow, IssuerDiscovery, ReviewQueueItem
from tell.resolve.aliases import (
    DESCRIPTOR_TOKENS,
    GENERIC_ALIAS_TOKENS,
    distinctive_tokens,
    shares_distinctive_token,
)
from tell.resolve.guards import (
    GUARD_REASONS,
    NO_SITE_EVIDENCE,
    R3_REVIEW_ONLY,
    TRUSTED_CIK_SOURCES,
    CompanyFacts,
    IssuerFacts,
    batch_year,
    cik10,
    filing_guard_failures,
    fund_signals,
    guard_failures,
    site_evidence,
)
from tell.resolve.match import (
    AliasIndex,
    backfill_aliases,
    issuer_facts,
    match_name,
    resolve_events,
)
from tell.resolve.review import apply_verdict
from tests.resolve.formd_cases import CASES, CASES_PATH, NOW, add_event, seed_cases


def _issuer(case: dict[str, Any]) -> IssuerFacts:
    payload = case["payload"]
    return IssuerFacts.from_payload(payload["issuer_name"], payload)


def _company_facts(case: dict[str, Any]) -> CompanyFacts:
    company = case["company"]
    return CompanyFacts(yc_status=company["yc_status"], yc_batch=company["yc_batch"])


# Every production link that was not SAME, and the guards that now stop it. Knock-on R2
# links (made through an alias a wrong R3 link wrote) are marked; they are stopped twice:
# the alias is never written now, and the guard fails anyway.
EXPECTED_FAILURES: dict[tuple[str, str], list[str]] = {
    ("Payload, Inc.", "R2"): ["yc_status_not_active"],  # Acquired
    ("Overlord Labs, Inc.", "R2"): ["incorporated_after_batch"],  # 2024 vs Winter 2023
    ("Compass, Inc.", "R3"): [R3_REVIEW_ONLY, "over_five_years_for_recent_batch"],
    ("Compass, Inc.", "R2"): ["over_five_years_for_recent_batch"],  # knock-on of the R3
    ("Scaler AI Labs Inc.", "R3"): [R3_REVIEW_ONLY, "incorporated_after_batch"],
    ("Scaler AI Labs Inc.", "R2"): ["incorporated_after_batch"],  # knock-on of the R3
    ("Inferra Inc.", "R3"): [R3_REVIEW_ONLY],
    ("AlpacaX Inc.", "R3"): [R3_REVIEW_ONLY, "incorporated_after_batch"],  # 2025 vs W19
    ("Sylvan Labs, Inc.", "R3"): [R3_REVIEW_ONLY, "yc_status_not_active"],
    ("GRAI, Inc.", "R2"): ["yc_status_not_active", "incorporated_after_batch"],  # UNCLEAR
    ("Hub Corp", "R2"): ["jurisdiction_not_delaware"],  # North Carolina
    ("VANTA HOLDINGS INC.", "R2"): ["jurisdiction_not_delaware"],  # British Columbia
    ("Modal Labs, Inc.", "R2"): ["yc_status_not_active"],  # Inactive
    ("Protocol Labs LLC", "R2"): [  # a 2026 Wyoming LLC vs the Summer 2014 company
        "entity_type_not_corporation",
        "jurisdiction_not_delaware",
        "incorporated_after_batch",
    ],
    ("Manatee, Inc.", "R2"): ["yc_status_not_active"],
    ("Sunflower Labs Inc.", "R2"): ["over_five_years_for_recent_batch"],  # vs Summer 2026
    ("Echo Ltd.", "R2"): ["jurisdiction_not_delaware", "yc_status_not_active"],  # Alberta
}
FUNDS = {"Jasper Holdings LP"}  # a Cayman limited partnership: never a candidate (R6)


def test_fixture_is_the_production_sample() -> None:
    verdicts: dict[str, int] = {}
    for case in CASES:
        verdicts[case["verdict"]] = verdicts.get(case["verdict"], 0) + 1
    assert verdicts == {"SAME": 20, "DIFFERENT": 20, "UNCLEAR": 1}
    assert sum(1 for c in CASES if c["rule"] == "R3") == 5
    assert all(c["rule"] in {"R2", "R3"} for c in CASES)
    assert CASES_PATH.stat().st_size < 50_000
    assert "@" not in CASES_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "case", CASES, ids=[f"{c['payload']['issuer_name']}:{c['rule']}" for c in CASES]
)
def test_guards_on_every_production_link(case: dict[str, Any]) -> None:
    """All 20 SAME links pass every guard; every other link fails one or is a fund."""
    issuer = _issuer(case)
    name = case["payload"]["issuer_name"]
    signals = fund_signals(issuer)
    failed = guard_failures(issuer, _company_facts(case))
    if case["rule"] == "R3":
        failed = [R3_REVIEW_ONLY, *failed]
    if case["verdict"] == "SAME":
        assert (signals, failed) == ([], []), name
        assert issuer.entity_type == "Corporation"
        assert issuer.jurisdiction == "DELAWARE"
        assert case["company"]["yc_status"] == "Active"
    elif name in FUNDS:
        assert signals == ["token:lp"]
    else:
        assert signals == [], name
        assert failed == EXPECTED_FAILURES[(name, case["rule"])], name
    assert set(failed) <= set(GUARD_REASONS)


def test_over_five_years_is_read_from_a_null_year() -> None:
    legion = next(c for c in CASES if c["payload"]["issuer_name"] == "Legion Health, Inc.")
    facts = _issuer(legion)
    assert (facts.year_of_inc, facts.over_five_years, facts.filed_year) == (None, True, 2026)
    # a payload without the key says nothing about the year
    assert IssuerFacts.from_payload("Acme", {}).over_five_years is False
    assert IssuerFacts.from_payload("Acme", {"date_filed": "not a date"}).filed_year is None
    assert IssuerFacts.from_payload("Acme", {"year_of_inc": True}).year_of_inc is None


@pytest.mark.parametrize(
    ("batch", "filed", "fails"),
    [
        ("Winter 2024", "2026-07-17", True),  # Compass vs nCompass
        ("Summer 2026", "2026-09-28", True),  # Sunflower Labs vs Sunflower
        ("Winter 2023", "2026-07-17", False),
        ("Summer 2021", "2026-07-01", False),  # Legion Health
        ("Winter 2024", "2030-01-15", False),  # relative to the filing: does not rot
        ("Winter 2024", None, True),  # no filing date: the 2024 floor
        ("Winter 2023", None, False),
        ("Unspecified", "2026-07-01", False),
    ],
)
def test_over_five_years_against_recent_batches(batch: str, filed: str | None, fails: bool) -> None:
    payload: dict[str, Any] = {
        "entity_type": "Corporation",
        "jurisdiction_of_inc": "DELAWARE",
        "year_of_inc": None,
    }
    if filed is not None:
        payload["date_filed"] = filed
    failures = guard_failures(
        IssuerFacts.from_payload("Acme, Inc.", payload), CompanyFacts("Active", batch)
    )
    assert ("over_five_years_for_recent_batch" in failures) is fails


def test_incorporation_year_against_batch() -> None:
    def failures(year: int, batch: str) -> list[str]:
        facts = IssuerFacts("Acme", "Corporation", "DELAWARE", year_of_inc=year, filed_year=2026)
        return guard_failures(facts, CompanyFacts("Active", batch))

    assert failures(2026, "Summer 2026") == []
    assert failures(2025, "Summer 2026") == []
    assert failures(2024, "Winter 2023") == ["incorporated_after_batch"]
    assert failures(2030, "Unspecified") == []


def test_entity_and_status_guards() -> None:
    corp = IssuerFacts("Acme", "corporation", "Delaware")
    assert guard_failures(corp, None) == []  # case-insensitive; no company facts known
    # no YC status: guard (f) wants site evidence (ADR 0035); batch unknown: (d) passes
    assert guard_failures(corp, CompanyFacts()) == [NO_SITE_EVIDENCE]
    assert guard_failures(corp, CompanyFacts("Active", None)) == []
    assert guard_failures(corp, CompanyFacts("Public", None)) == ["yc_status_not_active"]
    assert guard_failures(IssuerFacts("Acme"), None) == [
        "entity_type_not_corporation",
        "jurisdiction_not_delaware",
    ]
    llc = IssuerFacts("Acme", "Limited Liability Company", "DELAWARE")
    assert guard_failures(llc, None) == ["entity_type_not_corporation"]


@pytest.mark.parametrize(
    ("batch", "year"),
    [("Summer 2021", 2021), ("Spring 2026", 2026), ("Unspecified", None), ("", None), (None, None)],
)
def test_batch_year(batch: str | None, year: int | None) -> None:
    assert batch_year(batch) == year


@pytest.mark.parametrize(
    ("name", "industry", "signals"),
    [
        ("Arboretum Fund I, a series of CGF2026 LLC", None, ["token:fund", "phrase:a series of"]),
        ("Jasper Holdings LP", "Insurance", ["token:lp"]),
        ("Acme Opportunity Master Fund, L.P.", None, ["token:fund", "token:lp", "token:master"]),
        ("Acme Offshore Feeder Ltd.", None, ["token:feeder"]),
        ("Acme SPV 3 LLC", None, ["token:spv"]),
        ("Acme Statutory Trust", None, ["token:trust"]),
        ("Quiet Harbor Holdings LLC", "Pooled Investment Fund", ["industry"]),
        ("Acme, Inc.", "Other Technology", []),
        ("Trustly Inc.", None, []),  # a token, not a substring
        ("Fundrise LLC", None, []),
    ],
)
def test_fund_signals(name: str, industry: str | None, signals: list[str]) -> None:
    assert fund_signals(IssuerFacts(name, industry=industry)) == signals


# --- distinctive tokens --------------------------------------------------------------------------


def test_descriptor_list_covers_the_ticket_words() -> None:
    required = {
        "technologies",
        "therapeutics",
        "insurance",
        "health",
        "bio",
        "biosciences",
        "robotics",
        "energy",
        "systems",
        "solutions",
        "software",
        "labs",
        "studio",
        "network",
        "data",
        "analytics",
        "security",
        "payments",
        "finance",
        "capital",
        "medical",
        "diagnostics",
        "pharma",
        "foods",
    }
    assert required <= DESCRIPTOR_TOKENS


@pytest.mark.parametrize(
    ("alias", "tokens"),
    [
        ("lsk technologies", {"lsk"}),
        ("ai insurance", set()),
        ("h3x technologies", {"h3x"}),
        ("cb therapeutics", {"cb"}),
        ("the fund 2", set()),
        ("beacon health", {"beacon"}),
    ],
)
def test_distinctive_tokens(alias: str, tokens: set[str]) -> None:
    assert distinctive_tokens(alias) == tokens
    assert not tokens & (DESCRIPTOR_TOKENS | GENERIC_ALIAS_TOKENS)


@pytest.mark.parametrize(
    ("name", "alias", "shared"),
    [
        # production's 85.71 review items: only the descriptor is shared
        ("nimbus technologies", "lsk technologies", False),
        ("nimbus technologies", "h3x technologies", False),
        ("quantum insurance", "ai insurance", False),
        ("genix therapeutics", "cb therapeutics", False),
        # a real shared word, or a one-letter typo of one
        ("acme robotics group", "acme robotics", True),
        ("beakon health", "beacon health", True),
        ("acmee", "acme", True),
        ("methodd", "method", True),
        ("lsx technologies", "lsk technologies", False),  # 3 letters: equal or nothing
        ("compass", "ncompass", True),  # a near miss reaches a human, never a link
    ],
)
def test_shares_distinctive_token(name: str, alias: str, shared: bool) -> None:
    assert shares_distinctive_token([name], [alias]) is shared


# --- match_name with the guards -------------------------------------------------------------------


def _company(session: Session, domain: str, name: str, **extra: Any) -> Company:
    company = Company(domain=domain, name=name, **extra)
    session.add(company)
    session.flush()
    return company


def _formd_facts(name: str, **extra: Any) -> IssuerFacts:
    payload: dict[str, Any] = {
        "entity_type": "Corporation",
        "jurisdiction_of_inc": "DELAWARE",
        "year_of_inc": 2024,
        "date_filed": "2026-10-14",
        "industry_group_type": "Other Technology",
        **extra,
    }
    return IssuerFacts.from_payload(name, payload)


def test_descriptor_only_candidate_is_r6_not_review(db_session: Session) -> None:
    """The 2026-10-14 queue: two-token aliases sharing only a descriptor scored 85.71."""
    _company(db_session, "h3x.tech", "H3X Technologies", hq_state="CO")
    backfill_aliases(db_session)
    decision = match_name(db_session, "Nimbus Technologies Inc", state="WA")
    assert decision.candidates[0].alias == "h3x technologies"
    assert decision.candidates[0].ratio == 85.71
    assert decision.candidates[0].distinctive is False
    assert (decision.rule, decision.action) == ("R6", "unlinked")
    # a name sharing the distinctive word still reaches review
    assert match_name(db_session, "H3X Technologies Group", state="WA").rule == "R5"


def test_r3_is_review_only_for_form_d_but_not_for_news(db_session: Session) -> None:
    ncompass = _company(
        db_session,
        "ncompass.tech",
        "nCompass Technologies",
        hq_city="New York City",
        hq_state="NY",
        yc_status="Active",
    )
    backfill_aliases(db_session)
    plain = match_name(db_session, "Compass, Inc.", state="NY")
    assert (plain.rule, plain.company_id) == ("R3", ncompass.id)  # news and other forms
    formd = match_name(
        db_session, "Compass, Inc.", state="NY", issuer=_formd_facts("Compass, Inc.")
    )
    assert (formd.rule, formd.action, formd.company_id) == ("R5", "review", None)
    assert formd.guard_failed == [R3_REVIEW_ONLY]
    assert (formd.demoted_from, formd.proposed_company_id) == ("R3", ncompass.id)
    audit = formd.audit(now=NOW)
    assert audit["guard_failed"] == [R3_REVIEW_ONLY]
    assert audit["proposed_company_id"] == ncompass.id


def test_r2_and_r4_guards_demote_to_review(db_session: Session) -> None:
    manatee = _company(
        db_session,
        "trymanatee.com",
        "Manatee",
        hq_state="MD",
        yc_status="Inactive",
        yc_batch="Summer 2022",
    )
    beacon = _company(
        db_session,
        "beaconhealth.com",
        "Beacon Health",
        hq_state="MA",
        yc_status="Active",
        yc_batch="Winter 2025",
    )
    backfill_aliases(db_session)
    index = AliasIndex.from_session(db_session)
    r2 = match_name(
        db_session,
        "Manatee, Inc.",
        issuer=_formd_facts("Manatee, Inc.", year_of_inc=2022),
        index=index,
    )
    assert (r2.rule, r2.demoted_from, r2.guard_failed) == ("R5", "R2", ["yc_status_not_active"])
    r4 = match_name(
        db_session,
        "Beacon Helth Inc",
        issuer=_formd_facts("Beacon Helth Inc", jurisdiction_of_inc="WYOMING"),
        index=index,
    )
    assert (r4.rule, r4.demoted_from) == ("R5", "R4")
    assert r4.guard_failed == ["jurisdiction_not_delaware"]
    assert r4.proposed_company_id == beacon.id
    ok = match_name(
        db_session, "Beacon Helth Inc", issuer=_formd_facts("Beacon Helth Inc"), index=index
    )
    assert (ok.rule, ok.company_id) == ("R4", beacon.id)
    assert r2.proposed_company_id == manatee.id


def test_fund_issuer_is_r6_with_no_review(db_session: Session) -> None:
    _company(db_session, "jasper.ai", "Jasper.ai", yc_status="Active", yc_batch="Winter 2018")
    backfill_aliases(db_session)
    facts = _formd_facts(
        "Jasper Holdings LP",
        entity_type="Limited Partnership",
        jurisdiction_of_inc="CAYMAN ISLANDS",
        industry_group_type="Insurance",
    )
    decision = match_name(db_session, "Jasper Holdings LP", issuer=facts)
    assert (decision.rule, decision.action) == ("R6", "unlinked")
    assert decision.fund_signals == ["token:lp"]
    assert decision.audit(now=NOW)["excluded"] == "pooled_fund"
    # without Form D facts (news) the same name is an exact alias, as before
    assert match_name(db_session, "Jasper Holdings LP").rule == "R2"


# --- the resolver over the production sample ------------------------------------------------------


def test_resolver_on_the_production_sample_links_only_the_right_companies(
    db_session: Session,
) -> None:
    """Before: 41 links, 20 right. After: 20 links, all right; the rest reviewed or R6."""
    companies = seed_cases(db_session)
    for case in CASES:
        add_event(db_session, case)
    stats = resolve_events(db_session, now=NOW)
    assert stats.scanned == 41
    linked = wrong = 0
    for case in CASES:
        event = db_session.get(EventRow, case["event_id"])
        assert event is not None
        expected = companies[case["company"]["domain"]].id
        if event.company_id is not None:
            linked += 1
            wrong += event.company_id != expected or case["verdict"] != "SAME"
        else:
            assert case["verdict"] != "SAME", case["payload"]["issuer_name"]
    assert (linked, wrong) == (20, 0)
    assert stats.auto_linked == 20
    assert stats.by_rule.get("R3", 0) == 0
    assert stats.pooled_funds == 1  # Jasper Holdings LP
    jasper = next(c for c in CASES if c["payload"]["issuer_name"] == "Jasper Holdings LP")
    items = db_session.scalars(select(ReviewQueueItem)).all()
    assert jasper["event_id"] not in {i.payload["event_id"] for i in items}
    assert stats.review == len(items) == 20
    assert all(i.payload.get("guard_failed") for i in items)
    # the wrong issuers wrote nothing on the YC companies
    ncompass = companies["ncompass.tech"]
    db_session.refresh(ncompass)
    assert (ncompass.legal_name, ncompass.cik) == (None, None)
    aliases = set(
        db_session.scalars(select(CompanyAlias.alias).where(CompanyAlias.source == "edgar"))
    )
    assert {"compass", "scaler ai", "polyester", "bbot"}.isdisjoint(aliases)
    assert "metaphor systems" in aliases  # Exa's previous name, from a trusted R2


def test_issuer_facts_only_for_form_d(db_session: Session) -> None:
    case = CASES[0]
    event = add_event(db_session, case)
    assert issuer_facts(event) == _issuer(case)
    event.type = "form_s1_filed"
    assert issuer_facts(event) is None
    event.type = "form_d_filed"
    event.payload = {**event.payload, "issuer_name": "  "}
    assert issuer_facts(event) is None


def test_review_verdict_writes_enrichment_a_demoted_link_did_not(db_session: Session) -> None:
    """A human's link is trusted: the guarded issuer's name and CIK land on the company."""
    companies = seed_cases(db_session)
    payload_case = next(c for c in CASES if c["payload"]["issuer_name"] == "Payload, Inc.")
    add_event(db_session, payload_case)
    resolve_events(db_session, now=NOW)
    payload_co = companies["payloadcms.com"]
    db_session.refresh(payload_co)
    assert (payload_co.legal_name, payload_co.cik) == (None, None)
    (item,) = db_session.scalars(select(ReviewQueueItem)).all()
    assert item.payload["guard_failed"] == ["yc_status_not_active"]
    assert item.payload["proposed_company_id"] == payload_co.id
    apply_verdict(db_session, item.id, "link", company_id_or_domain="payloadcms.com", now=NOW)
    db_session.refresh(payload_co)
    assert payload_co.legal_name == "Payload, Inc."
    assert payload_co.cik == payload_case["payload"]["cik"]


# --- guard (f): companies without YC facts (ADR 0035) ------------------------------------------

MARBLE_CIK = "0002100001"


def test_production_sample_never_trips_guard_f() -> None:
    """Every company in the 2026-10-14 sample has a YC status: guard (f) changes nothing."""
    for case in CASES:
        assert case["company"]["yc_status"], case["payload"]["issuer_name"]
        assert NO_SITE_EVIDENCE not in guard_failures(_issuer(case), _company_facts(case))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0001916502", "0001916502"),
        ("1916502", "0001916502"),
        (1916502, "0001916502"),
        (" 42 ", "0000000042"),
        ("", None),
        ("CIK1", None),
        ("12345678901", None),
        (None, None),
        (True, None),
    ],
)
def test_cik10(value: object, expected: str | None) -> None:
    assert cik10(value) == expected


def test_site_evidence_needs_a_trusted_cik_or_a_stored_record() -> None:
    issuer = _formd_facts("Marble, Inc.", cik=MARBLE_CIK)
    assert issuer.cik == MARBLE_CIK
    assert site_evidence(issuer, CompanyFacts()) is False
    for source in sorted(TRUSTED_CIK_SOURCES):
        assert site_evidence(issuer, CompanyFacts(cik="2100001", cik_source=source)) is True
    # a CIK of unknown origin is no evidence, nor is someone else's trusted CIK
    assert site_evidence(issuer, CompanyFacts(cik=MARBLE_CIK)) is False
    assert site_evidence(issuer, CompanyFacts(cik=MARBLE_CIK, cik_source="guessed")) is False
    other = CompanyFacts(cik="0002100002", cik_source="r1")
    assert site_evidence(issuer, other) is False
    assert site_evidence(issuer, CompanyFacts(evidence_ciks=frozenset({MARBLE_CIK}))) is True
    no_cik = _formd_facts("Marble, Inc.")
    assert no_cik.cik is None
    assert site_evidence(no_cik, CompanyFacts(evidence_ciks=frozenset({MARBLE_CIK}))) is False
    # a YC company is never asked for site evidence
    assert guard_failures(issuer, CompanyFacts("Active", "Winter 2024")) == []


def _marble(session: Session, **extra: Any) -> Company:
    """The 2026-10-14 board-first example: an unrelated Paris studio named Marble."""
    company = _company(
        session, "marble.studio", "Marble", hq_city="Paris", source="board_first", **extra
    )
    backfill_aliases(session)
    return company


def _marble_event(session: Session, event_id: str = "ev-marble", **extra: Any) -> EventRow:
    payload: dict[str, Any] = {
        "issuer_name": "Marble, Inc.",
        "issuer_city": "San Francisco",
        "issuer_state": "CA",
        "cik": MARBLE_CIK,
        "previous_names": [],
        "entity_type": "Corporation",
        "jurisdiction_of_inc": "DELAWARE",
        "year_of_inc": 2025,
        "date_filed": "2026-10-01",
        "industry_group_type": "Other Technology",
        **extra,
    }
    case = {"event_id": event_id, "type": "form_d_filed", "payload": payload}
    return add_event(session, case)


def test_marble_exact_name_goes_to_review_not_a_link(db_session: Session) -> None:
    """An exact R2 alias to a company with no YC facts is no longer enough (guard f)."""
    marble = _marble(db_session)
    decision = match_name(
        db_session, "Marble, Inc.", issuer=_formd_facts("Marble, Inc.", cik=MARBLE_CIK)
    )
    assert (decision.rule, decision.action, decision.company_id) == ("R5", "review", None)
    assert decision.guard_failed == [NO_SITE_EVIDENCE]
    assert (decision.demoted_from, decision.proposed_company_id) == ("R2", marble.id)
    # news is matched as before; any other EDGAR filing gets guard (f) alone
    assert match_name(db_session, "Marble, Inc.").rule == "R2"
    s1 = match_name(db_session, "Marble, Inc.", edgar=True)
    assert (s1.rule, s1.guard_failed, s1.demoted_from) == ("R5", [NO_SITE_EVIDENCE], "R2")

    event = _marble_event(db_session)
    stats = resolve_events(db_session, now=NOW)
    assert (stats.auto_linked, stats.review, stats.guard_demoted) == (0, 1, 1)
    assert event.company_id is None
    assert event.payload["match"]["guard_failed"] == [NO_SITE_EVIDENCE]
    (item,) = db_session.scalars(select(ReviewQueueItem)).all()
    assert item.payload["guard_failed"] == [NO_SITE_EVIDENCE]
    assert item.payload["proposed_company_id"] == marble.id
    # nothing was written on the company: no edgar alias, no CIK, no legal name
    db_session.refresh(marble)
    assert (marble.cik, marble.cik_source, marble.legal_name) == (None, None, None)
    edgar = db_session.scalars(select(CompanyAlias).where(CompanyAlias.source == "edgar")).all()
    assert edgar == []


@pytest.mark.parametrize(
    ("status", "tier", "domain", "links"),
    [
        ("linked", "A", "marble.studio", True),
        ("linked", "B", "marble.studio", True),
        ("linked", "C", "marble.studio", False),  # tier C is never evidence
        ("tier_c", "C", "marble.studio", False),
        ("conflict", "A", "marble.studio", False),  # a human decides a conflict
        ("rejected", "A", "marble.studio", False),
        ("linked", "A", "marble.example", False),  # evidence for another domain
        ("linked", "A", None, False),
    ],
)
def test_stored_site_evidence_lets_the_link_through(
    db_session: Session, status: str, tier: str, domain: str | None, links: bool
) -> None:
    marble = _marble(db_session)
    db_session.add(
        IssuerDiscovery(
            cik=MARBLE_CIK,
            status=status,
            tier=tier,
            domain=domain,
            evidence={"path": "/terms", "span": "Marble, Inc."},
        )
    )
    event = _marble_event(db_session)
    stats = resolve_events(db_session, now=NOW)
    db_session.refresh(marble)
    if links:
        assert stats.by_rule == {"R2": 1}
        assert event.company_id == marble.id
        # a guarded R2 is a trusted link: the CIK lands with its source
        assert (marble.cik, marble.cik_source) == (MARBLE_CIK, "guarded_r2")
    else:
        assert stats.by_rule == {"R5": 1}
        assert event.company_id is None
        assert marble.cik is None


@pytest.mark.parametrize(
    ("cik_source", "rule"),
    [("issuer_first_a", "R0"), ("guarded_r2", "R0"), ("manual", "R0"), (None, "R5")],
)
def test_r0_links_a_trusted_cik_and_only_a_trusted_one(
    db_session: Session, cik_source: str | None, rule: str
) -> None:
    marble = _marble(db_session, cik=MARBLE_CIK, cik_source=cik_source)
    # renamed since: the name rules would find nothing, R0 links it by CIK anyway
    renamed = _marble_event(db_session, "ev-renamed", issuer_name="Veined Stone Labs, Inc.")
    first = _marble_event(db_session)
    stats = resolve_events(db_session, now=NOW)
    if rule == "R0":
        assert stats.by_rule == {"R0": 2}
        assert renamed.company_id == first.company_id == marble.id
        audit = renamed.payload["match"]
        assert (audit["rule"], audit["confidence"]) == ("R0", 0.99)
        assert "guard_failed" not in audit
        # a trusted link: the issuer names become edgar aliases, legal_name is filled
        edgar = set(
            db_session.scalars(
                select(CompanyAlias.alias).where(CompanyAlias.company_id == marble.id)
            )
        )
        assert {"marble", "veined stone"} <= edgar
    else:
        # a CIK of unknown origin never fires R0, nor does it pass guard (f)
        assert stats.by_rule == {"R6": 1, "R5": 1}
        assert renamed.company_id is None
        assert first.company_id is None
        assert first.payload["match"]["guard_failed"] == [NO_SITE_EVIDENCE]


def test_r0_ignores_news_and_untrusted_ciks(db_session: Session) -> None:
    marble = _marble(db_session, cik=MARBLE_CIK, cik_source="issuer_first_b")
    index = AliasIndex.from_session(db_session)
    assert index.cik_company("2100001") == marble.id
    assert index.cik_company("0002100002") is None
    assert index.cik_company(None) is None
    assert index.cik_company("not a cik") is None
    # match_name only tries R0 when given a CIK: news never carries one
    assert match_name(db_session, "Veined Stone Labs", index=index).rule == "R6"
    decision = match_name(db_session, "Veined Stone Labs", index=index, cik=MARBLE_CIK)
    assert (decision.rule, decision.company_id) == ("R0", marble.id)


def test_a_cik_written_in_a_run_links_the_next_filing_by_r0(db_session: Session) -> None:
    """A YC company's first guarded R2 writes its CIK; the renamed next filing is R0."""
    acme = _company(
        db_session, "acme.dev", "Acme Robotics", yc_status="Active", yc_batch="Winter 2024"
    )
    backfill_aliases(db_session)
    first = _marble_event(db_session, "ev-1", issuer_name="Acme Robotics, Inc.", year_of_inc=2023)
    later = _marble_event(db_session, "ev-2", issuer_name="Nova Arm Corp", date_filed="2026-10-02")
    later.occurred_at = first.occurred_at.replace(day=2)
    db_session.flush()
    stats = resolve_events(db_session, now=NOW)
    assert stats.by_rule == {"R2": 1, "R0": 1}
    assert first.company_id == later.company_id == acme.id
    db_session.refresh(acme)
    assert (acme.cik, acme.cik_source) == (MARBLE_CIK, "guarded_r2")


def test_a_trusted_link_records_the_source_of_a_known_cik(db_session: Session) -> None:
    """A CIK of unknown origin that a guarded R2 confirms gets ``cik_source``."""
    acme = _company(
        db_session,
        "acme.dev",
        "Acme Robotics",
        yc_status="Active",
        yc_batch="Winter 2024",
        cik="2100001",
    )
    backfill_aliases(db_session)
    event = _marble_event(db_session, issuer_name="Acme Robotics, Inc.", year_of_inc=2023)
    assert resolve_events(db_session, now=NOW).by_rule == {"R2": 1}
    assert event.company_id == acme.id
    db_session.refresh(acme)
    assert (acme.cik, acme.cik_source) == ("2100001", "guarded_r2")


def test_set_cik_on_a_hand_built_index() -> None:
    index = AliasIndex([("acme robotics", 1)])
    index.set_cik(1, "not a cik", "r1")
    assert index.cik_company("not a cik") is None
    index.set_cik(1, "42", None)  # an untrusted CIK never feeds R0
    assert index.cik_company("42") is None
    index.set_cik(1, "42", "r1")
    assert index.cik_company("0000000042") == 1
    assert index.company_facts(1) is None  # built without facts: nothing to update


def _s1_event(
    session: Session, name: str, event_id: str = "ev-s1", cik: str | None = MARBLE_CIK
) -> EventRow:
    """An S-1 (no issuer facts: the Form D guards cannot run on it)."""
    payload: dict[str, Any] = {"company_name": name, "date_filed": "2026-09-01"}
    if cik is not None:
        payload["cik"] = cik
    return add_event(session, {"event_id": event_id, "type": "form_s1_filed", "payload": payload})


def test_filing_guard_failures_is_guard_f_alone() -> None:
    """For a non-Form-D filing only guard (f) runs, on the filer's CIK."""
    assert filing_guard_failures(MARBLE_CIK, None) == []  # no company facts: nothing checked
    assert filing_guard_failures(MARBLE_CIK, CompanyFacts("Active", "Winter 2024")) == []
    assert filing_guard_failures(MARBLE_CIK, CompanyFacts("Inactive")) == []  # (c) is Form D's
    assert filing_guard_failures(MARBLE_CIK, CompanyFacts()) == [NO_SITE_EVIDENCE]
    assert filing_guard_failures(None, CompanyFacts()) == [NO_SITE_EVIDENCE]
    trusted = CompanyFacts(cik="2100001", cik_source="issuer_first_a")
    assert filing_guard_failures("2100001", trusted) == []
    assert filing_guard_failures(MARBLE_CIK, CompanyFacts(cik=MARBLE_CIK)) == [NO_SITE_EVIDENCE]
    evidence = CompanyFacts(evidence_ciks=frozenset({MARBLE_CIK}))
    assert filing_guard_failures(MARBLE_CIK, evidence) == []


def test_s1_exact_name_cannot_hand_a_non_yc_company_a_trusted_cik(db_session: Session) -> None:
    """The verifier's bypass: an S-1 "Marble, Inc." used to link marble.studio by R2 and write
    its CIK as ``guarded_r2``, so the issuer's Form D then linked by R0 past guard (f)."""
    marble = _marble(db_session)
    s1 = _s1_event(db_session, "Marble, Inc.")
    stats = resolve_events(db_session, now=NOW)
    assert (stats.by_rule, stats.guard_demoted) == ({"R5": 1}, 1)
    assert s1.company_id is None
    assert s1.payload["match"]["guard_failed"] == [NO_SITE_EVIDENCE]
    assert s1.payload["match"]["proposed_company_id"] == marble.id
    db_session.refresh(marble)
    assert (marble.cik, marble.cik_source, marble.legal_name) == (None, None, None)
    edgar = db_session.scalars(select(CompanyAlias).where(CompanyAlias.source == "edgar")).all()
    assert edgar == []

    formd = _marble_event(db_session)
    assert resolve_events(db_session, now=NOW).by_rule == {"R5": 1}
    assert formd.company_id is None
    assert formd.payload["match"]["rule"] == "R5"
    assert formd.payload["match"]["guard_failed"] == [NO_SITE_EVIDENCE]

    # an S-1 without a CIK has no evidence either
    no_cik = _s1_event(db_session, "Marble, Inc.", "ev-s1-nocik", cik=None)
    assert resolve_events(db_session, now=NOW).by_rule == {"R5": 1}
    assert no_cik.company_id is None


def test_s1_with_stored_site_evidence_links_but_earns_no_cik_source(db_session: Session) -> None:
    marble = _marble(db_session)
    db_session.add(
        IssuerDiscovery(
            cik=MARBLE_CIK,
            status="linked",
            tier="A",
            domain="marble.studio",
            evidence={"path": "/terms", "span": "Marble, Inc."},
        )
    )
    s1 = _s1_event(db_session, "Marble, Inc.")
    assert resolve_events(db_session, now=NOW).by_rule == {"R2": 1}
    assert s1.company_id == marble.id
    db_session.refresh(marble)
    # no Form D guard ran: the CIK is stored as of unknown origin, which R0 does not trust
    assert (marble.cik, marble.cik_source) == (MARBLE_CIK, None)


def test_an_unguarded_r2_never_writes_guarded_r2(db_session: Session) -> None:
    """A YC company's S-1 R2 writes the CIK with no source; its Form D, passing the guards,
    then records ``guarded_r2`` (the link is R2, not R0)."""
    acme = _company(
        db_session, "acme.dev", "Acme Robotics", yc_status="Active", yc_batch="Winter 2024"
    )
    backfill_aliases(db_session)
    s1 = _s1_event(db_session, "Acme Robotics, Inc.")
    assert resolve_events(db_session, now=NOW).by_rule == {"R2": 1}
    assert s1.company_id == acme.id
    db_session.refresh(acme)
    assert (acme.cik, acme.cik_source, acme.legal_name) == (MARBLE_CIK, None, "Acme Robotics, Inc.")
    assert AliasIndex.from_session(db_session).cik_company(MARBLE_CIK) is None

    formd = _marble_event(db_session, issuer_name="Acme Robotics, Inc.", year_of_inc=2023)
    assert resolve_events(db_session, now=NOW).by_rule == {"R2": 1}
    assert formd.company_id == acme.id
    db_session.refresh(acme)
    assert (acme.cik, acme.cik_source) == (MARBLE_CIK, "guarded_r2")
