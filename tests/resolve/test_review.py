"""Review workflow: CSV round trip for link / create / reject, verdict rules, password gate."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.models import Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve import review, review_app
from tell.resolve.match import backfill_aliases, resolve_events
from tell.resolve.review import (
    CSV_COLUMNS,
    DISCOVERY_COLUMNS,
    DISCOVERY_KEY,
    ReviewError,
    apply_verdict,
    check_password,
    discovery_details,
    export_review_csv,
    import_review_csv,
    open_items,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _company(session: Session, domain: str, name: str, **extra: Any) -> Company:
    extra.setdefault("yc_status", "Active")  # a YC directory company (guard f is off)
    company = Company(domain=domain, name=name, **extra)
    session.add(company)
    session.flush()
    return company


def _formd_event(
    session: Session, event_id: str, name: str, state: str = "WA", cik: str = "0000012345"
) -> EventRow:
    row = EventRow(
        event_id=event_id,
        source="edgar",
        type="form_d_filed",
        occurred_at=NOW,
        observed_at=NOW,
        magnitude=Decimal(1),
        url=f"https://example.test/{event_id}",
        payload={
            "issuer_name": name,
            "issuer_state": state,
            "issuer_city": "Seattle",
            "cik": cik,
            "previous_names": [],
            "date_filed": "2026-10-05",
            "entity_type": "Corporation",
            "jurisdiction_of_inc": "DELAWARE",
            "year_of_inc": 2024,
        },
        run_id="test-run",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def queue(db_session: Session) -> dict[str, Any]:
    """Two companies, three ambiguous Form D events, resolved once -> three open items."""
    acme = _company(db_session, "acme.com", "Acme", hq_state="CA")
    robotics = _company(db_session, "acmerobotics.com", "Acme Robotics", hq_state="NY")
    backfill_aliases(db_session)
    events = [
        _formd_event(db_session, "ev-link", "Acme Robotics Group"),
        _formd_event(db_session, "ev-create", "Acme Robotics Ventures"),
        _formd_event(db_session, "ev-reject", "Acme Robotics Partners"),
    ]
    stats = resolve_events(db_session, now=NOW)
    assert stats.review == 3
    items = {item.payload["event_id"]: item for item in open_items(db_session)}
    return {"acme": acme, "robotics": robotics, "events": events, "items": items}


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _aliases(session: Session, company_id: int) -> dict[str, str]:
    rows = session.execute(
        select(CompanyAlias.alias, CompanyAlias.source).where(CompanyAlias.company_id == company_id)
    )
    return dict(rows.all())


def test_export_writes_open_items_with_candidates(
    db_session: Session, queue: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "review.csv"
    assert export_review_csv(db_session, path) == 3
    rows = _rows(path)
    assert list(rows[0]) == list(CSV_COLUMNS)
    by_event = {row["event_id"]: row for row in rows}
    row = by_event["ev-link"]
    assert row["kind"] == "entity_match"
    assert row["name"] == "Acme Robotics Group"
    assert (row["state"], row["city"]) == ("WA", "Seattle")
    # "acme" is a single-token alias and never scores against a longer name (ADR 0033)
    assert row["candidate_1_id"] == str(queue["robotics"].id)
    assert str(queue["acme"].id) not in {row["candidate_2_id"], row["candidate_3_id"]}
    assert row["candidate_1_ratio"] == "100.0"
    assert row["candidate_3_id"] == ""
    assert (row["verdict"], row["company_id_or_domain"], row["new_company_name"]) == ("", "", "")


def test_export_of_empty_queue_writes_header_only(db_session: Session, tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    assert export_review_csv(db_session, path) == 0
    assert path.read_text(encoding="utf-8").strip() == ",".join(CSV_COLUMNS)


def test_csv_round_trip_link_create_reject(
    db_session: Session, queue: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "review.csv"
    export_review_csv(db_session, path)
    rows = _rows(path)
    for row in rows:
        if row["event_id"] == "ev-link":
            row["verdict"] = "link"
            row["company_id_or_domain"] = str(queue["robotics"].id)
        elif row["event_id"] == "ev-create":
            row["verdict"] = "create"
            row["company_id_or_domain"] = "https://www.acme-ventures.com/about"
            row["new_company_name"] = "Acme Ventures"
        else:
            row["verdict"] = "reject"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    stats = import_review_csv(db_session, path, now=NOW)
    assert stats.to_dict() == {
        "rows": 3,
        "linked": 1,
        "created": 1,
        "rejected": 1,
        "skipped": 0,
        "errors": [],
    }
    assert open_items(db_session) == []

    linked = db_session.get(EventRow, "ev-link")
    assert linked is not None
    assert linked.company_id == queue["robotics"].id
    assert linked.payload["match"]["rule"] == "manual"
    assert linked.payload["match"]["confidence"] == 1.0
    assert linked.payload["match"]["review_id"] == queue["items"]["ev-link"].id
    assert linked.payload["match"]["candidates"]  # the R5 audit trail is kept
    # the same enrichment an auto-link gets: the filing's name is an edgar alias, cik and
    # legal_name are filled when empty
    assert _aliases(db_session, queue["robotics"].id)["acme robotics group"] == "edgar"
    robotics = queue["robotics"]
    db_session.refresh(robotics)
    # rows apply in item order (ev-create first), so the created company took the shared
    # test CIK and this one is left alone; legal_name is filled either way
    assert (robotics.cik, robotics.legal_name) == (None, "Acme Robotics Group")

    created = db_session.scalars(select(Company).where(Company.domain == "acme-ventures.com")).one()
    assert created.name == "Acme Ventures"
    created_event = db_session.get(EventRow, "ev-create")
    assert created_event is not None
    assert created_event.company_id == created.id
    assert _aliases(db_session, created.id) == {"acme robotics ventures": "edgar"}
    assert (created.cik, created.legal_name) == ("0000012345", "Acme Robotics Ventures")
    # a verdict's CIK is trusted (ADR 0035) and the created company is not from the YC
    # directory
    assert (created.cik_source, created.source) == ("manual", "manual")

    rejected = db_session.get(EventRow, "ev-reject")
    assert rejected is not None
    assert rejected.company_id is None
    item = queue["items"]["ev-reject"]
    db_session.refresh(item)
    assert (item.status, item.payload["verdict"]) == ("rejected", "reject")
    assert item.resolved_at == NOW

    # the linked pair never returns: the same name now hits R2
    _formd_event(db_session, "ev-again", "Acme Robotics Group, Inc.", cik="0000099999")
    again = resolve_events(db_session, now=NOW)
    assert again.by_rule == {"R2": 1}
    assert open_items(db_session) == []
    # and the created company's next filing links by its CIK (R0), whatever its name
    _formd_event(db_session, "ev-next", "Entirely Renamed Holdings Inc")
    nxt = resolve_events(db_session, now=NOW)
    assert nxt.by_rule == {"R0": 1}
    next_event = db_session.get(EventRow, "ev-next")
    assert next_event is not None
    assert next_event.company_id == created.id
    assert next_event.payload["match"]["confidence"] == 0.99


def test_import_skips_blank_and_reports_errors(
    db_session: Session, queue: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "review.csv"
    export_review_csv(db_session, path)
    rows = _rows(path)
    rows[0]["verdict"] = "link"  # no company given -> error
    rows[1]["verdict"] = "maybe"  # unknown verdict -> error
    rows[2]["verdict"] = ""  # blank -> skipped
    rows.append({**rows[0], "id": "abc", "verdict": "reject"})
    rows.append({**rows[0], "id": "999999", "verdict": "reject"})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    stats = import_review_csv(db_session, path, now=NOW)
    assert (stats.rows, stats.linked, stats.created, stats.rejected, stats.skipped) == (
        5,
        0,
        0,
        0,
        1,
    )
    assert len(stats.errors) == 4
    assert any("existing company id or domain" in e for e in stats.errors)
    assert any("verdict must be one of" in e for e in stats.errors)
    assert any("is not a review item id" in e for e in stats.errors)
    assert any("no entity_match review item" in e for e in stats.errors)
    assert len(open_items(db_session)) == 3


def test_apply_verdict_rules(db_session: Session, queue: dict[str, Any]) -> None:
    item = queue["items"]["ev-link"]
    with pytest.raises(ReviewError, match="create needs a domain"):
        apply_verdict(db_session, item.id, "create", company_id_or_domain="not a domain")
    with pytest.raises(ReviewError, match="create needs a domain"):
        apply_verdict(db_session, item.id, "create")
    with pytest.raises(ReviewError, match="existing company"):
        apply_verdict(db_session, item.id, "link", company_id_or_domain="nobody.example")
    with pytest.raises(ReviewError, match="existing company"):
        apply_verdict(db_session, item.id, "link", company_id_or_domain="424242")
    # link by domain works and closes the item; a second verdict is refused
    done = apply_verdict(db_session, item.id, "link", company_id_or_domain="acme.com", now=NOW)
    assert (done.status, done.payload["company_id"]) == ("resolved", queue["acme"].id)
    with pytest.raises(ReviewError, match="already resolved"):
        apply_verdict(db_session, item.id, "reject")
    # create on an existing domain links to that company instead of inserting a duplicate
    other = queue["items"]["ev-create"]
    apply_verdict(db_session, other.id, "create", company_id_or_domain="acmerobotics.com", now=NOW)
    assert db_session.scalar(select(Company.id).where(Company.domain == "acmerobotics.com"))
    assert len(list(db_session.scalars(select(Company)))) == 2
    # create without a name falls back to the event name
    third = queue["items"]["ev-reject"]
    apply_verdict(db_session, third.id, "create", company_id_or_domain="acmeholdings.com", now=NOW)
    created = db_session.scalars(select(Company).where(Company.domain == "acmeholdings.com")).one()
    assert created.name == "Acme Robotics Partners"


def test_apply_verdict_unknown_item_or_kind(db_session: Session) -> None:
    with pytest.raises(ReviewError, match="no entity_match review item"):
        apply_verdict(db_session, 12345, "reject")
    board = ReviewQueueItem(kind="board_verify", payload={"domain": "x.example"})
    db_session.add(board)
    db_session.flush()
    with pytest.raises(ReviewError, match="no entity_match review item"):
        apply_verdict(db_session, board.id, "reject")


def test_manual_link_applies_previous_name_aliases(db_session: Session) -> None:
    """A review link is a link of a filing: previous names become aliases, so a later
    filing under the former name resolves through R2 (spec section 6, renamed companies)."""
    method = _company(db_session, "method.com", "Method", hq_state="NY")
    _company(db_session, "method.io", "Method Financial", hq_state="NY")
    backfill_aliases(db_session)
    event = _formd_event(db_session, "ev-methodd", "Methodd Inc", state="NY")
    event.payload = {
        **event.payload,
        "cik": "0000777777",
        "previous_names": ["Old Method Corp", "Methodology LLC"],
    }
    db_session.flush()
    assert resolve_events(db_session, now=NOW).review == 1
    (item,) = open_items(db_session)
    apply_verdict(db_session, item.id, "link", company_id_or_domain="method.com", now=NOW)

    db_session.refresh(method)
    assert (method.cik, method.legal_name) == ("0000777777", "Methodd Inc")
    assert _aliases(db_session, method.id) == {
        "method": "manual",
        "methodd": "edgar",
        "old method": "edgar",
        "methodology": "edgar",
    }
    assert event.company_id == method.id
    assert event.payload["match"]["rule"] == "manual"

    _formd_event(db_session, "ev-former", "Methodology, LLC", state="NY")
    stats = resolve_events(db_session, now=NOW)
    assert stats.by_rule == {"R2": 1}
    former = db_session.get(EventRow, "ev-former")
    assert former is not None
    assert former.company_id == method.id


def test_link_survives_a_missing_event(db_session: Session) -> None:
    company = _company(db_session, "acme.com", "Acme")
    item = ReviewQueueItem(
        kind="entity_match",
        payload={
            "event_id": "gone",
            "name": "Acme Inc",
            "state": None,
            "city": None,
            "candidates": [],
        },
    )
    db_session.add(item)
    db_session.flush()
    apply_verdict(db_session, item.id, "link", company_id_or_domain=str(company.id), now=NOW)
    assert item.status == "resolved"
    assert _aliases(db_session, company.id) == {"acme": "manual"}


def test_check_password() -> None:
    assert check_password("secret", SecretStr("secret")) is True
    assert check_password("secret", "secret") is True
    assert check_password("wrong", SecretStr("secret")) is False
    assert check_password("", SecretStr("")) is False
    assert check_password("anything", None) is False


def test_review_app_imports_without_streamlit_display() -> None:
    assert callable(review_app.main)
    assert review_app.TITLE
    text = review_app._candidates_text(
        {"candidates": [{"company_id": 1, "alias": "acme", "ratio": 90}]}
    )
    assert text == "1: acme (90)"
    assert review_app._candidates_text({}) == "(no candidates)"


def _discovery(reason: str, **extra: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "reason": reason,
        "cik": "0002990001",
        "tier": "C",
        "domains": ["quillfeather.com", "quillfeather.ai"],
        "evidence": {"path": "/terms", "span": "Quillfeather Robotics, Inc."},
        "amount_sold": 2_500_000,
    }
    info.update(extra)
    return {
        "event_id": "ev-d",
        "name": "Quillfeather Robotics, Inc.",
        "candidates": [],
        DISCOVERY_KEY: info,
    }


def test_review_app_shows_issuer_discovery_proposals() -> None:
    """ADR 0035: a tier C / several-domains item has no candidates; the page shows its
    proposed domains, tier, evidence and amount sold."""
    text = review_app._candidates_text(_discovery("tier_c"))
    assert text.splitlines() == [
        "issuer discovery: tier_c, tier C",
        "  domains: quillfeather.com quillfeather.ai",
        "  evidence: /terms: Quillfeather Robotics, Inc.",
        "  amount sold: 2500000",
    ]
    proposed = _discovery("domain_exists", tier="A", domains=["acme.com"], evidence=None)
    proposed["candidates"] = [{"company_id": 7, "alias": "acme.com", "ratio": None}]
    assert review_app._candidates_text(proposed).splitlines() == [
        "7: acme.com (None)",
        "issuer discovery: domain_exists, tier A",
        "  domains: acme.com",
        "  amount sold: 2500000",
    ]
    bare = _discovery("several_domains", tier=None, domains="junk", evidence={"path": 1})
    bare[DISCOVERY_KEY]["amount_sold"] = True
    assert review_app._candidates_text(bare).splitlines() == [
        "issuer discovery: several_domains, tier -",
        "  domains: (none)",
    ]
    assert discovery_details({DISCOVERY_KEY: "junk"}) == dict.fromkeys(DISCOVERY_COLUMNS, "")


def test_export_shows_issuer_discovery_columns(db_session: Session, tmp_path: Path) -> None:
    assert set(DISCOVERY_COLUMNS) < set(CSV_COLUMNS)
    db_session.add_all(
        [
            ReviewQueueItem(kind="entity_match", payload=_discovery("tier_c"), created_at=NOW),
            ReviewQueueItem(
                kind="entity_match",
                payload={"event_id": "ev-plain", "name": "Plain", "candidates": []},
                created_at=NOW,
            ),
        ]
    )
    db_session.flush()
    path = tmp_path / "review.csv"
    assert export_review_csv(db_session, path) == 2
    found, plain = _rows(path)
    assert (
        found["discovery_reason"],
        found["discovery_tier"],
        found["discovery_domains"],
        found["discovery_evidence"],
        found["discovery_amount_sold"],
    ) == (
        "tier_c",
        "C",
        "quillfeather.com quillfeather.ai",
        "/terms: Quillfeather Robotics, Inc.",
        "2500000",
    )
    assert found["candidate_1_id"] == ""
    assert all(plain[column] == "" for column in DISCOVERY_COLUMNS)
    assert review.VERDICTS == ("link", "create", "reject")
