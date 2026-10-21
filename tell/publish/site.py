"""The GitHub Pages site (spec section 9): ``tell-data/site/``, rendered with Jinja.

:func:`render_site` writes, under ``out_dir/site``::

    index.html                       this week's top movers, weekly excerpt, methods, repo, health
    companies/{domain}.html          account page per published company
    weekly/{ISO-week}.html           the current weekly edition (the last complete ISO week)
    form-d-filings.html              the edition's Form D filings (ADR 0042, ADR 0044)
    health.html                      run-health, red banner when a source is stale
    style.css
    api/v0/companies/{domain}.json   the section 9 contract
    api/v0/weekly/latest.json        the current weekly edition
    api/v0/form-d-filings.json       the edition's Form D filings
    api/v0/watchlists/{slug}.json    public watchlists only
    api/v0/health.json

Published companies
===================

Every company the ``default`` profile scores (spec section 8) that has at least one
signal starting in the last :data:`SIZE_BUDGET_DAYS` (180) days and has not opted out of
the public site (``companies.public_opt_out``, ADR 0044): the spec section 14 size budget
prunes account pages of companies with no signal in 180 days. Pages and JSON files of
companies that drop out (either way) are deleted, as are JSON files of watchlists that are
no longer public; earlier weekly pages are kept (they are the archive), except that the
rows they show for an opted-out company are removed (:func:`scrub_archive`). The files
the retired "Rounds filed, not announced" output wrote (:data:`RETIRED_FILES`, ADR 0042)
are deleted whenever the Form D filings are written.

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
alone (ADR 0023). The page footer says how a company asks to be removed (the opt-out
issue, prefilled with the domain; ADR 0044), as does the Form D filings page's.

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
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from lxml import html as lxml_html
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.models import Company, Signal, Watchlist
from tell.publish.health import Health, compute_health
from tell.publish.json_api import (
    FILINGS_FILE,
    LOCAL,
    build_company_json,
    build_filings_json,
    build_health_json,
    build_watchlist_json,
    build_weekly_json,
    code_commit_sha,
    feedback_link,
    generated_by,
    prepare_companies,
    validate_company_json,
    validate_filings_json,
    validate_health_json,
    validate_site_tree,
    validate_watchlist_json,
    validate_weekly_json,
)
from tell.publish.opt_out import opt_out_link, opted_out_domains
from tell.publish.weekly import (
    FilingsData,
    IsoWeek,
    WeeklyData,
    build_weekly,
    form_d_filings,
)
from tell.signals.profiles import DEFAULT_PROFILE_NAME, Profile, get_profile
from tell.signals.scoring import ScoreResult, score_companies, score_instant

__all__ = [
    "FILINGS_METHOD_DOC",
    "FILINGS_PAGE",
    "METHOD_PAGES",
    "RETIRED_FILES",
    "RETIRED_JSON_LINK",
    "RETIRED_SECTION_ID",
    "SIZE_BUDGET_DAYS",
    "TEMPLATE_DIR",
    "TOP_MOVERS",
    "SiteResult",
    "environment",
    "filings_view",
    "page_context",
    "published_company_ids",
    "render_site",
    "scrub_archive",
    "weekly_view",
    "write_filings",
    "write_json",
]

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SIZE_BUDGET_DAYS = 180
TOP_MOVERS = 10
WEEKLY_EXCERPT = 5
MOVER_WINDOW = timedelta(days=7)

FILINGS_PAGE = "form-d-filings.html"
"""The edition's Form D filings, at the site root (ADR 0042, ADR 0044)."""
FILINGS_METHOD_DOC = "docs/methods/publishing.md#5a-form-d-filings"
"""Where the Form D filings list's method sentence links (path in the code repo)."""
RETIRED_FILES: tuple[str, ...] = ("rounds-not-announced.html", "api/v0/rounds-not-announced.json")
"""What "Rounds filed, not announced" (ADR 0042) published before ADR 0044 renamed and
reframed it, relative to ``site/``: deleted whenever the Form D filings are written. The JSON
would otherwise fail :func:`tell.publish.json_api.validate_site_tree` (no schema)."""
RETIRED_SECTION_ID = "rounds-not-announced"
"""The ``id`` of that list's section heading in a weekly page rendered before ADR 0044."""
RETIRED_JSON_LINK = (
    ' and <a href="../api/v0/rounds-not-announced.json">api/v0/rounds-not-announced.json</a>'
    " (its rounds filed, not announced)."
)
"""The end of that weekly page's JSON line, exactly as ADR 0042's template rendered it."""

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
    scrubbed: list[str] = field(default_factory=list)
    """Archived weekly editions rewritten by :func:`scrub_archive`, by ISO week."""


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
    """Companies with a signal starting in the 180 days to ``as_of`` (the size budget).

    A company that opted out of the public site (``companies.public_opt_out``, ADR 0044) is
    never published, whatever its signals.
    """
    t = score_instant(as_of)
    stmt = (
        select(Signal.company_id)
        .join(Company, Company.id == Signal.company_id)
        .where(
            Signal.starts_at <= t,
            Signal.starts_at > t - timedelta(days=SIZE_BUDGET_DAYS),
            Company.public_opt_out.is_(False),
        )
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


def filings_view(data: FilingsData) -> dict[str, Any]:
    """The Form D filings partial's view: the JSON shape, formatted amounts, the method link.

    An amount column is shown only when at least one row's payload states that amount (an
    indefinite offering counts and reads "indefinite"); a row without it reads "undisclosed".
    """
    view = data.to_dict()
    for row in view["filings"]:
        indefinite = row["amount_usd"] is None and row["amount_indefinite"]
        row["amount_text"] = "indefinite" if indefinite else _usd(row["amount_usd"])
        row["sold_text"] = _usd(row["amount_sold_usd"])
    view["show_offering"] = any(
        r["amount_usd"] is not None or r["amount_indefinite"] for r in view["filings"]
    )
    view["show_sold"] = any(r["amount_sold_usd"] is not None for r in view["filings"])
    repo = get_settings().tell_repo_url.rstrip("/")
    view["method_url"] = f"{repo}/blob/main/{FILINGS_METHOD_DOC}"
    return view


def write_filings(
    site: Path, env: Environment, data: FilingsData, *, as_of: date, sha: str | None
) -> dict[str, Any]:
    """Write ``api/v0/form-d-filings.json`` (validated first) and its page.

    Deletes the :data:`RETIRED_FILES` when present. Returns the view the weekly page
    includes for the same list.
    """
    obj = build_filings_json(data, sha=sha)
    validate_filings_json(obj)
    write_json(site / "api" / "v0" / FILINGS_FILE, obj)
    view = filings_view(data)
    _write(
        site / FILINGS_PAGE,
        env.get_template("filings.html").render(
            **page_context(as_of, sha, data.week.label, ""),
            digest=view,
            opt_out=opt_out_link(),
        ),
    )
    for retired in RETIRED_FILES:
        (site / retired).unlink(missing_ok=True)
    return view


def _scrubbed(text: str, domains: Collection[str]) -> str | None:
    """``text`` (a weekly page) without the rows of ``domains`` and the retired section.

    A row is the ``<li>`` or ``<tr>`` around a ``muted`` element whose whole text is one of
    the domains (every list on the weekly page prints the domain that way). The retired
    section is the heading :data:`RETIRED_SECTION_ID` and everything up to the next ``<h2>``,
    plus the :data:`RETIRED_JSON_LINK` clause. ``None`` when there is nothing to remove.
    """
    retired_link = RETIRED_JSON_LINK in text
    doc = lxml_html.document_fromstring(text.replace(RETIRED_JSON_LINK, "."))
    doomed: list[lxml_html.HtmlElement] = []
    for heading in doc.iter("h2"):
        if heading.get("id") != RETIRED_SECTION_ID:
            continue
        doomed.append(heading)
        sibling = heading.getnext()
        while isinstance(sibling, lxml_html.HtmlElement) and sibling.tag != "h2":
            doomed.append(sibling)
            sibling = sibling.getnext()
    for element in doc.iter("span", "td"):
        if not isinstance(element, lxml_html.HtmlElement):
            continue
        if "muted" not in (element.get("class") or "").split():
            continue
        if element.text_content().strip() not in domains:
            continue
        row = next(element.iterancestors("li", "tr"), None)
        if isinstance(row, lxml_html.HtmlElement):
            doomed.append(row)
    if not doomed and not retired_link:
        return None
    for element in doomed:
        if element.getparent() is not None:  # a row listed twice is dropped once
            element.drop_tree()
    body = str(lxml_html.tostring(doc, encoding="unicode", doctype="<!doctype html>"))
    return body if body.endswith("\n") else body + "\n"


def scrub_archive(weekly_dir: Path, current: str, domains: Collection[str]) -> list[str]:
    """Remove opted-out companies' rows from the archived weekly pages (ADR 0044).

    An archived edition (every ``weekly/*.html`` but ``current``) is never rendered again, so
    a company that opted out after it was published would stay named there. Each page that
    shows one of ``domains`` in a list row, or still carries the retired "Rounds filed, not
    announced" section, is rewritten without them; everything else on the page says what it
    said (heading counts included, character references may be re-encoded). Returns the ISO
    weeks rewritten.
    """
    rewritten: list[str] = []
    if not weekly_dir.is_dir():
        return rewritten
    for path in sorted(weekly_dir.glob("*.html")):
        if path.stem == current:
            continue
        text = path.read_text(encoding="utf-8")
        retired = f'id="{RETIRED_SECTION_ID}"' in text or RETIRED_JSON_LINK in text
        if not retired and not any(domain in text for domain in domains):
            continue
        scrubbed = _scrubbed(text, domains)
        if scrubbed is not None:
            path.write_text(scrubbed, encoding="utf-8")
            rewritten.append(path.stem)
    return rewritten


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
            opt_out=opt_out_link(company.domain),
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
    filings = form_d_filings(session, week, now=now, published=companies.keys())
    filings_page = write_filings(site, env, filings, as_of=as_of, sha=commit)
    _write(
        site / "weekly" / f"{week.label}.html",
        env.get_template("weekly.html").render(
            **page_context(as_of, commit, week.label, "../"), w=weekly_page, digest=filings_page
        ),
    )
    scrubbed = scrub_archive(site / "weekly", week.label, opted_out_domains(session))

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
        scrubbed=scrubbed,
    )
