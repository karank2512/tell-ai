"""Publish acceptance (spec section 12, test family "Publish"; PLAN Phase 8 gate).

A real scenario (``tests/publish/scenario.py``: greenhouse, edgar and news fixtures replayed
through their collectors, resolution, velocity, signals, ``default`` scoring, one public and
one private watchlist) is rendered to a temporary ``tell-data`` tree, then:

* every ``api/v0/companies/*.json`` validates against ``docs/api/company.schema.json``;
* every contribution has a non-empty URL, and the contributions add up to the score;
* no HTML has an unresolved template variable (``{{``, ``}}``, ``None``, ``nan``);
* the size budget holds (no page for a company signal-less for 180 days, stale pages go);
* the weekly, watchlist and health files have their documented shapes;
* the output is deterministic.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from freezegun import freeze_time
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.models import Run, Signal, Watchlist
from tell.publish import json_api
from tell.publish import site as site_module
from tell.publish.json_api import (
    COMPANY_SCHEMA_PATH,
    build_watchlist_json,
    company_schema,
    validate_company_json,
    validate_site_tree,
)
from tell.publish.site import SiteResult, render_site
from tests.publish import scenario as sc

pytestmark = pytest.mark.db

SHA = "0123456789abcdef0123456789abcdef01234567"
UNRESOLVED = re.compile(r"\{\{|\}\}|\{%|%\}|\bNone\b|\bnan\b|\bundefined\b", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _frozen() -> Iterator[None]:
    with freeze_time(sc.NOW):
        yield


@pytest.fixture
def scenario(db_session: Session) -> sc.Scenario:
    return sc.build(db_session)


def _render(session: Session, scenario: sc.Scenario, out: Path, **kwargs: Any) -> SiteResult:
    kwargs.setdefault("sha", SHA)
    return render_site(
        session, out, as_of=sc.AS_OF, now=sc.NOW, profiles=scenario.profiles, **kwargs
    )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- the four Publish gates ---------------------------------------------------------------


def test_every_company_json_validates_against_the_schema(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    result = _render(db_session, scenario, tmp_path)
    files = sorted((tmp_path / "site" / "api" / "v0" / "companies").glob("*.json"))
    assert [f.stem for f in files] == sorted(result.companies)
    assert {f.stem for f in files} == {sc.ROBOTICS, sc.BLUEFIN, sc.GREENHOUSE, sc.QUILLSTONE}
    schema = _load(COMPANY_SCHEMA_PATH)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    for path in files:
        obj = _load(path)
        assert list(validator.iter_errors(obj)) == [], path.name
        assert "heat" not in obj  # heat only through a watchlist
        assert obj["generated_by"] == f"https://github.com/karank2512/tell-ai/commit/{SHA}"
        assert obj["as_of"] == sc.AS_OF.isoformat()
    assert result.validated == validate_site_tree(tmp_path / "site")


def test_every_contribution_has_a_url_and_they_sum_to_the_score(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    _render(db_session, scenario, tmp_path)
    seen = 0
    for path in (tmp_path / "site" / "api" / "v0" / "companies").glob("*.json"):
        obj = _load(path)
        for item in obj["contributions"]:
            assert item["url"].startswith("https://"), path.name
            for posting in item.get("postings", []):
                assert posting["url"].startswith("https://")
            seen += 1
        total = sum(item["contribution"] for item in obj["contributions"])
        assert abs(total - obj["score"]) < 1e-6, path.name
        for decayed in obj["why_not_higher"]:
            assert decayed["url"]
    assert seen >= 8


def test_html_has_no_unresolved_template_variables(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    _render(db_session, scenario, tmp_path)
    pages = sorted((tmp_path / "site").rglob("*.html"))
    assert {p.relative_to(tmp_path / "site").as_posix() for p in pages} >= {
        "index.html",
        "health.html",
        "weekly/2026-W40.html",
        f"companies/{sc.ROBOTICS}.html",
    }
    for page in pages:
        found = UNRESOLVED.findall(page.read_text(encoding="utf-8"))
        assert found == [], (page.name, found)


def test_size_budget_prunes_companies_signal_less_for_180_days(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    for stale in (
        site / "companies" / f"{sc.OLDCO}.html",
        site / "companies" / "gone.example.html",
    ):
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("old page")
    old_json = site / "api" / "v0" / "companies" / "gone.example.json"
    old_json.parent.mkdir(parents=True, exist_ok=True)
    old_json.write_text("{}")
    old_watchlist = site / "api" / "v0" / "watchlists" / "retired.json"
    old_watchlist.parent.mkdir(parents=True, exist_ok=True)
    old_watchlist.write_text("{}")
    old_weekly = site / "weekly" / "2026-W39.html"
    old_weekly.parent.mkdir(parents=True, exist_ok=True)
    old_weekly.write_text("archived edition")

    result = _render(db_session, scenario, tmp_path)
    assert sc.OLDCO not in result.companies  # its only signal is 200 days old
    assert sc.OLDCO in scenario.companies
    assert not (site / "companies" / f"{sc.OLDCO}.html").exists()
    assert not (site / "companies" / "gone.example.html").exists()
    assert not old_json.exists()
    assert not old_watchlist.exists()
    assert result.removed == ["gone.example", sc.OLDCO]
    assert old_weekly.read_text() == "archived edition"  # weekly pages are the archive


# --- shapes ----------------------------------------------------------------------------------


def test_company_json_contract_values(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    _render(db_session, scenario, tmp_path)
    api = tmp_path / "site" / "api" / "v0" / "companies"
    robotics = _load(api / f"{sc.ROBOTICS}.json")
    assert list(robotics) == [
        "domain",
        "name",
        "as_of",
        "profile",
        "score",
        "contributions",
        "open_postings",
        "long_open_roles",
        "sources",
        "generated_by",
        "why_not_higher",
    ]
    assert robotics["sources"] == {"ats": "no_board", "edgar_cik": "0009990001", "news": False}
    (funding,) = robotics["contributions"]
    assert funding["type"] == "funding"
    assert funding["occurred_at"] == "2026-10-01"
    assert funding["summary"] == "Form D: $18,000,000 offering, $12,500,000 sold"
    assert funding["url"].startswith("https://www.sec.gov/Archives/edgar/data/9990001/")
    assert "role_family" not in funding
    assert "postings" not in funding

    board = _load(api / f"{sc.GREENHOUSE}.json")
    assert board["sources"]["ats"] == "greenhouse:tell-fixture"
    assert board["open_postings"] == 5
    assert board["long_open_roles"] == 2
    (velocity,) = board["contributions"]
    assert velocity["type"] == "hiring_velocity"
    assert velocity["role_family"] == "sales"
    assert [p["title"] for p in velocity["postings"]] == list(sc.SPIKE_TITLES)
    assert [p["posted_at"] for p in velocity["postings"]] == [
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
    ]

    news = _load(api / f"{sc.QUILLSTONE}.json")
    assert news["sources"]["news"] is True
    values = [c["contribution"] for c in news["contributions"]]
    assert values == sorted(values, reverse=True)


def test_public_watchlist_json_has_heat_and_no_owner(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    result = _render(db_session, scenario, tmp_path)
    assert result.watchlists == ["pilot"]
    path = tmp_path / "site" / "api" / "v0" / "watchlists" / "pilot.json"
    obj = _load(path)
    assert set(obj) == {"slug", "name", "profile", "as_of", "generated_by", "companies"}
    assert obj["name"] == "Pilot list"
    assert "team-a" not in path.read_text()
    assert [c["domain"] for c in obj["companies"]] == [sc.ROBOTICS, sc.QUILLSTONE, sc.GREENHOUSE]
    assert [c["heat"] for c in obj["companies"]] == [100, 50, 0]
    for company in obj["companies"]:
        validate_company_json(company)
    page = (tmp_path / "site" / "companies" / f"{sc.ROBOTICS}.html").read_text()
    assert "heat 100 in" in page
    bluefin_page = (tmp_path / "site" / "companies" / f"{sc.BLUEFIN}.html").read_text()
    assert "heat" not in bluefin_page.split("<h2>")[0].split("</h1>")[1]
    assert not (tmp_path / "site" / "api" / "v0" / "watchlists" / "private-list.json").exists()
    with pytest.raises(ValueError, match="not public"):
        build_watchlist_json(db_session, scenario.private, sc.AS_OF, profiles=scenario.profiles)


def test_weekly_json_shape(db_session: Session, scenario: sc.Scenario, tmp_path: Path) -> None:
    result = _render(db_session, scenario, tmp_path)
    assert result.weekly == "2026-W40"
    weekly = _load(tmp_path / "site" / "api" / "v0" / "weekly" / "latest.json")
    assert weekly["week"] == "2026-W40"
    assert (weekly["start"], weekly["end"]) == ("2026-09-28", "2026-10-04")
    assert [r["domain"] for r in weekly["top_hiring"]] == [sc.GREENHOUSE]
    assert weekly["top_hiring"][0]["velocity"] == 3
    assert len(weekly["top_hiring"][0]["receipts"][0]["postings"]) == 3
    assert [(r["domain"], r["amount_usd"]) for r in weekly["top_funding"]] == [
        (sc.ROBOTICS, 18_000_000),
        (sc.BLUEFIN, None),
    ]
    assert weekly["coverage"] == {"companies_monitored": 3, "boards_polled": 0, "filings_read": 2}
    assert weekly["repo_url"] == "https://github.com/karank2512/tell-ai"
    assert weekly["method_change"]
    page = (tmp_path / "site" / "weekly" / "2026-W40.html").read_text()
    assert "$18,000,000" in page
    assert "What changed in the method" in page


def test_form_d_filings_files(db_session: Session, scenario: sc.Scenario, tmp_path: Path) -> None:
    _render(db_session, scenario, tmp_path)
    site = tmp_path / "site"
    filings = _load(site / "api" / "v0" / "form-d-filings.json")
    assert (filings["week"], filings["filed_from"], filings["filed_to"], filings["as_of"]) == (
        "2026-W40",
        "2026-09-21",
        "2026-10-04",
        "2026-10-06",
    )
    # Both Form D filings of 2026-10-01; news is not read (ADR 0044).
    assert [(r["domain"], r["amount_usd"], r["amount_indefinite"]) for r in filings["filings"]] == [
        (sc.BLUEFIN, None, True),
        (sc.ROBOTICS, 18_000_000, False),
    ]
    for row in filings["filings"]:
        assert row["url"].startswith("https://www.sec.gov/Archives/edgar/data/")
    page = (site / "form-d-filings.html").read_text()
    weekly = (site / "weekly" / "2026-W40.html").read_text()
    assert '<a href="companies/bluefin.example.html">Bluefin Data Systems</a>' in page
    assert '<a href="../companies/bluefin.example.html">Bluefin Data Systems</a>' in weekly
    assert '<h2 id="form-d-filings">Form D filings</h2>' in weekly
    assert '<a href="form-d-filings.html">Form D filings</a>' in (site / "index.html").read_text()
    section = weekly.split('id="form-d-filings"')[1].split("<h2>")[0]
    text = page + section + json.dumps(filings)
    # Nothing about press, and nothing about watchlists (names, owners, membership).
    for word in ("press", "news", "headline", "announc", "stealth"):
        assert word not in text.lower(), word
    for private in ("Pilot list", "Private list", "pilot", "team-a", "team-b", "heat"):
        assert private not in text
    # The retired name survives only as the root redirect stub; its JSON is gone.
    assert list(site.rglob("rounds-not-announced*")) == [site / site_module.RETIRED_PAGE]
    assert (site / site_module.RETIRED_PAGE).read_text() == site_module.RETIRED_PAGE_STUB


def test_opt_out_line_on_every_account_page_and_nowhere_else(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    result = _render(db_session, scenario, tmp_path)
    site = tmp_path / "site"
    line = "A company that would rather not appear on this site can ask to be removed: "
    for domain in result.companies:
        page = (site / "companies" / f"{domain}.html").read_text()
        footer = page.split('<footer class="foot">')[1]
        assert line in footer, domain
        assert "titled 'Opt out: &lt;domain&gt;'. The next publish removes it.</p>" in footer, (
            domain
        )
        assert _href(page, "open a GitHub issue") == (
            "https://github.com/karank2512/tell-ai/issues/new?"
            f"template=opt-out.md&title=Opt+out%3A+{domain}"
        )
    assert line in (site / "form-d-filings.html").read_text()
    for other in ("index.html", "health.html", "weekly/2026-W40.html"):
        assert line not in (site / other).read_text(), other


def test_health_json_fields(db_session: Session, scenario: sc.Scenario, tmp_path: Path) -> None:
    # The scenario's news replay would be Quillstone's first Google News query, which is a first
    # look, not tell's lag (ADR 0043); a query the day before is on record here.
    earlier = datetime(2026, 10, 4, 20, 17, tzinfo=UTC)
    db_session.add(
        Run(
            run_id="news-20261004T201700-test01",
            source="news",
            started_at=earlier,
            finished_at=earlier + timedelta(minutes=2),
            status="ok",
            notes={"polled": {str(scenario.companies[sc.QUILLSTONE]): earlier.isoformat()}},
        )
    )
    db_session.flush()
    _render(db_session, scenario, tmp_path)
    health = _load(tmp_path / "site" / "api" / "v0" / "health.json")
    assert set(health) == {
        "generated_at",
        "stale_sources",
        "freshness_p95_h",
        "sources",
        "generated_by",
    }
    by_source = {row["source"]: row for row in health["sources"]}
    assert set(by_source) >= {"greenhouse", "edgar", "news", "resolve", "score", "publish"}
    for row in health["sources"]:
        assert set(row) == {
            "source",
            "kind",
            "last_ok_at",
            "success_rate_7d",
            "fetched_24h",
            "inserted_24h",
            "freshness_p95_h",
            "stale",
            "status",
        }
    news = by_source["news"]
    assert news["status"] == "ok"
    assert news["stale"] is False
    assert news["success_rate_7d"] == 1.0
    assert news["fetched_24h"] > 0
    # Every item is dated before that query, which did not list it: 24 h since, not its age.
    assert news["freshness_p95_h"] == 24.0
    assert by_source["greenhouse"]["stale"] is True  # last success 2026-09-25
    assert health["stale_sources"] == ["edgar", "greenhouse"]
    assert by_source["ashby"]["status"] == "never_run"
    page = (tmp_path / "site" / "health.html").read_text()
    assert 'class="banner stale"' in page
    assert "Stale: edgar, greenhouse" in page


def _href(page: str, label: str) -> str:
    """The one link on ``page`` whose text is ``label``, with HTML entities decoded."""
    (href,) = re.findall(rf'<a href="([^"]*)">{re.escape(label)}</a>', page)
    return html.unescape(href)


def test_account_page_feedback_link_is_an_issue_draft_even_with_a_form(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Page feedback is not a signal verdict. With the form configured, the bottom link used
    # to open the form with no signal id and a verdict ("page") that is not one of its
    # options, so the form looked blank and `tell intake feedback-sync` rejected the row.
    monkeypatch.setenv(
        "TELL_FEEDBACK_FORM_URL",
        "https://forms.example/f?entry.1={signal_id}&entry.2={domain}&entry.3={verdict}",
    )
    get_settings.cache_clear()
    _render(db_session, scenario, tmp_path)
    page = (tmp_path / "site" / "companies" / f"{sc.ROBOTICS}.html").read_text()
    (signal_id,) = db_session.scalars(
        select(Signal.id).where(Signal.company_id == scenario.companies[sc.ROBOTICS])
    )

    assert _href(page, "Feedback on this page") == (
        "https://github.com/karank2512/tell-ai/issues/new?"
        f"title=Feedback+%28page%29%3A+{sc.ROBOTICS}&labels=feedback"
    )
    # The thumbs row beside the contribution still opens the form, filled in.
    form = f"https://forms.example/f?entry.1={signal_id}&entry.2={sc.ROBOTICS}&entry.3="
    assert _href(page, "yes") == form + "useful"
    assert _href(page, "no") == form + "not_useful"
    assert _href(page, "already knew") == form + "already_knew"


# --- determinism and validation ----------------------------------------------------------------


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_render_is_deterministic(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    _render(db_session, scenario, tmp_path / "a")
    _render(db_session, scenario, tmp_path / "b")
    first, second = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    assert first == second
    _render(db_session, scenario, tmp_path / "c", sha="f" * 40)
    third = _tree(tmp_path / "c")
    assert set(third) == set(first)
    for name, content in first.items():
        expected = content.replace(SHA.encode(), b"f" * 40).replace(SHA[:12].encode(), b"f" * 12)
        assert third[name] == expected, name


def test_local_build_says_local(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(site_module, "code_commit_sha", lambda: None)
    render_site(db_session, tmp_path, as_of=sc.AS_OF, now=sc.NOW, profiles=scenario.profiles)
    obj = _load(tmp_path / "site" / "api" / "v0" / "companies" / f"{sc.ROBOTICS}.json")
    assert obj["generated_by"] == "local"
    assert "a local build" in (tmp_path / "site" / "index.html").read_text()


def test_generator_raises_on_the_first_invalid_file(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = json_api.build_company_json

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        obj = real(*args, **kwargs)
        for item in obj["contributions"]:
            item["url"] = ""
        return obj

    monkeypatch.setattr("tell.publish.site.build_company_json", broken)
    with pytest.raises(jsonschema.ValidationError):
        _render(db_session, scenario, tmp_path)


def test_site_tree_validation_names_the_bad_file(
    db_session: Session, scenario: sc.Scenario, tmp_path: Path
) -> None:
    _render(db_session, scenario, tmp_path)
    site = tmp_path / "site"
    bad = site / "api" / "v0" / "companies" / f"{sc.ROBOTICS}.json"
    obj = _load(bad)
    del obj["contributions"][0]["url"]
    bad.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match=re.escape(bad.name)):
        validate_site_tree(site)
    bad.unlink()
    stray = site / "api" / "v0" / "stray.json"
    stray.write_text("{}")
    with pytest.raises(ValueError, match="no schema"):
        validate_site_tree(site)


def test_company_schema_is_a_valid_2020_12_schema() -> None:
    schema = company_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "why_not_higher" in schema["required"]
    contribution = schema["$defs"]["contribution"]
    assert "url" in contribution["required"]


# --- watchlists.slug / is_public (migration 0006, ADR 0022) -----------------------------------------


def test_watchlist_public_columns_default_private_and_slug_unique(db_session: Session) -> None:
    db_session.execute(
        text("INSERT INTO watchlists (owner, name) VALUES ('team-c', 'Defaults list')")
    )
    row = db_session.scalars(select(Watchlist).where(Watchlist.name == "Defaults list")).one()
    assert row.is_public is False
    assert row.slug is None
    db_session.add_all(
        [
            Watchlist(owner="team-d", name="One", slug="same", is_public=True),
            Watchlist(owner="team-e", name="Two", slug="same", is_public=True),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
