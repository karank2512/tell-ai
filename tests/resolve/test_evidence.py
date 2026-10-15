"""Site evidence for issuer-first discovery (ADR 0035, ``tell.resolve.evidence``).

``issuer_evidence_20261014.json`` freezes the design's hand check of the issuer-first
sample (2026-10-14): per issuer, the Form D facts and the trimmed page text of every
domain tried (title, description, and only the text blocks a decision rests on; street
lines and phone numbers cut), the hand-check verdict (``SAME`` / ``DIFFERENT`` /
``UNCLEAR``) and the decision the rule gives. Company names only; no person appears in it.
One case (``payload_trap``) is synthetic: the board-first design's "Payload, including"
false hit. No network, no database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tell.resolve.evidence import (
    BLOCK_SEPARATOR,
    IssuerProfile,
    SiteEvidence,
    SitePage,
    assess,
    brand_words,
    core_words,
    decide,
    is_parked,
    legal_base,
    legal_form_families,
    legal_links,
    parse_page,
    sanitize_markup,
    site_addresses,
    site_years,
)

CASES_PATH = Path(__file__).with_name("issuer_evidence_20261014.json")
CASES: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _profile(data: dict[str, Any]) -> IssuerProfile:
    return IssuerProfile(
        cik=data["cik"],
        name=data["name"],
        previous_names=tuple(data["previous_names"]),
        city=data["city"],
        state=data["state"],
    )


def _assessments(case: dict[str, Any]) -> list[SiteEvidence]:
    profile = _profile(case["issuer"])
    return [
        assess(profile, d["domain"], [SitePage.from_dict(p) for p in d["pages"]])
        for d in case["domains"]
    ]


def issuer(
    name: str = "Acme Robotics, Inc.", city: str = "Austin", state: str = "TX", **kw: Any
) -> IssuerProfile:
    return IssuerProfile(cik="0000000001", name=name, city=city, state=state, **kw)


def page(*blocks: str, path: str = "/", title: str = "", description: str = "") -> SitePage:
    return SitePage(path, title=title, description=description, blocks=tuple(blocks))


# --- the frozen hand check ------------------------------------------------------------


def test_fixture_is_small_and_names_no_person() -> None:
    assert CASES_PATH.stat().st_size < 50_000
    assert len(CASES) >= 20
    for case in CASES:
        assert set(case["issuer"]) == {"cik", "name", "previous_names", "city", "state"}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixture_decisions_are_frozen(case: dict[str, Any]) -> None:
    decision = decide(_assessments(case))
    evidence = decision.evidence
    got = {
        "outcome": decision.outcome,
        "tier": decision.tier,
        "domain": evidence.domain if evidence else None,
        "path": evidence.path if evidence else None,
        "span": evidence.span if evidence else None,
        "domains": list(decision.domains),
    }
    assert got == case["expected"]


def test_every_auto_link_in_the_fixture_was_hand_checked_same() -> None:
    linked = [c for c in CASES if c["expected"]["outcome"] == "linked"]
    assert len(linked) == 8
    assert {c["hand_check"] for c in linked} == {"SAME"}
    assert {c["expected"]["tier"] for c in linked} == {"A", "B"}
    different = [c for c in CASES if c["hand_check"] == "DIFFERENT"]
    assert different
    assert all(c["expected"]["outcome"] != "linked" for c in different)


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["expected"]["outcome"] == "linked"], ids=lambda c: c["id"]
)
def test_evidence_record_is_path_and_span_only(case: dict[str, Any]) -> None:
    decision = decide(_assessments(case))
    assert decision.evidence is not None
    record = decision.evidence.record()
    assert record is not None
    assert set(record) == {"path", "span"}
    # The span is the legal name (or brand) as the site writes it, never surrounding text.
    assert len(record["span"]) <= len(case["issuer"]["name"]) + 2
    assert BLOCK_SEPARATOR not in record["span"]


def test_blocks_run_together_lose_the_way_bio_match() -> None:
    """The "FastWay" case: without block separators "Move Fast" + "Way Biosolutions, Inc."
    reads "Move FastWay Biosolutions, Inc." and the legal name no longer matches."""
    [case] = [c for c in CASES if c["id"] == "way_bio"]
    blocks = case["domains"][0]["pages"][0]["blocks"]
    assert assess(_profile(case["issuer"]), "waybio.us", [page(*blocks)]).tier == "A"
    joined = page("".join(blocks))
    assert assess(_profile(case["issuer"]), "waybio.us", [joined]).tier is None


def test_shared_core_word_plus_same_city_is_never_evidence() -> None:
    [case] = [c for c in CASES if c["id"] == "mangrove"]
    for item in _assessments(case):
        assert not item.qualifies


# --- tier A -------------------------------------------------------------------------------


def test_tier_a_needs_a_legal_form_with_a_word_boundary() -> None:
    profile = issuer("Payload, Inc.")
    assert (
        assess(profile, "payload.example", [page("Payload, including its affiliates")]).tier != "A"
    )
    found = assess(profile, "payload.example", [page("Copyright Payload, Inc. 2026")])
    assert (found.tier, found.span, found.brand) == ("A", "Payload, Inc.", "Payload")


@pytest.mark.parametrize(
    "text",
    [
        "write to hello@acme robotics, inc. today",
        "see www.Acme Robotics, Inc. here",
        "path /Acme Robotics, Inc. here",
        "XAcme Robotics, Inc. is not it",
    ],
)
def test_tier_a_name_must_not_follow_a_dot_at_slash_or_letter(text: str) -> None:
    assert assess(issuer(), "acmerobotics.com", [page(text)]).tier != "A"


def test_tier_a_needs_every_token_including_descriptors() -> None:
    profile = issuer("Acme Robotics, Inc.")
    assert assess(profile, "acme.com", [page("Acme, Inc. makes robots")]).tier != "A"
    assert (
        assess(profile, "acme.com", [page("Acme Robotics Incorporated makes robots")]).tier == "A"
    )
    assert assess(profile, "acme.com", [page("© AcmeRobotics, Inc.")]).tier == "A"


def test_tier_a_accepts_a_previous_name() -> None:
    profile = issuer("Acme Robotics, Inc.", previous_names=("Widgetry Labs, Inc.",))
    found = assess(profile, "acme.com", [page("Terms of Widgetry Labs, Inc.")])
    assert (found.tier, found.span) == ("A", "Widgetry Labs, Inc.")


def test_co_needs_its_period() -> None:
    profile = issuer("Saltbox Tinned Fish Co., PBC", city="Tenants Harbor", state="ME")
    assert legal_base(profile.name) == ["Saltbox", "Tinned", "Fish"]
    assert assess(profile, "s.com", [page("Saltbox Tinned Fish Co. 2026")]).tier == "A"
    assert assess(profile, "s.com", [page("Saltbox Tinned Fish co-op")]).tier != "A"


def test_title_only_is_never_evidence() -> None:
    profile = issuer()
    found = assess(profile, "acme.com", [page(title="Acme Robotics, Inc. | Home")])
    assert found.tier == "C"
    assert "title_only" in found.reasons
    described = assess(profile, "acme.com", [page(description="Acme Robotics, Inc. builds")])
    assert described.tier == "C"
    assert "title_only" in described.reasons


def test_an_address_in_another_state_disqualifies_tier_a() -> None:
    found = assess(
        issuer(state="CA", city="San Francisco"),
        "acme.com",
        [page("Acme Robotics, Inc."), page("Austin, TX 78701", path="/terms")],
    )
    assert found.tier == "C"
    assert found.reasons == ("different_state",)
    by_name = assess(
        issuer(state="CA", city="San Francisco"),
        "acme.com",
        [page("Acme Robotics, Inc.", "North Carolina 27606")],
    )
    assert by_name.tier == "C"


def test_a_street_address_without_a_zip_in_another_state_disqualifies_tier_a() -> None:
    sf = issuer(state="CA", city="San Francisco")
    for address in (
        "500 Congress Avenue, Austin, TX",
        "500 Congress Ave, Suite 4, Austin TX",
        "12B Main St., Round Rock, Texas",
    ):
        found = assess(sf, "acme.com", [page("Acme Robotics, Inc.", address)])
        assert (found.tier, found.reasons) == ("C", ("different_state",)), address
    # the same state, a venue or a law, and a street line in another block are no address
    for text in (
        ("Acme Robotics, Inc.", "1 Market Street, San Francisco, CA"),
        ("Acme Robotics, Inc.", "the courts of Austin, TX have jurisdiction"),
        ("Acme Robotics, Inc.", "Code 1798.83 Shine the Light, CA Civil Code"),
        ("Acme Robotics, Inc.", "500 Congress Avenue,", "Austin, TX"),
    ):
        assert assess(sf, "acme.com", [page(*text)]).tier == "A", text


def test_same_state_address_keeps_tier_a() -> None:
    found = assess(issuer(), "acme.com", [page("Acme Robotics, Inc.", "Austin, TX 78701")])
    assert found.tier == "A"


# --- tier B -------------------------------------------------------------------------------


def test_tier_b_full_brand_plus_city() -> None:
    found = assess(issuer(), "acme.com", [page("Acme Robotics", "Austin, TX")])
    assert (found.tier, found.span, found.path) == ("B", "Acme Robotics", "/")
    by_state_name = assess(issuer(), "acme.com", [page("Acme Robotics", "in Austin, Texas")])
    assert by_state_name.tier == "B"


def test_tier_b_city_never_spans_a_block_separator() -> None:
    sf = issuer(state="CA", city="San Francisco")
    assert assess(sf, "acme.com", [page("Acme Robotics", "San Francisco, CA")]).tier == "B"
    split = assess(sf, "acme.com", [page("Acme Robotics", "San", "Francisco, CA")])
    assert split.tier == "C"
    location = sf.location_pattern()
    assert location is not None
    assert location.search("San\nFrancisco, CA") is None


def test_tier_b_state_with_zip_when_the_city_differs() -> None:
    found = assess(
        issuer(city="Round Rock"), "acme.com", [page("Acme Robotics", "Austin, TX 78701")]
    )
    assert found.tier == "B"
    no_zip = assess(issuer(city="Round Rock"), "acme.com", [page("Acme Robotics", "Austin, TX")])
    assert no_zip.tier == "C"


def test_tier_b_needs_the_brand_and_address_on_one_page() -> None:
    found = assess(
        issuer(), "acme.com", [page("Acme Robotics"), page("Austin, TX 78701", path="/contact")]
    )
    assert found.tier == "C"


def test_tier_b_never_for_a_registered_agent_city() -> None:
    profile = issuer("Acme Robotics, Inc.", city="LEWES", state="DE")
    assert profile.agent_address
    found = assess(profile, "acme.com", [page("Acme Robotics", "Lewes, DE 19958")])
    assert found.tier == "C"
    assert "registered_agent_city" in found.reasons
    # Tier A (the legal name) still counts.
    assert assess(profile, "acme.com", [page("Acme Robotics, Inc.")]).tier == "A"


def test_tier_b_state_abbreviation_is_upper_case_only() -> None:
    profile = issuer("Acme Robotics, Inc.", city="Portland", state="OR")
    assert assess(profile, "acme.com", [page("Acme Robotics", "Portland, or Seattle")]).tier == "C"
    assert assess(profile, "acme.com", [page("Acme Robotics", "Portland, OR")]).tier == "B"


def test_foreign_issuer_has_no_tier_b_and_any_us_address_is_another_state() -> None:
    profile = issuer("Acme Robotics, Inc.", city="Toronto", state="A6")
    assert profile.location_pattern() is None
    assert assess(profile, "acme.com", [page("Acme Robotics", "Toronto")]).tier == "C"
    found = assess(profile, "acme.com", [page("Acme Robotics, Inc.", "Austin, TX 78701")])
    assert found.reasons == ("different_state",)


# --- disqualifiers and decisions -----------------------------------------------------------


def test_platform_and_parked_domains_give_nothing() -> None:
    assert assess(issuer(), "acme.vercel.app", [page("Acme Robotics, Inc.")]).reasons == (
        "platform_domain",
    )
    parked = assess(issuer(), "acme.com", [page("Acme Robotics, Inc.", "Coming soon")])
    assert parked.reasons == ("parked",)
    assert assess(issuer(), "acme.com", []).reasons == ("no_pages",)
    assert assess(issuer(), "acme.com", [page("Acme Robotics, Inc.")], platform=False).tier == "A"


def test_is_parked() -> None:
    assert is_parked(page(title="acme.io for sale | Spaceship"))
    assert is_parked(page(title="acme.io"), "acme.io")
    assert is_parked(page())
    assert not is_parked(page(title="Acme"), "acme.io")
    assert not is_parked(page("Acme builds robots", title="acme.io"), "acme.io")
    assert not is_parked(page("for sale " + "x" * 3000))


def test_no_brand_anywhere_is_no_evidence() -> None:
    found = assess(issuer(), "acme.com", [page("Robots for everyone", title="Robots")])
    assert found.tier is None
    assert found.reasons == ("no_brand",)
    assert found.record() is None


def test_decide() -> None:
    a = SiteEvidence("a.com", "A", "/", "Acme, Inc.")
    b = SiteEvidence("b.com", "B", "/", "Acme")
    c = SiteEvidence("c.com", "C", "/", "Acme")
    none = SiteEvidence("d.com", None)
    linked = decide([none, c, a])
    assert (linked.outcome, linked.tier, linked.evidence) == ("linked", "A", a)
    several = decide([a, b, c])
    assert (several.outcome, several.domains, several.tier) == (
        "several_domains",
        ("a.com", "b.com"),
        None,
    )
    assert decide([c, none]).outcome == "tier_c"
    assert decide([c]).tier == "C"
    assert decide([none]).outcome == "no_domain"
    assert decide([]).outcome == "no_domain"
    # One domain assessed twice (redirect duplicates) still counts once.
    assert decide([a, a]).outcome == "linked"


# --- names and pages -----------------------------------------------------------------------


def test_name_helpers() -> None:
    assert legal_base("Way Biosolutions, Inc.") == ["Way", "Biosolutions"]
    assert legal_base("Inc") == ["Inc"]
    assert brand_words("Restara Medical, Inc.") == ["restara", "medical"]
    assert core_words("Restara Medical, Inc.") == ["restara"]
    assert core_words("Medical Inc.") == ["medical"]
    assert core_words("Sparked Innovations.AI, Inc.") == ["sparked", "innovations"]


def test_parse_page_blocks() -> None:
    content = (
        b"<html><head><title> Acme  | Home </title>"
        b"<meta name='description' content='Acme builds robots'>"
        b"<style>p{}</style><script>var x = 'Acme, Inc.';</script></head>"
        b"<body><div>Move Fast</div><div>Way <b>Bio</b>solutions, Inc.<br>Cambridge</div>"
        b"<!-- Acme, Inc. --><noscript>enable js</noscript><span>tail</span> text</body></html>"
    )
    parsed = parse_page(content, "/terms")
    assert parsed.path == "/terms"
    assert parsed.title == "Acme | Home"
    assert parsed.description == "Acme builds robots"
    assert parsed.blocks == ("Move Fast", "Way Biosolutions, Inc.", "Cambridge", "tail text")
    assert parsed.text == BLOCK_SEPARATOR.join(parsed.blocks)


def test_parse_page_og_description_and_empty_content() -> None:
    og = parse_page(
        b"<html><head><meta property='og:description' content='x  y'></head></html>", "/"
    )
    assert og.description == "x y"
    assert parse_page(b"", "/") == SitePage("/")
    assert parse_page(b"   ", "/") == SitePage("/")
    assert parse_page(b"<!-- only a comment -->", "/") == SitePage("/")  # lxml: empty document
    assert parse_page(b"\x00\x01\x02", "/") == SitePage("/")


def test_legal_links() -> None:
    content = (
        b"<a href='/terms'>Terms</a><a href='/terms'>Terms again</a>"
        b"<a href='https://other.com/privacy'>Privacy</a>"
        b"<a href='/legal/privacy-policy'>Privacy</a><a href='/about'>Legal notice</a>"
        b"<a href='mailto:legal@acme.com'>legal</a><a href='#privacy'>x</a>"
        b"<a href='/blog'>Blog</a><a href='https://www.acme.com/'>Privacy home</a>"
        b"<a href='http://[bad'>privacy</a>"
    )
    assert legal_links(content, "https://www.acme.com/", "acme.com") == [
        "/terms",
        "/legal/privacy-policy",
        "/about",
    ]
    assert legal_links(b"", "https://acme.com/", "acme.com") == []


def test_site_page_from_dict_defaults() -> None:
    assert SitePage.from_dict({}) == SitePage("/")


def test_issuer_without_a_city_uses_state_and_zip() -> None:
    profile = IssuerProfile(cik="1", name="Acme Robotics, Inc.", state="TX")
    assert assess(profile, "acme.com", [page("Acme Robotics", "TX 78701")]).tier == "B"
    assert assess(profile, "acme.com", [page("Acme Robotics", "Austin, Texas")]).tier == "C"


def test_brand_with_an_out_of_state_address_is_tier_c() -> None:
    found = assess(issuer(), "acme.com", [page("Acme Robotics", "Austin, TX", "Denver, CO 80202")])
    assert (found.tier, found.reasons) == ("C", ("different_state",))
    both = assess(
        issuer(), "acme.com", [page("Acme Robotics, Inc.", "Austin, TX", "Denver, CO 80202")]
    )
    assert (both.tier, both.reasons) == ("C", ("different_state",))


def test_a_name_without_letters_has_no_patterns() -> None:
    profile = IssuerProfile(cik="1", name="&& --", state="TX")
    assert profile.legal_patterns() == []
    assert profile.brand_pattern() is None
    assert profile.core_pattern() is None
    assert assess(profile, "acme.com", [page("anything")]).reasons == ("no_brand",)


def test_legal_span_is_bounded_by_the_legal_name_and_its_suffix() -> None:
    """Worst case: three separator characters between every token and the longest form."""
    profile = issuer("A B C D E F, Inc.")
    found = assess(profile, "abc.com", [page("x A - B - C - D - E - F, Incorporated. and more")])
    assert found.tier == "A"
    assert found.span == "A - B - C - D - E - F, Incorporated"
    base = legal_base(profile.name)
    bound = len("".join(base)) + 3 * (len(base) - 1) + len(", incorporated.")
    assert found.span is not None
    assert len(found.span) <= bound


# --- legal form families (shadow run 2026-10-15, defect 1) -------------------------------------


@pytest.mark.parametrize(
    ("name", "families"),
    [
        ("Cado Inc.", {"inc"}),
        ("Runwayz Corp.", {"corp"}),
        ("Acme Robotics Corporation", {"corp"}),
        ("Saltbox Tinned Fish Co., PBC", {"co", "pbc"}),
        ("Acme Public Benefit Corporation", {"pbc"}),
        ("QUICKTAKE HEALTH CO", {"co"}),
        ("Acme Robotics Company", {"co"}),
        ("Acme Robotics", set()),
        ("Acme Robotics LLC", set()),
    ],
)
def test_legal_form_families(name: str, families: set[str]) -> None:
    assert legal_form_families(name) == families


def test_edgar_state_tag_is_not_part_of_the_name() -> None:
    assert legal_base("Cicero, Inc /DE/") == ["Cicero"]
    assert legal_base("AiGent, Inc. / DE") == ["AiGent"]
    assert legal_form_families("Primis Inc. /DE") == {"inc"}
    assert legal_base("Acme Public Benefit Corporation") == ["Acme"]
    found = assess(issuer("Cicero, Inc /DE/"), "cicero.ai", [page("© 2026 Cicero, Inc.")])
    assert (found.tier, found.span) == ("A", "Cicero, Inc.")


def test_the_sites_legal_form_must_be_in_the_issuers_family() -> None:
    inc = issuer("Acme Robotics, Inc.")
    other = assess(inc, "acme.com", [page("Acme Robotics Corp. makes robots")])
    assert (other.tier, other.reasons) == ("C", ("other_legal_form",))
    assert assess(inc, "acme.com", [page("Acme Robotics Company")]).tier == "C"
    corp = issuer("Acme Robotics Corp.")
    assert assess(corp, "acme.com", [page("Acme Robotics Corporation")]).tier == "A"
    assert assess(corp, "acme.com", [page("Acme Robotics, Inc.")]).tier == "C"
    pbc = issuer("Acme Robotics, PBC")
    assert assess(pbc, "acme.com", [page("Acme Robotics, PBC")]).tier == "A"
    found = assess(pbc, "acme.com", [page("Acme Robotics Public Benefit Corporation")])
    assert (found.tier, found.brand) == ("A", "Acme Robotics")
    co = issuer("QUICKTAKE HEALTH CO")
    assert assess(co, "q.com", [page("QuickTake Health Co. is")]).tier == "A"
    assert assess(co, "q.com", [page("QuickTake Health Company")]).tier == "A"
    assert assess(co, "q.com", [page("the QuickTake Health company")]).tier != "A"
    assert assess(co, "q.com", [page("QuickTake Health, Inc.")]).tier != "A"
    # Both forms of a two-form name are its own.
    saltbox = issuer("Saltbox Tinned Fish Co., PBC", city="Tenants Harbor", state="ME")
    assert assess(saltbox, "s.com", [page("Saltbox Tinned Fish, PBC")]).tier == "A"
    # A name stating no form accepts any family; one stating only an LLC form gets no tier A.
    assert assess(issuer("Acme Robotics"), "acme.com", [page("Acme Robotics Corp.")]).tier == "A"
    llc = assess(issuer("Acme Robotics LLC"), "acme.com", [page("Acme Robotics, Inc.")])
    assert (llc.tier, llc.reasons) == ("C", ("other_legal_form",))


@pytest.mark.parametrize(
    "text",
    [
        "© 2026 acme robotics Co., Ltd.",
        "Acme Robotics Co.,Ltd. Tokyo",
        "Acme Robotics Co. Limited",
        "Acme Robotics Inc. K.K.",
        "Acme Robotics, Inc. GmbH",
        "Acme Robotics Corp. S.A.",
        "Acme Robotics Corp B.V.",
        "Acme Robotics Inc. Pty",
        "Acme Robotics, Inc. LLC",
    ],
)
def test_a_match_followed_by_another_legal_form_is_not_tier_a(text: str) -> None:
    for name in (
        "Acme Robotics Co.",
        "Acme Robotics, Inc.",
        "Acme Robotics Corp.",
        "Acme Robotics",
    ):
        assert assess(issuer(name), "acme.com", [page(text)]).tier != "A", (name, text)


def test_cado_co_ltd_is_not_cado_inc() -> None:
    """Shadow #5: "cado Co., Ltd." (a Japanese company) gave Cado Inc. tier A."""
    cado = issuer("Cado Inc.", city="Evanston", state="IL")
    found = assess(cado, "cado.com", [page("© 2026 cado Co., Ltd.")])
    assert found.tier == "C"
    assert assess(cado, "cado.com", [page("© 2026 Cado Inc.")]).tier == "A"


