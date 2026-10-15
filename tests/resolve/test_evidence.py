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
    legal_links,
    parse_page,
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
    assert assess(profile, "acme.com", [page("Acme Robotics Corp. makes robots")]).tier == "A"
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
