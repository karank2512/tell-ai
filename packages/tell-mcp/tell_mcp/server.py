"""The ``tell-mcp`` stdio MCP server (``docs/SPEC.md`` section 10, PLAN Phase 9).

Five tools and one prompt over tell's public static JSON (``TELL_BASE_URL``):

=================  ==========================================================================
``watch_accounts``  v0: the intake form link, prefilled with the domains (nothing is written)
``in_market``       a public watchlist's accounts above ``min_score``, newest receipts first
``explain``         every contribution with its receipt URL, plus the "why not higher" list
``events``          contributions and counted postings, newest first, at most 200 (v0 view)
``coverage``        per domain: job board, SEC CIK, news feed, or "not monitored"
``why_now_email``   prompt: the two-sentence "why now" from the top two receipts
=================  ==========================================================================

Every tool is a plain ``async`` function registered with the SDK's decorator, so tests
call them directly. Network and HTTP failures surface as tool errors (``ToolError``: the
client sees ``isError`` with a one-line message); the server never exits on them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from tell_mcp import __version__
from tell_mcp.client import TellClient, TellError, normalize_domain, normalize_slug

try:  # mcp >= 2: FastMCP was renamed MCPServer.
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - mcp 1.x still calls it FastMCP
    from mcp.server import fastmcp as _fastmcp_v1

    MCPServer = _fastmcp_v1.FastMCP  # type: ignore[attr-defined,misc]  # mcp 1.x only
    ToolError = _fastmcp_v1.exceptions.ToolError  # type: ignore[attr-defined,misc]  # mcp 1.x only

__all__ = [
    "INTAKE_ISSUE_FORM",
    "MAX_DOMAINS",
    "MAX_EVENTS",
    "MCPServer",
    "ToolError",
    "coverage",
    "coverage_row",
    "derive_events",
    "events",
    "explain",
    "in_market",
    "intake_link",
    "main",
    "mcp",
    "select_in_market",
    "watch_accounts",
    "why_now_email",
    "why_now_text",
]

INTAKE_ISSUE_FORM = "https://github.com/karank2512/tell-ai/issues/new?template=watch-domains.yml"
"""The "Watch these domains" GitHub issue form, used when ``TELL_INTAKE_FORM_URL`` is unset."""

MAX_DOMAINS = 50
"""The intake form takes at most 50 domains per request."""

MAX_COVERAGE_DOMAINS = 200
MAX_EVENTS = 200

CITE = (
    "Cite the `url` of every contribution, posting or signal you mention, and call `explain` "
    "for an account before writing any outreach copy about it."
)

V0_EVENTS_NOTE = (
    "v0 limitation: the static API has no event endpoint, so these are the scored "
    "contributions and the counted job postings behind them, not every raw event tell "
    "ingested. Signals that decayed to zero are listed by `explain` under why_not_higher."
)

INSTRUCTIONS = (
    "tell reports public buying signals (hiring velocity, Form D funding, leadership changes, "
    "launches, expansion) for company domains, each with a receipt URL. Data is refreshed by a "
    "scheduled job a few times a day. " + CITE + " A domain without published data is not "
    "monitored (or quiet for 180 days): say so plainly instead of guessing."
)

mcp = MCPServer(name="tell", instructions=INSTRUCTIONS)


def _client() -> TellClient:
    return TellClient.from_env()


def _fail(exc: Exception) -> ToolError:
    return ToolError(str(exc))


# --- helpers ----------------------------------------------------------------------------------


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "watchlist"


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _today() -> date:
    return datetime.now(UTC).date()


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _contributions(company: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = company.get("contributions")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _brief(contribution: Mapping[str, Any]) -> dict[str, Any]:
    """A contribution without its postings: what ``in_market`` shows per account."""
    keys = ("type", "role_family", "contribution", "occurred_at", "summary", "url")
    return {key: contribution[key] for key in keys if key in contribution}


def _split_domains(domains: Iterable[str]) -> tuple[list[str], list[str]]:
    good: list[str] = []
    bad: list[str] = []
    for raw in domains:
        try:
            domain = normalize_domain(raw)
        except ValueError:
            bad.append(raw)
            continue
        if domain not in good:
            good.append(domain)
    return good, bad


def intake_link(domains: Sequence[str], watchlist: str, form_url: str | None = None) -> str:
    """The intake request link, prefilled with ``domains`` (one per line) and ``watchlist``.

    ``form_url`` (default ``TELL_INTAKE_FORM_URL``) may carry ``{domains}`` / ``{watchlist}``
    placeholders (a Google Form prefill link); without them both values are appended as
    query parameters. Without a form the link opens the repo's "Watch these domains" issue
    form with ``title``, ``domains`` and ``notes`` prefilled (GitHub fills issue-form
    fields from query parameters named after their ids).
    """
    form = form_url if form_url is not None else os.environ.get("TELL_INTAKE_FORM_URL", "")
    joined = "\n".join(domains)
    if form:
        values = {"domains": joined, "watchlist": watchlist}
        if any(f"{{{key}}}" in form for key in values):
            out = form
            for key, value in values.items():
                out = out.replace(f"{{{key}}}", quote(value, safe=""))
            return out
        joiner = "&" if "?" in form else "?"
        return form + joiner + urlencode(values, quote_via=quote)
    fields = {
        "title": f"Watch these domains: {watchlist}",
        "domains": joined,
        "notes": f"Watchlist: {watchlist} (requested through tell-mcp)",
    }
    return INTAKE_ISSUE_FORM + "&" + urlencode(fields, quote_via=quote)


# --- tools ------------------------------------------------------------------------------------


@mcp.tool(
    name="watch_accounts",
    description=(
        "Ask tell to start watching company domains for buying signals, as a named watchlist. "
        "v0 cannot create watchlists directly: this returns the intake form link prefilled with "
        "the domains (at most 50 per request). Give the user the link to submit; the watchlist "
        "appears after the next scheduled (nightly) job picks the request up. "
    )
    + CITE,
)
async def watch_accounts(domains: list[str], watchlist: str) -> dict[str, Any]:
    """v0: the prefilled intake link; v1 will create the watchlist through the API."""
    name = watchlist.strip()
    if not name:
        raise ToolError("watchlist must be a non-empty name")
    good, bad = _split_domains(domains)
    if not good:
        raise ToolError("no valid company domains given (expected something like acme.com)")
    included, overflow = good[:MAX_DOMAINS], good[MAX_DOMAINS:]
    slug = _slugify(name)
    client = _client()
    message = (
        f"tell v0 cannot create watchlists directly. Open form_url to submit {len(included)} "
        f"domain(s) for the watchlist {name!r}. Nothing changes until the request is submitted; "
        "the watchlist will appear after the next nightly job picks it up. Watchlists are "
        "private by default; once one is published, in_market can read it as "
        f"{client.watchlist_url(slug)}. Until then, use explain and coverage per domain."
    )
    if overflow:
        message += (
            f" The form takes {MAX_DOMAINS} domains per request; submit the other "
            f"{len(overflow)} (listed in not_included) separately."
        )
    return {
        "status": "form_link",
        "watchlist": name,
        "slug": slug,
        "domains": included,
        "not_included": overflow,
        "invalid": bad,
        "form_url": intake_link(included, name),
        "message": message,
    }


def select_in_market(
    watchlist: Mapping[str, Any],
    *,
    since_days: int = 7,
    min_score: float = 6.0,
    families: Sequence[str] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Filter and sort a watchlist object (pure; :func:`in_market` fetches and calls this).

    An account qualifies when its ``score`` is at least ``min_score`` and its top
    contribution occurred within ``since_days`` of the watchlist's ``as_of`` (``today``
    when the file has none). With ``families`` only ``hiring_velocity`` contributions in
    one of those role families count, and the top one of those must be recent. Results
    are sorted by score, then heat, highest first; each carries its top three
    contributions (receipt URLs included, postings left to ``explain``).
    """
    reference = _day(watchlist.get("as_of")) or today or _today()
    cutoff = reference - timedelta(days=since_days)
    wanted = {f.strip().lower() for f in families or () if f.strip()}
    rows: list[dict[str, Any]] = []
    for company in watchlist.get("companies") or ():
        if not isinstance(company, dict):
            continue
        score = _number(company.get("score"))
        if score < min_score:
            continue
        contributions = _contributions(company)
        relevant = contributions
        if wanted:
            relevant = [
                c
                for c in contributions
                if c.get("type") == "hiring_velocity"
                and str(c.get("role_family", "")).lower() in wanted
            ]
        if not relevant:
            continue
        top = max(relevant, key=lambda c: _number(c.get("contribution")))
        occurred = _day(top.get("occurred_at"))
        if occurred is None or occurred < cutoff:
            continue
        rows.append(
            {
                "domain": company.get("domain"),
                "name": company.get("name"),
                "score": company.get("score"),
                "heat": company.get("heat"),
                "top_contributions": [_brief(c) for c in contributions[:3]],
            }
        )
    rows.sort(key=lambda r: (-_number(r["score"]), -_number(r["heat"]), str(r["domain"])))
    return rows