def test_a_legal_span_holds_no_token_the_name_lacks() -> None:
    """``&`` and ``+`` sit between legal-name tokens only where the issuer's name has one."""
    plain = issuer("Smith Jones, Inc.")
    assert assess(plain, "sj.com", [page("Smith & Jones, Inc.")]).tier != "A"
    assert assess(plain, "sj.com", [page("Smith + Jones, Inc.")]).tier != "A"
    assert assess(plain, "sj.com", [page("Smith-Jones, Inc.")]).tier == "A"
    joined = issuer("Smith & Jones, Inc.")
    for text in (
        "Smith & Jones, Inc.",
        "Smith&Jones, Inc.",
        "Smith + Jones, Inc.",
        "Smith Jones, Inc.",
    ):
        found = assess(joined, "sj.com", [page(text)])
        assert (found.tier, found.span) == ("A", text), text


# --- age (shadow run 2026-10-15, defect 2) ------------------------------------------------------


def test_site_years() -> None:
    years = site_years(
        [
            page("©2001 - 2022 Core Automation, Inc. All rights reserved"),
            page("Founded in 1998 in Sacramento", "Copyright © 2024 Acme", path="/about"),
        ]
    )
    assert (years.earliest, years.latest_copyright) == (1998, 2024)
    assert site_years([page("© Acme Robotics Inc. 2025", "Est. 2023")]).earliest == 2023
    assert site_years([page("Copyright © 2026 Left Main REI")]).latest_copyright == 2026
    assert site_years([page("(c) 2019\u20132026 Acme")]).latest_copyright == 2026
    # Not a notice: the Copyright Act, a year with no notice, a year after ``until``.
    none = site_years(
        [page("under the Copyright Act of 1976", "since 1999 we", "© 2031 Acme")], until=2027
    )
    assert (none.earliest, none.latest_copyright) == (None, None)
    assert site_years([page("© 2031 Acme")]).latest_copyright == 2031


