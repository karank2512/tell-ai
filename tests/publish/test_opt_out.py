"""The public-site opt-out (ADR 0044): the record, the CLI, what a publish leaves out, what not.

The scenario is ``tests/publish/scenario.py`` (synthetic ``.example`` companies, no person's
name). Example Robotics is the company that opts out: it has an account page, a Form D in the
2026-W40 top list and in the Form D filings, a place on the public ``pilot`` watchlist, and
a threshold crossing nudged to the private watchlist.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell.config import get_settings
from tell.crm.hubspot import load_receipt, push_company
from tell.db import SessionLocal
from tell.models import Company, WatchlistItem
from tell.publish import cli
from tell.publish.csv_export import csv_rows
from tell.publish.nudge import build_nudge_blocks, pending_nudges, send_nudges
from tell.publish.opt_out import (
    OptOutChange,
    add_opt_out,
    normalize_domain,
    opt_out_link,
    opted_out,
    opted_out_domains,
    remove_opt_out,
)
from tell.publish.site import RETIRED_SECTION_ID, SiteResult, render_site, scrub_archive
from tell.publish.slack import crossed_accounts
from tell.publish.weekly import IsoWeek, build_weekly
from tell.signals.profiles import get_profile
from tests.publish import scenario as sc

ROBOTICS = sc.ROBOTICS
SHA = "0123456789abcdef0123456789abcdef01234567"
PRIVATE = "https://hooks.slack.invalid/services/T0/B0/PRIVATE"
BASE = "https://pages.example/tell-data"
SLUG = "private-list"
LATER = date(2026, 10, 13)
"""A week after the scenario's publish day: the edition is 2026-W41 and 2026-W40 is archived."""

runner = CliRunner()


@pytest.fixture
def frozen() -> Iterator[None]:
    with freeze_time(sc.NOW):
        yield


def _company(session: Session, domain: str) -> Company:
    return session.scalars(select(Company).where(Company.domain == domain)).one()


# --- the record ----------------------------------------------------------------------------------


def test_normalize_domain_takes_what_an_issue_title_might_hold() -> None:
    assert normalize_domain(" https://www.ExampleRobotics.example/about ") == ROBOTICS
    assert normalize_domain("WWW.examplerobotics.example.") == ROBOTICS
    assert normalize_domain(ROBOTICS) == ROBOTICS


def test_opt_out_link_opens_the_template_prefilled() -> None:
    repo = "https://github.com/karank2512/tell-ai/issues/new"
    assert opt_out_link() == f"{repo}?template=opt-out.md"
    assert opt_out_link("acme.example") == (
        f"{repo}?template=opt-out.md&title=Opt+out%3A+acme.example"
    )


@pytest.mark.db
def test_add_and_remove_are_idempotent_and_keep_the_first_date(db_session: Session) -> None:
    sc.build(db_session)
    first = sc.NOW
    assert add_opt_out(db_session, f"www.{ROBOTICS}", now=first) == OptOutChange(
        ROBOTICS, True, first, True
    )
    again = add_opt_out(db_session, ROBOTICS, now=first + timedelta(days=1))
    assert again == OptOutChange(ROBOTICS, True, first, False)
    company = _company(db_session, ROBOTICS)
    assert (company.public_opt_out, company.public_opted_out_at) == (True, first)
    assert [c.domain for c in opted_out(db_session)] == [ROBOTICS]
    assert opted_out_domains(db_session) == {ROBOTICS}
    assert again.as_record() == {
        "domain": ROBOTICS,
        "public_opt_out": True,
        "public_opted_out_at": "2026-10-06T12:00:00+00:00",
        "changed": False,
    }

    assert remove_opt_out(db_session, ROBOTICS, now=first) == OptOutChange(
        ROBOTICS, False, None, True
    )
    assert remove_opt_out(db_session, ROBOTICS, now=first) == OptOutChange(
        ROBOTICS, False, None, False
    )
    assert (company.public_opt_out, company.public_opted_out_at) == (False, None)
    assert opted_out(db_session) == []
    assert opted_out_domains(db_session) == frozenset()
    with pytest.raises(LookupError, match=re.escape("no company with domain 'nobody.example'")):
        add_opt_out(db_session, "nobody.example", now=first)
    with pytest.raises(LookupError, match="no company"):
        remove_opt_out(db_session, "nobody.example", now=first)


# --- the CLI -------------------------------------------------------------------------------------


