"""Issuer-first discovery (ADR 0035): create companies from Form D issuers on site evidence.

``tell discover issuers`` takes the Form D issuers no company holds yet, finds each one's
own website, and creates the company only when the site itself states who runs it
(:mod:`tell.resolve.evidence`, tiers A and B). The filing then links exactly by CIK
(rule R0), and so do the issuer's amendments and later rounds.

**Input** (:func:`select_issuers`): unlinked ``form_d_filed`` events filed on or after
``since`` (default: the last :data:`DEFAULT_WINDOW_DAYS` days) whose issuer is not a pooled
fund (guard e), is a ``Corporation`` incorporated in Delaware, states ``year_of_inc`` (so
it was incorporated within the last five years), and whose CIK is in no ``companies.cik``.
An issuer already tried is skipped, unless it is ``no_domain`` and its retry is due:
:data:`RETRY_AFTER_DAYS` (30 and 90 days after the first try) or a new unlinked Form D
event of the CIK observed after the last try (its next filing). A scheduled retry is due
whatever ``since`` says: its CIK's filings are read with no date bound, so the 90-day
retry of an issuer first tried a day after its filing still runs in the plain daily step.
New issuers come first (newest filing first), then due retries, up to ``limit``.

**Per issuer, network only** (:func:`investigate`; no database session is touched, so no
transaction is open while it runs):

1. ATS probes: the slugs ``{joined}``, ``{hyphenated}`` and ``{core}`` of the normalized
   name against Greenhouse, Lever, Ashby and SmartRecruiters through discovery's own
   probe helpers (at most 12 requests). A board with jobs whose name reaches ratio
   :data:`tell.discovery.ats.MATCH_THRESHOLD` against the issuer name proposes the first
   site it links (:func:`tell.discovery.ats.ownership_evidence`'s ``sites``) as a candidate
   domain. The board name is never evidence by itself.
2. Up to :data:`MAX_DOMAIN_GUESSES` domain guesses (:func:`domain_guesses`). Each must
   resolve in DNS; then ``robots.txt`` and the homepage (redirects followed by hand, every
   hop robots-checked). The company's key is the **final registrable domain after
   redirects**; platform domains and parked pages give no evidence.
3. On a domain whose homepage names the brand: up to :data:`MAX_LEGAL_FETCHES` legal pages
   (the footer's terms/privacy links, then :data:`tell.resolve.evidence.LEGAL_PATHS`),
   robots honoured, stopping at the first tier A page.

Every request goes through the run's client with a per-host token bucket
(:data:`HOST_RPS`, one request per second per host) installed as an ``httpx`` request
hook, so up to ``concurrency`` issuers in flight never exceed it on any host. The
User-Agent is the client's (``TELL_HTTP_USER_AGENT``: ``tell/0.1 (+repo URL)``).

**Outcome** (:func:`record`, the main thread, one transaction per issuer with
``--apply``; ``--shadow``, the default, writes nothing):

* Tier A or B on exactly one domain: a new company (``source = 'issuer_first'``, the
  domain, the brand as ``name``, the issuer name as ``legal_name``, ``cik`` with
  ``cik_source = issuer_first_a / issuer_first_b``, the Form D city and state). The
  issuer's names become trusted ``edgar`` aliases, the brand and domain label ``manual``
  ones; every unlinked EDGAR event of the CIK is linked with rule R0 (open
  ``entity_match`` items about them are resolved); ``issuer_discovery`` is ``linked``
  with the evidence ``{path, span}``. The new company has no ``ats_boards`` row, so the
  next ``tell discover batch`` picks it up (:func:`tell.discovery.ats.select_new_companies`).
* The domain already belongs to a company: an ``entity_match`` review item on the filing
  proposes that company (a ``link`` verdict writes the CIK with ``cik_source =
  'manual'``); ``conflict``. Nothing on the existing company is changed.
* Two or more domains qualify: review item, ``conflict``.
* Tier C only: a review item when the amount sold is at least
  :data:`TIER_C_REVIEW_MIN_SOLD`, else logged only; ``tier_c``.
* Nothing: ``no_domain`` with the retry schedule above.

One ``runs`` row (``source = 'issuer_discovery'``) per ``--apply`` run, and one JSON run
line (the standard fields) for both modes.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, iso_utc, new_run_id, start_run
from tell.config import get_settings
from tell.discovery import is_platform_domain, registrable_domain
from tell.discovery.ats import (
    MATCH_THRESHOLD,
    SUPPORTED_VENDORS,
    RobotsCache,
    _get_json,
    _get_page,
    _job_count,
    _Page,
    _probe_request,
    name_ratio,
    ownership_evidence,
)
from tell.http import Clock, HostRateBudgets, Sleeper
from tell.models import Company, EventRow, IssuerDiscovery, ReviewQueueItem
from tell.resolve.aliases import label_alias_problem
from tell.resolve.evidence import (
    LEGAL_PATHS,
    US_STATES,
    Decision,
    IssuerProfile,
    SiteEvidence,
    SitePage,
    assess,
    brand_words,
    core_words,
    decide,
    legal_links,
    parse_page,
)
from tell.resolve.guards import FORM_D_TYPES, IssuerFacts, cik10, fund_signals, guard_failures
from tell.resolve.match import CONFIDENCE, REVIEW_KIND, add_aliases, link_event
from tell.resolve.normalize import domain_label, normalize_name

__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_MINUTES",
    "DEFAULT_WINDOW_DAYS",
    "HOST_RPS",
    "MAX_DOMAIN_GUESSES",
    "MAX_LEGAL_FETCHES",
    "RETRY_AFTER_DAYS",
    "SOURCE",
    "TIER_C_REVIEW_MIN_SOLD",
    "Commit",
    "Issuer",
    "IssuerFinding",
    "IssuerResult",
    "IssuerRun",
    "discover_issuers",
    "dns_resolves",
    "domain_guesses",
    "investigate",
    "record",
    "select_issuers",
    "slug_guesses",
]

_logger = logging.getLogger("tell.discovery.issuers")

SOURCE = "issuer_discovery"
"""``runs.source`` and the marker on everything this module writes."""

HOST_RPS = 1.0
"""Requests per second per host (company sites, vendor APIs, ``robots.txt``)."""

DEFAULT_CONCURRENCY = 8
"""Issuers investigated at once (they share the per-host budget)."""

DEFAULT_LIMIT = 100
"""Issuers per run (about 12 a day are new; a backfill passes ``--limit``)."""

DEFAULT_WINDOW_DAYS = 90
"""Filings older than this are not read unless ``--since`` says otherwise."""

DEFAULT_MAX_MINUTES = 12.0
"""No new issuer starts after this many minutes (the workflow step allows 15)."""

MAX_DOMAIN_GUESSES = 8
MAX_LEGAL_FETCHES = 8
MAX_HOMEPAGE_REDIRECTS = 3
MAX_LEGAL_REDIRECTS = 2

RETRY_AFTER_DAYS: tuple[int, ...] = (30, 90)
"""A ``no_domain`` issuer is tried again this many days after its first try (and on its
next filing); after the last one, only a new filing triggers a retry."""

TIER_C_REVIEW_MIN_SOLD = 1_000_000
"""Tier C opens a review item only when the issuer sold at least this much (USD)."""

PLACEHOLDER_DOMAIN = "invalid.invalid"
"""The domain handed to :func:`ownership_evidence` when only the board's own sites matter."""