def test_an_older_company_with_the_same_legal_name_is_tier_c() -> None:
    """Shadow #9: a Sacramento firm (© 2001-2022) passed tier A for a 2026 SF issuer."""
    core = issuer(
        "Core Automation, Inc.", city="SAN FRANCISCO", state="CA", year_of_inc=2026, filed_year=2026
    )
    found = assess(
        core, "coreautomation.com", [page("©2001 - 2022 Core Automation, Inc. All rights reserved")]
    )
    assert (found.tier, found.reasons) == ("C", ("older_site",))
    # Without the years on the page, or without the filing's years, nothing changes.
    assert assess(core, "c.com", [page("Core Automation, Inc.")]).tier == "A"
    bare = issuer("Core Automation, Inc.", city="SAN FRANCISCO", state="CA")
    assert assess(bare, "c.com", [page("©2001 - 2022 Core Automation, Inc.")]).tier == "A"


@pytest.mark.parametrize(
    ("text", "tier", "reasons"),
    [
        ("© 2022-2026 Acme Robotics, Inc.", "A", ()),  # two years before incorporation: allowed
        ("© 2021 Acme Robotics, Inc.", "C", ("older_site",)),
        ("Acme Robotics, Inc. Founded in 2019.", "C", ("older_site",)),
        ("© 2022-2025 Acme Robotics, Inc.", "A", ()),  # latest = filing year - 1: allowed
        ("© 2023-2024 Acme Robotics, Inc.", "C", ("stale_site",)),
        ("© 2024 Acme Robotics, Inc.", "C", ("stale_site",)),
        ("© 2027 Acme Robotics, Inc.", "A", ()),
    ],
)
def test_age_check_bounds(text: str, tier: str, reasons: tuple[str, ...]) -> None:
    profile = issuer(year_of_inc=2024, filed_year=2026)
    found = assess(profile, "acme.com", [page(text)])
    assert (found.tier, found.reasons) == (tier, reasons)


