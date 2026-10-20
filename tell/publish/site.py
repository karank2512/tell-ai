"""The GitHub Pages site (spec section 9): ``tell-data/site/``, rendered with Jinja.

:func:`render_site` writes, under ``out_dir/site``::

    index.html                       this week's top movers, weekly excerpt, methods, repo, health
    companies/{domain}.html          account page per published company
    weekly/{ISO-week}.html           the current weekly edition (the last complete ISO week)
    rounds-not-announced.html        the edition's rounds filed, not announced (ADR 0042)
    health.html                      run-health, red banner when a source is stale
    style.css
    api/v0/companies/{domain}.json   the section 9 contract
    api/v0/weekly/latest.json        the current weekly edition
    api/v0/rounds-not-announced.json the edition's rounds filed, not announced
    api/v0/watchlists/{slug}.json    public watchlists only
    api/v0/health.json

Published companies
===================

Every company the ``default`` profile scores (spec section 8) that has at least one
signal starting in the last :data:`SIZE_BUDGET_DAYS` (180) days: the spec section 14
size budget prunes account pages of companies with no signal in 180 days. Pages and JSON
files of companies that drop out are deleted, as are JSON files of watchlists that are no
longer public; earlier weekly pages are kept (they are the archive).

Account page
============

Score (raw sum, one decimal); a bar per contribution with its decayed value and the receipt
link beside it; the counted postings expanded under a velocity bar; long-open roles in
grey; a "why not higher" footer of signals that decayed to zero in the last 30 days; a
thumbs row per contribution linking to the feedback form prefilled with ``signal_id``
(``TELL_FEEDBACK_FORM_URL``) or, without one, to a prefilled GitHub issue. The
"Feedback on this page" link at the bottom is not a signal verdict, so it is always
a prefilled GitHub issue draft, form or no form. Heat is a percentile *within a
watchlist*, so the public page shows it only for the public watchlists that include
the company ("heat 87 in pilot"); a company on no public watchlist shows its score
alone (ADR 0023).

Determinism
===========

Same database, ``as_of``, ``now`` and commit sha → byte-identical files. Only
``generated_by`` (the commit) and ``as_of`` / ``now`` (health's ``generated_at``) vary
between runs on unchanged data. Every JSON object is validated before it is written and
the whole ``api/v0`` tree again at the end (:func:`tell.publish.json_api.validate_site_tree`);
the first failure raises.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.models import Company, Signal, Watchlist
from tell.publish.health import Health, compute_health
from tell.publish.json_api import (
    LOCAL,
    ROUNDS_FILE,
    build_company_json,
    build_health_json,
    build_rounds_json,
    build_watchlist_json,
    build_weekly_json,
    code_commit_sha,
    feedback_link,
    generated_by,
    prepare_companies,
    validate_company_json,
    validate_health_json,
    validate_rounds_json,
    validate_site_tree,
    validate_watchlist_json,
    validate_weekly_json,
)
from tell.publish.weekly import (
    IsoWeek,
    RoundsData,
    WeeklyData,
    build_weekly,
    rounds_not_announced,
)
from tell.signals.profiles import DEFAULT_PROFILE_NAME, Profile, get_profile
from tell.signals.scoring import ScoreResult, score_companies, score_instant

__all__ = [
    "METHOD_PAGES",
    "ROUNDS_METHOD_DOC",
    "ROUNDS_PAGE",
    "SIZE_BUDGET_DAYS",
    "TEMPLATE_DIR",
    "TOP_MOVERS",
    "SiteResult",
    "environment",
    "page_context",
    "published_company_ids",
    "render_site",
    "rounds_view",
    "weekly_view",
    "write_json",
    "write_rounds",
]

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SIZE_BUDGET_DAYS = 180
TOP_MOVERS = 10
WEEKLY_EXCERPT = 5
MOVER_WINDOW = timedelta(days=7)

ROUNDS_PAGE = "rounds-not-announced.html"
"""The edition's rounds filed, not announced, at the site root (ADR 0042)."""
ROUNDS_METHOD_DOC = "docs/methods/publishing.md#5a-rounds-filed-not-announced"
"""Where the rounds list's method sentence links (path in the code repo)."""