_ASCII_LABEL = re.compile(r"^[a-z0-9]+$")

Outcome = Literal["linked", "conflict", "tier_c", "no_domain", "known_cik"]
_OUTCOMES: tuple[Outcome, ...] = ("linked", "conflict", "tier_c", "no_domain", "known_cik")

Commit = Callable[[Session], None]
"""Makes the work so far durable (the CLI passes ``Session.commit``)."""

ResolveHost = Callable[[str], bool]
"""Whether a host name resolves in DNS (:func:`dns_resolves`; tests pass a fake)."""


def dns_resolves(host: str) -> bool:
    """``True`` when ``host`` has an address record."""
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return True


# --- names -----------------------------------------------------------------------------------


def _ascii(words: Iterable[str]) -> list[str]:
    return [w for w in words if _ASCII_LABEL.match(w)]


def slug_guesses(name: str) -> list[str]:
    """``{joined}``, ``{hyphenated}``, ``{core}`` of the normalized name (3+ characters)."""
    words = _ascii(brand_words(name))
    core = _ascii(core_words(name))
    slugs = ["".join(words), "-".join(words), "".join(core)]
    return [s for s in dict.fromkeys(slugs) if len(s.replace("-", "")) >= 3]


def domain_guesses(name: str) -> list[str]:
    """At most :data:`MAX_DOMAIN_GUESSES` domains a company with this name might own.

    ``{core}.com``, ``.ai``, ``.io``, ``.co``, ``get{core}.com``, ``{core}hq.com``,
    ``{joined}.com``, ``{joined}.ai``; ``core`` is the name without filler and descriptor
    words, ``joined`` every word. ASCII labels of three characters or more only.
    """
    words = _ascii(brand_words(name))
    core = "".join(_ascii(core_words(name)))
    joined = "".join(words)
    guesses = (
        [f"{core}.com", f"{core}.ai", f"{core}.io", f"{core}.co", f"get{core}.com", f"{core}hq.com"]
        if core
        else []
    )
    if joined:
        guesses += [f"{joined}.com", f"{joined}.ai"]
    out = [d for d in dict.fromkeys(guesses) if len(d.split(".", 1)[0]) >= 3]
    return [d for d in out if registrable_domain(d) == d][:MAX_DOMAIN_GUESSES]