def test_age_check_demotes_tier_b_too() -> None:
    profile = issuer(year_of_inc=2025, filed_year=2026)
    found = assess(profile, "acme.com", [page("Acme Robotics", "Austin, TX", "© 2010 Acme")])
    assert (found.tier, found.reasons) == ("C", ("older_site",))
    stale = issuer(filed_year=2026)
    found = assess(stale, "acme.com", [page("Acme Robotics", "Austin, TX", "© 2020 Acme")])
    assert (found.tier, found.reasons) == ("C", ("stale_site",))


# --- whose address (shadow run 2026-10-15, defect 4) --------------------------------------------


def test_another_entitys_address_is_not_the_sites() -> None:
    """Left Main REI: a vendor's "principal place of business" in another state demoted it."""
    nc = issuer("Left Main REI Inc.", city="CHARLOTTE", state="NC")
    vendor = (
        "Example Cloud, inc. a Delaware corporation having its principal place of business at "
        "Example Tower, 415 Mission Street, 3rd Floor, San Francisco, California, 94105."
    )
    found = assess(
        nc, "leftmainrei.co", [page("Left Main REI Inc, a North Carolina Corporation", vendor)]
    )
    assert (found.tier, found.span) == ("A", "Left Main REI Inc")
    assert site_addresses(nc, [page(vendor)]) == []
    # The issuer's own name before an address keeps it the site's.
    own = page("Left Main REI Inc., 500 Congress Avenue, Austin, TX 78701")
    assert [(a.state, a.city) for a in site_addresses(nc, [own])] == [("TX", "Austin")]
    assert assess(nc, "leftmainrei.co", [own]).reasons == ("different_state",)


