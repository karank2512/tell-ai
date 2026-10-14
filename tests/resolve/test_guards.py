"""Form D linking guards and the distinctive-token floor (ADR 0034).

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

from tell.models import Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve.aliases import (
    DESCRIPTOR_TOKENS,
    GENERIC_ALIAS_TOKENS,
    distinctive_tokens,
    shares_distinctive_token,
)
from tell.resolve.guards import (
    GUARD_REASONS,
    R3_REVIEW_ONLY,
    CompanyFacts,
    IssuerFacts,
    batch_year,
    fund_signals,
    guard_failures,
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
    assert guard_failures(corp, CompanyFacts()) == []  # status and batch unknown: pass
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
        db_session, "ncompass.tech", "nCompass Technologies", hq_city="New York City", hq_state="NY"
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
