"""Name normalization (spec section 6 steps 1-5): table-driven."""

from __future__ import annotations

import pytest

from tell.resolve import normalize
from tell.resolve.normalize import domain_label, normalize_name, normalize_name_alt, slug_words

NORMALIZE_CASES: list[tuple[str, str]] = [
    # step 1: NFKC, casefold, accents
    ("ACME", "acme"),
    ("\uff21\uff43\uff4d\uff45", "acme"),  # fullwidth ACME
    ("Société Générale S.A.", "societe generale"),
    ("Ümlaut GmbH", "umlaut"),
    ("Café Ñandú PBC", "cafe nandu"),
    ("Straße Co", "strasse"),
    # step 2: legal suffixes at the end only
    ("Acme Inc", "acme"),
    ("Acme, Inc.", "acme"),
    ("Acme Incorporated", "acme"),
    ("Acme LLC", "acme"),
    ("Acme L.L.C.", "acme"),
    ("Acme Ltd", "acme"),
    ("Acme Limited", "acme"),
    ("Acme Corp", "acme"),
    ("Acme Corporation", "acme"),
    ("Acme Co", "acme"),
    ("Acme Company", "acme"),
    ("Acme plc", "acme"),
    ("Acme GmbH", "acme"),
    ("Acme S.A.", "acme"),
    ("Acme PBC", "acme"),
    ("Acme Holdings", "acme"),
    ("Acme Holdings, Inc.", "acme"),
    ("Inc Acme", "inc acme"),
    ("Company Store", "company store"),
    ("Corporation", "corporation"),
    # the labs rule
    ("Labs", "labs"),
    ("Acme Labs", "acme"),
    ("Acme Labs, Inc.", "acme"),
    ("Labs Inc", "labs"),
    # step 3: punctuation, whitespace, leading "the"
    ("The Acme Co.", "acme"),
    ("The  Acme   Company", "acme"),
    ("the", "the"),
    ("Acme (Delaware) Corporation", "acme delaware"),
    ("O'Reilly Media, Inc.", "oreilly media"),
    ("AT&T Inc.", "at t"),
    ("E-Commerce Co - Ltd", "e-commerce"),
    ("-Acme- Inc", "acme"),
    ("Acme.io", "acme io"),
    ("acme/beta", "acme beta"),
    ("  Acme  ", "acme"),
    ("", ""),
    ("...", ""),
    # digits and tech tokens are untouched in the plain form
    ("3Scale", "3scale"),
    ("Acme Tech", "acme tech"),
    ("Acme Technologies", "acme technologies"),
    ("Acme AI", "acme ai"),
]


@pytest.mark.parametrize(
    ("raw", "expected"), NORMALIZE_CASES, ids=[c[0] or "<empty>" for c in NORMALIZE_CASES]
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_table_has_at_least_forty_cases() -> None:
    assert len(NORMALIZE_CASES) >= 40


ALT_CASES: list[tuple[str, str]] = [
    ("3Scale", "threescale"),
    ("Acme Tech", "acme technologies"),
    ("Acme Technologies", "acme technologies"),
    ("Acme Technology Inc", "acme technologies"),
    ("Acme AI", "acme ai"),
    ("Studio 54", "studio fivefour"),
    ("Area 1 Security", "area one security"),
    ("Acme 0 Labs", "acme zero"),
    ("Tech", "technologies"),
    ("", ""),
]


@pytest.mark.parametrize(("raw", "expected"), ALT_CASES, ids=[c[0] or "<empty>" for c in ALT_CASES])
def test_normalize_name_alt(raw: str, expected: str) -> None:
    assert normalize_name_alt(raw) == expected


def test_alt_form_spells_every_digit() -> None:
    assert normalize_name_alt("x0123456789") == "xzeroonetwothreefourfivesixseveneightnine"
    assert set(normalize.DIGIT_WORDS) == set("0123456789")


DOMAIN_CASES: list[tuple[str, str]] = [
    ("acme.com", "acme"),
    ("www.acme.com", "acme"),
    ("https://www.Acme-Labs.co.uk/careers", "acme-labs"),
    ("jobs.acme.io", "acme"),
    ("3scale.net", "3scale"),
    ("acme.vercel.app", "acme"),
]


@pytest.mark.parametrize(("raw", "expected"), DOMAIN_CASES, ids=[c[0] for c in DOMAIN_CASES])
def test_domain_label(raw: str, expected: str) -> None:
    assert domain_label(raw) == expected


def test_slug_words() -> None:
    assert slug_words("acme-labs") == "acme labs"
    assert slug_words("acme_labs") == "acme labs"
    assert slug_words("acme.labs") == "acme labs"
    assert normalize_name(slug_words("acme-labs-inc")) == "acme"
    assert normalize_name(slug_words("acme")) == "acme"


def test_normalization_is_idempotent() -> None:
    for raw, _ in NORMALIZE_CASES:
        once = normalize_name(raw)
        assert normalize_name(once) == once
        alt = normalize_name_alt(raw)
        assert normalize_name_alt(alt) == alt


def test_legal_suffix_list_matches_spec() -> None:
    spec = {
        "inc",
        "incorporated",
        "llc",
        "l.l.c",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "gmbh",
        "s.a",
        "pbc",
        "holdings",
    }
    assert frozenset(spec) == normalize.LEGAL_SUFFIXES
    assert frozenset({"labs"}) == normalize.CONDITIONAL_SUFFIXES
