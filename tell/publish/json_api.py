"""The static JSON API v0 (spec sections 9 and 10): builders, schemas, validation.

``api/v0/companies/{domain}.json`` is the contract every other surface reads;
:func:`build_company_json` produces exactly the section 9 object plus ``why_not_higher``
(signals that decayed to zero in the last 30 days), and ``docs/api/company.schema.json``
(JSON Schema 2020-12) is its machine-readable form. ``docs/api/README.md`` documents it.

Numbers
=======

``score`` is the raw sum of decayed contributions rounded half-up to one decimal. Each
contribution is rounded to one decimal by largest-remainder apportionment
(:func:`apportion_tenths`), so the contributions a reader adds up with a calculator equal
the displayed score exactly (spec section 16 M2), never off by a rounding tenth.

Provenance
==========

``generated_by`` is ``{TELL_REPO_URL}/commit/{sha}`` for the code commit that produced the
file, the sha taken from ``GITHUB_SHA`` (Actions) or ``git rev-parse HEAD``; ``local``
when neither is available.

Validation
==========

:func:`validate_company_json` checks one object against the company schema; the weekly,
watchlist and health files have their own (smaller) schemas here
(:data:`WEEKLY_SCHEMA`, :data:`WATCHLIST_SCHEMA`, :data:`HEALTH_SCHEMA`). The site
generator validates every object before writing it and :func:`validate_site_tree`
re-validates every file under ``site/api/v0`` before a push; both raise on the first
failure.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import parse_iso
from tell.config import get_settings
from tell.models import (
    AtsBoard,
    Company,
    CompanyFeed,
    EventRow,
    Posting,
    Signal,
    Watchlist,
    WatchlistItem,
)
from tell.publish.health import Health
from tell.publish.weekly import WeeklyData, velocity_postings
from tell.signals.profiles import Profile, get_profile
from tell.signals.scoring import (
    ScoreResult,
    heat,
    score_companies,
    score_instant,
    why_not_higher,
    why_not_higher_many,
)
from tell.signals.velocity import HIRING_VELOCITY, LongOpenRole, long_open_roles_many

__all__ = [
    "COMPANY_SCHEMA_PATH",
    "HEALTH_SCHEMA",
    "LOCAL",
    "WATCHLIST_SCHEMA",
    "WEEKLY_SCHEMA",
    "CompanyBatch",
    "CompanyFacts",
    "apportion_tenths",
    "build_company_json",
    "build_health_json",
    "build_watchlist_json",
    "build_weekly_json",
    "code_commit_sha",
    "company_facts",
    "company_facts_many",
    "company_schema",
    "feedback_link",
    "generated_by",
    "intake_link",
    "prepare_companies",
    "round_half_up",
    "validate_company_json",
    "validate_health_json",
    "validate_site_tree",
    "validate_watchlist_json",
    "validate_weekly_json",
    "watchlist_profile",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANY_SCHEMA_PATH = REPO_ROOT / "docs" / "api" / "company.schema.json"
"""The published schema; ``tell`` runs from its checkout (it is never shipped as a wheel)."""

LOCAL = "local"
"""``generated_by`` of a build with no known commit."""

_DATE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
_URL = {"type": "string", "minLength": 1, "pattern": "^https?://\\S+$"}
_USD = {"type": ["integer", "null"], "minimum": 0}
_GENERATED_BY = {"type": "string", "pattern": "^(local|https?://\\S+)$"}

WEEKLY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "tell weekly (api/v0/weekly/latest.json)",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "week",
        "start",
        "end",
        "top_hiring",
        "top_funding",
        "method_change",
        "coverage",
        "repo_url",
        "generated_by",
    ],
    "properties": {
        "week": {"type": "string", "pattern": "^[0-9]{4}-W[0-9]{2}$"},
        "start": _DATE,
        "end": _DATE,
        "top_hiring": {
            "type": "array",
            "maxItems": 25,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["domain", "name", "velocity", "receipts"],
                "properties": {
                    "domain": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "velocity": {"type": "integer", "minimum": 0},
                    "receipts": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["occurred_at", "url", "summary", "postings"],
                            "properties": {
                                "occurred_at": _DATE,
                                "url": _URL,
                                "summary": {"type": "string", "minLength": 1},
                                "postings": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["title", "posted_at", "url"],
                                        "properties": {
                                            "title": {"type": "string"},
                                            "posted_at": _DATE,
                                            "url": _URL,
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        "top_funding": {
            "type": "array",
            "maxItems": 25,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "domain",
                    "name",
                    "amount_usd",
                    "amount_sold_usd",
                    "date_filed",
                    "url",
                ],
                "properties": {
                    "domain": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "amount_usd": _USD,
                    "amount_sold_usd": _USD,
                    "date_filed": _DATE,
                    "url": _URL,
                },
            },
        },
        "method_change": {"type": "string", "minLength": 1},
        "coverage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["companies_monitored", "boards_polled", "filings_read"],
            "properties": {
                "companies_monitored": {"type": "integer", "minimum": 0},
                "boards_polled": {"type": "integer", "minimum": 0},
                "filings_read": {"type": "integer", "minimum": 0},
            },
        },
        "repo_url": _URL,
        "generated_by": _GENERATED_BY,
    },
}

HEALTH_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "tell run-health (api/v0/health.json)",
    "type": "object",
    "additionalProperties": False,
    "required": ["generated_at", "stale_sources", "freshness_p95_h", "sources", "generated_by"],
    "properties": {
        "generated_at": {"type": "string", "minLength": 20},
        "stale_sources": {"type": "array", "items": {"type": "string"}},
        "freshness_p95_h": {"type": ["number", "null"], "minimum": 0},
        "generated_by": _GENERATED_BY,
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source",
                    "kind",
                    "last_ok_at",
                    "success_rate_7d",
                    "fetched_24h",
                    "inserted_24h",
                    "freshness_p95_h",
                    "stale",
                    "status",
                ],
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "kind": {"enum": ["collector", "job"]},
                    "last_ok_at": {"type": ["string", "null"]},
                    "success_rate_7d": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "fetched_24h": {"type": "integer", "minimum": 0},
                    "inserted_24h": {"type": "integer", "minimum": 0},
                    "freshness_p95_h": {"type": ["number", "null"], "minimum": 0},
                    "stale": {"type": "boolean"},
                    "status": {"enum": ["ok", "stale", "never_run"]},
                },
            },
        },
    },
}

WATCHLIST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "tell public watchlist (api/v0/watchlists/{slug}.json)",
    "type": "object",
    "additionalProperties": False,
    "required": ["slug", "name", "profile", "as_of", "generated_by", "companies"],
    "properties": {
        "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "name": {"type": "string", "minLength": 1},
        "profile": {"type": "string", "minLength": 1},
        "as_of": _DATE,
        "generated_by": _GENERATED_BY,
        "companies": {"type": "array", "items": {"type": "object"}},
    },
}
"""Each entry of ``companies`` is a company object with ``heat`` (validated separately)."""


# --- schemas and validation ------------------------------------------------------------------


@lru_cache(maxsize=1)
def company_schema() -> dict[str, Any]:
    """``docs/api/company.schema.json``, parsed and checked as a 2020-12 schema."""
    schema: dict[str, Any] = json.loads(COMPANY_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _validate(obj: Any, schema: Mapping[str, Any]) -> None:
    validator = jsonschema.Draft202012Validator(dict(schema))
    error = jsonschema.exceptions.best_match(validator.iter_errors(obj))
    if error is not None:
        raise error


def _reject_non_finite(obj: Any, where: str = "$") -> None:
    if isinstance(obj, float) and not math.isfinite(obj):
        msg = f"{where}: non-finite number {obj!r}"
        raise ValueError(msg)
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            _reject_non_finite(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _reject_non_finite(value, f"{where}[{index}]")


def validate_company_json(obj: Any) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``obj`` matches the company schema.

    Also rejects ``NaN`` / ``Infinity`` anywhere (``ValueError``): JSON has no such numbers.
    """
    _reject_non_finite(obj)
    _validate(obj, company_schema())