@pytest.fixture
def bound(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Point the publish sub-app at the temporary database, with the scenario committed."""

    @contextmanager
    def scope() -> Iterator[Session]:
        session = SessionLocal(bind=engine)
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(cli, "session_scope", scope)
    with freeze_time(sc.NOW):
        with SessionLocal(bind=engine) as session:
            sc.build(session)
            session.commit()
        yield engine


def _line(args: list[str], code: int = 0) -> dict[str, Any]:
    result = runner.invoke(cli.app, ["opt-out", *args])
    assert result.exit_code == code, result.output
    (line,) = result.output.strip().splitlines()  # one JSON line, nothing else
    record: dict[str, Any] = json.loads(line)
    return record


@pytest.mark.db
def test_cli_add_list_remove(bound: Engine) -> None:
    at = "2026-10-06T12:00:00+00:00"
    with freeze_time(sc.NOW):
        assert _line(["add", f"https://www.{ROBOTICS}/"]) == {
            "action": "add",
            "domain": ROBOTICS,
            "public_opt_out": True,
            "public_opted_out_at": at,
            "changed": True,
        }
    with freeze_time(sc.NOW + timedelta(days=2)):
        assert _line(["add", ROBOTICS])["changed"] is False
        assert _line(["list"]) == {
            "action": "list",
            "count": 1,
            "opted_out": [{"domain": ROBOTICS, "public_opted_out_at": at}],
        }
    with SessionLocal(bind=bound) as session:
        company = _company(session, ROBOTICS)
        assert company.public_opt_out is True
        assert company.public_opted_out_at == sc.NOW
    assert _line(["remove", ROBOTICS]) == {
        "action": "remove",
        "domain": ROBOTICS,
        "public_opt_out": False,
        "public_opted_out_at": None,
        "changed": True,
    }
    assert _line(["remove", ROBOTICS])["changed"] is False
    assert _line(["list"]) == {"action": "list", "count": 0, "opted_out": []}


@pytest.mark.db
def test_cli_unknown_domain_exits_1_with_one_json_line(bound: Engine) -> None:
    for action in ("add", "remove"):
        record = _line([action, "Nobody.example"], code=1)
        assert record == {
            "action": action,
            "domain": "nobody.example",
            "error": "no company with domain 'nobody.example': tell publishes nothing for it",
        }
    with SessionLocal(bind=bound) as session:
        assert opted_out(session) == []


def test_cli_has_no_note_option() -> None:
    # The request and how it was verified stay on the GitHub issue (ADR 0044).
    result = runner.invoke(cli.app, ["opt-out", "add", "--help"])
    assert result.exit_code == 0
    assert "--note" not in result.output


# --- the site ------------------------------------------------------------------------------------


def _render(session: Session, out: Path, scenario: sc.Scenario, day: date) -> SiteResult:
    now = sc.NOW + (day - sc.AS_OF)
    return render_site(session, out, as_of=day, now=now, profiles=scenario.profiles, sha=SHA)


def _files(site: Path) -> dict[str, str]:
    return {
        p.relative_to(site).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(site.rglob("*"))
        if p.is_file()
    }


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_the_next_publish_leaves_an_opted_out_company_off_every_page(
    db_session: Session, tmp_path: Path
) -> None:
    scenario = sc.build(db_session)
    same, next_week = tmp_path / "same", tmp_path / "next"
    first = _render(db_session, same, scenario, sc.AS_OF)
    _render(db_session, next_week, scenario, sc.AS_OF)
    assert ROBOTICS in first.companies
    before = _files(same / "site")
    for path in (f"companies/{ROBOTICS}.html", f"api/v0/companies/{ROBOTICS}.json"):
        assert path in before
    for path in (
        "index.html",
        "weekly/2026-W40.html",
        "api/v0/weekly/latest.json",
        "api/v0/watchlists/pilot.json",
        "api/v0/form-d-filings.json",
        "form-d-filings.html",
    ):
        assert ROBOTICS in before[path], path

    add_opt_out(db_session, ROBOTICS, now=sc.NOW)

    # The same edition, published again: nothing names the company any more.
    second = _render(db_session, same, scenario, sc.AS_OF)
    assert ROBOTICS not in second.companies
    assert ROBOTICS in second.removed  # the files the first publish wrote are deleted
    after = _files(same / "site")
    named = {path for path, text in after.items() if ROBOTICS in text or "Example Robotics" in text}
    assert named == set()
    weekly = json.loads(after["api/v0/weekly/latest.json"])
    assert [r["domain"] for r in weekly["top_funding"]] == [sc.BLUEFIN]
    filings = json.loads(after["api/v0/form-d-filings.json"])
    assert [r["domain"] for r in filings["filings"]] == [sc.BLUEFIN]
    pilot = json.loads(after["api/v0/watchlists/pilot.json"])
    assert [c["domain"] for c in pilot["companies"]] == [sc.QUILLSTONE, sc.GREENHOUSE]
    assert [c["heat"] for c in pilot["companies"]] == [100, 0]  # among the members shown
    # The aggregate counts still count it: tell reads its sources for the private outputs.
    assert weekly["coverage"] == json.loads(before["api/v0/weekly/latest.json"])["coverage"]

    # A week later the 2026-W40 page is archived: its rows for the company are removed.
    third = _render(db_session, next_week, scenario, LATER)
    assert (third.weekly, third.scrubbed) == ("2026-W41", ["2026-W40"])
    archived = (next_week / "site" / "weekly" / "2026-W40.html").read_text()
    assert ROBOTICS not in archived
    assert "Example Robotics" not in archived
    assert sc.BLUEFIN in archived  # only the opted-out company's rows go
    assert archived.count("<tr>") == before["weekly/2026-W40.html"].count("<tr>") - 2
    later = _files(next_week / "site")
    assert not any(ROBOTICS in text for text in later.values())

    # A further publish has nothing more to remove and rewrites no archived page.
    fourth = _render(db_session, next_week, scenario, LATER)
    assert (fourth.removed, fourth.scrubbed) == ([], [])
    assert _files(next_week / "site") == later

    # Taking the opt-out back publishes the company again; the archive stays as scrubbed.
    remove_opt_out(db_session, ROBOTICS, now=sc.NOW)
    fifth = _render(db_session, next_week, scenario, LATER)
    assert ROBOTICS in fifth.companies
    assert (next_week / "site" / "companies" / f"{ROBOTICS}.html").is_file()
    assert ROBOTICS not in (next_week / "site" / "weekly" / "2026-W40.html").read_text()


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_both_weekly_top_lists_skip_an_opted_out_company(db_session: Session) -> None:
    sc.build(db_session)
    week = IsoWeek(2026, 40)
    before = build_weekly(db_session, week)
    assert [r.domain for r in before.top_hiring] == [sc.GREENHOUSE]
    assert [r.domain for r in before.top_funding] == [ROBOTICS, sc.BLUEFIN]
    add_opt_out(db_session, sc.GREENHOUSE, now=sc.NOW)
    add_opt_out(db_session, ROBOTICS, now=sc.NOW)
    after = build_weekly(db_session, week)
    assert (after.top_hiring, [r.domain for r in after.top_funding]) == ([], [sc.BLUEFIN])
    assert after.coverage == before.coverage


# --- archived weekly pages -------------------------------------------------------------------------

ARCHIVED = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>tell weekly 2026-W42</title>
</head>
<body>
<main>
<h1>tell weekly 2026-W42</h1>
<h2>Top 2 by sales-hiring velocity</h2>
<ol class="ranked">
  <li>
    <strong><a href="../companies/alpha.example.html">Alpha Labs</a></strong> <span class="muted">alpha.example</span>: velocity 3
    <ul>
      <li>2026-10-14: <a href="https://boards.example/alpha">3 new sales roles in 7 days</a></li>
    </ul>
  </li>
  <li>
    <strong>Bravo Labs</strong> <span class="muted">bravo.example</span>: velocity 2
    <ul>
      <li>2026-10-15: <a href="https://boards.example/bravo">2 new sales roles in 7 days</a></li>
    </ul>
  </li>
</ol>
<h2 id="rounds-not-announced">Rounds filed, not announced</h2>
<table class="list">
  <tbody>
  <tr>
    <td><a href="../companies/charlie.example.html">Charlie Labs</a></td>
    <td class="muted">charlie.example</td>
    <td>2026-10-13</td>
    <td><a href="https://www.sec.gov/Archives/edgar/data/3/000000000326000001/">sec.gov</a></td>
  </tr>
  </tbody>
</table>
<p class="muted"><a href="https://github.com/karank2512/tell-ai/blob/main/docs/methods/publishing.md#5a-rounds-filed-not-announced">Method</a>: with no funding headline in tell&#39;s news data; the list changes as news arrives.</p>

<h2>What changed in the method</h2>
<p>No method changes this week</p>
<p>Code and methods: <a href="https://github.com/karank2512/tell-ai">https://github.com/karank2512/tell-ai</a>. JSON: <a href="../api/v0/weekly/latest.json">api/v0/weekly/latest.json</a> (latest edition) and <a href="../api/v0/rounds-not-announced.json">api/v0/rounds-not-announced.json</a> (its rounds filed, not announced).</p>
</main>
</body>
</html>
"""
"""A weekly page as ADR 0042's templates rendered it, trimmed by hand; synthetic companies."""


def test_scrub_archive_drops_the_retired_section_and_opted_out_rows(tmp_path: Path) -> None:
    weekly = tmp_path / "weekly"
    weekly.mkdir()
    (weekly / "2026-W42.html").write_text(ARCHIVED, encoding="utf-8")
    current = weekly / "2026-W43.html"
    current.write_text(ARCHIVED.replace("2026-W42", "2026-W43"), encoding="utf-8")
    clean = weekly / "2026-W41.html"
    clean.write_text("<!doctype html>\n<p>nothing to remove</p>\n", encoding="utf-8")

    assert scrub_archive(weekly, "2026-W43", {"alpha.example"}) == ["2026-W42"]
    page = (weekly / "2026-W42.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>\n<html")
    assert "alpha.example" not in page  # the opted-out company's row, link and name
    assert "Alpha Labs" not in page
    assert '<strong>Bravo Labs</strong> <span class="muted">bravo.example</span>' in page
    assert "2 new sales roles in 7 days" in page
    for gone in (RETIRED_SECTION_ID, "not announced", "news", "charlie.example", "sec.gov"):
        assert gone not in page, gone
    assert "<h2>What changed in the method</h2>" in page
    assert (
        'JSON: <a href="../api/v0/weekly/latest.json">api/v0/weekly/latest.json</a>'
        " (latest edition).</p>"
    ) in page
    assert current.read_text(encoding="utf-8") == ARCHIVED.replace("2026-W42", "2026-W43")
    assert clean.read_text(encoding="utf-8") == "<!doctype html>\n<p>nothing to remove</p>\n"

    # Nothing left to remove: no page is rewritten again.
    assert scrub_archive(weekly, "2026-W43", {"alpha.example"}) == []
    assert (weekly / "2026-W42.html").read_text(encoding="utf-8") == page
    assert scrub_archive(tmp_path / "missing", "2026-W43", {"alpha.example"}) == []


def test_scrub_archive_without_opt_outs_only_retires_the_old_section(tmp_path: Path) -> None:
    weekly = tmp_path / "weekly"
    weekly.mkdir()
    (weekly / "2026-W42.html").write_text(ARCHIVED, encoding="utf-8")
    assert scrub_archive(weekly, "2026-W43", frozenset()) == ["2026-W42"]
    page = (weekly / "2026-W42.html").read_text(encoding="utf-8")
    assert "alpha.example" in page
    assert "bravo.example" in page
    assert "not announced" not in page


# --- private outputs are not affected ---------------------------------------------------------------


def _private_outputs(session: Session, scenario: sc.Scenario) -> dict[str, Any]:
    profile = get_profile("default")
    members = sorted(
        session.scalars(
            select(WatchlistItem.company_id).where(
                WatchlistItem.watchlist_id == scenario.private.id
            )
        )
    )
    receipt = load_receipt(session, ROBOTICS, profile)
    push = push_company(
        None,
        receipt,
        now=sc.NOW,
        base_url=BASE,
        threshold=profile.alert_threshold,
        task=True,
        dry_run=True,
    )
    accounts = pending_nudges(session, scenario.private, sc.NOW)
    return {
        "csv": csv_rows(session, members, profile, sc.AS_OF),
        "nudges": [build_nudge_blocks(a, pages_base_url=BASE) for a in accounts],
        "digest": crossed_accounts(session, scenario.private, sc.NOW),
        "hubspot": (receipt, push.payloads),
    }


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_nudges_digest_hubspot_push_and_csv_ignore_the_opt_out(db_session: Session) -> None:
    scenario = sc.build(db_session)
    scenario.private.slug = SLUG
    db_session.flush()
    before = _private_outputs(db_session, scenario)
    assert ROBOTICS in [row["domain"] for row in before["csv"]]
    assert [a.domain for a in before["digest"]] == [ROBOTICS, sc.BLUEFIN]

    add_opt_out(db_session, ROBOTICS, now=sc.NOW)
    after = _private_outputs(db_session, scenario)
    assert after == before
    assert after["hubspot"][1]["company"]["properties"]["domain"] == ROBOTICS

    # The nudge itself still goes out to the watchlist's own webhook, account page link and all.
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        route = mock.post(PRIVATE).respond(200, text="ok")
        outcome = send_nudges(
            db_session, now=sc.NOW, private_webhooks={SLUG: PRIVATE}, sleep=lambda _: None
        )
    assert (outcome.candidates, outcome.sent, outcome.errors) == (2, 2, 0)
    bodies = [json.loads(call.request.content) for call in route.calls]
    robotics = next(b for b in bodies if b["text"].startswith("tell · Example Robotics"))
    pages = get_settings().tell_base_url.rstrip("/")
    assert robotics["blocks"][1]["elements"][0]["text"] == (
        f"<{pages}/companies/{ROBOTICS}.html|Account page>"
    )