def test_privacy_law_boilerplate_is_not_an_address() -> None:
    nc = issuer("Acme Robotics, Inc.", city="Charlotte", state="NC")
    for block in (
        "Under the California Consumer Privacy Act, California residents may write to California 94105",
        "Residents of Nevada may opt out. Nevada 89501",
        "CCPA requests: San Francisco, CA 94105",
        "You may contact the Attorney General, 1300 I Street, Sacramento, CA 95814",
    ):
        assert site_addresses(nc, [page(block)]) == [], block
        assert assess(nc, "acme.com", [page("Acme Robotics, Inc.", block)]).tier == "A", block
    # A law naming one state leaves an address in another state in the same block counted.
    mixed = page("California residents: our office is at 500 Congress Avenue, Austin, TX 78701")
    assert [a.state for a in site_addresses(nc, [mixed])] == ["TX"]
    found = assess(nc, "acme.com", [page("Acme Robotics, Inc.", "California residents")])
    assert found.tier == "A"


def test_a_registered_agent_address_on_the_site_is_not_the_sites() -> None:
    sf = issuer(state="CA", city="San Francisco")
    agent = page("Acme Robotics, Inc.", "c/o Agent, 8 The Green, Dover, DE 19901")
    assert site_addresses(sf, [agent]) == []
    assert assess(sf, "acme.com", [agent]).tier == "A"


