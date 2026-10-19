"""Rounds filed, not announced (ADR 0042): which filings are listed, in what order, and how.

Every row here is hand-authored: synthetic companies on ``.example`` domains, made-up CIKs and
accession numbers, ``sec.gov`` receipt URLs of the collector's shape, and no person's name.
The publish instant is Monday 2026-10-19 06:23 UTC, when ``weekly.yml`` publishes edition
2026-W42 (2026-10-12 to 2026-10-18); the list covers filings dated 2026-10-05 to 2026-10-18.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from sqlalchemy.orm import Session

from tell.backtest import speed_vs_press as svp
from tell.models import Company, EventRow
from tell.publish.json_api import (
    ROUNDS_SCHEMA,
    build_rounds_json,
    validate_rounds_json,
    validate_site_tree,
)
from tell.publish.site import environment, page_context, rounds_view, write_rounds
from tell.publish.weekly import (
    IsoWeek,
    RoundRow,
    RoundsData,
    rounds_not_announced,
    rounds_window,
    trusted_match,
)

NOW = datetime(2026, 10, 19, 6, 23, tzinfo=UTC)
WEEK = IsoWeek(2026, 42)
R2: dict[str, Any] = {"rule": "R2", "confidence": 0.95}
_ids = itertools.count(1)


def _company(
    session: Session,
    domain: str,
    *,
    source: str = "yc",
    cik: str | None = None,
    cik_source: str | None = None,
) -> Company:
    row = Company(
        domain=domain,
        name=domain.split(".", maxsplit=1)[0].title() + " Labs",
        source=source,
        cik=cik,
        cik_source=cik_source,
    )
    session.add(row)
    session.flush()
    return row


def _event(
    session: Session,
    owner: Company | None,
    type_: str,
    at: datetime,
    payload: dict[str, Any],
    *,
    source: str,
    url: str,
) -> EventRow:
    row = EventRow(
        event_id=f"rounds-test-{next(_ids)}",
        company_id=owner.id if owner is not None else None,
        source=source,
        type=type_,
        occurred_at=at,
        observed_at=at + timedelta(hours=26),
        magnitude=Decimal(1),
        url=url,
        payload=payload,
        run_id="rounds-test",
    )
    session.add(row)
    session.flush()
    return row


def _filing(
    session: Session,
    owner: Company | None,
    filed: date,
    *,
    match: Any = R2,
    type_: str = "form_d_filed",
    amendment: bool = False,
    cik: str = "0000000001",
    amount: int | None = 5_000_000,
    sold: int | None = 1_000_000,
    **extra: Any,
) -> EventRow:
    """A Form D event with the collector's payload keys (no related persons, ever)."""
    accession = f"{cik}-26-{next(_ids):06d}"
    payload: dict[str, Any] = {
        "accession_number": accession,
        "cik": cik,
        "date_filed": filed.isoformat(),
        "is_amendment": amendment,
        "issuer_name": f"{owner.name if owner else 'Unlinked'}, Inc.",
        "entity_type": "Corporation",
        "jurisdiction_of_inc": "DELAWARE",
        "industry_group_type": "Other Technology",
        "total_offering_amount": amount,
        "total_offering_amount_indefinite": False,
        "total_amount_sold": sold,
        **extra,
    }
    if match is not None:
        payload["match"] = match
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    midnight = datetime.combine(filed, time(0), tzinfo=UTC)
    return _event(session, owner, type_, midnight, payload, source="edgar", url=folder)


def _news(session: Session, owner: Company, at: datetime, type_: str = "news_funding") -> EventRow:
    return _event(
        session,
        owner,
        type_,
        at,
        {"title": f"{owner.name} raises a round", "category": type_.removeprefix("news_")},
        source="news",
        url=f"https://news.example/{owner.domain}/{next(_ids)}",
    )


def _at(day: str, hour: int = 9) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(hour), tzinfo=UTC)


