"""Taxonomy edge cases beyond the 60 labelled titles in ``tests/test_velocity.py``."""

from __future__ import annotations

import re

import pytest

from tell.signals import taxonomy
from tell.signals.taxonomy import (
    DEPARTMENT_HINTS,
    FAMILIES,
    FAMILY_KEYWORDS,
    KEYWORD_VARIANTS,
    OTHER,
    fingerprint,
    is_non_english,
    normalize_department,
    normalize_location,
    normalize_title,
    role_family,
)


def test_table_matches_the_spec_shape() -> None:
    assert len(FAMILIES) == 10
    assert FAMILIES[-1] == "exec"
    assert FAMILY_KEYWORDS["sales"][-1] == "revenue"
    assert "sales engineer" in FAMILY_KEYWORDS["sales"]
    assert "solutions engineer" in FAMILY_KEYWORDS["sales"]
    every_keyword = {k for keywords in FAMILY_KEYWORDS.values() for k in keywords}
    assert set(KEYWORD_VARIANTS.values()) <= every_keyword
    assert {family for _, family in DEPARTMENT_HINTS} <= set(FAMILIES)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Aerospace Engineer", OTHER),  # "ae" only as a word
        ("AEs, Enterprise", "sales"),  # plural suffix
        ("Mobile Biologist", "engineering"),  # "bi" never inside a word
        ("Head of Sales Engineering", "sales"),  # sales engineer + ing
        ("Software Engineering Manager", "engineering"),
        ("Revenue Accountant", "sales"),  # spec-literal: revenue is sales
        ("Revenue Operations Lead", "gtm_ops"),  # revenue masked by revenue operations
        ("Marketing Operations Manager", "gtm_ops"),
        ("Back End Developer", "engineering"),  # spelling variant
        ("Site Reliability Engineer", "engineering"),
        ("SVP, Partnerships", "exec"),
        ("Office Coordinator", OTHER),
        ("", OTHER),
        ("12345", OTHER),
        ("Développeur Full-Stack", "engineering"),  # accented Latin is still matched
    ],
)
def test_title_edge_cases(title: str, expected: str) -> None:
    assert role_family(title) == expected


def test_department_hint_is_only_a_tie_breaker() -> None:
    assert role_family("Associate", "Marketing") == "marketing"  # shared hint -> broader
    assert role_family("Associate", "Data") == "data"
    assert role_family("Associate", "Research") == "ml_ai"
    assert role_family("Associate", "Product") == "product_design"
    assert role_family("Associate", "Operations") == "gtm_ops"
    assert role_family("Associate", "G&A") == "finance_ops"
    assert role_family("Associate", "Support") == "customer"
    assert role_family("Associate", "Engineering") == "engineering"
    assert role_family("Associate", "Facilities") == OTHER
    assert role_family("Associate", None) == OTHER
    assert role_family("Director", "Sales") == "exec"  # a title keyword wins over a hint
    assert role_family("Data Engineer", "Sales") == "data"


def test_non_english_detection() -> None:
    assert is_non_english("営業マネージャー")
    assert is_non_english("مدير مبيعات")
    assert is_non_english("Менеджер по продажам (B2B)")  # a few Latin letters, mostly not
    assert is_non_english("---")
    assert not is_non_english("Développeur")
    assert not is_non_english("Account Executive")
    assert role_family("Менеджер AE", "Sales") == OTHER


def test_normalize_title() -> None:
    assert normalize_title("Sr. Staff Software Engineer II") == "software engineer"
    assert normalize_title("Account Executive (L5)") == "account executive"
    assert normalize_title("Software Engineer, IC3 / E4") == "software engineer"
    assert normalize_title("Engineer 2") == "engineer"
    assert normalize_title("Principal  Lead   PM, Growth!") == "pm growth"
    once = normalize_title("Senior Account Executive IV")
    assert normalize_title(once) == once


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (None, ""),
        ("", ""),
        ("New York, NY", "new york, us"),
        ("New York, NY, United States", "new york, us"),
        ("San Francisco, CA 94105", "san francisco, us"),
        ("Portland, OR", "portland, us"),
        ("Toronto, ON", "toronto, ca"),
        ("Toronto, Ontario, Canada", "toronto, ca"),
        ("London, England, United Kingdom", "london, gb"),
        ("London, UK", "london, gb"),
        ("Berlin, Germany (Hybrid)", "berlin, de"),
        ("Paris, Île-de-France, France", "paris, fr"),
        ("Remote - US", "remote, us"),
        ("US (Remote)", "remote, us"),
        ("Remote, U.S.", "remote, us"),
        ("Remote", "remote"),
        ("Anywhere", "remote"),
        ("Remote - EMEA", "remote, emea"),
        ("New York or Remote", "new york"),
        ("Austin, TX; Remote", "austin, us"),
        ("London", "london"),
        ("United States", "us"),
        ("Hybrid", ""),
        ("1 Main St, Springfield, IL, USA", "main st, us"),
        ("Lisbon, Portugal", "lisbon, pt"),
        ("Zurich, Switzerland", "zurich, ch"),
        ("Nairobi, Kenya", "nairobi, kenya"),
    ],
)
def test_normalize_location(location: str | None, expected: str) -> None:
    assert normalize_location(location) == expected


def test_normalize_department() -> None:
    assert normalize_department(None) == ""
    assert normalize_department("  Sales & Marketing ") == "sales & marketing"
    assert normalize_department("R&D / Platform") == "r&d platform"


def test_fingerprint_shape_and_renormalization() -> None:
    fp = fingerprint("account executive", "Sales", "New York, NY", False)
    assert re.fullmatch(r"[0-9a-f]{40}", fp)
    # A raw title is normalized again, so callers cannot split a requisition by accident.
    assert fingerprint("Senior Account Executive", "Sales", "New York, NY", False) == fp
    assert fingerprint("account executive", "Sales", "New York, NY", None) != fp
    assert fingerprint("account executive", None, "New York, NY", False) != fp
    assert fingerprint("account executive", "Sales", None, False) != fp


def test_module_exports() -> None:
    assert set(taxonomy.__all__) >= {"FAMILIES", "TARGET_FAMILIES_DEFAULT", "is_intern"}