def validate_weekly_json(obj: Any) -> None:
    """Raise unless ``obj`` matches :data:`WEEKLY_SCHEMA`."""
    _reject_non_finite(obj)
    _validate(obj, WEEKLY_SCHEMA)


def validate_health_json(obj: Any) -> None:
    """Raise unless ``obj`` matches :data:`HEALTH_SCHEMA`."""
    _reject_non_finite(obj)
    _validate(obj, HEALTH_SCHEMA)


def validate_watchlist_json(obj: Any) -> None:
    """Raise unless ``obj`` matches :data:`WATCHLIST_SCHEMA` and every company validates."""
    _reject_non_finite(obj)
    _validate(obj, WATCHLIST_SCHEMA)
    for company in obj["companies"]:
        validate_company_json(company)
        if "heat" not in company:
            msg = f"watchlist {obj['slug']!r}: company {company.get('domain')!r} has no heat"
            raise jsonschema.ValidationError(msg)


def validate_site_tree(site_dir: Path) -> int:
    """Validate every JSON file under ``site_dir/api/v0``; return how many were checked.

    Raises on the first failure, naming the file. A JSON file in an unknown place is an
    error too: nothing unvalidated is pushed.
    """
    root = site_dir / "api" / "v0"
    checked = 0
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        rel = path.relative_to(root).as_posix()
        obj = json.loads(path.read_text(encoding="utf-8"))
        try:
            if rel.startswith("companies/"):
                validate_company_json(obj)
            elif rel.startswith("watchlists/"):
                validate_watchlist_json(obj)
            elif rel.startswith("weekly/"):
                validate_weekly_json(obj)
            elif rel == "health.json":
                validate_health_json(obj)
            else:
                msg = "no schema for this path"
                raise ValueError(msg)
        except (jsonschema.ValidationError, ValueError) as exc:
            msg = f"{path}: {exc}"
            raise ValueError(msg) from exc
        checked += 1
    return checked