def _domains(session: Session, published: set[int], now: datetime = NOW) -> list[str]:
    data = rounds_not_announced(session, WEEK, now=now, published=published)
    return [r.domain for r in data.rounds]


# --- the window --------------------------------------------------------------------------------


def test_window_is_the_edition_week_and_the_week_before() -> None:
    assert rounds_window(WEEK) == (
        datetime(2026, 10, 5, tzinfo=UTC),
        datetime(2026, 10, 19, tzinfo=UTC),
    )
    data = RoundsData(week=WEEK, now=NOW, rounds=[])
    assert (data.filed_from, data.filed_to, data.as_of) == (
        date(2026, 10, 5),
        date(2026, 10, 18),
        date(2026, 10, 19),
    )


# --- what is listed ------------------------------------------------------------------------------


@pytest.mark.db
def test_list_keeps_unannounced_rounds_newest_first(db_session: Session) -> None:
    s = db_session
    kept: list[Company] = []

    # Kept: no press at all; the newest filing.
    alpha = _company(s, "alpha.example")
    _filing(s, alpha, date(2026, 10, 16))
    # Kept: the only funding headline is 24 days before the filing (before the window).
    bravo = _company(s, "bravo.example")
    _filing(s, bravo, date(2026, 10, 14), match={"rule": "R0", "confidence": 0.99})
    _news(s, bravo, _at("2026-09-20"))
    # Excluded: press inside the window, after the filing.
    charlie = _company(s, "charlie.example")
    _filing(s, charlie, date(2026, 10, 13))
    _news(s, charlie, _at("2026-10-15"))
    # Excluded: press exactly 14 days before the filing (the window's first day).
    delta = _company(s, "delta.example")
    _filing(s, delta, date(2026, 10, 9))
    _news(s, delta, _at("2026-09-25", 0))
    # Kept: press 15 days before the filing; ties on the filing date sort by domain.
    echo = _company(s, "echo.example")
    _filing(s, echo, date(2026, 10, 9))
    _news(s, echo, _at("2026-09-24", 23))
    foxtrot = _company(s, "foxtrot.example")
    _filing(s, foxtrot, date(2026, 10, 9), amount=None, sold=None)
    # Kept: a funding headline after the publish instant is not yet known.
    golf = _company(s, "golf.example")
    _filing(s, golf, date(2026, 10, 12))
    _news(s, golf, NOW + timedelta(hours=3))
    # Kept: a launch headline is not a funding announcement.
    hotel = _company(s, "hotel.example")
    _filing(s, hotel, date(2026, 10, 7))
    _news(s, hotel, _at("2026-10-08"), type_="news_launch")
    kept += [alpha, bravo, echo, foxtrot, golf, hotel]

    # Excluded: amendments, as a flagged form_d_filed and as the amendment event type.
    india = _company(s, "india.example")
    _filing(s, india, date(2026, 10, 15), amendment=True)
    _filing(s, india, date(2026, 10, 15), type_="form_d_amendment", amendment=True)
    # Excluded: a company added by hand (intake, review verdict, tell discover).
    juliet = _company(s, "juliet.example", source="manual")
    _filing(s, juliet, date(2026, 10, 15))
    # Excluded: a company without an account page.
    kilo = _company(s, "kilo.example")
    _filing(s, kilo, date(2026, 10, 15))
    # Excluded: pooled funds, by industry group and by a fund word in the issuer's name.
    lima = _company(s, "lima.example")
    _filing(s, lima, date(2026, 10, 15), industry_group_type="Pooled Investment Fund")
    _filing(s, lima, date(2026, 10, 15), issuer_name="Lima Labs Opportunity Fund LP")
    # Excluded: filed outside the window (the day before it opens, the day after it ends).
    mike = _company(s, "mike.example")
    _filing(s, mike, date(2026, 10, 4))
    _filing(s, mike, date(2026, 10, 19))
    # Excluded: an untrusted link (review candidate, guard failure) and no link at all.
    november = _company(s, "november.example")
    _filing(s, november, date(2026, 10, 15), match={"rule": "R5", "confidence": 0.6})
    _filing(s, november, date(2026, 10, 15), match={**R2, "guard_failed": ["no_site_evidence"]})
    _filing(s, None, date(2026, 10, 15))
    # Excluded: a receipt that is not an EDGAR filing.
    oscar = _company(s, "oscar.example")
    _filing(s, oscar, date(2026, 10, 15)).url = "https://mirror.example/filing"
    s.flush()

    published = {c.id for c in [*kept, charlie, delta, india, juliet, lima, mike, november, oscar]}
    data = rounds_not_announced(s, WEEK, now=NOW, published=published)
    assert [r.domain for r in data.rounds] == [
        "alpha.example",
        "bravo.example",
        "golf.example",
        "echo.example",
        "foxtrot.example",
        "hotel.example",
    ]
    assert [r.date_filed for r in data.rounds] == sorted(
        (r.date_filed for r in data.rounds), reverse=True
    )
    first = data.rounds[0]
    assert first == RoundRow(
        domain="alpha.example",
        name="Alpha Labs",
        date_filed="2026-10-16",
        amount_usd=5_000_000,
        amount_indefinite=False,
        amount_sold_usd=1_000_000,
        url=first.url,
    )
    assert first.url.startswith("https://www.sec.gov/Archives/edgar/data/1/")
    foxtrot_row = next(r for r in data.rounds if r.domain == "foxtrot.example")
    assert (foxtrot_row.amount_usd, foxtrot_row.amount_sold_usd) == (None, None)

    # Reads only, so a second read is identical; news arriving later removes the row.
    assert rounds_not_announced(s, WEEK, now=NOW, published=published) == data
    _news(s, alpha, NOW + timedelta(hours=1))
    later = NOW + timedelta(days=1)
    assert "alpha.example" not in _domains(s, published, later)
    assert "golf.example" not in _domains(s, published, later)  # its headline is known now