def test_a_registered_agent_filing_is_not_compared_with_the_sites_address() -> None:
    """Form D at an agent's city (Lewes, DE) says nothing about where the company is."""
    lewes = issuer(city="LEWES", state="DE")
    assert assess(lewes, "acme.com", [page("Acme Robotics, Inc.", "Austin, TX 78701")]).tier == "A"
    brand = assess(lewes, "acme.com", [page("Acme Robotics", "Lewes, DE 19958")])
    assert (brand.tier, brand.reasons) == ("C", ("registered_agent_city",))


def test_tier_a_needs_another_city_and_another_state() -> None:
    portland = issuer(city="Portland", state="OR")
    same_city = page("Acme Robotics, Inc.", "Portland, ME 04101")
    assert assess(portland, "acme.com", [same_city]).tier == "A"
    assert (
        assess(portland, "acme.com", [page("Acme Robotics, Inc.", "Boston, MA 02110")]).tier == "C"
    )
    # no city before the ZIP: another state is enough
    assert assess(portland, "acme.com", [page("Acme Robotics, Inc.", "ME 04101")]).tier == "C"
    # tier B still needs every address in the Form D state
    brand = assess(
        portland, "acme.com", [page("Acme Robotics", "Portland, OR", "Portland, ME 04101")]
    )
    assert (brand.tier, brand.reasons) == ("C", ("different_state",))