@mcp.tool(
    name="in_market",
    description=(
        "Accounts on a public tell watchlist that are in market now: score at least min_score "
        "(default 6.0) and a top signal within since_days (default 7) of the data date. "
        "families narrows to hiring_velocity in those role families (for example sales, "
        "engineering). Returns [{domain, name, score, heat, top_contributions[:3]}] sorted by "
        "score; heat is the 0-100 percentile within the watchlist. "
    )
    + CITE,
)
async def in_market(
    watchlist: str,
    since_days: int = 7,
    min_score: float = 6.0,
    families: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The in-market accounts of a public watchlist, highest score first."""
    if since_days < 0:
        raise ToolError("since_days must be zero or more")
    client = _client()
    try:
        slug = normalize_slug(watchlist)
        data = await client.watchlist(slug)
    except (ValueError, TellError) as exc:
        raise _fail(exc) from exc
    if data is None:
        msg = (
            f"watchlist {slug!r} is not published at {client.watchlist_url(slug)}: only public "
            "watchlists have a JSON file. Use watch_accounts to request one, or explain per domain."
        )
        raise ToolError(msg)
    return select_in_market(data, since_days=since_days, min_score=min_score, families=families)


def _not_monitored(domain: str) -> str:
    return (
        f"tell has no published data for {domain}: it is not monitored, or it has had no "
        "signal for 180 days. We cannot see this one; do not infer anything about it. "
        "Ask for it with watch_accounts."
    )


@mcp.tool(
    name="explain",
    description=(
        "Why an account scores what it does: every contribution with its type, decayed "
        "value, date, summary and receipt url (hiring_velocity includes the counted postings), "
        "plus why_not_higher, the signals that decayed to zero in the last 30 days. Use it "
        "before writing outreach copy. "
    )
    + CITE,
)
async def explain(domain: str) -> dict[str, Any]:
    """The full company object, plus ``account_page``; ``monitored: false`` on a 404."""
    client = _client()
    try:
        name = normalize_domain(domain)
        company = await client.company(name)
    except (ValueError, TellError) as exc:
        raise _fail(exc) from exc
    if company is None:
        return {"domain": name, "monitored": False, "message": _not_monitored(name)}
    return {
        "monitored": True,
        **company,
        "contributions": _contributions(company),
        "why_not_higher": [w for w in company.get("why_not_higher") or () if isinstance(w, dict)],
        "account_page": client.account_page(name),
    }


def derive_events(
    company: Mapping[str, Any],
    *,
    since: date | None = None,
    types: Sequence[str] | None = None,
    limit: int = MAX_EVENTS,
) -> tuple[list[dict[str, Any]], bool]:
    """Events from a company object: contributions and their counted postings, newest first.

    ``types`` matches an event's ``type`` (``hiring_velocity`` keeps the velocity
    contributions and their postings) or its ``kind`` (``posting`` keeps postings only).
    Postings shared by two contributions are listed once. Returns ``(events, truncated)``.
    """
    wanted = {t.strip().lower() for t in types or () if t.strip()}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in _contributions(company):
        kind_type = str(c.get("type", ""))
        out.append(
            {
                "kind": "contribution",
                **_brief(c),
            }
        )
        for p in c.get("postings") or ():
            if not isinstance(p, dict) or str(p.get("url")) in seen:
                continue
            seen.add(str(p.get("url")))
            posting: dict[str, Any] = {
                "kind": "posting",
                "type": kind_type,
                "occurred_at": p.get("posted_at"),
                "summary": p.get("title"),
                "url": p.get("url"),
            }
            if "role_family" in c:
                posting["role_family"] = c["role_family"]
            out.append(posting)
    if wanted:
        out = [e for e in out if str(e.get("type")) in wanted or e["kind"] in wanted]
    if since is not None:
        out = [e for e in out if (d := _day(e.get("occurred_at"))) is not None and d >= since]
    order = {"contribution": 0, "posting": 1}
    out.sort(key=lambda e: str(e.get("url")))
    out.sort(key=lambda e: order[e["kind"]])
    out.sort(key=lambda e: _day(e.get("occurred_at")) or date.min, reverse=True)
    return out[:limit], len(out) > limit


@mcp.tool(
    name="events",
    description=(
        "Dated events behind an account's signals, newest first, at most 200, each with its "
        "url: the scored contributions and the counted job postings. since (YYYY-MM-DD) keeps "
        "events on or after that date; types filters by signal type (hiring_velocity, funding, "
        "leadership, launch, expansion, tech_adoption) or by kind (posting). v0 limitation: no "
        "raw event endpoint exists yet, so events tell saw but did not score are not listed. "
    )
    + CITE,
)
async def events(
    domain: str, since: str | None = None, types: list[str] | None = None
) -> dict[str, Any]:
    """Contributions and counted postings for ``domain`` (the v0 stand-in for raw events)."""
    since_day: date | None = None
    if since:
        since_day = _day(since.strip())
        if since_day is None:
            raise ToolError(f"since must be an ISO date like 2026-10-01, got {since!r}")
    client = _client()
    try:
        name = normalize_domain(domain)
        company = await client.company(name)
    except (ValueError, TellError) as exc:
        raise _fail(exc) from exc
    if company is None:
        return {"domain": name, "monitored": False, "events": [], "message": _not_monitored(name)}
    found, truncated = derive_events(company, since=since_day, types=types)
    return {
        "domain": name,
        "monitored": True,
        "as_of": company.get("as_of"),
        "events": found,
        "truncated": truncated,
        "note": V0_EVENTS_NOTE,
    }


def coverage_row(domain: str, company: Mapping[str, Any] | None) -> dict[str, Any]:
    """What tell can and cannot see for one domain (``company`` is ``None`` on a 404)."""
    if company is None:
        return {
            "domain": domain,
            "status": "not monitored",
            "has_board": None,
            "has_cik": None,
            "has_feed": None,
            "note": _not_monitored(domain),
        }
    sources = company.get("sources")
    sources = sources if isinstance(sources, dict) else {}
    ats = sources.get("ats")
    has_board = isinstance(ats, str) and bool(ats) and ats != "no_board"
    cik = sources.get("edgar_cik")
    has_cik = isinstance(cik, str) and bool(cik)
    has_feed = sources.get("news") is True
    blind: list[str] = []
    if not has_board:
        blind.append("no supported job board, so hiring velocity is invisible")
    if not has_cik:
        blind.append("no SEC CIK, so Form D funding is invisible")
    if not has_feed:
        blind.append("no news feed or news event, so launches and leadership news may be missed")
    note = "; ".join(blind) if blind else "job board, SEC filings and news are all monitored"
    return {
        "domain": domain,
        "status": "monitored",
        "name": company.get("name"),
        "has_board": has_board,
        "board": ats if has_board else None,
        "has_cik": has_cik,
        "cik": cik if has_cik else None,
        "has_feed": has_feed,
        "as_of": company.get("as_of"),
        "note": note,
    }


@mcp.tool(
    name="coverage",
    description=(
        "What tell can see for each domain: has_board (a supported job board), has_cik (an SEC "
        "CIK for Form D filings), has_feed (news), or status 'not monitored' when tell has no "
        "data, which is the honest answer: do not guess about those. "
    )
    + CITE,
)
async def coverage(domains: list[str]) -> list[dict[str, Any]]:
    """One row per requested domain, in request order."""
    if len(domains) > MAX_COVERAGE_DOMAINS:
        raise ToolError(f"at most {MAX_COVERAGE_DOMAINS} domains per call")
    client = _client()

    async def one(raw: str) -> dict[str, Any]:
        try:
            domain = normalize_domain(raw)
        except ValueError as exc:
            return {"domain": raw, "status": "invalid domain", "note": str(exc)}
        try:
            company = await client.company(domain)
        except TellError as exc:
            return {"domain": domain, "status": "error", "note": str(exc)}
        return coverage_row(domain, company)

    return list(await asyncio.gather(*(one(raw) for raw in domains)))


# --- prompt -----------------------------------------------------------------------------------


def _receipt_line(index: int, c: Mapping[str, Any]) -> str:
    family = f" ({c['role_family']})" if c.get("role_family") else ""
    return (
        f"{index}. {c.get('type')}{family}, {c.get('occurred_at')}, contribution "
        f"{c.get('contribution')}: {c.get('summary')}\n   receipt: {c.get('url')}"
    )


def why_now_text(domain: str, company: Mapping[str, Any] | None) -> str:
    """The ``why_now_email`` prompt body for ``domain`` and its fetched company object."""
    if company is None:
        return (
            f"tell has no published data for {domain}: it is not monitored, or quiet for 180 "
            "days. Tell the user there is no evidence-backed 'why now' for this account and do "
            "not invent one. Suggest the watch_accounts tool to start monitoring it."
        )
    top = _contributions(company)[:2]
    if not top:
        return (
            f"tell monitors {domain} but it has no live signal (score {company.get('score')}). "
            "Tell the user there is no evidence-backed 'why now' and do not invent one."
        )
    lines = "\n".join(_receipt_line(i, c) for i, c in enumerate(top, start=1))
    name = company.get("name") or domain
    return (
        f"Write the 'why now' for an outreach email to {name} ({domain}): exactly two "
        f"sentences, built only from the top {len(top)} tell signal(s) below. Each sentence "
        "states one signal in plain words with its date and ends with its receipt URL in "
        "parentheses. No claims beyond the receipts, no superlatives, no guesses about intent.\n\n"
        f"Data as of {company.get('as_of')} (score {company.get('score')}, profile "
        f"{company.get('profile')}):\n{lines}\n\n"
        "After the two sentences, list the receipt URLs you cited under 'Receipts:'."
    )


@mcp.prompt(
    name="why_now_email",
    description=(
        "The two-sentence 'why now' for an outreach email to a domain, built from its top two "
        "tell contributions, each cited by its receipt URL."
    ),
)
async def why_now_email(domain: str) -> str:
    """Fetch ``domain`` and return the prompt text with its top two receipts inlined."""
    try:
        name = normalize_domain(domain)
    except ValueError as exc:
        return f"{exc}. Ask the user for the company's domain."
    try:
        company = await _client().company(name)
    except TellError as exc:
        return (
            f"tell's data for {name} could not be fetched ({exc}). Tell the user, and do not "
            "write a 'why now' without receipts."
        )
    return why_now_text(name, company)


# --- entry point ------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tell-mcp",
        description=(
            "MCP server (stdio) for tell buying signals: tools watch_accounts, in_market, "
            "explain, events, coverage and the why_now_email prompt."
        ),
        epilog=(
            "Environment: TELL_BASE_URL (default https://karank2512.github.io/tell-data), "
            "TELL_API_KEY and TELL_API_URL (optional, the FastAPI service), "
            "TELL_INTAKE_FORM_URL (optional watchlist intake form). "
            "Docs: https://github.com/karank2512/tell-ai/blob/main/docs/mcp.md"
        ),
    )
    parser.add_argument("--version", action="version", version=f"tell-mcp {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """``tell-mcp``: parse ``--help`` / ``--version``, then serve MCP over stdio."""
    _parser().parse_args(argv)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
