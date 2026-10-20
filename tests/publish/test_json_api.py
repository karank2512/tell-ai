"""JSON API helpers: rounding, links, provenance, validation edge cases."""

from __future__ import annotations

import math
import random
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from sqlalchemy.orm import Session

from tell.collectors.base import Event, write_events
from tell.config import get_settings
from tell.models import AtsBoard, Company, Watchlist, WatchlistItem
from tell.publish import json_api
from tell.publish.json_api import (
    apportion_tenths,
    build_company_json,
    code_commit_sha,
    company_facts,
    company_facts_many,
    feedback_link,
    generated_by,
    intake_link,
    prepare_companies,
    round_half_up,
    validate_company_json,
    validate_health_json,
    validate_watchlist_json,
    validate_weekly_json,
)
from tell.publish.site import environment, page_context
from tell.signals.scoring import build_signals, score_companies, score_instant
from tests.publish import scenario as sc


def _company(**overrides: Any) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "domain": "acme.com",
        "name": "Acme",
        "as_of": "2026-10-06",
        "profile": "default",
        "score": 9.4,
        "contributions": [
            {
                "type": "funding",
                "contribution": 5.9,
                "occurred_at": "2026-09-29",
                "summary": "Form D: $18,000,000 offering, $18,000,000 sold",
                "url": "https://www.sec.gov/Archives/edgar/data/1/",
            },
            {
                "type": "hiring_velocity",
                "role_family": "sales",
                "contribution": 3.5,
                "occurred_at": "2026-10-03",
                "summary": "4 new sales roles in 7 days vs 0.5 baseline",
                "url": "https://boards.greenhouse.io/acme",
                "postings": [
                    {
                        "title": "Account Executive, Mid-Market",
                        "posted_at": "2026-10-01",
                        "url": "https://boards.greenhouse.io/acme/jobs/1",
                    }
                ],
            },
        ],
        "open_postings": 23,
        "long_open_roles": 2,
        "sources": {"ats": "greenhouse:acme", "edgar_cik": "0001234567", "news": True},
        "generated_by": "https://github.com/karank2512/tell-ai/commit/abc",
        "why_not_higher": [],
    }
    obj.update(overrides)
    return obj


def test_spec_example_validates_and_drift_fails() -> None:
    validate_company_json(_company())
    validate_company_json(_company(heat=87))
    for broken in (
        _company(heat=101),
        _company(score="9.4"),
        _company(extra=1),
        _company(generated_by="by hand"),
        _company(sources={"ats": "greenhouse:acme", "edgar_cik": "123", "news": True}),
    ):
        with pytest.raises(jsonschema.ValidationError):
            validate_company_json(broken)
    no_url = _company()
    del no_url["contributions"][0]["url"]
    with pytest.raises(jsonschema.ValidationError, match="url"):
        validate_company_json(no_url)
    empty_url = _company()
    empty_url["contributions"][1]["url"] = ""
    with pytest.raises(jsonschema.ValidationError):
        validate_company_json(empty_url)
    with pytest.raises(ValueError, match="non-finite"):
        validate_company_json(_company(score=math.nan))


def test_other_schemas_reject_drift() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_weekly_json({"week": "2026-W40"})
    with pytest.raises(jsonschema.ValidationError):
        validate_health_json({"sources": []})
    base = {
        "slug": "pilot",
        "name": "Pilot",
        "profile": "default",
        "as_of": "2026-10-06",
        "generated_by": "local",
    }
    validate_watchlist_json({**base, "companies": [_company(heat=50)]})
    with pytest.raises(jsonschema.ValidationError, match="no heat"):
        validate_watchlist_json({**base, "companies": [_company()]})
    with pytest.raises(jsonschema.ValidationError):
        validate_watchlist_json({**base, "slug": "Not A Slug", "companies": []})


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.25, 0.3), (0.35, 0.4), (2.449999, 2.4), (9.45, 9.5), (0.0, 0.0), (27.749, 27.7)],
)
def test_round_half_up(value: float, expected: float) -> None:
    assert round_half_up(value) == expected