# --- provenance and links ---------------------------------------------------------------------


def code_commit_sha() -> str | None:
    """``GITHUB_SHA`` when set, else ``git rev-parse HEAD`` in the checkout, else ``None``."""
    env = os.environ.get("GITHUB_SHA", "").strip()
    if env:
        return env
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # Fixed argv (absolute git path, constant arguments); no shell, no user input.
        out = subprocess.run(  # noqa: S603
            [git, "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def generated_by(sha: str | None) -> str:
    """``{TELL_REPO_URL}/commit/{sha}``, or ``local`` without a sha."""
    if not sha:
        return LOCAL
    return f"{get_settings().tell_repo_url.rstrip('/')}/commit/{sha}"


def feedback_link(
    *,
    signal_id: int | None,
    domain: str,
    verdict: str,
    form_url: str | None = None,
    watchlist: str | None = None,
) -> str:
    """Where a thumbs click goes: the prefilled feedback form, else a GitHub issue draft.

    ``TELL_FEEDBACK_FORM_URL`` is a Google Form prefilled link; ``{signal_id}``,
    ``{domain}``, ``{verdict}`` and ``{watchlist}`` (the slug, known to the nudge only) in
    it are replaced (URL-encoded). A form URL without placeholders gets them appended as
    query parameters. Without a form the link opens a new issue on the code repo with a
    prefilled title, never a ``mailto:``.
    """
    form = form_url if form_url is not None else get_settings().tell_feedback_form_url
    values = {
        "signal_id": "" if signal_id is None else str(signal_id),
        "domain": domain,
        "verdict": verdict,
        "watchlist": watchlist or "",
    }
    if form:
        if any(f"{{{key}}}" in form for key in values):
            out = form
            for key, value in values.items():
                out = out.replace(f"{{{key}}}", quote(value, safe=""))
            return out
        joiner = "&" if "?" in form else "?"
        appended = {k: v for k, v in values.items() if k != "watchlist" or v}
        return form + joiner + urlencode(appended)
    repo = get_settings().tell_repo_url.rstrip("/")
    subject = f"signal {values['signal_id']} on {domain}" if signal_id is not None else domain
    title = f"Feedback ({verdict}): {subject}"
    return f"{repo}/issues/new?" + urlencode({"title": title, "labels": "feedback"})


def intake_link() -> str:
    """The watchlist intake form (``TELL_INTAKE_FORM_URL``), else the repo's issue form."""
    form = get_settings().tell_intake_form_url
    if form:
        return form
    return f"{get_settings().tell_repo_url.rstrip('/')}/issues/new?template=watch-domains.yml"


# --- numbers --------------------------------------------------------------------------------------


def round_half_up(value: float, places: int = 1) -> float:
    """``value`` rounded half away from zero (on its shortest repr) to ``places`` decimals."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))


def apportion_tenths(values: Sequence[float], total: float) -> list[float]:
    """Round ``values`` to tenths so they sum exactly to ``total`` rounded to tenths.

    Largest remainder: every value is floored to a tenth, then the leftover tenths go to
    the values with the largest remainders (earlier entries first on ties). ``total`` is
    the exact (unrounded) sum of ``values``; no result is negative.
    """
    if not values:
        return []
    target = int(Decimal(repr(round_half_up(total))) * 10)
    scaled = [max(float(v), 0.0) * 10 for v in values]
    floors = [math.floor(s + 1e-9) for s in scaled]
    leftover = target - sum(floors)
    order = sorted(range(len(values)), key=lambda i: (-(scaled[i] - floors[i]), i))
    index = 0
    while leftover > 0 and order:
        floors[order[index % len(order)]] += 1
        leftover -= 1
        index += 1
    while leftover < 0:  # only reachable through float noise; take from the largest
        largest = max(range(len(floors)), key=lambda i: (floors[i], -i))
        if floors[largest] == 0:
            break
        floors[largest] -= 1
        leftover += 1
    return [f / 10 for f in floors]


# --- facts about a company --------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyFacts:
    """What the JSON, the page and the CSV say about coverage and open roles."""

    open_postings: int
    long_open: list[LongOpenRole]
    ats: str
    """``vendor:identifier`` of the active board, or ``no_board``."""
    coverage_note: str
    """``greenhouse:acme`` | ``no_board`` | ``unsupported:workday`` | ``dead:lever:acme``."""
    edgar_cik: str | None
    news: bool


_IN_CHUNK = 500
"""Company ids per ``IN (...)`` list of the batch queries."""


def _chunks(ids: Sequence[int]) -> list[list[int]]:
    return [list(ids[start : start + _IN_CHUNK]) for start in range(0, len(ids), _IN_CHUNK)]


def company_facts_many(
    session: Session, companies: Sequence[Company], t: datetime
) -> dict[int, CompanyFacts]:
    """:func:`company_facts` of every company in ``companies``: a fixed number of queries
    per 500 companies (boards, open postings, feed counts, news presence), never one
    round trip per company."""
    ids = sorted({c.id for c in companies})
    boards: dict[int, list[AtsBoard]] = defaultdict(list)
    open_counts: dict[int, int] = defaultdict(int)
    feeds: dict[int, int] = {}
    news: set[int] = set()
    for chunk in _chunks(ids):
        for board in session.scalars(
            select(AtsBoard)
            .where(AtsBoard.company_id.in_(chunk))
            .order_by(AtsBoard.vendor, AtsBoard.identifier)
        ):
            boards[cast("int", board.company_id)].append(board)  # IN filter: never NULL
        for cid, count in session.execute(
            select(AtsBoard.company_id, func.count())
            .select_from(Posting)
            .join(AtsBoard, AtsBoard.id == Posting.board_id)
            .where(AtsBoard.company_id.in_(chunk), Posting.closed_at.is_(None))
            .group_by(AtsBoard.company_id)
        ):
            open_counts[cast("int", cid)] = int(count)  # IN filter: never NULL
        for cid, count in session.execute(
            select(CompanyFeed.company_id, func.count())
            .where(CompanyFeed.company_id.in_(chunk))
            .group_by(CompanyFeed.company_id)
        ):
            feeds[int(cid)] = int(count)
        news.update(
            cast("int", cid)  # IN filter: never NULL
            for (cid,) in session.execute(
                select(EventRow.company_id)
                .where(EventRow.company_id.in_(chunk), EventRow.source == "news")
                .distinct()
            )
        )
    long_open = long_open_roles_many(session, ids, t)
    out: dict[int, CompanyFacts] = {}
    for company in companies:
        own = boards.get(company.id, [])
        active = [b for b in own if b.status == "active"]
        unsupported = [b for b in own if b.status == "unsupported"]
        lapsed = [b for b in own if b.status in ("dead", "paused")]
        ats = f"{active[0].vendor}:{active[0].identifier}" if active else "no_board"
        if active:
            note = ats
        elif unsupported:
            note = f"unsupported:{unsupported[0].vendor}"
        elif lapsed:
            note = f"{lapsed[0].status}:{lapsed[0].vendor}:{lapsed[0].identifier}"
        else:
            note = "no_board"
        out[company.id] = CompanyFacts(
            open_postings=open_counts.get(company.id, 0),
            long_open=long_open.get(company.id, []),
            ats=ats,
            coverage_note=note,
            edgar_cik=company.cik.zfill(10) if company.cik and company.cik.isdigit() else None,
            news=feeds.get(company.id, 0) > 0 or company.id in news,
        )
    return out


def company_facts(session: Session, company: Company, t: datetime) -> CompanyFacts:
    """Coverage, open postings and long-open roles of one company at instant ``t``."""
    return company_facts_many(session, [company], t)[company.id]


@dataclass(frozen=True)
class CompanyBatch:
    """What :func:`build_company_json` reads beyond the score, prefetched for many companies.

    Built by :func:`prepare_companies` with a fixed number of queries, so rendering
    hundreds of account pages costs a handful of round trips instead of several per
    company (the site runs against a remote database).
    """

    facts: dict[int, CompanyFacts]
    why_not: dict[int, list[dict[str, Any]]]
    velocity: dict[int, dict[str, Any]]


def prepare_companies(
    session: Session,
    companies: Sequence[Company],
    profile: Profile,
    as_of: date,
    results: Mapping[int, ScoreResult],
) -> CompanyBatch:
    """Prefetch facts, ``why_not_higher`` rows and velocity receipts for ``companies``."""
    t = score_instant(as_of)
    ids = [c.id for c in companies]
    velocity_ids = sorted(
        {
            int(c["signal_id"])
            for cid in ids
            if cid in results
            for c in results[cid].contributions
            if c["signal_type"] == HIRING_VELOCITY
        }
    )
    return CompanyBatch(
        facts=company_facts_many(session, companies, t),
        why_not=why_not_higher_many(session, ids, profile, as_of),
        velocity=_velocity_payloads(session, velocity_ids),
    )


# --- builders --------------------------------------------------------------------------------------


def _day(value: Any) -> str:
    """The UTC calendar date of an ISO 8601 timestamp string."""
    return parse_iso(str(value)).date().isoformat()


def _velocity_payloads(session: Session, signal_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(signal_ids))):
        stmt = (
            select(Signal.id, EventRow.payload, EventRow.occurred_at)
            .join(EventRow, EventRow.event_id == Signal.event_id)
            .where(Signal.id.in_(chunk))
        )
        for sid, payload, occurred_at in session.execute(stmt):
            out[int(sid)] = {"payload": payload or {}, "occurred_at": occurred_at}
    return out


def watchlist_profile(watchlist: Watchlist, profiles: Mapping[str, Profile] | None) -> Profile:
    """The ICP profile a watchlist is scored under."""
    if profiles is not None:
        return profiles[watchlist.icp_profile]
    return get_profile(watchlist.icp_profile)


def _watchlist_members(session: Session, watchlist: Watchlist) -> list[Company]:
    return list(
        session.scalars(
            select(Company)
            .join(WatchlistItem, WatchlistItem.company_id == Company.id)
            .where(WatchlistItem.watchlist_id == watchlist.id)
            .order_by(Company.domain)
        )
    )


def build_company_json(
    session: Session,
    company: Company,
    profile: Profile,
    as_of: date,
    watchlist: Watchlist | None = None,
    *,
    result: ScoreResult | None = None,
    heat_value: int | None = None,
    sha: str | None = None,
    facts: CompanyFacts | None = None,
    batch: CompanyBatch | None = None,
) -> dict[str, Any]:
    """The section 9 per-domain object for ``company`` under ``profile`` at ``as_of``.

    ``result`` (a precomputed :class:`ScoreResult`), ``facts``, ``batch`` (from
    :func:`prepare_companies`) and ``heat_value`` let a batch caller avoid recomputing
    and issue no query per company; by default the score is computed fresh (not written),
    and with a ``watchlist`` heat is the percentile of the fresh scores of its members.
    ``heat`` is present only when ``watchlist`` is given.
    """
    t = score_instant(as_of)
    scored = result or score_companies(session, profile, as_of, [company.id], write=False)[0]
    if facts is None and batch is not None:
        facts = batch.facts.get(company.id)
    info = facts or company_facts(session, company, t)
    shown = apportion_tenths([float(c["contribution"]) for c in scored.contributions], scored.score)
    velocity_ids = [
        int(c["signal_id"]) for c in scored.contributions if c["signal_type"] == HIRING_VELOCITY
    ]
    velocity = (
        batch.velocity
        if batch is not None and all(i in batch.velocity for i in velocity_ids)
        else _velocity_payloads(session, velocity_ids)
    )
    contributions: list[dict[str, Any]] = []
    for item, value in zip(scored.contributions, shown, strict=True):
        entry: dict[str, Any] = {"type": item["signal_type"]}
        if item.get("role_family"):
            entry["role_family"] = item["role_family"]
        entry["contribution"] = value
        entry["occurred_at"] = _day(item["occurred_at"])
        entry["summary"] = item["summary"]
        entry["url"] = item["url"]
        if item["signal_type"] == HIRING_VELOCITY:
            found = velocity.get(int(item["signal_id"]))
            entry["postings"] = (
                velocity_postings(found["payload"], found["occurred_at"]) if found else []
            )
        contributions.append(entry)
    out: dict[str, Any] = {
        "domain": company.domain,
        "name": company.name,
        "as_of": as_of.isoformat(),
        "profile": profile.name,
        "score": round_half_up(scored.score),
    }
    if watchlist is not None:
        if heat_value is None:
            members = _watchlist_members(session, watchlist)
            fresh = score_companies(session, profile, as_of, [m.id for m in members], write=False)
            heat_value = heat({r.company_id: r.score for r in fresh}).get(company.id, 0)
        out["heat"] = int(heat_value)
    decayed: list[dict[str, Any]] = []
    why_not = batch.why_not.get(company.id) if batch is not None else None
    if why_not is None:
        why_not = why_not_higher(session, company.id, profile, as_of)
    for row in why_not:
        entry = {"type": row["signal_type"]}
        if row.get("role_family"):
            entry["role_family"] = row["role_family"]
        entry["decayed_at"] = _day(row["expired_at"])
        entry["url"] = row["url"]
        entry["summary"] = row["summary"]
        decayed.append(entry)
    out.update(
        {
            "contributions": contributions,
            "open_postings": info.open_postings,
            "long_open_roles": len(info.long_open),
            "sources": {"ats": info.ats, "edgar_cik": info.edgar_cik, "news": info.news},
            "generated_by": generated_by(sha),
            "why_not_higher": decayed,
        }
    )
    return out


def build_watchlist_json(
    session: Session,
    watchlist: Watchlist,
    as_of: date,
    *,
    profiles: Mapping[str, Profile] | None = None,
    sha: str | None = None,
) -> dict[str, Any]:
    """``api/v0/watchlists/{slug}.json`` for a public watchlist: its members, highest first.

    Raises ``ValueError`` for a private watchlist or one without a slug: only lists
    marked public are ever published (spec section 9, ADR 0022). ``owner`` is never
    included.
    """
    if not watchlist.is_public or not watchlist.slug:
        msg = f"watchlist {watchlist.id} is not public (or has no slug); refusing to publish"
        raise ValueError(msg)
    profile = watchlist_profile(watchlist, profiles)
    members = _watchlist_members(session, watchlist)
    results = {
        r.company_id: r
        for r in score_companies(session, profile, as_of, [m.id for m in members], write=False)
    }
    heats = heat({cid: r.score for cid, r in results.items()})
    batch = prepare_companies(session, members, profile, as_of, results)
    companies = [
        build_company_json(
            session,
            member,
            profile,
            as_of,
            watchlist,
            result=results[member.id],
            heat_value=heats.get(member.id, 0),
            sha=sha,
            batch=batch,
        )
        for member in members
    ]
    companies.sort(key=lambda c: (-c["score"], c["domain"]))
    return {
        "slug": watchlist.slug,
        "name": watchlist.name,
        "profile": profile.name,
        "as_of": as_of.isoformat(),
        "generated_by": generated_by(sha),
        "companies": companies,
    }


def build_weekly_json(data: WeeklyData, *, sha: str | None = None) -> dict[str, Any]:
    """``api/v0/weekly/latest.json``: the weekly edition's lists and coverage."""
    return {**data.to_dict(), "generated_by": generated_by(sha)}


def build_health_json(health: Health, *, sha: str | None = None) -> dict[str, Any]:
    """``api/v0/health.json``: per-source run-health."""
    return {**health.to_dict(), "generated_by": generated_by(sha)}
