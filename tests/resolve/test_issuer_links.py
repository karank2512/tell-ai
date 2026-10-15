"""The issuer-first shadow run's hand check, frozen (2026-10-15, ADR 0035).

``issuer_links_20261015.json`` holds the 60 tier A/B companies the shadow run created and a
person checked by hand (``random.Random(60)`` over the 300, sorted by CIK): the issuer's
Form D facts, the domain, the evidence the run stored (``{path, span}``) and the verdict.
The shadow saved no page text, so ``regressions`` carries trimmed page excerpts, fetched
once on 2026-10-15 (robots honoured, 1 request per second), for the three rows the fixes
are about: the two DIFFERENT links (Cado Inc., a "Co., Ltd." trap; Core Automation, Inc.,
an older company with the same legal name) and Left Main REI Inc., a correct site demoted
to tier C by another company's address in its terms. No person, e-mail address, phone
number or street line of the issuer appears in the file. No network, no database.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tell.resolve.evidence import IssuerProfile, SitePage, assess, decide

FIXTURE = Path(__file__).with_name("issuer_links_20261015.json")
DOC: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
SAMPLE: list[dict[str, Any]] = DOC["sample"]
REGRESSIONS: list[dict[str, Any]] = DOC["regressions"]


def _profile(data: dict[str, Any]) -> IssuerProfile:
    return IssuerProfile(
        cik=data["cik"],
        name=data["name"],
        previous_names=tuple(data["previous_names"]),
        city=data["city"],
        state=data["state"],
        year_of_inc=data["year_of_inc"],
        filed_year=data["filed_year"],
    )


def _decide(case: dict[str, Any]) -> dict[str, Any]:
    profile = _profile(case["issuer"])
    decision = decide(
        [
            assess(profile, d["domain"], [SitePage.from_dict(p) for p in d["pages"]])
            for d in case["domains"]
        ]
    )
    evidence = decision.evidence
    return {
        "outcome": decision.outcome,
        "tier": decision.tier,
        "domain": evidence.domain if evidence else None,
        "path": evidence.path if evidence else None,
        "span": evidence.span if evidence else None,
        "domains": list(decision.domains),
    }


def _wilson_lower(successes: int, n: int, z: float = 1.959964) -> float:
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


# --- the frozen hand check ------------------------------------------------------------------


def test_fixture_is_small_and_holds_no_personal_data() -> None:
    assert FIXTURE.stat().st_size < 50_000
    text = FIXTURE.read_text(encoding="utf-8")
    assert "@" not in text  # no e-mail address
    assert re.search(r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}", text) is None  # no phone number
    assert "related" not in text.casefold()  # Form D relatedPersonsList is never read
    for row in SAMPLE:
        assert set(row) == {
            "n",
            "cik",
            "accession",
            "issuer",
            "previous_names",
            "filing",
            "domain",
            "tier",
            "evidence",
            "verdict",
            "reason",
        }
        assert set(row["filing"]) == {
            "city",
            "state",
            "year_of_inc",
            "date_filed",
            "industry",
            "amount_sold",
            "entity_type",
            "jurisdiction",
        }


def test_the_hand_check_is_frozen() -> None:
    assert [row["n"] for row in SAMPLE] == list(range(1, 61))
    assert len({row["cik"] for row in SAMPLE}) == 60
    verdicts = Counter(row["verdict"] for row in SAMPLE)
    assert verdicts == Counter(DOC["verdicts"]) == Counter({"SAME": 58, "DIFFERENT": 2})
    assert Counter(row["tier"] for row in SAMPLE) == Counter({"A": 57, "B": 3})
    assert {row["filing"]["jurisdiction"] for row in SAMPLE} == {"DELAWARE"}
    assert {row["filing"]["entity_type"] for row in SAMPLE} == {"Corporation"}
    different = {row["issuer"]: row["domain"] for row in SAMPLE if row["verdict"] == "DIFFERENT"}
    assert different == {"Cado Inc.": "cado.com", "Core Automation, Inc.": "coreautomation.com"}
    # Precision 58/60 = 0.967, Wilson 95% lower bound 0.886: under the bar of 59 of 60.
    assert round(58 / 60, 3) == 0.967
    assert round(_wilson_lower(58, 60), 3) == 0.886


def test_every_stored_span_is_short_and_from_the_shadow_evidence() -> None:
    for row in SAMPLE:
        evidence = row["evidence"]
        assert set(evidence) == {"path", "span"}
        assert evidence["path"].startswith("/")
        assert len(evidence["span"]) <= len(row["issuer"]) + 2, row["issuer"]


# --- the regressions the fixes close -------------------------------------------------------


@pytest.mark.parametrize("case", REGRESSIONS, ids=[c["id"] for c in REGRESSIONS])
def test_regression_excerpts_decide_as_expected(case: dict[str, Any]) -> None:
    assert _decide(case) == case["expected"]
    # each excerpt reproduces a decision the fix changes (checked against the shadow's code)
    assert case["expected"]["outcome"] != case["shadow"]["outcome"]


def test_the_two_different_links_are_no_longer_tier_a_or_b() -> None:
    cases = {c["id"]: c for c in REGRESSIONS}
    for key in ("cado", "core_automation"):
        case = cases[key]
        assert case["hand_check"] == "DIFFERENT"
        assert case["shadow"]["tier"] == "A"
        got = _decide(case)
        assert got["tier"] not in ("A", "B"), key
        assert got["outcome"] != "linked", key
        row = SAMPLE[case["sample_n"] - 1]
        assert (row["verdict"], row["evidence"]["span"]) == ("DIFFERENT", case["shadow"]["span"])


def test_left_main_rei_is_tier_a_again() -> None:
    [case] = [c for c in REGRESSIONS if c["id"] == "left_main_rei"]
    assert (case["hand_check"], case["shadow"]["tier"]) == ("SAME", "C")
    got = _decide(case)
    assert got["tier"] in ("A", "B")
    assert (got["outcome"], got["span"]) == ("linked", "Left Main REI Inc")