# --- input -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issuer:
    """One Form D issuer to investigate, from its newest unlinked ``form_d_filed`` event."""

    cik: str
    name: str
    event_id: str
    """The newest unlinked ``form_d_filed`` event (review items are opened on it)."""
    filed_on: str
    previous_names: tuple[str, ...] = ()
    city: str | None = None
    state: str | None = None
    amount_sold: int | None = None
    """The largest ``total_amount_sold`` across the issuer's unlinked filings."""
    retry: bool = False
    """Selected as a due retry of a ``no_domain`` issuer."""

    def profile(self) -> IssuerProfile:
        return IssuerProfile(
            cik=self.cik,
            name=self.name,
            previous_names=self.previous_names,
            city=self.city,
            state=self.state,
        )


def _eligible(payload: dict[str, Any]) -> IssuerFacts | None:
    """The issuer's facts when it is an input: a Delaware corporation with ``year_of_inc``,
    not a pooled fund, with a CIK."""
    name = payload.get("issuer_name")
    if not isinstance(name, str) or not name.strip():
        return None
    facts = IssuerFacts.from_payload(name.strip(), payload)
    if facts.cik is None or facts.year_of_inc is None:
        return None
    if fund_signals(facts) or guard_failures(facts, None):
        return None
    return facts


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _amount(payload: dict[str, Any]) -> int | None:
    value = payload.get("total_amount_sold")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _aware(value: datetime | None) -> datetime | None:
    """``value`` in UTC (naive means UTC), so day arithmetic never shifts across DST."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _due(row: IssuerDiscovery, newest_observed: datetime | None, now: datetime) -> bool:
    if row.status != "no_domain":
        return False
    retry_at = _aware(row.next_retry_at)
    if retry_at is not None and retry_at <= now:
        return True
    tried = _aware(row.last_tried_at)
    return newest_observed is not None and tried is not None and newest_observed > tried


@dataclass
class _Filings:
    """What the unlinked Form D events in the window say, per CIK."""

    newest: dict[str, Issuer] = field(default_factory=dict)
    """The newest eligible ``form_d_filed`` per CIK."""
    sold: dict[str, int] = field(default_factory=dict)
    observed: dict[str, datetime] = field(default_factory=dict)
    """The newest ``observed_at`` of any unlinked Form D event per CIK (next-filing retry)."""


def _cik_forms(ciks: Iterable[str]) -> list[str]:
    """Every spelling of each CIK an event payload may carry (10-digit and unpadded)."""
    return sorted({form for cik in ciks for form in (cik, str(int(cik)) if cik.isdigit() else cik)})


def _scan_filings(
    session: Session, start: datetime | None, *, ciks: Iterable[str] | None = None
) -> _Filings:
    """Unlinked Form D events filed on or after ``start`` (``None``: any date), newest first;
    only those of ``ciks`` when given."""
    conditions = [
        EventRow.source == "edgar",
        EventRow.type.in_(sorted(FORM_D_TYPES)),
        EventRow.company_id.is_(None),
        func.jsonb_typeof(EventRow.payload["year_of_inc"]) == "number",
    ]
    if start is not None:
        conditions.append(EventRow.occurred_at >= start)
    if ciks is not None:
        conditions.append(EventRow.payload["cik"].astext.in_(_cik_forms(ciks)))
    stmt = (
        select(
            EventRow.event_id,
            EventRow.type,
            EventRow.occurred_at,
            EventRow.observed_at,
            EventRow.payload,
        )
        .where(*conditions)
        .order_by(EventRow.occurred_at.desc(), EventRow.event_id.desc())
    )
    out = _Filings()
    for event_id, kind, occurred_at, observed_at, payload in session.execute(stmt):
        cik = cik10(payload.get("cik"))
        if cik is None:
            continue
        seen = _aware(observed_at)
        if seen is not None and (cik not in out.observed or seen > out.observed[cik]):
            out.observed[cik] = seen
        facts = _eligible(payload)
        if facts is None:
            continue
        amount = _amount(payload)
        if amount is not None:
            out.sold[cik] = max(out.sold.get(cik, 0), amount)
        if kind != "form_d_filed" or cik in out.newest:
            continue
        previous = payload.get("previous_names")
        out.newest[cik] = Issuer(
            cik=cik,
            name=facts.name,
            event_id=str(event_id),
            filed_on=occurred_at.date().isoformat(),
            previous_names=tuple(
                p.strip()
                for p in (previous if isinstance(previous, list) else [])
                if isinstance(p, str) and p.strip()
            ),
            city=_text(payload, "issuer_city"),
            state=_text(payload, "issuer_state"),
        )
    return out


def select_issuers(
    session: Session,
    *,
    now: datetime,
    since: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    retry_due: bool = False,
) -> list[Issuer]:
    """The issuers to investigate (module docstring, "Input"); read-only.

    New issuers first, newest filing first; then ``no_domain`` issuers whose retry is due,
    least recently tried first. ``retry_due`` selects only the latter. ``since`` bounds the
    new issuers and the next-filing retries only: an issuer whose scheduled retry
    (``next_retry_at``) is due is selected from its newest eligible filing of any date.
    """
    if limit < 1:
        return []
    start = since if since is not None else now - timedelta(days=DEFAULT_WINDOW_DAYS)
    filings = _scan_filings(session, start)
    # A scheduled retry is due whatever the window says: the 90-day retry of an issuer
    # first tried a day after its filing falls 91+ days after it, outside the default
    # window, so those CIKs' filings are read with no date bound.
    scheduled = {
        row.cik: row
        for row in session.scalars(
            select(IssuerDiscovery).where(
                IssuerDiscovery.status == "no_domain",
                IssuerDiscovery.next_retry_at.is_not(None),
                IssuerDiscovery.next_retry_at <= now,
            )
        )
    }
    outside = sorted(cik for cik in scheduled if cik not in filings.newest)
    if outside:
        # Only these CIKs' events, of every date: a superset of what the window saw of them.
        older = _scan_filings(session, None, ciks=outside)
        filings.newest.update(older.newest)
        filings.sold.update(older.sold)
        filings.observed.update(older.observed)
    newest, sold, observed = filings.newest, filings.sold, filings.observed
    if not newest:
        return []
    known = {
        key
        for key in (
            cik10(c) for c in session.scalars(select(Company.cik).where(Company.cik.is_not(None)))
        )
        if key is not None
    }
    rows = {
        **{cik: row for cik, row in scheduled.items() if cik in newest},
        **{
            row.cik: row
            for row in session.scalars(
                select(IssuerDiscovery).where(
                    IssuerDiscovery.cik.in_(sorted(set(newest) - set(scheduled)))
                )
            )
        },
    }
    fresh: list[Issuer] = []
    due: list[tuple[datetime, Issuer]] = []
    for cik, issuer in newest.items():
        if cik in known:
            continue
        full = replace(issuer, amount_sold=sold.get(cik))
        row = rows.get(cik)
        if row is None:
            if not retry_due:
                fresh.append(full)
        elif _due(row, observed.get(cik), now):
            tried = _aware(row.last_tried_at) or now
            due.append((tried, replace(full, retry=True)))
    due.sort(key=lambda item: (item[0], item[1].cik))
    return [*fresh, *(issuer for _, issuer in due)][:limit]


# --- network ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardProbe:
    """An ATS board the slug probes found (with jobs)."""

    vendor: str
    identifier: str
    board_name: str | None
    ratio: float
    sites: tuple[str, ...]

    def log_record(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "identifier": self.identifier,
            "ratio": self.ratio,
            "sites": list(self.sites),
        }


@dataclass(frozen=True)
class IssuerFinding:
    """What :func:`investigate` found for one issuer (no database involved)."""

    issuer: Issuer
    decision: Decision
    boards: tuple[BoardProbe, ...] = ()
    candidates: tuple[str, ...] = ()
    """Every domain tried, in order (the board's site first, then the guesses)."""
    notes: dict[str, str] = field(default_factory=dict)
    """Why a candidate gave no pages: ``no_dns``, ``robots_blocked``, ``unreachable``,
    ``duplicate:<final domain>``."""


def _probe_boards(
    client: httpx.Client, issuer: Issuer, *, robots: RobotsCache, sleep: Sleeper
) -> tuple[list[BoardProbe], str | None]:
    """Slug probes (<= 12 requests); the first matching board's first site, if any.

    The vendor-hosted board pages :func:`ownership_evidence` reads are robots-checked
    (``robots``); the documented vendor APIs are not (SPEC sections 3.1-3.4).
    """
    boards: list[BoardProbe] = []
    names = [issuer.name, normalize_name(issuer.name)]
    for slug in slug_guesses(issuer.name):
        for vendor in SUPPORTED_VENDORS:
            url, params = _probe_request(vendor, slug)
            data = _get_json(client, url, params=params, budget=None, sleep=sleep)
            if data is None or _job_count(vendor, data) < 1:
                continue
            profile = ownership_evidence(
                client,
                vendor,
                slug,
                PLACEHOLDER_DOMAIN,
                probe=data,
                sleep=sleep,
                allowed=robots.allowed,
            )
            ratio = name_ratio(profile.name, names)
            sites = tuple(s for s in profile.evidence.sites if not is_platform_domain(s))
            boards.append(BoardProbe(vendor, slug, profile.name, ratio, sites))
            if ratio >= MATCH_THRESHOLD and sites:
                return boards, sites[0]
    return boards, None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _answered(page: _Page) -> bool:
    return page.note is None and page.status == httpx.codes.OK


def _site_pages(
    client: httpx.Client,
    profile: IssuerProfile,
    home: _Page,
    domain: str,
    robots: RobotsCache,
) -> list[SitePage]:
    """The homepage, then legal pages until one gives tier A (<= :data:`MAX_LEGAL_FETCHES`)."""
    home_path = urlsplit(home.url).path or "/"
    pages = [parse_page(home.content, home_path)]
    first = assess(profile, domain, pages)
    if first.tier is None or first.tier == "A":
        return pages  # parked, platform, no brand on the homepage, or already tier A
    origin = _origin(home.url)
    paths = [p for p in legal_links(home.content, home.url, domain) if p != home_path]
    paths += [p for p in LEGAL_PATHS if p not in paths and p != home_path]
    for path in paths[:MAX_LEGAL_FETCHES]:
        url = f"{origin}{path}"
        if not robots.allowed(url):
            continue
        page = _get_page(
            client, url, budget=None, max_redirects=MAX_LEGAL_REDIRECTS, allowed=robots.allowed
        )
        if not _answered(page) or registrable_domain(page.url) != domain:
            continue
        pages.append(parse_page(page.content, path))
        if assess(profile, domain, pages).tier == "A":
            break
    return pages


def investigate(
    client: httpx.Client,
    issuer: Issuer,
    *,
    robots: RobotsCache,
    resolve_host: ResolveHost = dns_resolves,
    sleep: Sleeper = time.sleep,
) -> IssuerFinding:
    """Steps 1-3 of the module docstring for one issuer; network only, no database."""
    profile = issuer.profile()
    boards, board_site = _probe_boards(client, issuer, robots=robots, sleep=sleep)
    candidates = list(
        dict.fromkeys([*([board_site] if board_site else []), *domain_guesses(issuer.name)])
    )
    notes: dict[str, str] = {}
    assessed: list[SiteEvidence] = []
    finals: set[str] = set()
    for candidate in candidates:
        if not resolve_host(candidate):
            notes[candidate] = "no_dns"
            continue
        url = f"https://{candidate}/"
        if not robots.allowed(url):
            notes[candidate] = "robots_blocked"
            continue
        home = _get_page(
            client,
            url,
            budget=None,
            max_redirects=MAX_HOMEPAGE_REDIRECTS,
            allowed=robots.allowed,
        )
        final = registrable_domain(home.url) if _answered(home) else None
        if final is None:
            notes[candidate] = home.note or "unreachable"
            continue
        if final in finals:
            notes[candidate] = f"duplicate:{final}"
            continue
        finals.add(final)
        if is_platform_domain(home.url):
            assessed.append(assess(profile, final, [], platform=True))
            continue
        pages = _site_pages(client, profile, home, final, robots)
        assessed.append(assess(profile, final, pages))
    return IssuerFinding(issuer, decide(assessed), tuple(boards), tuple(candidates), notes)


# --- database --------------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuerResult:
    """What one issuer came to (one JSON line per issuer)."""

    cik: str
    name: str
    outcome: Outcome
    tier: str | None = None
    domain: str | None = None
    evidence: dict[str, str] | None = None
    domains: tuple[str, ...] = ()
    company_id: int | None = None
    review_id: int | None = None
    linked_events: int = 0
    retry: bool = False
    candidates: tuple[str, ...] = ()
    boards: tuple[dict[str, Any], ...] = ()

    def log_record(self) -> dict[str, Any]:
        return {
            "cik": self.cik,
            "issuer": self.name,
            "outcome": self.outcome,
            "tier": self.tier,
            "domain": self.domain,
            "evidence": self.evidence,
            "domains": list(self.domains),
            "company_id": self.company_id,
            "review_id": self.review_id,
            "linked_events": self.linked_events,
            "retry": self.retry,
            "candidates": list(self.candidates),
            "boards": list(self.boards),
        }


def _result(finding: IssuerFinding, outcome: Outcome, **extra: Any) -> IssuerResult:
    decision = finding.decision
    evidence = decision.evidence
    base: dict[str, Any] = {
        "cik": finding.issuer.cik,
        "name": finding.issuer.name,
        "outcome": outcome,
        "tier": decision.tier,
        "domain": evidence.domain if evidence is not None else None,
        "evidence": evidence.record() if evidence is not None else None,
        "domains": decision.domains,
        "retry": finding.issuer.retry,
        "candidates": finding.candidates,
        "boards": tuple(b.log_record() for b in finding.boards),
    }
    base.update(extra)
    return IssuerResult(**base)


def _planned(finding: IssuerFinding) -> Outcome:
    """The outcome a finding leads to without looking at the database (shadow mode)."""
    decision = finding.decision
    if decision.outcome == "linked":
        return "linked"
    if decision.outcome == "several_domains":
        return "conflict"
    if decision.outcome == "tier_c":
        return "tier_c"
    return "no_domain"


def _company_by_cik(session: Session, cik: str) -> Company | None:
    wanted = {cik, str(int(cik))}
    return session.scalars(select(Company).where(Company.cik.in_(sorted(wanted)))).first()


def _issuer_events(session: Session, cik: str) -> list[EventRow]:
    """Every unlinked EDGAR event of the CIK (Form D and any other form), oldest first."""
    wanted = _cik_forms([cik])
    stmt = (
        select(EventRow)
        .where(
            EventRow.source == "edgar",
            EventRow.company_id.is_(None),
            EventRow.payload["cik"].astext.in_(wanted),
        )
        .order_by(EventRow.occurred_at, EventRow.event_id)
    )
    return list(session.scalars(stmt))


def _open_items(session: Session, event_ids: Sequence[str]) -> list[ReviewQueueItem]:
    if not event_ids:
        return []
    stmt = select(ReviewQueueItem).where(
        ReviewQueueItem.kind == REVIEW_KIND,
        ReviewQueueItem.status == "open",
        ReviewQueueItem.payload["event_id"].astext.in_(list(event_ids)),
    )
    return list(session.scalars(stmt.order_by(ReviewQueueItem.id)))


def _queue_review(
    session: Session,
    finding: IssuerFinding,
    *,
    reason: str,
    now: datetime,
    proposed: Company | None = None,
) -> int:
    """One open ``entity_match`` item on the issuer's newest filing; reruns update it."""
    issuer = finding.issuer
    decision = finding.decision
    evidence = decision.evidence
    info: dict[str, Any] = {
        "reason": reason,
        "cik": issuer.cik,
        "tier": decision.tier,
        "domains": list(decision.domains) or ([evidence.domain] if evidence else []),
        "evidence": evidence.record() if evidence is not None else None,
        "amount_sold": issuer.amount_sold,
    }
    candidates: list[dict[str, Any]] = []
    if proposed is not None:
        info["proposed_company_id"] = proposed.id
        candidates = [
            {"company_id": proposed.id, "alias": proposed.domain, "ratio": None, "sort_ratio": None}
        ]
    existing = _open_items(session, [issuer.event_id])
    if existing:
        item = existing[0]
        payload = {**item.payload, SOURCE: info}
        if candidates and not payload.get("candidates"):
            payload["candidates"] = candidates
        item.payload = payload
    else:
        item = ReviewQueueItem(
            kind=REVIEW_KIND,
            payload={
                "event_id": issuer.event_id,
                "name": issuer.name,
                "state": issuer.state,
                "city": issuer.city,
                "candidates": candidates,
                SOURCE: info,
            },
            created_at=now,
        )
        session.add(item)
    session.flush()
    return item.id


def _title(city: str | None) -> str | None:
    return " ".join(w.capitalize() for w in city.split()) if city else None


def _create_company(session: Session, finding: IssuerFinding, evidence: SiteEvidence) -> Company:
    issuer = finding.issuer
    state = (issuer.state or "").strip().upper() or None
    company = Company(
        domain=evidence.domain,
        name=evidence.brand or issuer.name,
        legal_name=issuer.name,
        cik=issuer.cik,
        cik_source=f"issuer_first_{(evidence.tier or '').lower()}",
        hq_city=_title(issuer.city),
        hq_state=state,
        hq_country="US" if state in US_STATES else None,
        source="issuer_first",
    )
    session.add(company)
    session.flush()
    # The filing's names first, as trusted ``edgar`` aliases; then the brand and the domain
    # label (``manual``, the ADR 0033 label rule), which usually repeat the first.
    add_aliases(session, company.id, [issuer.name, *issuer.previous_names], source="edgar")
    names = [company.name]
    label = domain_label(company.domain)
    if label_alias_problem(label, company.domain) is None:
        names.append(label)
    add_aliases(session, company.id, names, source="manual")
    return company


def _link_events(session: Session, finding: IssuerFinding, company: Company, now: datetime) -> int:
    """Link every unlinked EDGAR event of the CIK with R0; resolve their open review items."""
    events = _issuer_events(session, finding.issuer.cik)
    tier = finding.decision.tier
    for event in events:
        link_event(session, event, company.id, trusted=True, cik_source=company.cik_source)
        previous = event.payload.get("match")
        match: dict[str, Any] = {
            "rule": "R0",
            "ratio": None,
            "candidates": [],
            "confidence": CONFIDENCE["R0"],
            "resolved_at": iso_utc(now),
            "linked_by": SOURCE,
            "tier": tier,
        }
        if isinstance(previous, dict):
            match["previous"] = previous
        event.payload = {**event.payload, "match": match}
    for item in _open_items(session, [e.event_id for e in events]):
        item.status = "resolved"
        item.resolved_at = now
        item.payload = {**item.payload, "resolved_by": SOURCE, "company_id": company.id}
    session.flush()
    return len(events)


def _next_retry(row: IssuerDiscovery, now: datetime) -> datetime | None:
    if row.attempts > len(RETRY_AFTER_DAYS):
        return None
    first = _aware(row.created_at) or now
    return first + timedelta(days=RETRY_AFTER_DAYS[row.attempts - 1])


def record(session: Session, finding: IssuerFinding, *, now: datetime) -> IssuerResult:
    """Write one issuer's outcome (module docstring, "Outcome"); flushes, never commits.

    Never replaces an existing ``companies.cik`` or domain: a CIK some company took while
    the network half ran is ``known_cik`` and nothing is written; a domain that belongs to
    a company becomes a review item.
    """
    issuer = finding.issuer
    if _company_by_cik(session, issuer.cik) is not None:
        return _result(finding, "known_cik")
    row = session.get(IssuerDiscovery, issuer.cik)
    if row is None:
        row = IssuerDiscovery(cik=issuer.cik, status="no_domain", attempts=0, created_at=now)
        session.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.last_tried_at = now
    row.next_retry_at = None
    decision = finding.decision
    evidence = decision.evidence
    row.tier = decision.tier
    row.domain = evidence.domain if evidence is not None else None
    row.evidence = evidence.record() if evidence is not None else None
    result: IssuerResult
    if decision.outcome == "linked" and evidence is not None:
        owner = session.scalars(select(Company).where(Company.domain == evidence.domain)).first()
        if owner is not None:
            row.status = "conflict"
            review = _queue_review(
                session, finding, reason="domain_exists", now=now, proposed=owner
            )
            result = _result(finding, "conflict", company_id=owner.id, review_id=review)
        else:
            company = _create_company(session, finding, evidence)
            linked = _link_events(session, finding, company, now)
            row.status = "linked"
            result = _result(finding, "linked", company_id=company.id, linked_events=linked)
    elif decision.outcome == "several_domains":
        row.status = "conflict"
        row.domain = decision.domains[0]
        review = _queue_review(session, finding, reason="several_domains", now=now)
        result = _result(finding, "conflict", review_id=review)
    elif decision.outcome == "tier_c":
        row.status = "tier_c"
        row.domain = decision.domains[0]
        review_id = None
        if (issuer.amount_sold or 0) >= TIER_C_REVIEW_MIN_SOLD:
            review_id = _queue_review(session, finding, reason="tier_c", now=now)
        result = _result(finding, "tier_c", review_id=review_id)
    else:
        row.status = "no_domain"
        row.next_retry_at = _next_retry(row, now)
        result = _result(finding, "no_domain")
    session.flush()
    return result


# --- the run ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuerRun:
    """What one :func:`discover_issuers` run did."""

    run_id: str
    apply: bool
    started_at: datetime
    finished_at: datetime
    results: list[IssuerResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    selected: int = 0
    deferred: int = 0
    requests: int = 0
    elapsed_s: float = 0.0

    @property
    def outcomes(self) -> dict[str, int]:
        counts = Counter(result.outcome for result in self.results)
        return {name: counts.get(name, 0) for name in _OUTCOMES}

    def counts(self) -> dict[str, int]:
        """The ``runs`` counters: issuers fetched, companies inserted (none in shadow mode,
        which writes nothing; ``outcomes`` says what it would do), the rest skipped."""
        inserted = self.outcomes["linked"] if self.apply else 0
        return {
            "fetched": len(self.results) + len(self.errors),
            "inserted": inserted,
            "skipped": len(self.results) - inserted,
            "errors": len(self.errors),
        }

    def run_line(self) -> dict[str, Any]:
        """The one JSON run line: the standard fields plus the outcome counts."""
        return {
            "run_id": self.run_id,
            "source": SOURCE,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            **self.counts(),
            "mode": "apply" if self.apply else "shadow",
            "selected": self.selected,
            "deferred": self.deferred,
            "requests": self.requests,
            "outcomes": self.outcomes,
            "elapsed_s": round(self.elapsed_s, 1),
        }


class _HostLimiter:
    """An ``httpx`` request hook: one :class:`HostRateBudgets` slot per request, counted."""

    def __init__(self, rps: float, *, clock: Clock, sleep: Sleeper) -> None:
        self._budgets = HostRateBudgets(rps, 1, clock=clock, sleep=sleep)
        self._lock = threading.Lock()
        self.requests = 0

    def __call__(self, request: httpx.Request) -> None:
        with self._lock:
            self.requests += 1
        self._budgets.for_host(request.url.host).acquire()


@contextmanager
def _request_hook(client: httpx.Client, hook: Callable[[httpx.Request], None]) -> Iterator[None]:
    """``hook`` runs before every request ``client`` sends, for the duration of the block."""
    hooks = client.event_hooks
    previous = list(hooks.get("request", []))
    client.event_hooks = {**hooks, "request": [*previous, hook]}
    try:
        yield
    finally:
        client.event_hooks = {**client.event_hooks, "request": previous}


def _drive(
    count: int,
    workers: int,
    *,
    investigate_one: Callable[[int], IssuerFinding],
    record_one: Callable[[int, Future[IssuerFinding]], None],
    out_of_time: Callable[[], bool],
) -> int:
    """Investigate items on up to ``workers`` threads; record each on this thread as it ends."""
    submitted = 0
    pending: dict[Future[IssuerFinding], int] = {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tell-issuers")
    try:
        while True:
            while submitted < count and len(pending) < workers and not out_of_time():
                pending[pool.submit(investigate_one, submitted)] = submitted
                submitted += 1
            if not pending:
                return submitted
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(finished, key=pending.__getitem__):
                record_one(pending.pop(future), future)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"now must be a timezone-aware datetime, got naive {value!r}"
        raise ValueError(msg)


def discover_issuers(
    session: Session,
    client: httpx.Client,
    *,
    now: datetime,
    apply: bool = False,
    since: datetime | None = None,
    limit: int | None = None,
    retry_due: bool = False,
    concurrency: int | None = None,
    max_minutes: float | None = None,
    rps: float = HOST_RPS,
    resolve_host: ResolveHost | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleeper | None = None,
    commit: Commit | None = None,
) -> IssuerRun:
    """One issuer-first discovery run (module docstring).

    ``apply=False`` (shadow) writes nothing at all; ``apply=True`` records every issuer on
    this thread in its own savepoint and calls ``commit`` after each one, so no database
    transaction stays open while the network half runs and a killed job keeps every
    finished issuer. ``commit=None`` only flushes (the caller owns the transaction, as the
    tests do). No new issuer starts once ``max_minutes`` of ``clock`` time have passed.
    """
    _require_aware(now)
    if since is not None:
        _require_aware(since)
    workers = DEFAULT_CONCURRENCY if concurrency is None else concurrency
    minutes = DEFAULT_MAX_MINUTES if max_minutes is None else max_minutes
    if workers < 1:
        msg = f"concurrency must be >= 1, got {workers}"
        raise ValueError(msg)
    if minutes <= 0:
        msg = f"max_minutes must be > 0, got {minutes}"
        raise ValueError(msg)
    started = clock()
    run_id = start_run(session, SOURCE, now=now) if apply else new_run_id(SOURCE, now)
    issuers = select_issuers(
        session,
        now=now,
        since=since,
        limit=DEFAULT_LIMIT if limit is None else limit,
        retry_due=retry_due,
    )
    if commit is not None:
        commit(session)  # end the read (and the runs row) before any network request

    pause = sleep if sleep is not None else time.sleep
    limiter = _HostLimiter(rps, clock=clock, sleep=pause)
    user_agent = client.headers.get("User-Agent") or get_settings().tell_http_user_agent
    robots = RobotsCache(client, user_agent=user_agent)
    done: list[tuple[int, IssuerResult]] = []
    errors: list[tuple[int, str]] = []

    resolve = resolve_host if resolve_host is not None else dns_resolves

    def investigate_one(index: int) -> IssuerFinding:
        return investigate(client, issuers[index], robots=robots, resolve_host=resolve, sleep=pause)

    def record_one(index: int, future: Future[IssuerFinding]) -> None:
        issuer = issuers[index]
        try:
            finding = future.result()
            if apply:
                with session.begin_nested():
                    result = record(session, finding, now=now)
            else:
                result = _result(finding, _planned(finding))
        except Exception:
            _logger.exception("issuer discovery failed for CIK %s", issuer.cik)
            errors.append((index, issuer.cik))
        else:
            done.append((index, result))
            _logger.info(json.dumps(result.log_record(), sort_keys=True))
        if apply and commit is not None:
            commit(session)

    with _request_hook(client, limiter):
        submitted = _drive(
            len(issuers),
            workers,
            investigate_one=investigate_one,
            record_one=record_one,
            out_of_time=lambda: clock() - started >= minutes * 60.0,
        )

    elapsed = max(0.0, clock() - started)
    run = IssuerRun(
        run_id=run_id,
        apply=apply,
        started_at=now,
        finished_at=now + timedelta(seconds=elapsed),
        results=[result for _, result in sorted(done, key=lambda item: item[0])],
        errors=[cik for _, cik in sorted(errors)],
        selected=len(issuers),
        deferred=len(issuers) - submitted,
        requests=limiter.requests,
        elapsed_s=elapsed,
    )
    _finish(session, run, retry_due=retry_due, commit=commit)
    return run


def _finish(session: Session, run: IssuerRun, *, retry_due: bool, commit: Commit | None) -> None:
    """Close the ``runs`` row (``--apply``; it logs the run line) or log the shadow run line."""
    if not run.apply:
        _logger.info(json.dumps(run.run_line(), sort_keys=True))
        return
    finish_run(
        session,
        run.run_id,
        status="ok",
        **run.counts(),
        notes={
            "outcomes": run.outcomes,
            "selected": run.selected,
            "deferred": run.deferred,
            "requests": run.requests,
            "retry_due": retry_due,
        },
        now=run.finished_at,
    )
    session.flush()
    if commit is not None:
        commit(session)