@pytest.mark.db
def test_cik_links_need_a_trusted_source_or_a_trusted_link(db_session: Session) -> None:
    s = db_session
    trusted = _company(s, "papa.example", cik="0000000101", cik_source="issuer_first_a")
    _filing(s, trusted, date(2026, 10, 14), match=None, cik="0000000101")
    # Its CIK has no recorded source, but a trusted link (an 8-K matched by R2) carried it.
    vouched = _company(s, "quebec.example", cik="0000000102")
    _filing(s, vouched, date(2026, 10, 13), match=None, cik="0000000102")
    _filing(s, vouched, date(2026, 10, 1), type_="form_8k_filed", cik="0000000102")
    # Only a guard-failed link ever carried this CIK.
    unvouched = _company(s, "romeo.example", cik="0000000103")
    _filing(s, unvouched, date(2026, 10, 12), match=None, cik="0000000103")
    _filing(
        s,
        unvouched,
        date(2026, 10, 1),
        type_="form_8k_filed",
        cik="0000000103",
        match={**R2, "guard_failed": True},
    )
    # The filing's CIK is not the company's.
    other = _company(s, "sierra.example", cik="0000000104", cik_source="r1")
    _filing(s, other, date(2026, 10, 12), match=None, cik="0000000999")
    # A reviewer's link wins over the guard failure left from the candidate it resolved.
    reviewed = _company(s, "tango.example")
    _filing(s, reviewed, date(2026, 10, 11), match={"rule": "manual", "guard_failed": True})
    published = {c.id for c in (trusted, vouched, unvouched, other, reviewed)}
    assert _domains(s, published) == ["papa.example", "quebec.example", "tango.example"]


