"""The alias rule (ADR 0033): which normalized strings may stand for a company."""

from __future__ import annotations

import pytest

from tell.resolve.aliases import (
    GENERIC_ALIAS_TOKENS,
    MIN_ALIAS_LENGTH,
    SHORT_ENGLISH_WORDS,
    SHORT_LABEL_LENGTH,
    alias_problem,
    is_single_token,
    label_alias_problem,
)


@pytest.mark.parametrize(
    ("alias", "problem"),
    [
        # the production offenders of 2026-10-13
        ("of", "too_short"),
        ("ai", "too_short"),
        ("ad", "too_short"),
        ("l", "too_short"),
        ("the", "generic"),
        # filler only: function words, fund vocabulary, numbers, single letters
        ("the fund", "generic"),
        ("capital partners ii", "generic"),
        ("fund i a series of", "generic"),
        ("2026", "numeric"),
        ("12 34", "numeric"),
        ("a b c", "generic"),
        ("labs", "generic"),
        # good aliases
        ("ofone", None),
        ("apple", None),
        ("acme", None),
        ("acme robotics", None),
        ("acme capital", None),
        ("e-commerce", None),
        ("a0 dev", None),
        ("3scale", None),
        ("h3x", None),
    ],
)
def test_alias_problem(alias: str, problem: str | None) -> None:
    assert alias_problem(alias) == problem


@pytest.mark.parametrize(
    ("label", "domain", "problem"),
    [
        ("of", "of.one", "too_short"),
        ("ramp", "ramp.com", "short_word"),
        ("mesh", "mesh.com", "short_word"),
        ("apple", "apple.com", "platform_domain"),
        ("google", "google.com", "platform_domain"),
        ("zylo", "zylo.vercel.app", "platform_domain"),
        ("brex", "brex.com", None),
        ("deel", "deel.com", None),
        ("zephyr", "zephyr.io", None),
        ("supermedium", "supermedium.com", None),
    ],
)
def test_label_alias_problem(label: str, domain: str, problem: str | None) -> None:
    assert label_alias_problem(label, domain) == problem


def test_word_lists_are_what_the_rule_says() -> None:
    assert MIN_ALIAS_LENGTH == 3
    assert all(3 <= len(word) <= SHORT_LABEL_LENGTH for word in SHORT_ENGLISH_WORDS)
    assert all(word == word.lower() and word.isalpha() for word in SHORT_ENGLISH_WORDS)
    assert {"the", "of", "and", "ai", "app", "inc", "co", "labs", "hq", "io"} <= (
        GENERIC_ALIAS_TOKENS
    )
    assert {"group", "capital", "partners", "fund", "ventures", "holdings"} <= (
        GENERIC_ALIAS_TOKENS
    )
    assert {"technologies"} <= GENERIC_ALIAS_TOKENS
    assert all(t == t.lower() and " " not in t for t in GENERIC_ALIAS_TOKENS)


def test_is_single_token() -> None:
    assert is_single_token("acme")
    assert is_single_token("acme-labs")
    assert not is_single_token("acme labs")