def test_apportion_tenths_sums_exactly_to_the_rounded_total() -> None:
    assert apportion_tenths([], 0.0) == []
    assert apportion_tenths([5.94, 3.46], 9.4) == [5.9, 3.5]
    assert apportion_tenths([0.04, 0.04, 0.04], 0.12) == [0.1, 0.0, 0.0]
    assert apportion_tenths([1 / 3] * 3, 1.0) == [0.4, 0.3, 0.3]
    rng = random.Random(41)
    for _ in range(500):
        values = sorted((rng.uniform(0, 8) for _ in range(rng.randint(1, 9))), reverse=True)
        total = sum(values)
        shown = apportion_tenths(values, total)
        assert all(v >= 0 for v in shown)
        assert abs(sum(shown) - round_half_up(total)) < 1e-9
        assert all(abs(s - v) < 0.1 + 1e-9 for s, v in zip(shown, values, strict=True))


def test_feedback_link_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    issue = feedback_link(signal_id=7, domain="acme.com", verdict="useful")
    assert issue.startswith("https://github.com/karank2512/tell-ai/issues/new?")
    assert "signal+7+on+acme.com" in issue
    assert "mailto" not in issue
    page = feedback_link(signal_id=None, domain="acme.com", verdict="page")
    assert "Feedback+%28page%29%3A+acme.com" in page
    templated = feedback_link(
        signal_id=7,
        domain="acme.com",
        verdict="not useful",
        form_url="https://docs.google.com/forms/d/e/x/viewform?entry.1={signal_id}&entry.2={verdict}",
    )
    assert templated.endswith("entry.1=7&entry.2=not%20useful")
    appended = feedback_link(
        signal_id=7, domain="acme.com", verdict="useful", form_url="https://forms.example/f?a=1"
    )
    assert appended == "https://forms.example/f?a=1&signal_id=7&domain=acme.com&verdict=useful"
    monkeypatch.setenv("TELL_FEEDBACK_FORM_URL", "https://forms.example/f")
    get_settings.cache_clear()
    assert feedback_link(signal_id=1, domain="a.com", verdict="useful").startswith(
        "https://forms.example/f?signal_id=1"
    )
    # Page feedback is not a signal verdict: issue_only keeps the issue draft even with a form
    # configured, whether it comes from the environment or from the argument.
    assert feedback_link(signal_id=None, domain="acme.com", verdict="page", issue_only=True) == page
    assert (
        feedback_link(
            signal_id=None,
            domain="acme.com",
            verdict="page",
            form_url="https://forms.example/f?entry.1={signal_id}&entry.2={verdict}",
            issue_only=True,
        )
        == page
    )


def test_intake_link_and_generated_by(monkeypatch: pytest.MonkeyPatch) -> None:
    assert intake_link().endswith("/issues/new?template=watch-domains.yml")
    assert generated_by(None) == "local"
    assert generated_by("abc") == "https://github.com/karank2512/tell-ai/commit/abc"
    monkeypatch.setenv("TELL_INTAKE_FORM_URL", "https://forms.example/intake")
    monkeypatch.setenv("TELL_REPO_URL", "https://git.example/fork/")
    get_settings.cache_clear()
    assert intake_link() == "https://forms.example/intake"
    assert generated_by("abc") == "https://git.example/fork/commit/abc"


def test_code_commit_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", " deadbeef ")
    assert code_commit_sha() == "deadbeef"
    monkeypatch.delenv("GITHUB_SHA")
    monkeypatch.setattr("tell.publish.json_api.shutil.which", lambda _: None)
    assert code_commit_sha() is None
    monkeypatch.setattr("tell.publish.json_api.shutil.which", lambda _: "/usr/bin/git")

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="cafe\n", stderr="")

    monkeypatch.setattr("tell.publish.json_api.subprocess.run", fake_run)
    assert code_commit_sha() == "cafe"

    def failing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr("tell.publish.json_api.subprocess.run", failing)
    assert code_commit_sha() is None

    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("no git")

    monkeypatch.setattr("tell.publish.json_api.subprocess.run", missing)
    assert code_commit_sha() is None


