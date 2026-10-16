"""Enrichment gate v0 (ADR 0038): the pure rules, the ledger and ``judge``."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.models import EnrichmentVerdict
from tell.publish.nudge import pending_nudges
from tell.signals.gate import (
    DEFAULT_MONTHLY_CREDITS,
    LEDGER_WINDOW,
    Verdict,
    credits_used_this_month,
    decide,
    enriched_recently,
    families_with_receipts,
    judge,
    record_verdict,
)
from tests.publish import scenario as sc


def _c(family: str, url: str | None = "https://receipt.example/x") -> dict[str, Any]:
    item: dict[str, Any] = {"signal_type": family, "contribution": 1.0, "summary": family}
    if url:
        item["url"] = url
    return item


TWO = [_c("funding"), _c("hiring_velocity")]
ONE = [_c("funding"), _c("funding")]


# --- pure rules ----------------------------------------------------------------------------------


def test_families_with_receipts_are_distinct_ordered_and_need_a_url() -> None:
    assert families_with_receipts(TWO) == ["funding", "hiring_velocity"]
    assert families_with_receipts(ONE) == ["funding"]
    assert families_with_receipts([_c("launch", None), _c("funding")]) == ["funding"]
    assert families_with_receipts([{"signal_type": "", "url": "https://x"}]) == []


def test_every_rule_passing_is_enrich() -> None:
    verdict = decide(TWO, coverage_note="greenhouse:acme")
    assert verdict == Verdict(
        verdict="enrich",
        reasons=["two_families", "coverage_ok", "crm_unchecked", "no_recent_credit", "budget_ok"],
        families=["funding", "hiring_velocity"],
    )
    assert verdict.line == "Worth enriching: yes · funding + hiring_velocity"


def test_one_family_is_skip_and_every_rule_is_still_recorded() -> None:
    verdict = decide(ONE, coverage_note="dead:lever:acme", credits_used=100)
    assert verdict.verdict == "skip"
    assert verdict.reasons == [
        "one_family",
        "thin_coverage:dead:lever:acme",
        "crm_unchecked",
        "no_recent_credit",
        "budget_spent",
    ]
    assert verdict.line == "Worth enriching: skip · one signal family"


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("dead:lever:acme", "hold"),
        ("unsupported:workday", "hold"),
        ("no_board", "enrich"),  # a seed company without a public board is normal
        ("greenhouse:acme", "enrich"),
        (None, "enrich"),
        ("", "enrich"),
    ],
)
def test_thin_coverage_is_a_hold_but_no_board_is_not(note: str | None, expected: str) -> None:
    verdict = decide(TWO, coverage_note=note)
    assert verdict.verdict == expected
    if expected == "hold":
        assert verdict.line == f"Worth enriching: hold · board unreadable ({note})"


def test_recent_credit_is_a_skip_that_beats_a_hold() -> None:
    verdict = decide(TWO, coverage_note="dead:lever:acme", enriched_recently=True)
    assert verdict.verdict == "skip"
    assert verdict.line == (
        "Worth enriching: skip · a credit went to this company in the last 90 days"
    )


def test_budget_spent_is_a_hold() -> None:
    assert decide(TWO, credits_used=DEFAULT_MONTHLY_CREDITS - 1).verdict == "enrich"
    spent = decide(TWO, credits_used=DEFAULT_MONTHLY_CREDITS)
    assert spent.verdict == "hold"
    assert spent.line == "Worth enriching: hold · this month's credit budget is spent"
    assert decide(TWO, credits_used=0, credit_budget=0).verdict == "hold"
    assert decide(TWO, credits_used=2, credit_budget=3).verdict == "enrich"


def test_line_without_a_known_reason_is_still_a_sentence() -> None:
    assert Verdict(verdict="hold", reasons=["mystery"]).line == (
        "Worth enriching: hold · no reason recorded"
    )


# --- the ledger ----------------------------------------------------------------------------------


@pytest.mark.db
def test_judge_records_once_reads_back_and_applies_the_history_rules(
    db_session: Session,
) -> None:
    with freeze_time(sc.NOW):
        scenario = sc.build(db_session)
    watchlist = scenario.private
    robotics, bluefin = pending_nudges(db_session, watchlist, sc.NOW)
    assert (robotics.domain, bluefin.domain) == (sc.ROBOTICS, sc.BLUEFIN)

    first = judge(
        db_session,
        watchlist,
        event_id=robotics.event_id,
        company_id=robotics.company_id,
        contributions=TWO,
        coverage_note="no_board",
        now=sc.NOW,
    )
    assert first.verdict == "enrich"
    rows = list(db_session.scalars(select(EnrichmentVerdict)))
    assert [(r.event_id, r.watchlist_id, r.company_id, r.verdict) for r in rows] == [
        (robotics.event_id, watchlist.id, robotics.company_id, "enrich")
    ]
    assert rows[0].reasons == first.reasons
    assert rows[0].families == ["funding", "hiring_velocity"]
    assert rows[0].decided_at == sc.NOW

    # The same crossing again (a resend): the stored verdict, whatever the facts now say.
    again = judge(
        db_session,
        watchlist,
        event_id=robotics.event_id,
        company_id=robotics.company_id,
        contributions=ONE,
        coverage_note="dead:lever:x",
        now=sc.NOW + timedelta(hours=6),
        credit_budget=0,
    )
    assert again == first
    assert (
        db_session.scalar(select(EnrichmentVerdict.id).where(EnrichmentVerdict.id > 0)) is not None
    )
    assert len(list(db_session.scalars(select(EnrichmentVerdict)))) == 1

    # Rule 4: a later crossing of the same company inside 90 days is a skip; after, not.
    assert enriched_recently(
        db_session,
        watchlist_id=watchlist.id,
        company_id=robotics.company_id,
        now=sc.NOW + timedelta(days=10),
    )
    assert not enriched_recently(
        db_session,
        watchlist_id=watchlist.id,
        company_id=robotics.company_id,
        now=sc.NOW + timedelta(days=10),
        exclude_event_id=robotics.event_id,
    )
    assert not enriched_recently(
        db_session,
        watchlist_id=watchlist.id,
        company_id=robotics.company_id,
        now=sc.NOW + LEDGER_WINDOW + timedelta(seconds=1),
    )
    # Another watchlist's spend is invisible (per watchlist, never across customers).
    assert not enriched_recently(
        db_session,
        watchlist_id=scenario.public.id,
        company_id=robotics.company_id,
        now=sc.NOW + timedelta(days=10),
    )
    later = judge(
        db_session,
        watchlist,
        event_id=bluefin.event_id,  # a different crossing, same company by construction
        company_id=robotics.company_id,
        contributions=TWO,
        coverage_note="no_board",
        now=sc.NOW + timedelta(days=10),
    )
    assert later.verdict == "skip"
    assert "enriched_recently" in later.reasons

    # Rule 5: the month's enrich verdicts count against the budget; a hold does not.
    assert credits_used_this_month(db_session, watchlist_id=watchlist.id, now=sc.NOW) == 1
    assert credits_used_this_month(db_session, watchlist_id=scenario.public.id, now=sc.NOW) == 0
    next_month = sc.NOW.replace(month=11, day=1)
    assert credits_used_this_month(db_session, watchlist_id=watchlist.id, now=next_month) == 0


@pytest.mark.db
def test_judge_holds_when_the_budget_is_spent(db_session: Session) -> None:
    with freeze_time(sc.NOW):
        scenario = sc.build(db_session)
    watchlist = scenario.private
    robotics, bluefin = pending_nudges(db_session, watchlist, sc.NOW)
    kwargs: dict[str, Any] = {"contributions": TWO, "coverage_note": "no_board", "now": sc.NOW}
    first = judge(
        db_session,
        watchlist,
        event_id=robotics.event_id,
        company_id=robotics.company_id,
        credit_budget=1,
        **kwargs,
    )
    second = judge(
        db_session,
        watchlist,
        event_id=bluefin.event_id,
        company_id=bluefin.company_id,
        credit_budget=1,
        **kwargs,
    )
    assert (first.verdict, second.verdict) == ("enrich", "hold")
    assert second.reasons[-1] == "budget_spent"
    assert second.line == "Worth enriching: hold · this month's credit budget is spent"


@pytest.mark.db
def test_record_verdict_is_idempotent_per_event_and_watchlist(db_session: Session) -> None:
    with freeze_time(sc.NOW):
        scenario = sc.build(db_session)
    (robotics, _) = pending_nudges(db_session, scenario.private, sc.NOW)
    verdict = decide(TWO)
    row = record_verdict(
        db_session,
        event_id=robotics.event_id,
        watchlist_id=scenario.private.id,
        company_id=robotics.company_id,
        verdict=verdict,
        now=sc.NOW,
    )
    same = record_verdict(
        db_session,
        event_id=robotics.event_id,
        watchlist_id=scenario.private.id,
        company_id=robotics.company_id,
        verdict=decide(ONE),
        now=sc.NOW + timedelta(days=1),
    )
    assert same is row
    assert same.verdict == "enrich"
    other = record_verdict(
        db_session,
        event_id=robotics.event_id,
        watchlist_id=scenario.public.id,
        company_id=robotics.company_id,
        verdict=decide(ONE),
        now=sc.NOW,
    )
    assert other is not row
    assert other.verdict == "skip"
