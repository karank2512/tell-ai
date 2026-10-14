"""Alias hygiene maintenance (ADR 0033): ``prune_aliases`` and ``close_junk_reviews``."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.models import Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve import hygiene
from tell.resolve.hygiene import (
    RESOLVED_BY,
    close_junk_reviews,
    failing_aliases,
    prune_aliases,
)
from tell.resolve.match import resolve_events
from tell.resolve.review import apply_verdict

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _company(session: Session, domain: str, name: str, **extra: Any) -> Company:
    company = Company(domain=domain, name=name, **extra)
    session.add(company)
    session.flush()
    return company


def _alias(session: Session, company: Company, alias: str, source: str = "manual") -> None:
    session.add(CompanyAlias(company_id=company.id, alias=alias, source=source))
    session.flush()


def _aliases(session: Session) -> set[tuple[int, str, str]]:
    rows = session.execute(
        select(CompanyAlias.company_id, CompanyAlias.alias, CompanyAlias.source)
    ).all()
    return {(int(c), str(a), str(s)) for c, a, s in rows}


@pytest.fixture
def legacy(db_session: Session) -> dict[str, Company]:
    """Aliases the way ``backfill-aliases`` wrote them before the rule (ADR 0033)."""
    ofone = _company(db_session, "of.one", "OfOne")
    famegame = _company(db_session, "apple.com", "FameGame")
    ramp = _company(db_session, "ramp.com", "Ramp Business")
    acme = _company(db_session, "acme.com", "Acme", hq_state="CA")
    for company, alias in (
        (ofone, "ofone"),
        (ofone, "of"),
        (famegame, "famegame"),
        (famegame, "apple"),
        (ramp, "ramp business"),
        (ramp, "ramp"),
        (acme, "acme"),
        (acme, "the fund"),
    ):
        _alias(db_session, company, alias)
    _alias(db_session, acme, "ai", source="edgar")
    _alias(db_session, acme, "co", source="ats_board")
    return {"ofone": ofone, "famegame": famegame, "ramp": ramp, "acme": acme}


def test_failing_aliases_and_actions(db_session: Session, legacy: dict[str, Company]) -> None:
    found = {(v.alias, v.source): (v.reason, v.action) for v in failing_aliases(db_session)}
    assert found == {
        ("of", "manual"): ("too_short", "delete"),
        ("apple", "manual"): ("platform_domain", "delete"),
        ("ramp", "manual"): ("short_word", "delete"),
        ("the fund", "manual"): ("generic", "delete"),
        ("ai", "edgar"): ("too_short", "keep_source"),
        ("co", "ats_board"): ("too_short", "keep_source"),
    }


def test_prune_aliases_dry_run_then_apply(db_session: Session, legacy: dict[str, Company]) -> None:
    before = _aliases(db_session)
    report = prune_aliases(db_session)
    assert report["dry_run"] is True
    assert (report["failing"], report["to_delete"], report["deleted"]) == (6, 4, 0)
    assert report["kept_other_sources"] == {"ats_board": 1, "edgar": 1}
    assert report["by_reason"] == {
        "generic": 1,
        "platform_domain": 1,
        "short_word": 1,
        "too_short": 1,
    }
    assert {e["alias"] for e in report["examples"]} == {"of", "apple", "ramp", "the fund"}
    assert _aliases(db_session) == before

    applied = prune_aliases(db_session, apply=True)
    assert (applied["dry_run"], applied["deleted"]) == (False, 4)
    left = {alias for _, alias, _ in _aliases(db_session)}
    assert left == {"ofone", "famegame", "ramp business", "acme", "ai", "co"}
    # idempotent
    assert prune_aliases(db_session, apply=True)["deleted"] == 0


def test_prune_aliases_batches_deletes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hygiene, "_DELETE_BATCH", 2)
    company = _company(db_session, "many.example", "Many Names")
    for n in range(5):
        _alias(db_session, company, str(n * 1000))
    assert prune_aliases(db_session, apply=True)["deleted"] == 5
    assert _aliases(db_session) == set()


def _event(session: Session, event_id: str, name: str, state: str = "WA") -> EventRow:
    row = EventRow(
        event_id=event_id,
        source="edgar",
        type="form_d_filed",
        occurred_at=NOW,
        observed_at=NOW,
        magnitude=Decimal(1),
        url=f"https://example.test/{event_id}",
        payload={"issuer_name": name, "issuer_state": state, "previous_names": []},
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row


def _item(session: Session, event_id: str, name: str, candidates: list[dict[str, Any]]) -> int:
    item = ReviewQueueItem(
        kind="entity_match",
        payload={
            "event_id": event_id,
            "name": name,
            "state": "WA",
            "city": None,
            "candidates": candidates,
        },
        created_at=NOW,
    )
    session.add(item)
    session.flush()
    return item.id


def test_prune_keeps_aliases_a_review_wrote(db_session: Session) -> None:
    """A human linked "Ramp" to ramp.com; the alias that verdict wrote survives the prune."""
    company = _company(db_session, "ramp.com", "Ramp Business")
    item_id = _item(db_session, "ev-human", "Ramp", [])
    apply_verdict(db_session, item_id, "link", company_id_or_domain="ramp.com", now=NOW)
    assert (company.id, "ramp", "manual") in _aliases(db_session)
    found = failing_aliases(db_session)
    assert [(v.alias, v.reason, v.action) for v in found] == [
        ("ramp", "short_word", "keep_human_review")
    ]
    # the same label written by backfill (no review) on another company is pruned
    other = _company(db_session, "ramp.io", "Ramp Holdings Group")
    _alias(db_session, other, "ramp")
    report = prune_aliases(db_session, apply=True)
    assert (report["deleted"], report["kept_human_review"]) == (1, 1)
    assert (company.id, "ramp", "manual") in _aliases(db_session)
    assert (other.id, "ramp", "manual") not in _aliases(db_session)


def test_human_review_alias_failing_the_base_rule_is_kept(db_session: Session) -> None:
    company = _company(db_session, "of.one", "OfOne")
    item_id = _item(db_session, "ev-x", "Of", [])
    apply_verdict(db_session, item_id, "link", company_id_or_domain="of.one", now=NOW)
    _alias(db_session, company, "of")  # as if written before the rule
    report = prune_aliases(db_session, apply=True)
    assert (report["failing"], report["kept_human_review"], report["deleted"]) == (1, 1, 0)


def _candidate(company: Company, alias: str, ratio: float = 100.0) -> dict[str, Any]:
    return {"company_id": company.id, "alias": alias, "ratio": ratio, "sort_ratio": 20.0}


def test_close_junk_reviews(db_session: Session, legacy: dict[str, Company]) -> None:
    ofone, famegame, acme = legacy["ofone"], legacy["famegame"], legacy["acme"]
    robotics = _company(db_session, "acmerobotics.com", "Acme Robotics", hq_state="NY")
    _alias(db_session, robotics, "acme robotics")
    junk_of = _item(
        db_session,
        "ev-1",
        "Arboretum Fund I, a series of CGF2026 LLC",
        [_candidate(ofone, "of")],
    )
    junk_apple = _item(
        db_session,
        "ev-2",
        "Arboretum Apple a Series of CGF2026 LLC",
        [_candidate(famegame, "apple"), _candidate(ofone, "of")],
    )
    junk_acme = _item(db_session, "ev-3", "Blue Acme Bakery", [_candidate(acme, "acme")])
    real = _item(
        db_session,
        "ev-4",
        "Acme Robotics Group",
        [_candidate(robotics, "acme robotics"), _candidate(acme, "acme")],
    )
    _item(db_session, "ev-5", "", [])  # no name: kept open

    dry = close_junk_reviews(db_session, now=NOW)
    assert dry["dry_run"] is True
    assert (dry["open_scanned"], dry["junk"], dry["closed"]) == (5, 3, 0)
    assert dry["kept_open_by_rule"] == {"R5": 1, "no_name": 1}
    assert dry["junk_aliases"] == {"of": 2, "apple": 1, "acme": 1}
    assert dry["junk_alias_reasons"] == {"single_token_superset": 2, "too_short": 2}
    assert [e["id"] for e in dry["examples"]] == [junk_of, junk_apple, junk_acme]
    statuses = db_session.scalars(select(ReviewQueueItem.status)).all()
    assert statuses == ["open"] * 5

    applied = close_junk_reviews(db_session, now=NOW, apply=True)
    assert (applied["junk"], applied["closed"]) == (3, 3)
    for item_id in (junk_of, junk_apple, junk_acme):
        item = db_session.get(ReviewQueueItem, item_id)
        assert item is not None
        assert (item.status, item.resolved_at) == ("rejected", NOW)
        assert item.payload["resolved_by"] == RESOLVED_BY
        assert item.payload["verdict"] == "reject"
        assert item.payload["candidates"]  # the audit trail is kept
    still = db_session.get(ReviewQueueItem, real)
    assert still is not None
    assert still.status == "open"
    # idempotent: nothing left to close
    assert close_junk_reviews(db_session, now=NOW, apply=True)["closed"] == 0


def test_closed_junk_items_do_not_come_back(db_session: Session) -> None:
    """After closing, a ``resolve run --retry`` leaves the event unlinked and opens nothing."""
    ofone = _company(db_session, "of.one", "OfOne")
    _event(db_session, "ev-of", "Arboretum Fund I, a series of CGF2026 LLC")
    _item(
        db_session, "ev-of", "Arboretum Fund I, a series of CGF2026 LLC", [_candidate(ofone, "of")]
    )
    assert close_junk_reviews(db_session, now=NOW, apply=True)["closed"] == 1
    stats = resolve_events(db_session, now=NOW, retry=True)
    assert stats.by_rule == {"R6": 1}
    open_items = db_session.scalars(
        select(ReviewQueueItem).where(ReviewQueueItem.status == "open")
    ).all()
    assert open_items == []


def test_reports_ignore_malformed_candidates(db_session: Session) -> None:
    _item(db_session, "ev-m", "Nothing Like It LLC", ["junk", {"alias": 5}, {"alias": "x"}])
    report = close_junk_reviews(db_session, now=NOW)
    assert (report["junk"], report["junk_aliases"]) == (1, {})
    review = ReviewQueueItem(
        kind="entity_match",
        status="resolved",
        payload={"name": 7, "company_id": "nope"},
        created_at=NOW,
    )
    db_session.add(review)
    db_session.flush()
    assert prune_aliases(db_session)["failing"] == 0