# --- unreadable pages (shadow run 2026-10-15, defect 3) -----------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"<html><body><p>Acme</p><!-- c -->\x01tail\x0b<p>Robotics, Inc.</p></body></html>",
        b"<html><body><p>Acme</p><script>x</script>\x1ftail\x00<p>b</p></body></html>",
    ],
)
def test_control_characters_never_crash_parse_page(content: bytes) -> None:
    parsed = parse_page(content, "/")
    assert parsed.blocks[0] == "Acme"
    assert all("\x01" not in b and "\x1f" not in b and "\x00" not in b for b in parsed.blocks)


def test_sanitize_markup() -> None:
    assert sanitize_markup(b"a\x00b\x01c\td\ne\rf\x7f") == b"abc\td\ne\rf"
    wide = "<p>caf\u00e9</p>".encode("utf-16")
    assert sanitize_markup(wide) == wide
    assert parse_page(wide, "/").blocks == ("caf\u00e9",)


def test_a_page_lxml_cannot_read_is_an_empty_page() -> None:
    """A UTF-16 page keeps its zero bytes; a control character in it still fails lxml."""
    broken = "<html><body><p>a</p><!-- c -->\x01tail</body></html>".encode("utf-16")
    assert parse_page(broken, "/x") == SitePage("/x")