METHOD_PAGES: tuple[tuple[str, str], ...] = (
    ("Signals, decay and scoring", "docs/methods/scoring.md"),
    ("Hiring velocity", "docs/methods/hiring-velocity.md"),
    ("Entity resolution", "docs/methods/entity-resolution.md"),
    ("Publishing: layout, retention, digest timing", "docs/methods/publishing.md"),
    ("Static API contract", "docs/api/README.md"),
)


@dataclass(frozen=True)
class SiteResult:
    """What :func:`render_site` wrote."""

    site_dir: Path
    companies: list[str]
    removed: list[str]
    watchlists: list[str]
    weekly: str
    validated: int
    stale_sources: list[str] = field(default_factory=list)


# --- rendering helpers ------------------------------------------------------------------------


def _fmt_hours(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} h"


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _usd(value: int | None) -> str:
    return "undisclosed" if value is None else f"${value:,}"


def environment() -> Environment:
    """The Jinja environment: autoescape on, undefined variables raise."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["fmt_hours"] = _fmt_hours
    env.globals["fmt_rate"] = _fmt_rate
    return env


def write_json(path: Path, obj: Any) -> None:
    """Deterministic JSON file: insertion order, two-space indent, UTF-8, trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.write_text(text, encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _remove_others(directory: Path, suffix: str, keep: Iterable[str]) -> list[str]:
    """Delete ``directory/*{suffix}`` files whose stem is not in ``keep``; return the stems."""
    kept = set(keep)
    removed: list[str] = []
    if not directory.is_dir():
        return removed
    for path in sorted(directory.glob(f"*{suffix}")):
        stem = path.name[: -len(suffix)]
        if stem not in kept:
            path.unlink()
            removed.append(stem)
    return removed


# --- selection ----------------------------------------------------------------------------------


def published_company_ids(session: Session, as_of: date) -> list[int]:
    """Companies with a signal starting in the 180 days to ``as_of`` (the size budget)."""
    t = score_instant(as_of)
    stmt = (
        select(Signal.company_id)
        .where(Signal.starts_at <= t, Signal.starts_at > t - timedelta(days=SIZE_BUDGET_DAYS))
        .distinct()
        .order_by(Signal.company_id)
    )
    return [int(r[0]) for r in session.execute(stmt)]


def _resolve_profile(name: str, profiles: Mapping[str, Profile] | None) -> Profile:
    return profiles[name] if profiles is not None else get_profile(name)


# --- page contexts ------------------------------------------------------------------------------


def page_context(as_of: date, sha: str | None, weekly_label: str, root: str) -> dict[str, Any]:
    """Variables every page's ``base.html`` needs (``root`` is the relative path to site/)."""
    settings = get_settings()
    by = generated_by(sha)
    return {
        "root": root,
        "as_of": as_of.isoformat(),
        "repo_url": settings.tell_repo_url,
        "data_repo_url": settings.tell_data_repo_url,
        "weekly_label": weekly_label,
        "generated_by_url": settings.tell_repo_url if by == LOCAL else by,
        "generated_by_label": "a local build"
        if by == LOCAL
        else f"commit {sha[:12] if sha else ''}",
    }


def _bars(obj: Mapping[str, Any], result: ScoreResult) -> list[dict[str, Any]]:
    contributions = obj["contributions"]
    largest = max((float(c["contribution"]) for c in contributions), default=0.0)
    bars: list[dict[str, Any]] = []
    for shown, raw in zip(contributions, result.contributions, strict=True):
        signal_id = int(raw["signal_id"])
        value = float(shown["contribution"])
        width = round(100 * value / largest, 1) if largest > 0 else 0.0

        def link(verdict: str, signal_id: int = signal_id) -> str:
            return feedback_link(signal_id=signal_id, domain=str(obj["domain"]), verdict=verdict)

        bars.append(
            {
                "type": shown["type"],
                "role_family": shown.get("role_family", ""),
                "contribution": value,
                "width": width,
                "occurred_at": shown["occurred_at"],
                "summary": shown["summary"],
                "url": shown["url"],
                "postings": shown.get("postings", []),
                "useful": link("useful"),
                "not_useful": link("not_useful"),
                "already_knew": link("already_knew"),
            }
        )
    return bars


def weekly_view(data: WeeklyData, published: set[str]) -> dict[str, Any]:
    """The weekly template's view: the JSON shape plus page links and formatted amounts."""
    view = data.to_dict()
    for row in view["top_hiring"]:
        row["page"] = row["domain"] in published
    for row in view["top_funding"]:
        row["page"] = row["domain"] in published
        row["amount_text"] = _usd(row["amount_usd"])
        row["sold_text"] = _usd(row["amount_sold_usd"])
    return view


def rounds_view(data: RoundsData) -> dict[str, Any]:
    """The rounds partial's view: the JSON shape, formatted amounts and the method link.

    An amount column is shown only when at least one row's payload states that amount (an
    indefinite offering counts and reads "indefinite"); a row without it reads "undisclosed".
    """
    view = data.to_dict()
    for row in view["rounds"]:
        indefinite = row["amount_usd"] is None and row["amount_indefinite"]
        row["amount_text"] = "indefinite" if indefinite else _usd(row["amount_usd"])
        row["sold_text"] = _usd(row["amount_sold_usd"])
    view["show_offering"] = any(
        r["amount_usd"] is not None or r["amount_indefinite"] for r in view["rounds"]
    )
    view["show_sold"] = any(r["amount_sold_usd"] is not None for r in view["rounds"])
    repo = get_settings().tell_repo_url.rstrip("/")
    view["method_url"] = f"{repo}/blob/main/{ROUNDS_METHOD_DOC}"
    return view


def write_rounds(
    site: Path, env: Environment, data: RoundsData, *, as_of: date, sha: str | None
) -> dict[str, Any]:
    """Write ``api/v0/rounds-not-announced.json`` (validated first) and its page.

    Returns the view the weekly page includes for the same list.
    """
    obj = build_rounds_json(data, sha=sha)
    validate_rounds_json(obj)
    write_json(site / "api" / "v0" / ROUNDS_FILE, obj)
    view = rounds_view(data)
    _write(
        site / ROUNDS_PAGE,
        env.get_template("rounds.html").render(
            **page_context(as_of, sha, data.week.label, ""), rounds=view
        ),
    )
    return view


def _movers(
    session: Session,
    profile: Profile,
    as_of: date,
    companies: Mapping[int, Company],
    current: Mapping[int, ScoreResult],
) -> list[dict[str, Any]]:
    before = {
        r.company_id: r.score
        for r in score_companies(
            session, profile, as_of - MOVER_WINDOW, sorted(companies), write=False
        )
    }
    rows: list[dict[str, Any]] = []
    for cid, result in current.items():
        change = result.score - before.get(cid, 0.0)
        if change <= 0:
            continue
        top = result.contributions[0] if result.contributions else None
        rows.append(
            {
                "domain": companies[cid].domain,
                "name": companies[cid].name,
                "score": result.score,
                "change": change,
                "top_url": str(top["url"]) if top else "",
                "top_summary": str(top["summary"]) if top else "",
            }
        )
    rows.sort(key=lambda r: (-r["change"], r["domain"]))
    return rows[:TOP_MOVERS]


# --- the site ---------------------------------------------------------------------------------------


def _render_watchlists(
    session: Session,
    api: Path,
    *,
    as_of: date,
    profile: Profile,
    profiles: Mapping[str, Profile] | None,
    sha: str | None,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Write every public watchlist; return the slugs and ``{domain: [heat badges]}``."""
    public = list(
        session.scalars(
            select(Watchlist)
            .where(Watchlist.is_public.is_(True), Watchlist.slug.is_not(None))
            .order_by(Watchlist.slug)
        )
    )
    slugs: list[str] = []
    badges: dict[str, list[dict[str, Any]]] = {}
    for watchlist in public:
        obj = build_watchlist_json(session, watchlist, as_of, profiles=profiles, sha=sha)
        validate_watchlist_json(obj)
        write_json(api / "watchlists" / f"{obj['slug']}.json", obj)
        slugs.append(str(obj["slug"]))
        if obj["profile"] != profile.name:
            continue  # heat beside a default-profile score must come from the same profile
        for company in obj["companies"]:
            badges.setdefault(company["domain"], []).append(
                {"slug": obj["slug"], "name": obj["name"], "heat": company["heat"]}
            )
    _remove_others(api / "watchlists", ".json", slugs)
    return slugs, badges


def render_site(
    session: Session,
    out_dir: Path,
    *,
    as_of: date,
    profile: str = DEFAULT_PROFILE_NAME,
    now: datetime,
    profiles: Mapping[str, Profile] | None = None,
    sha: str | None = None,
    changelog: Path | None = None,
    health: Health | None = None,
) -> SiteResult:
    """Render the whole Pages tree under ``out_dir/site`` (see the module docstring).

    ``sha`` defaults to :func:`tell.publish.json_api.code_commit_sha`; ``profiles``
    (name → profile) defaults to the shipped and private profiles. Reads only; never
    writes the database.
    """
    prof = _resolve_profile(profile, profiles)
    commit = sha if sha is not None else code_commit_sha()
    site = out_dir / "site"
    api = site / "api" / "v0"
    env = environment()
    week = IsoWeek.containing(as_of).previous()

    ids = published_company_ids(session, as_of)
    companies = {
        c.id: c
        for c in session.scalars(
            select(Company).where(Company.id.in_(ids)).order_by(Company.domain)
        )
    }
    results = {r.company_id: r for r in score_companies(session, prof, as_of, ids, write=False)}
    slugs, badges = _render_watchlists(
        session, api, as_of=as_of, profile=prof, profiles=profiles, sha=commit
    )

    company_tpl = env.get_template("company.html")
    ordered = sorted(companies.values(), key=lambda c: c.domain)
    batch = prepare_companies(session, ordered, prof, as_of, results)
    published: list[str] = []
    for company in ordered:
        facts = batch.facts[company.id]
        obj = build_company_json(
            session,
            company,
            prof,
            as_of,
            result=results[company.id],
            sha=commit,
            facts=facts,
            batch=batch,
        )
        validate_company_json(obj)
        write_json(api / "companies" / f"{company.domain}.json", obj)
        page = company_tpl.render(
            **page_context(as_of, commit, week.label, "../"),
            c=obj,
            heats=badges.get(company.domain, []),
            bars=_bars(obj, results[company.id]),
            long_open=facts.long_open,
            feedback=feedback_link(
                signal_id=None, domain=company.domain, verdict="page", issue_only=True
            ),
        )
        _write(site / "companies" / f"{company.domain}.html", page)
        published.append(company.domain)
    removed = sorted(
        set(_remove_others(site / "companies", ".html", published))
        | set(_remove_others(api / "companies", ".json", published))
    )

    weekly = build_weekly(session, week, changelog=changelog)
    weekly_obj = build_weekly_json(weekly, sha=commit)
    validate_weekly_json(weekly_obj)
    write_json(api / "weekly" / "latest.json", weekly_obj)
    weekly_page = weekly_view(weekly, set(published))
    rounds = rounds_not_announced(session, week, now=now, published=companies.keys())
    rounds_page = write_rounds(site, env, rounds, as_of=as_of, sha=commit)
    _write(
        site / "weekly" / f"{week.label}.html",
        env.get_template("weekly.html").render(
            **page_context(as_of, commit, week.label, "../"), w=weekly_page, rounds=rounds_page
        ),
    )

    report = health or compute_health(session, now)
    health_obj = build_health_json(report, sha=commit)
    validate_health_json(health_obj)
    write_json(api / "health.json", health_obj)
    _write(
        site / "health.html",
        env.get_template("health.html").render(
            **page_context(as_of, commit, week.label, ""), h=health_obj
        ),
    )

    settings = get_settings()
    _write(
        site / "index.html",
        env.get_template("index.html").render(
            **page_context(as_of, commit, week.label, ""),
            profile=prof.name,
            movers=_movers(session, prof, as_of, companies, results),
            weekly=weekly_page,
            excerpt=WEEKLY_EXCERPT,
            methods=[
                {"title": title, "url": f"{settings.tell_repo_url.rstrip('/')}/blob/main/{path}"}
                for title, path in METHOD_PAGES
            ],
        ),
    )
    shutil.copyfile(TEMPLATE_DIR / "style.css", site / "style.css")
    validated = validate_site_tree(site)
    return SiteResult(
        site_dir=site,
        companies=published,
        removed=removed,
        watchlists=slugs,
        weekly=week.label,
        validated=validated,
        stale_sources=report.stale_sources,
    )