@pytest.mark.db
@pytest.mark.parametrize(
    ("boards", "ats", "note"),
    [
        ([], "no_board", "no_board"),
        ([("greenhouse", "acme", "active")], "greenhouse:acme", "greenhouse:acme"),
        ([("workday", "acme.wd1", "unsupported")], "no_board", "unsupported:workday"),
        ([("lever", "acme", "dead")], "no_board", "dead:lever:acme"),
        ([("lever", "acme", "no_board")], "no_board", "no_board"),
    ],
)
def test_company_facts_coverage_notes(
    db_session: Session, boards: list[tuple[str, str, str]], ats: str, note: str
) -> None:
    company = Company(domain="acme.example", name="Acme", cik="1234")
    db_session.add(company)
    db_session.flush()
    for vendor, identifier, status in boards:
        db_session.add(
            AtsBoard(
                company_id=company.id,
                vendor=vendor,
                identifier=identifier,
                confidence=Decimal("0.9"),
                status=status,
            )
        )
    db_session.flush()
    facts = company_facts(db_session, company, score_instant(sc.AS_OF))
    assert (facts.ats, facts.coverage_note) == (ats, note)
    assert facts.edgar_cik == "0000001234"
    assert facts.news is False
    assert facts.open_postings == 0


def test_schema_file_is_where_the_docs_say() -> None:
    expected = Path(__file__).resolve().parents[2] / "docs" / "api" / "company.schema.json"
    assert expected == json_api.COMPANY_SCHEMA_PATH


@pytest.mark.db
def test_company_json_through_a_watchlist_with_a_decayed_signal(db_session: Session) -> None:
    profile = sc.profiles()["default"]
    company = Company(domain="decay.example", name="Decay Co")
    other = Company(domain="other.example", name="Other Co")
    db_session.add_all([company, other])
    db_session.flush()
    events = []
    for n, (target, days, kind) in enumerate(
        ((company, 190, "news_launch"), (company, 3, "news_expansion"), (other, 2, "news_launch"))
    ):
        occurred = sc.NOW - timedelta(days=days)
        events.append(
            Event(
                source="news",
                type=kind,
                external_id=f"decay-{n}",
                version="1",
                occurred_at=occurred,
                observed_at=occurred,
                url=f"https://news.example/decay-{n}",
                payload={"title": f"Headline {n}"},
                run_id="r",
                company_id=target.id,
            )
        )
    write_events(db_session, events)
    build_signals(db_session, now=sc.NOW, profile=profile)
    watchlist = Watchlist(owner="team-q", name="Q", slug="q", is_public=True)
    db_session.add(watchlist)
    db_session.flush()
    db_session.add_all(
        WatchlistItem(watchlist_id=watchlist.id, company_id=c.id) for c in (company, other)
    )
    db_session.flush()
    obj = build_company_json(db_session, company, profile, sc.AS_OF, watchlist, sha=None)
    validate_company_json(obj)
    assert obj["heat"] == 0  # expansion 1.0 decayed vs a fresh launch 1.5
    assert obj["generated_by"] == "local"
    assert obj["why_not_higher"] == [
        {
            "type": "launch",
            "decayed_at": "2026-09-26",
            "url": "https://news.example/decay-0",
            "summary": "Headline 0",
        }
    ]
    assert [c["type"] for c in obj["contributions"]] == ["expansion"]
    # The account page renders a decayed signal without a role family (StrictUndefined:
    # this crashed `tell publish` on the production copy, 2026-10-14).
    html = (
        environment()
        .get_template("company.html")
        .render(
            **page_context(sc.AS_OF, None, "2026-W40", "../"),
            c=obj,
            heats=[],
            bars=[],
            long_open=[],
            feedback="https://forms.example/feedback",
        )
    )
    assert "launch decayed to zero on 2026-09-26" in html
    # Batched facts and inputs match the one-company path exactly.
    batch = prepare_companies(
        db_session,
        [company, other],
        profile,
        sc.AS_OF,
        {
            r.company_id: r
            for r in score_companies(
                db_session, profile, sc.AS_OF, [company.id, other.id], write=False
            )
        },
    )
    t = score_instant(sc.AS_OF)
    assert batch.facts[company.id] == company_facts(db_session, company, t)
    assert batch.facts == company_facts_many(db_session, [other, company], t)
    batched = build_company_json(
        db_session, company, profile, sc.AS_OF, watchlist, sha=None, batch=batch
    )
    assert batched == obj