@pytest.mark.db
def test_naive_publish_instant_is_refused(db_session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        rounds_not_announced(db_session, WEEK, now=NOW.replace(tzinfo=None), published=())


# --- the trusted-link rule is the speed-vs-press cohort's ---------------------------------------

LINKS: list[tuple[str, Any, bool]] = [
    ("R0", {"rule": "R0"}, True),
    ("R1", {"rule": "R1"}, True),
    ("R2", R2, True),
    ("R3", {"rule": "R3"}, True),
    ("R4", {"rule": "R4"}, True),
    ("R5", {"rule": "R5"}, False),
    ("R6", {"rule": "R6"}, False),
    ("no rule", {"confidence": 0.9}, False),
    ("null record", None, False),
    ("guard true", {**R2, "guard_failed": True}, False),
    ("guard false", {**R2, "guard_failed": False}, True),
    ("guard list", {**R2, "guard_failed": ["jurisdiction_not_delaware"]}, False),
    ("guard empty list", {**R2, "guard_failed": []}, True),
    ("guard object", {**R2, "guard_failed": {"year": "after batch"}}, False),
    ("guard empty object", {**R2, "guard_failed": {}}, True),
    ("guard string", {**R2, "guard_failed": "state"}, False),
    ("guard empty string", {**R2, "guard_failed": ""}, True),
    ("guard number", {**R2, "guard_failed": 1}, False),
    ("guard zero", {**R2, "guard_failed": 0}, True),
    ("reviewer over guard", {"rule": "manual", "guard_failed": True}, True),
]


@pytest.mark.parametrize(
    ("match", "expected"), [(m, e) for _, m, e in LINKS], ids=[n for n, _, _ in LINKS]
)
def test_trusted_match(match: Any, expected: bool) -> None:
    assert trusted_match(match) is expected


CIK_CASES: list[tuple[str, str | None, str | None, str | None, bool]] = [
    # (case, company cik, cik_source, CIK carried by an older R2-linked 8-K, expected)
    ("trusted source", "0000000201", "guarded_r2", None, True),
    ("vouched by a trusted link", "0000000201", None, "0000000201", True),
    ("vouched for another CIK", "0000000201", None, "0000000202", False),
    ("unknown source", "0000000201", None, None, False),
    ("company without a CIK", None, None, None, False),
]


@pytest.mark.db
@pytest.mark.parametrize(
    ("match", "expected"),
    [(m, e) for _, m, e in LINKS if m is not None],
    ids=[n for n, m, _ in LINKS if m is not None],
)
def test_match_rule_agrees_with_speed_vs_press(
    db_session: Session, match: Any, expected: bool
) -> None:
    owner = _company(db_session, "uniform.example")
    _filing(db_session, owner, date(2026, 10, 14), match=match)
    listed = _domains(db_session, {owner.id}) == ["uniform.example"]
    assert listed is expected
    assert (svp.compute(db_session, NOW).n == 1) is expected


@pytest.mark.db
def test_null_match_record_agrees_with_speed_vs_press(db_session: Session) -> None:
    owner = _company(db_session, "victor.example", cik="0000000001", cik_source="r1")
    row = _filing(db_session, owner, date(2026, 10, 14), match=None)
    row.payload = {**row.payload, "match": None}  # a JSON null record is not a CIK link
    db_session.flush()
    assert _domains(db_session, {owner.id}) == []
    assert svp.compute(db_session, NOW).n == 0


@pytest.mark.db
@pytest.mark.parametrize(
    ("cik", "cik_source", "voucher", "expected"),
    [(c, src, v, e) for _, c, src, v, e in CIK_CASES],
    ids=[case for case, *_ in CIK_CASES],
)
def test_cik_rule_agrees_with_speed_vs_press(
    db_session: Session,
    cik: str | None,
    cik_source: str | None,
    voucher: str | None,
    expected: bool,
) -> None:
    owner = _company(db_session, "whiskey.example", cik=cik, cik_source=cik_source)
    _filing(db_session, owner, date(2026, 10, 14), match=None, cik="0000000201")
    if voucher is not None:
        _filing(db_session, owner, date(2026, 9, 1), type_="form_8k_filed", cik=voucher)
    listed = _domains(db_session, {owner.id}) == ["whiskey.example"]
    assert listed is expected
    assert (svp.compute(db_session, NOW).n == 1) is expected


# --- JSON and HTML -------------------------------------------------------------------------------


def _data(*rows: RoundRow) -> RoundsData:
    return RoundsData(week=WEEK, now=NOW, rounds=list(rows))


def _row(domain: str, **overrides: Any) -> RoundRow:
    fields: dict[str, Any] = {
        "domain": domain,
        "name": domain.split(".", maxsplit=1)[0].title(),
        "date_filed": "2026-10-14",
        "amount_usd": None,
        "amount_indefinite": False,
        "amount_sold_usd": None,
        "url": f"https://www.sec.gov/Archives/edgar/data/1/{domain}/",
    }
    fields.update(overrides)
    return RoundRow(**fields)


def test_json_shape_validates_and_drift_fails() -> None:
    jsonschema.Draft202012Validator.check_schema(ROUNDS_SCHEMA)
    obj = build_rounds_json(_data(_row("alpha.example", amount_usd=5_000_000)), sha=None)
    validate_rounds_json(obj)
    assert list(obj) == [
        "week",
        "filed_from",
        "filed_to",
        "as_of",
        "rounds",
        "method",
        "generated_by",
    ]
    assert obj["generated_by"] == "local"
    assert list(obj["rounds"][0]) == [
        "domain",
        "name",
        "date_filed",
        "amount_usd",
        "amount_indefinite",
        "amount_sold_usd",
        "url",
    ]
    method = obj["method"]
    assert "2026-10-05 to 2026-10-18" in method
    assert "14 days before the filing through 2026-10-19" in method
    assert method.endswith("the list changes as news arrives.")
    assert "%" not in method
    for broken in (
        {**obj, "extra": 1},
        {**obj, "week": "W42"},
        {**obj, "rounds": [{**obj["rounds"][0], "url": "https://mirror.example/filing/"}]},
        {**obj, "rounds": [{**obj["rounds"][0], "contact": "someone"}]},
        {**obj, "rounds": [{**obj["rounds"][0], "amount_usd": -1}]},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validate_rounds_json(broken)


def test_page_shows_only_the_amounts_the_payload_states(tmp_path: Path) -> None:
    env = environment()
    site = tmp_path / "site"
    write_rounds(site, env, _data(_row("alpha.example")), as_of=NOW.date(), sha=None)
    page = (site / "rounds-not-announced.html").read_text()
    assert "Offering" not in page
    assert "Sold" not in page
    assert '<a href="companies/alpha.example.html">Alpha</a>' in page
    assert 'href="https://www.sec.gov/Archives/edgar/data/1/alpha.example/"' in page
    assert validate_site_tree(site) == 1

    view = write_rounds(
        site,
        env,
        _data(
            _row("alpha.example", amount_indefinite=True, amount_sold_usd=2_750_000),
            _row("bravo.example", amount_usd=18_000_000),
        ),
        as_of=NOW.date(),
        sha=None,
    )
    assert (view["show_offering"], view["show_sold"]) == (True, True)
    page = (site / "rounds-not-announced.html").read_text()
    for text in ("indefinite", "$2,750,000", "$18,000,000", "undisclosed"):
        assert text in page

    write_rounds(site, env, _data(), as_of=NOW.date(), sha=None)
    page = (site / "rounds-not-announced.html").read_text()
    assert "Nothing to list for this window." in page
    assert "<table" not in page


def test_view_links_the_method_section() -> None:
    view = rounds_view(_data())
    assert view["method_url"] == (
        "https://github.com/karank2512/tell-ai/blob/main/"
        "docs/methods/publishing.md#5a-rounds-filed-not-announced"
    )
    html = (
        environment()
        .get_template("rounds.html")
        .render(**page_context(NOW.date(), None, WEEK.label, ""), rounds=view)
    )
    assert "weekly/2026-W42.html" in html
    assert "Method</a>: Every original Form D" in html
