"""ATS discovery: domain to job board (spec section 4).

Given ``acme.com``, find the company's job board or record that none was found.

1. Fetch ``https://acme.com/careers``, ``/jobs``, ``/join``, ``/company/careers`` and the
   homepage, in that order, stopping at the first 200. Redirects are followed by hand,
   at most :data:`MAX_REDIRECTS`; a redirect whose target is itself a board URL (or a
   hiring channel such as LinkedIn or Notion) is the answer and is not fetched.
   ``robots.txt`` is honoured per origin (RFC 9309 status rules); a disallowed candidate
   is skipped and recorded as ``robots_blocked:<path>``.
2. Scan the page: ``<iframe src>``, ``<script src>``, ``<a href>`` (via ``lxml.html``),
   then the raw HTML text (inline scripts, data attributes, JSON with ``\\/`` escapes
   undone), against :data:`PATTERNS`. Supported vendors win over unsupported ATS vendors,
   which win over listing sites (YC Work at a Startup); within a class the first hit
   wins. ``grnh.se`` short links rank last: they are per-posting links with an opaque
   code, not board tokens. A chosen short link is resolved with one GET (redirects not
   followed) whose ``Location`` must name ``boards.greenhouse.io/{token}`` or
   ``job-boards.greenhouse.io/{token}``; otherwise discovery falls through to step 3.

   **Deeper scan** (2026-10-14 recall audit), only when that first answer named no ATS,
   all inside :data:`MAX_DEEP_REQUESTS` per domain, stopping at the first ATS found: the
   homepage (unless it was the first answer), up to :data:`MAX_LINKS` same-site careers
   links of the first page and the homepage (one hop), and, only when nothing at all
   was found, up to :data:`MAX_SCRIPTS` same-site JavaScript bundles
   (:data:`MAX_SCRIPT_BYTES` in total) of the careers pages (else the homepage). Careers
   links to Notion, Google Docs/Forms or LinkedIn jobs are recorded as hiring channels
   (:func:`channel_of`); they are never fetched.
3. Nothing found (or an unresolvable short link): guess slugs from the domain label
   (``acme``, ``acmeinc``, ``acme-inc``) against each documented public API in turn; a
   200 with at least one job counts. Skipped when a hiring channel was found and no
   board at all. Page hits get confidence 0.95, guesses 0.6.
4. Verify ownership: the board's company name (Greenhouse ``/v1/boards/{token}`` ->
   ``name``; Lever and Ashby: the ``<title>`` of the hosted board page; SmartRecruiters:
   ``content[0].company.name`` of the postings API) must reach
   ``rapidfuzz.fuzz.token_set_ratio >= 85`` against the domain label or the company
   name. Otherwise a ``review_queue`` item (``kind='board_verify'``) is written and the
   board is not activated (a new row is stored with ``status='paused'``, which no
   collector polls, until a human resolves the item).
5. Write ``ats_boards``: ``status='active'`` for a verified supported vendor,
   ``'unsupported'`` for :data:`UNSUPPORTED_VENDORS` (Workday / Oracle / Workable per
   ``docs/decisions/0003``, further ATS vendors and YC Work at a Startup; never fetched
   further, never polled) and, when nothing is found, one row
   ``vendor='none', identifier=<domain>, status='no_board'`` whose ``discovered_from``
   is ``checked:`` plus the comma-joined pages checked, then ``;via:`` plus the hiring
   channels seen, if any, so coverage is reportable.

Re-verification (:func:`reverify_due`): every 30 days, and whenever a board has
answered 404 twice in a row. ``ats_boards`` has no 404 counter; every ATS collector
already marks a board ``status='dead'`` on its second consecutive 404 (Greenhouse,
Ashby and SmartRecruiters compare ``last_polled_at`` with ``last_verified_at``; Lever
keeps the streak in an open ``review_queue`` item), so ``dead`` *is* the
"two consecutive 404s" marker. Every discovery outcome stamps ``last_verified_at = now``
on the company's ``dead`` boards and its ``no_board`` marker, so a dead board is due
again only after 30 days or after a poll newer than that stamp, not on every batch.

Every result is logged as one JSON line on the ``tell.discovery`` logger:
``{domain, outcome, vendor, identifier, confidence, pages_checked, channels}``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from lxml import etree
from lxml import html as lxml_html
from rapidfuzz import fuzz
from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, start_run
from tell.config import get_settings
from tell.discovery import domain_label, registrable_domain
from tell.http import Clock, RateBudget, Sleeper, fetch
from tell.models import AtsBoard, Company, ReviewQueueItem, Run

__all__ = [
    "CANDIDATE_PATHS",
    "CHANNELS",
    "GREENHOUSE_SHORT_LINK",
    "GUESS_CONFIDENCE",
    "LISTING_VENDORS",
    "MATCH_THRESHOLD",
    "MAX_DEEP_REQUESTS",
    "MAX_LINKS",
    "MAX_REDIRECTS",
    "MAX_SCRIPTS",
    "MAX_SCRIPT_BYTES",
    "NO_BOARD_VENDOR",
    "PAGE_CONFIDENCE",
    "PATTERNS",
    "REVERIFY_AFTER",
    "SCRIPT_DEADLINE_S",
    "SITE_TIMEOUT_S",
    "SUPPORTED_VENDORS",
    "UNSUPPORTED_VENDORS",
    "BatchResult",
    "BoardHit",
    "BoardPattern",
    "Commit",
    "DiscoveryResult",
    "Outcome",
    "RobotsCache",
    "board_name",
    "channel_of",
    "choose_hit",
    "discover_batch",
    "discover_domain",
    "find_boards",
    "find_boards_in_script",
    "guess_slugs",
    "hosted_board_url",
    "name_ratio",
    "resolve_short_link",
    "reverify_due",
    "select_due_boards",
    "select_new_companies",
]

Outcome = Literal["board", "unsupported", "no_board", "review"]

CANDIDATE_PATHS: tuple[str, ...] = ("/careers", "/jobs", "/join", "/company/careers", "/")
"""Pages fetched in this order; the first 200 is scanned and the rest are not fetched
(the deeper scan may still read the homepage, see the module docstring)."""

MAX_REDIRECTS = 2
"""Redirects followed per candidate page (spec section 4 step 1)."""

ROBOTS_MAX_REDIRECTS = 5
"""Redirects followed for ``robots.txt`` (RFC 9309 asks for at least five)."""

SITE_TIMEOUT_S = 10.0
"""Per-request timeout for company pages and ``robots.txt``; vendor APIs keep the default.

A dead or tar-pitted company host otherwise holds a worker for the full client timeout
(20 s) on every candidate path."""

PAGE_CONFIDENCE = Decimal("0.95")
GUESS_CONFIDENCE = Decimal("0.60")
NO_BOARD_CONFIDENCE = Decimal("0.00")

MATCH_THRESHOLD = 85.0
"""Minimum ``token_set_ratio`` between the board's company name and the domain label / name."""

REVERIFY_AFTER = timedelta(days=30)

SUPPORTED_VENDORS: tuple[str, ...] = ("greenhouse", "lever", "ashby", "smartrecruiters")
"""Vendors with a documented public API; slug guesses try them in this order."""

LISTING_VENDORS: tuple[str, ...] = ("yc_waas",)
"""Job listing sites that are not an ATS (YC Work at a Startup): recorded like an
unsupported vendor, but they rank after every ATS and do not end the deeper scan."""

UNSUPPORTED_VENDORS: tuple[str, ...] = (
    "workday",
    "oracle",
    "workable",
    "dover",
    "gem",
    "comeet",
    "recruitee",
    "personio",
    "teamtailor",
    "bamboohr",
    "rippling",
    "breezy",
    "wellfound",
    "jobvite",
    "jazzhr",
    "pinpoint",
    "icims",
    "homerun",
    "join",
    "zohorecruit",
    "polymer",
    *LISTING_VENDORS,
)
"""Detected and recorded with ``status='unsupported'``; never fetched further or polled.

Workday, Oracle and Workable per ``docs/decisions/0003``; the rest from the 2026-10-14
recall audit, so coverage reports can say where a company hires instead of ``no_board``."""

MAX_LINKS = 2
"""Same-site careers links followed (one hop) when the first page found no ATS."""

MAX_SCRIPTS = 5
"""Same-site JavaScript bundles read when the pages found nothing at all."""

MAX_SCRIPT_BYTES = 2_000_000
"""Total bytes read from those bundles per domain (a bundle is cut off at the remainder)."""

SCRIPT_DEADLINE_S = 2 * SITE_TIMEOUT_S
"""Wall-clock limit on reading one bundle. :data:`SITE_TIMEOUT_S` applies per read, so a
host that drips bytes could otherwise hold a worker for minutes; reading stops at the
first chunk after the deadline and the bytes read so far are scanned. Worst case per
bundle: this plus one read timeout."""

MAX_DEEP_REQUESTS = 12
"""Hard cap on the requests of the deeper scan (homepage, links with their redirect hops,
bundles) per domain; ``robots.txt`` of a new origin is not counted (once per run)."""

NO_BOARD_VENDOR = "none"
"""``ats_boards.vendor`` of the coverage marker row written when nothing is found."""

REVIEW_KIND = "board_verify"
DISCOVERY_SOURCE = "discovery"

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"
LEVER_API = "https://api.lever.co/v0/postings"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board"
SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies"
LEVER_EU_PREFIX = "eu:"
GREENHOUSE_SHORT_LINK = "https://grnh.se"
"""Greenhouse per-posting short links (``grnh.se/{code}``); the code is not a board token."""

GRNH_SE_PATTERN = "grnh_se"

_logger = logging.getLogger("tell.discovery")


# --- patterns ---------------------------------------------------------------------------


def _group1(match: re.Match[str]) -> str:
    return match.group(1)


def _lever_identifier(match: re.Match[str]) -> str:
    site = match.group(2).rstrip(".")
    return f"{LEVER_EU_PREFIX}{site}" if match.group(1) else site


def _ashby_identifier(match: re.Match[str]) -> str:
    return unquote(match.group(1).rstrip("."))


def _workday_identifier(match: re.Match[str]) -> str:
    return f"{match.group(1).lower()}.{match.group(2).lower()}/{match.group(3)}"


def _oracle_identifier(match: re.Match[str]) -> str:
    return match.group(1).lower()


@dataclass(frozen=True)
class BoardPattern:
    """One vendor URL shape: ``regex`` finds it, ``identifier`` turns the match into a key."""

    vendor: str
    name: str
    regex: re.Pattern[str]
    identifier: Callable[[re.Match[str]], str]


_START = r"(?<![A-Za-z0-9.-])"
"""A host must start at a boundary (``//``, a quote, whitespace), not inside another name."""

_SLUG = r"[A-Za-z0-9][A-Za-z0-9_-]*"

_SUB = (
    r"(?!(?:www|app|api|help|support|blog|docs|status|cdn|assets|static|careers|jobs)\.)"
    r"([A-Za-z0-9][A-Za-z0-9-]*)"
)
"""A vendor-hosted company subdomain (``acme`` in ``acme.recruitee.com``), not the vendor's own."""


def _pattern(
    vendor: str, name: str, regex: str, identifier: Callable[[re.Match[str]], str]
) -> BoardPattern:
    return BoardPattern(vendor, name, re.compile(regex, re.IGNORECASE), identifier)


PATTERNS: tuple[BoardPattern, ...] = (
    _pattern(
        "greenhouse",
        "greenhouse_embed",
        _START
        + r"(?:job-boards|boards)\.greenhouse\.io/embed/job_(?:board(?:/js)?|app)/?"
        + r"[^\s\"'<>]*?[?&;]for=("
        + _SLUG
        + ")",
        _group1,
    ),
    _pattern(
        "greenhouse",
        "greenhouse_board",
        _START + r"(?:job-boards|boards)\.greenhouse\.io/(?!embed(?:[/?#\"'\s]|$))(" + _SLUG + ")",
        _group1,
    ),
    _pattern(
        "greenhouse",
        "greenhouse_api",
        _START + r"boards-api\.greenhouse\.io/v1/boards/(" + _SLUG + ")",
        _group1,
    ),
    _pattern("greenhouse", GRNH_SE_PATTERN, _START + r"grnh\.se/(" + _SLUG + ")", _group1),
    _pattern(
        "lever",
        "lever",
        _START + r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9][A-Za-z0-9._-]*)",
        _lever_identifier,
    ),
    _pattern(
        "lever",
        "lever_api",
        _START + r"api\.(eu\.)?lever\.co/v0/postings/([A-Za-z0-9][A-Za-z0-9._-]*)",
        _lever_identifier,
    ),
    _pattern(
        "ashby",
        "ashby_board",
        _START + r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._%-]*)",
        _ashby_identifier,
    ),
    _pattern(
        "ashby",
        "ashby_api",
        _START + r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9][A-Za-z0-9._%-]*)",
        _ashby_identifier,
    ),
    _pattern(
        "smartrecruiters",
        "smartrecruiters",
        _START + r"careers\.smartrecruiters\.com/(" + _SLUG + ")",
        _group1,
    ),
    _pattern(
        "smartrecruiters",
        "smartrecruiters_jobs",
        _START + r"jobs\.smartrecruiters\.com/(?!oneclick-ui(?:[/?#\"'\s]|$))(" + _SLUG + ")",
        _group1,
    ),
    _pattern(
        "smartrecruiters",
        "smartrecruiters_api",
        _START + r"api\.smartrecruiters\.com/v1/companies/(" + _SLUG + ")",
        _group1,
    ),
    _pattern(
        "workday",
        "workday",
        _START
        + r"([A-Za-z0-9-]+)\.(wd[1-5])\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?("
        + _SLUG
        + ")",
        _workday_identifier,
    ),
    _pattern(
        "workable",
        "workable",
        _START + r"apply\.workable\.com/(?!(?:j|api)(?:[/?#\"'\s]|$))(" + _SLUG + ")",
        _group1,
    ),
    _pattern(
        "oracle",
        "oracle",
        _START + r"((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})/hcmUI/CandidateExperience",
        _oracle_identifier,
    ),
    # Further ATS vendors without a collector: recorded as ``unsupported``, never polled.
    _pattern(
        "dover", "dover", _START + r"app\.dover\.(?:com|io)/(?:jobs|apply)/(" + _SLUG + ")", _group1
    ),
    _pattern("gem", "gem", _START + r"jobs\.gem\.com/(" + _SLUG + ")", _group1),
    _pattern(
        "comeet", "comeet", _START + r"(?:www\.)?comeet\.(?:com|co)/jobs/(" + _SLUG + ")/", _group1
    ),
    _pattern(
        "comeet",
        "comeet_embed",
        r"COMEET\.init\(\s*\{[^}]{0,400}?[\"']company-uid[\"']\s*:\s*[\"']([A-Za-z0-9.]+)[\"']",
        _group1,
    ),
    _pattern("recruitee", "recruitee", _START + _SUB + r"\.recruitee\.com", _group1),
    _pattern("personio", "personio", _START + _SUB + r"\.jobs\.personio\.(?:de|com)", _group1),
    _pattern("teamtailor", "teamtailor", _START + _SUB + r"\.teamtailor\.com", _group1),
    _pattern("bamboohr", "bamboohr", _START + _SUB + r"\.bamboohr\.com/(?:careers|jobs)", _group1),
    _pattern("rippling", "rippling", _START + r"ats\.rippling\.com/(" + _SLUG + ")", _group1),
    _pattern("breezy", "breezy", _START + _SUB + r"\.breezy\.hr", _group1),
    _pattern(
        "wellfound",
        "wellfound",
        _START + r"(?:wellfound\.com|angel\.co)/company/(" + _SLUG + ")/jobs",
        _group1,
    ),
    _pattern("jobvite", "jobvite", _START + r"jobs\.jobvite\.com/(" + _SLUG + ")", _group1),
    _pattern("jazzhr", "jazzhr", _START + _SUB + r"\.applytojob\.com", _group1),
    _pattern("pinpoint", "pinpoint", _START + _SUB + r"\.pinpointhq\.com", _group1),
    _pattern(
        "icims",
        "icims",
        _START + r"(?:careers|jobs)-([A-Za-z0-9][A-Za-z0-9-]*)\.icims\.com",
        _group1,
    ),
    _pattern("homerun", "homerun", _START + _SUB + r"\.homerun\.co(?![A-Za-z0-9.-])", _group1),
    _pattern("join", "join", _START + r"join\.com/companies/(" + _SLUG + ")", _group1),
    _pattern(
        "zohorecruit", "zohorecruit", _START + _SUB + r"\.zohorecruit\.(?:com|eu|in)", _group1
    ),
    _pattern("polymer", "polymer", _START + r"jobs\.polymer\.co/(" + _SLUG + ")", _group1),
    # Job listing sites (not an ATS): recorded as ``unsupported``, never fetched or polled.
    _pattern(
        "yc_waas",
        "yc_company_jobs",
        _START + r"(?:www\.)?ycombinator\.com/companies/(" + _SLUG + ")/jobs",
        _group1,
    ),
    _pattern(
        "yc_waas",
        "workatastartup",
        _START + r"(?:www\.)?workatastartup\.com/companies/(" + _SLUG + ")",
        _group1,
    ),
)
"""Every vendor URL shape of spec section 4 step 2, in priority order within a vendor.

The spec's shapes come first; the further vendors after Oracle were added from the
2026-10-14 recall audit (``docs/methods/discovery.md``, "Patterns")."""

_CHANNEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("notion", re.compile(_START + r"(?:[A-Za-z0-9-]+\.notion\.site|(?:www\.)?notion\.so)/", re.I)),
    ("google_forms", re.compile(_START + r"(?:docs\.google\.com/forms/|forms\.gle/)", re.I)),
    (
        "google_docs",
        re.compile(_START + r"docs\.google\.com/(?:document|spreadsheets|presentation)/", re.I),
    ),
    (
        "linkedin",
        re.compile(
            _START + r"(?:[a-z]{2,3}\.)?linkedin\.com/(?:company/[^/\s\"'<>]+/jobs|jobs/)", re.I
        ),
    ),
)
"""Hiring channels that are not job boards: a careers link (or a candidate page's redirect)
pointing here is recorded on the ``no_board`` marker as ``via:<name>``; never fetched."""

CHANNELS: tuple[str, ...] = tuple(name for name, _ in _CHANNEL_PATTERNS)


def channel_of(url: str) -> str | None:
    """The hiring channel a URL points at (``notion``, ``linkedin``, ...), or ``None``."""
    for name, regex in _CHANNEL_PATTERNS:
        if regex.search(url):
            return name
    return None


@dataclass(frozen=True)
class BoardHit:
    """A board reference found on a page (or by a slug guess)."""

    vendor: str
    identifier: str
    matched: str
    source: str
    """``redirect``, ``iframe``, ``script``, ``a``, ``text`` or ``guess``."""
    pattern: str = ""
    """The :data:`PATTERNS` name that matched (empty for guesses)."""


def _hits_in(text: str, source: str) -> Iterable[BoardHit]:
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            identifier = pattern.identifier(match)
            if identifier:
                yield BoardHit(pattern.vendor, identifier, match.group(0), source, pattern.name)


def _attribute_values(content: bytes) -> list[tuple[str, str]]:
    """``(source, value)`` for every iframe/script ``src`` and ``a href``, in that order."""
    if not content.strip():
        return []
    try:
        document = lxml_html.document_fromstring(content)
    except (etree.ParserError, ValueError):
        return []
    values: list[tuple[str, str]] = []
    for tag, attribute in (("iframe", "src"), ("script", "src"), ("a", "href")):
        for element in document.iter(tag):
            value = element.get(attribute)
            if value:
                values.append((tag, value))
    return values


_ESCAPED_SLASH = re.compile(r"\\/|\\u002[fF]")


def _unescape(text: str) -> str:
    r"""``https:\/\/jobs.lever.co\/acme`` (JSON, JS strings) -> ``https://jobs.lever.co/acme``."""
    return _ESCAPED_SLASH.sub("/", text)


def _distinct(sources: Iterable[tuple[str, str]]) -> list[BoardHit]:
    hits: list[BoardHit] = []
    seen: set[tuple[str, str]] = set()
    for source, text in sources:
        for hit in _hits_in(text, source):
            key = (hit.vendor, hit.identifier)
            if key not in seen:
                seen.add(key)
                hits.append(hit)
    return hits


def find_boards(content: bytes | str, *, redirects: Sequence[str] = ()) -> list[BoardHit]:
    """Every distinct board reference in a page, in scan order (redirects, attributes, text).

    The raw text is scanned with JSON/JS slash escapes undone (``jobs.ashbyhq.com\\/acme``).
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    sources: list[tuple[str, str]] = [("redirect", url) for url in redirects]
    sources += _attribute_values(raw)
    sources.append(("text", _unescape(raw.decode("utf-8", errors="replace"))))
    return _distinct(sources)


def find_boards_in_script(content: bytes | str) -> list[BoardHit]:
    """Every distinct board reference in a JavaScript bundle (plain text, escapes undone)."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    return _distinct([("bundle", _unescape(text))])


def _hit_rank(hit: BoardHit) -> int:
    if hit.pattern == GRNH_SE_PATTERN:
        return 3
    if hit.vendor in LISTING_VENDORS:
        return 2
    return 0 if hit.vendor in SUPPORTED_VENDORS else 1


def choose_hit(hits: Sequence[BoardHit]) -> BoardHit | None:
    """The first supported-vendor hit, else the first unsupported ATS, else a listing site.

    Listing sites (:data:`LISTING_VENDORS`, YC Work at a Startup) rank after every ATS:
    companies with a real ATS often link their YC listing too. ``grnh.se`` short links
    rank after everything else: the code is a per-posting link, not a board token, and
    only becomes a board through :func:`resolve_short_link`.
    """
    return min(hits, key=_hit_rank) if hits else None


def _is_ats_hit(hit: BoardHit) -> bool:
    """A board on a real ATS (supported or not): the answer, no deeper scan needed."""
    return hit.pattern != GRNH_SE_PATTERN and hit.vendor not in LISTING_VENDORS


def _matches_board(url: str) -> bool:
    return any(pattern.regex.search(url) for pattern in PATTERNS)


def _known_destination(url: str) -> bool:
    """A redirect target that is already the answer: a board URL or a hiring channel."""
    return _matches_board(url) or channel_of(url) is not None


# --- fetching ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Page:
    url: str
    """The last URL requested (the one that answered, or the one that redirected)."""
    status: int | None
    content: bytes = b""
    redirects: tuple[str, ...] = ()
    note: str | None = None
    """``error``, ``too_many_redirects``, ``robots_blocked`` or ``redirect_to_board``."""


class _Budget(Protocol):
    """Anything that hands out request slots: :class:`RateBudget` or a per-domain meter."""

    def acquire(self) -> float: ...


def _get_page(
    client: httpx.Client,
    url: str,
    *,
    budget: _Budget | None,
    max_redirects: int = MAX_REDIRECTS,
    allowed: Callable[[str], bool] | None = None,
    stop_at: Callable[[str], bool] | None = None,
    timeout: float = SITE_TIMEOUT_S,
) -> _Page:
    """GET ``url`` following at most ``max_redirects`` redirects by hand; one attempt each.

    Company sites are not retried (a flaky homepage is a miss, not a reason to wait 15 s)
    and each request times out after ``timeout`` seconds (:data:`SITE_TIMEOUT_S`).
    ``allowed`` is consulted before each redirect hop (robots), ``stop_at`` stops on a
    redirect target that is already the answer (a board URL), without fetching it.
    """
    current = url
    redirects: list[str] = []
    status: int | None = None
    for hop in range(max_redirects + 1):
        if budget is not None:
            budget.acquire()
        try:
            response = client.get(current, follow_redirects=False, timeout=timeout)
        except httpx.HTTPError:
            return _Page(current, None, redirects=tuple(redirects), note="error")
        if not response.is_redirect:
            return _Page(current, response.status_code, response.content, tuple(redirects))
        target = urljoin(current, response.headers["location"])
        redirects.append(target)
        status = response.status_code
        if stop_at is not None and stop_at(target):
            return _Page(current, status, redirects=tuple(redirects), note="redirect_to_board")
        if hop == max_redirects:
            break
        if allowed is not None and not allowed(target):
            return _Page(current, status, redirects=tuple(redirects), note="robots_blocked")
        current = target
    return _Page(current, status, redirects=tuple(redirects), note="too_many_redirects")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


_DISALLOW_ALL: tuple[str, ...] = ("User-agent: *", "Disallow: /")
"""Rules equivalent to "keep out entirely" (401/403 and unreachable robots.txt)."""


class RobotsCache:
    """``robots.txt`` per origin, fetched once through the shared client and budget.

    Status handling follows RFC 9309 section 2.3.1: 2xx is parsed; 4xx (other than
    401/403, which CPython's ``robotparser`` and most crawlers treat as "keep out")
    means no rules; 5xx, transport errors and redirect loops mean "unreachable", which
    the RFC says to treat as a complete disallow.

    Thread-safe: discovery workers share one cache. Each origin has its own lock, so
    ``robots.txt`` is fetched exactly once per origin even when several workers ask at the
    same moment, while different origins load in parallel.
    """

    def __init__(
        self, client: httpx.Client, *, user_agent: str, budget: RateBudget | None = None
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._budget = budget
        self._parsers: dict[str, RobotFileParser] = {}
        self._lock = threading.Lock()
        self._origin_locks: dict[str, threading.Lock] = {}

    def _load(self, origin: str) -> RobotFileParser:
        parser = RobotFileParser()
        page = _get_page(
            self._client,
            f"{origin}/robots.txt",
            budget=self._budget,
            max_redirects=ROBOTS_MAX_REDIRECTS,
        )
        status = page.status if page.note is None else None
        if status is not None and 200 <= status < 300:
            parser.parse(page.content.decode("utf-8", errors="replace").splitlines())
        elif status in (401, 403):
            parser.parse(_DISALLOW_ALL)
        elif status is not None and 400 <= status < 500:
            parser.parse([])
        else:
            parser.parse(_DISALLOW_ALL)
        return parser

    def allowed(self, url: str) -> bool:
        """``True`` when ``url`` may be fetched by this user agent."""
        return self._parser(_origin(url)).can_fetch(self._user_agent, url)

    def _parser(self, origin: str) -> RobotFileParser:
        with self._lock:
            parser = self._parsers.get(origin)
            if parser is not None:
                return parser
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:
            # Another worker may have loaded it while this one waited for the origin lock.
            with self._lock:
                parser = self._parsers.get(origin)
            if parser is None:
                parser = self._load(origin)
                with self._lock:
                    self._parsers[origin] = parser
        return parser


@dataclass(frozen=True)
class _Scan:
    hits: list[BoardHit]
    pages: dict[tuple[str, str], str]
    """``(vendor, identifier)`` -> the URL of the page (or bundle) that carried the hit."""
    pages_checked: tuple[str, ...]
    channels: tuple[str, ...] = ()
    """Hiring channels seen (:func:`channel_of`), in the order first seen."""

    def page_for(self, hit: BoardHit) -> str | None:
        return self.pages.get((hit.vendor, hit.identifier))


def _url_key(url: str) -> str:
    """``https://www.acme.com/careers/`` and ``https://acme.com/careers`` are one page."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def _short(url: str, domain: str) -> str:
    """The path of a same-site URL, else ``host/path``; queries dropped (``pages_checked``)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    return path if host in (domain, f"www.{domain}") else f"{host}{path}"


class _Found:
    """What one domain's page scan has seen so far (used by one worker thread only)."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.hits: list[BoardHit] = []
        self.pages: dict[tuple[str, str], str] = {}
        self.channels: list[str] = []
        self.checked: list[str] = []
        self.fetched: set[str] = set()
        """:func:`_url_key` of every URL requested (candidates, redirect hops, links)."""

    def add(self, hits: Iterable[BoardHit], url: str) -> None:
        for hit in hits:
            key = (hit.vendor, hit.identifier)
            if key not in self.pages:
                self.pages[key] = url
                self.hits.append(hit)

    def channel(self, url: str) -> None:
        name = channel_of(url)
        if name is not None and name not in self.channels:
            self.channels.append(name)

    def has_ats(self) -> bool:
        return any(_is_ats_hit(hit) for hit in self.hits)

    def result(self) -> _Scan:
        return _Scan(list(self.hits), dict(self.pages), tuple(self.checked), tuple(self.channels))


class _Meter:
    """Counts one domain's deeper-scan requests against :data:`MAX_DEEP_REQUESTS`.

    Every slot still comes from the run's shared budget; the meter only counts them.
    """

    def __init__(self, budget: _Budget | None, limit: int) -> None:
        self._budget = budget
        self.limit = limit
        self.used = 0

    @property
    def left(self) -> int:
        return self.limit - self.used

    def acquire(self) -> float:
        self.used += 1
        return self._budget.acquire() if self._budget is not None else 0.0


_CAREERS_TEXT = re.compile(
    r"\b(?:careers?|jobs?|join\s+(?:us|the\s+team|our\s+team)|hiring|open\s+(?:roles|positions)"
    r"|positions|vacanc(?:y|ies)|work\s+with\s+us|trabaja(?:\s+con\s+nosotros)?|karriere"
    r"|emplois?|stellenangebote)\b",
    re.IGNORECASE,
)
"""Anchor text of a careers link (``Careers``, ``We're hiring``, ``Open roles``, ...)."""

_CAREERS_PATH = re.compile(
    r"(?:^|[/_.-])(?:careers?|jobs?|join-?us|hiring|open-?roles|positions|vacanc(?:y|ies)"
    r"|work-?with-?us|trabaja\w*|karriere|emplois?)(?=$|[/_.-])",
    re.IGNORECASE,
)
"""URL path of a careers link (``/careers``, ``/company/jobs``, ``/careers/position-1``)."""


def _careers_anchor(url: str, text: str) -> bool:
    return bool(
        _CAREERS_TEXT.search(" ".join(text.split())) or _CAREERS_PATH.search(urlsplit(url).path)
    )


def _document(content: bytes) -> Any | None:
    if not content.strip():
        return None
    try:
        return lxml_html.document_fromstring(content)
    except (etree.ParserError, ValueError):
        return None


def _anchors(page: _Page) -> list[tuple[str, str]]:
    """``(absolute URL without fragment, anchor text)`` of every ``<a href>`` on the page."""
    document = _document(page.content)
    if document is None:
        return []
    anchors: list[tuple[str, str]] = []
    for element in document.iter("a"):
        href = (element.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(page.url, href)
        parts = urlsplit(url)
        if parts.scheme in ("http", "https"):
            anchors.append((parts._replace(fragment="").geturl(), element.text_content()))
    return anchors


def _same_site(url: str, domain: str) -> bool:
    return registrable_domain(urlsplit(url).hostname or "") == domain


_SCRIPT_CDNS: tuple[tuple[str, str], ...] = (("framerusercontent.com", "/sites/"),)
"""``(host, path prefix)`` of site-builder CDNs that serve a site's own page bundles."""

_SCRIPT_FIRST = re.compile(r"careers?|jobs?|join|hiring", re.IGNORECASE)
_SCRIPT_PAGE = re.compile(
    r"/(?:pages|app|sites)/|(?:^|/)(?:index|main|app|_app)[-.]", re.IGNORECASE
)
_SCRIPT_LAST = re.compile(r"polyfill|webpack|framework|jquery|vendor|runtime", re.IGNORECASE)


def _script_rank(url: str) -> int:
    """Careers-named bundles first, then page/app bundles, then the rest, framework last."""
    path = urlsplit(url).path
    if _SCRIPT_FIRST.search(path):
        return 0
    if _SCRIPT_LAST.search(path):
        return 3
    return 1 if _SCRIPT_PAGE.search(path) else 2


def _script_ok(url: str, domain: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if any(host == cdn and parts.path.startswith(prefix) for cdn, prefix in _SCRIPT_CDNS):
        return True
    return _same_site(url, domain)


def _script_urls(domain: str, pages: Iterable[_Page]) -> list[str]:
    """Same-site ``<script src>`` URLs of the pages, best first (:func:`_script_rank`)."""
    urls: dict[str, None] = {}
    for page in pages:
        document = _document(page.content)
        if document is None:
            continue
        for element in document.iter("script"):
            src = (element.get("src") or "").strip()
            if not src:
                continue
            url = urlsplit(urljoin(page.url, src))._replace(fragment="").geturl()
            if _script_ok(url, domain):
                urls.setdefault(url, None)
    return sorted(urls, key=_script_rank)


def _get_script(
    client: httpx.Client,
    url: str,
    *,
    budget: _Budget,
    limit: int,
    clock: Clock = time.monotonic,
) -> bytes | None:
    """One GET (no redirects, no retries) reading at most ``limit`` bytes; ``None`` unless 200.

    Reading stops after :data:`SCRIPT_DEADLINE_S` seconds (measured with ``clock`` from
    the request), keeping what was read.
    """
    budget.acquire()
    deadline = clock() + SCRIPT_DEADLINE_S
    try:
        with client.stream("GET", url, follow_redirects=False, timeout=SITE_TIMEOUT_S) as response:
            if response.status_code != httpx.codes.OK:
                return None
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) >= limit or clock() >= deadline:
                    break
            return bytes(body[:limit])
    except httpx.HTTPError:
        return None


def _visit(
    client: httpx.Client,
    url: str,
    found: _Found,
    *,
    budget: _Budget | None,
    robots: RobotsCache,
    max_redirects: int = MAX_REDIRECTS,
) -> _Page:
    """Fetch one page and record what it shows: board hits, channels in its redirects."""
    page = _get_page(
        client,
        url,
        budget=budget,
        max_redirects=max_redirects,
        allowed=robots.allowed,
        stop_at=_known_destination,
    )
    found.fetched.update(_url_key(u) for u in (url, *page.redirects, page.url))
    for target in page.redirects:
        found.channel(target)
    if page.note == "redirect_to_board":
        found.add(find_boards(b"", redirects=page.redirects), page.url)
    elif page.status == httpx.codes.OK:
        found.add(find_boards(page.content, redirects=page.redirects), page.url)
    return page


def _answered(page: _Page) -> bool:
    return page.status == httpx.codes.OK and page.note is None


def _scan_pages(
    client: httpx.Client, domain: str, *, robots: RobotsCache, budget: RateBudget | None
) -> _Scan:
    """Steps 1-2: the candidate pages up to the first answer, then the deeper scan if needed."""
    found = _Found(domain)
    for path in CANDIDATE_PATHS:
        url = f"https://{domain}{path}"
        if not robots.allowed(url):
            found.checked.append(f"robots_blocked:{path}")
            continue
        found.checked.append(path)
        page = _visit(client, url, found, budget=budget, robots=robots)
        if page.note == "redirect_to_board" or page.status == httpx.codes.OK:
            if not found.has_ats():
                _deepen(client, found, page, robots=robots, budget=budget)
            break
    return found.result()


def _deep_fetch(
    client: httpx.Client,
    found: _Found,
    url: str,
    label: str,
    *,
    meter: _Meter,
    robots: RobotsCache,
) -> _Page | None:
    """One page of the deeper scan, inside the meter; the page when it answered 200."""
    if meter.left < 1:
        return None
    if not robots.allowed(url):
        found.checked.append(f"robots_blocked:{label}")
        return None
    found.checked.append(label)
    page = _visit(
        client,
        url,
        found,
        budget=meter,
        robots=robots,
        max_redirects=min(MAX_REDIRECTS, meter.left - 1),
    )
    return page if _answered(page) else None


def _note_channels(found: _Found, page: _Page, *, careers_page: bool) -> None:
    """Channels linked from a careers page, or from a careers link on any other page."""
    for url, text in _anchors(page):
        if channel_of(url) is not None and (careers_page or _careers_anchor(url, text)):
            found.channel(url)


def _career_links(found: _Found, pages: Iterable[_Page]) -> list[str]:
    """Same-site careers links on the pages that were not requested yet, in page order."""
    links: dict[str, str] = {}
    for page in pages:
        for url, text in _anchors(page):
            key = _url_key(url)
            if key in found.fetched or key in links or not _same_site(url, found.domain):
                continue
            if _careers_anchor(url, text):
                links[key] = url
    return list(links.values())


def _deepen(
    client: httpx.Client,
    found: _Found,
    first: _Page,
    *,
    robots: RobotsCache,
    budget: RateBudget | None,
) -> None:
    """The deeper scan, run when the first answering page named no ATS.

    In order, stopping as soon as an ATS board (supported or not) is known, all inside
    :data:`MAX_DEEP_REQUESTS`: the homepage (unless it was the first page), up to
    :data:`MAX_LINKS` same-site careers links from the first page and the homepage (one
    hop), and, only when nothing at all was found, up to :data:`MAX_SCRIPTS` same-site
    bundles (:data:`MAX_SCRIPT_BYTES` in total) referenced by the careers pages, or by the
    homepage when there is no careers page.
    """
    meter = _Meter(budget, MAX_DEEP_REQUESTS)
    home_url = f"https://{found.domain}/"
    careers: list[_Page] = []
    home: _Page | None = None
    if _answered(first):
        if _url_key(first.url) == _url_key(home_url):
            home = first
        else:
            careers.append(first)
    if home is None and _url_key(home_url) not in found.fetched:
        home = _deep_fetch(client, found, home_url, "/", meter=meter, robots=robots)
        if found.has_ats():
            return
    for careers_page in careers:
        _note_channels(found, careers_page, careers_page=True)
    if home is not None:
        _note_channels(found, home, careers_page=False)
    sources = [*careers, *([home] if home is not None else [])]
    for link in _career_links(found, sources)[:MAX_LINKS]:
        label = "link:" + _short(link, found.domain)
        linked = _deep_fetch(client, found, link, label, meter=meter, robots=robots)
        if linked is not None:
            careers.append(linked)
            _note_channels(found, linked, careers_page=True)
        if found.has_ats():
            return
    if found.hits or found.channels:
        return
    _scan_scripts(client, found, careers or sources, meter=meter, robots=robots)


def _scan_scripts(
    client: httpx.Client,
    found: _Found,
    pages: Sequence[_Page],
    *,
    meter: _Meter,
    robots: RobotsCache,
) -> None:
    remaining = MAX_SCRIPT_BYTES
    for url in _script_urls(found.domain, pages)[:MAX_SCRIPTS]:
        if meter.left < 1 or remaining <= 0:
            return
        label = "script:" + _short(url, found.domain)
        if not robots.allowed(url):
            found.checked.append(f"robots_blocked:{label}")
            continue
        found.checked.append(label)
        body = _get_script(client, url, budget=meter, limit=remaining)
        if body is None:
            continue
        remaining -= len(body)
        found.add(find_boards_in_script(body), url)
        if found.hits:
            return


_GREENHOUSE_BOARD_URL = re.compile(
    r"^https?://(?:job-boards|boards)\.greenhouse\.io/(?!embed(?:[/?#]|$))(" + _SLUG + ")",
    re.IGNORECASE,
)
"""A short link's ``Location`` naming a board: ``[job-]boards.greenhouse.io/{token}``."""


def resolve_short_link(
    client: httpx.Client, hit: BoardHit, *, budget: RateBudget | None = None
) -> BoardHit | None:
    """Turn a ``grnh.se`` hit into the Greenhouse board its redirect names, or ``None``.

    One GET with ``follow_redirects=False`` (no retries); only a redirect whose
    ``Location`` is ``boards.greenhouse.io/{token}`` or ``job-boards.greenhouse.io/{token}``
    resolves. Anything else (no redirect, an error, a redirect to the company's own
    site) returns ``None`` and discovery falls through to the slug guesses.
    """
    url = f"{GREENHOUSE_SHORT_LINK}/{_quoted(hit.identifier)}"
    if budget is not None:
        budget.acquire()
    try:
        response = client.get(url, follow_redirects=False)
    except httpx.HTTPError:
        return None
    if not response.is_redirect:
        return None
    location = urljoin(url, response.headers["location"])
    match = _GREENHOUSE_BOARD_URL.match(location)
    if match is None:
        return None
    return BoardHit("greenhouse", match.group(1), location, hit.source, "greenhouse_board")


def _get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    budget: RateBudget | None,
    sleep: Sleeper,
) -> Any | None:
    """A documented vendor API GET (retried per ``tell.http``); ``None`` unless 200 + JSON."""
    try:
        response = fetch(url, client=client, params=params, budget=budget, sleep=sleep)
    except httpx.HTTPError:
        return None
    if response.status_code != httpx.codes.OK:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _page_title(
    client: httpx.Client, url: str, *, budget: RateBudget | None, sleep: Sleeper
) -> str | None:
    try:
        response = fetch(url, client=client, budget=budget, sleep=sleep)
    except httpx.HTTPError:
        return None
    if response.status_code != httpx.codes.OK or not response.content.strip():
        return None
    try:
        document = lxml_html.document_fromstring(response.content)
    except (etree.ParserError, ValueError):
        return None
    title = document.findtext(".//title")
    text = " ".join(title.split()) if title else ""
    return text or None


def _quoted(identifier: str) -> str:
    return quote(identifier, safe="")


def hosted_board_url(vendor: str, identifier: str) -> str:
    """The vendor-hosted public board page for a supported vendor."""
    if vendor == "greenhouse":
        return f"https://job-boards.greenhouse.io/{_quoted(identifier)}"
    if vendor == "lever":
        if identifier.startswith(LEVER_EU_PREFIX):
            return f"https://jobs.eu.lever.co/{_quoted(identifier[len(LEVER_EU_PREFIX) :])}"
        return f"https://jobs.lever.co/{_quoted(identifier)}"
    if vendor == "ashby":
        return f"https://jobs.ashbyhq.com/{_quoted(identifier)}"
    if vendor == "smartrecruiters":
        return f"https://careers.smartrecruiters.com/{_quoted(identifier)}"
    msg = f"no hosted board page for vendor {vendor!r}"
    raise ValueError(msg)


def _smartrecruiters_postings(identifier: str) -> str:
    return f"{SMARTRECRUITERS_API}/{_quoted(identifier)}/postings"


def _smartrecruiters_name(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None
    company = content[0].get("company")
    if isinstance(company, dict) and isinstance(company.get("name"), str):
        return str(company["name"])
    return None


def board_name(
    client: httpx.Client,
    vendor: str,
    identifier: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> str | None:
    """The company name a supported board publishes about itself (step 4), or ``None``."""
    if vendor == "greenhouse":
        data = _get_json(
            client, f"{GREENHOUSE_API}/{_quoted(identifier)}", budget=budget, sleep=sleep
        )
        name = data.get("name") if isinstance(data, dict) else None
        return name if isinstance(name, str) and name.strip() else None
    if vendor == "smartrecruiters":
        data = _get_json(
            client,
            _smartrecruiters_postings(identifier),
            params={"limit": "1"},
            budget=budget,
            sleep=sleep,
        )
        return _smartrecruiters_name(data)
    if vendor in ("lever", "ashby"):
        return _page_title(client, hosted_board_url(vendor, identifier), budget=budget, sleep=sleep)
    msg = f"board_name is only defined for supported vendors, not {vendor!r}"
    raise ValueError(msg)


# --- slug guesses ---------------------------------------------------------------------------


def guess_slugs(domain: str) -> tuple[str, ...]:
    """``acme.com`` -> ``("acme", "acmeinc", "acme-inc")`` (spec section 4 step 3)."""
    label = domain_label(domain)
    if not label:
        return ()
    slugs = (label, f"{label}inc", f"{label}-inc")
    return tuple(dict.fromkeys(slugs))


@dataclass(frozen=True)
class _Probe:
    vendor: str
    identifier: str
    url: str
    board_name: str | None


def _probe_request(vendor: str, slug: str) -> tuple[str, dict[str, str] | None]:
    quoted = _quoted(slug)
    if vendor == "greenhouse":
        return f"{GREENHOUSE_API}/{quoted}/jobs", None
    if vendor == "lever":
        return f"{LEVER_API}/{quoted}", {"mode": "json", "limit": "1"}
    if vendor == "ashby":
        return f"{ASHBY_API}/{quoted}", None
    return _smartrecruiters_postings(slug), {"limit": "1"}


def _job_count(vendor: str, data: Any) -> int:
    if vendor == "lever":
        return len(data) if isinstance(data, list) else 0
    if not isinstance(data, dict):
        return 0
    if vendor == "smartrecruiters":
        total = data.get("totalFound")
        content = data.get("content")
        listed = len(content) if isinstance(content, list) else 0
        return max(total if isinstance(total, int) else 0, listed)
    jobs = data.get("jobs")
    return len(jobs) if isinstance(jobs, list) else 0


def _guess(
    client: httpx.Client, domain: str, *, budget: RateBudget | None, sleep: Sleeper
) -> _Probe | None:
    for slug in guess_slugs(domain):
        for vendor in SUPPORTED_VENDORS:
            url, params = _probe_request(vendor, slug)
            data = _get_json(client, url, params=params, budget=budget, sleep=sleep)
            if data is None or _job_count(vendor, data) < 1:
                continue
            name = _smartrecruiters_name(data) if vendor == "smartrecruiters" else None
            return _Probe(vendor, slug, url, name)
    return None


# --- ownership -------------------------------------------------------------------------------


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise(text: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", text.lower()).split())


def name_ratio(board: str | None, candidates: Iterable[str | None]) -> float:
    """Best ``token_set_ratio`` (0-100) between the board's name and any candidate name."""
    if not board or not _normalise(board):
        return 0.0
    left = _normalise(board)
    scores = [
        float(fuzz.token_set_ratio(left, _normalise(candidate)))
        for candidate in candidates
        if candidate and _normalise(candidate)
    ]
    return max(scores, default=0.0)


# --- results ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryResult:
    """What :func:`discover_domain` decided for one domain."""

    domain: str
    outcome: Outcome
    vendor: str
    identifier: str
    confidence: Decimal
    discovered_from: str
    pages_checked: tuple[str, ...]
    company_id: int
    board_id: int | None = None
    board_name: str | None = None
    ratio: float | None = None
    review_id: int | None = None
    channels: tuple[str, ...] = ()
    """Hiring channels that are not job boards, seen on the company's careers links."""

    def log_record(self) -> dict[str, Any]:
        """The one JSON line logged per result."""
        return {
            "domain": self.domain,
            "outcome": self.outcome,
            "vendor": self.vendor,
            "identifier": self.identifier,
            "confidence": float(self.confidence),
            "pages_checked": list(self.pages_checked),
            "channels": list(self.channels),
        }


def _log(result: DiscoveryResult) -> DiscoveryResult:
    _logger.info(json.dumps(result.log_record(), sort_keys=True))
    return result


# --- database writes ------------------------------------------------------------------------


def _ensure_company(session: Session, domain: str, company_name: str | None) -> Company:
    company = session.scalars(select(Company).where(Company.domain == domain)).first()
    if company is None:
        company = Company(domain=domain, name=company_name or domain)
        session.add(company)
        session.flush()
    return company


def _find_board(session: Session, vendor: str, identifier: str) -> AtsBoard | None:
    stmt = select(AtsBoard).where(AtsBoard.vendor == vendor, AtsBoard.identifier == identifier)
    return session.scalars(stmt).first()


def _write_board(
    session: Session,
    company: Company,
    *,
    vendor: str,
    identifier: str,
    discovered_from: str,
    confidence: Decimal,
    status: str,
    now: datetime,
) -> AtsBoard:
    board = _find_board(session, vendor, identifier)
    if board is None:
        board = AtsBoard(company_id=company.id, vendor=vendor, identifier=identifier)
        session.add(board)
    elif board.company_id is None:
        board.company_id = company.id
    board.discovered_from = discovered_from
    board.confidence = confidence
    board.status = status
    board.last_verified_at = now
    session.flush()
    return board


def _drop_no_board_marker(session: Session, domain: str) -> None:
    session.execute(
        delete(AtsBoard).where(AtsBoard.vendor == NO_BOARD_VENDOR, AtsBoard.identifier == domain)
    )


def _queue_review(session: Session, payload: dict[str, Any], now: datetime) -> ReviewQueueItem:
    """One open ``board_verify`` item per (domain, vendor, identifier); reruns reuse it."""
    key = {k: payload[k] for k in ("domain", "vendor", "identifier")}
    stmt = (
        select(ReviewQueueItem)
        .where(
            ReviewQueueItem.kind == REVIEW_KIND,
            ReviewQueueItem.status == "open",
            ReviewQueueItem.payload.contains(key),
        )
        .order_by(ReviewQueueItem.id)
        .limit(1)
    )
    item = session.scalars(stmt).first()
    if item is None:
        item = ReviewQueueItem(kind=REVIEW_KIND, payload=payload, created_at=now)
        session.add(item)
    else:
        item.payload = {**item.payload, **payload}
    session.flush()
    return item


def _resolve_reviews(
    session: Session, domain: str, vendor: str, identifier: str, now: datetime
) -> None:
    """A board that now verifies closes the open ``board_verify`` items discovery raised for it."""
    key = {"domain": domain, "vendor": vendor, "identifier": identifier}
    stmt = select(ReviewQueueItem).where(
        ReviewQueueItem.kind == REVIEW_KIND,
        ReviewQueueItem.status == "open",
        ReviewQueueItem.payload.contains(key),
    )
    for item in session.scalars(stmt):
        item.status = "resolved"
        item.resolved_at = now
    session.flush()


# --- the procedure ---------------------------------------------------------------------------


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"now must be a timezone-aware datetime, got naive {value!r}"
        raise ValueError(msg)


def _user_agent(client: httpx.Client) -> str:
    return client.headers.get("User-Agent") or get_settings().tell_http_user_agent


def _normalized(domain: str) -> str:
    normalized = registrable_domain(domain)
    if normalized is None:
        msg = f"not a registrable domain: {domain!r}"
        raise ValueError(msg)
    return normalized


def discover_domain(
    session: Session,
    client: httpx.Client,
    domain: str,
    company_name: str | None = None,
    *,
    now: datetime,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
    robots: RobotsCache | None = None,
) -> DiscoveryResult:
    """Run spec section 4 steps 1-5 for one domain and write the outcome to the database.

    Creates the ``companies`` row (``name = company_name or domain``) when the domain
    is unknown. Flushes but does not commit.
    """
    _require_aware(now)
    normalized = _normalized(domain)
    company = _ensure_company(session, normalized, company_name)
    robots = robots or RobotsCache(client, user_agent=_user_agent(client), budget=budget)
    finding = _investigate(client, normalized, budget=budget, sleep=sleep, robots=robots)
    return _log(_store(session, company, finding, company_name, now))


def _store(
    session: Session,
    company: Company,
    finding: _Finding,
    company_name: str | None,
    now: datetime,
) -> DiscoveryResult:
    """Steps 4-5 (database only): record what :func:`_investigate` found, stamp dead boards."""
    result = _record(session, company, finding, company_name, now)
    _mark_verified(session, company, finding.domain, now)
    return result


def _mark_verified(session: Session, company: Company, domain: str, now: datetime) -> None:
    """Stamp ``last_verified_at = now`` on the company's ``dead`` boards and ``none`` marker.

    Runs after every outcome, so a dead board that discovery could not revive is not
    picked again by the next batch (:func:`reverify_due`).
    """
    stmt = select(AtsBoard).where(
        or_(
            and_(AtsBoard.company_id == company.id, AtsBoard.status == "dead"),
            and_(AtsBoard.vendor == NO_BOARD_VENDOR, AtsBoard.identifier == domain),
        )
    )
    for board in session.scalars(stmt):
        board.last_verified_at = now
    session.flush()


@dataclass(frozen=True)
class _Finding:
    """What the network half of discovery found for one domain (no database involved)."""

    domain: str
    pages_checked: tuple[str, ...]
    hit: BoardHit | None
    """``None``: nothing found (the ``no_board`` outcome)."""
    confidence: Decimal = NO_BOARD_CONFIDENCE
    discovered_from: str = ""
    board_name: str | None = None
    """The board's own company name (supported vendors only; ``None`` when unavailable)."""
    channels: tuple[str, ...] = ()
    """Hiring channels seen on the way (``notion``, ``linkedin``, ...); see :func:`channel_of`."""


def _investigate(
    client: httpx.Client,
    normalized: str,
    *,
    budget: RateBudget | None,
    sleep: Sleeper,
    robots: RobotsCache,
) -> _Finding:
    """Steps 1-4's requests for one domain: page scan, short link, guesses, board name.

    Touches no database, so batch workers run it in parallel while one writer records the
    findings (:func:`discover_batch`).
    """
    scan = _scan_pages(client, normalized, robots=robots, budget=budget)
    hit = choose_hit(scan.hits)
    page_url = scan.page_for(hit) if hit is not None else None
    if hit is not None and hit.pattern == GRNH_SE_PATTERN:
        hit = resolve_short_link(client, hit, budget=budget)
    probe_name: str | None = None
    if hit is not None and page_url is not None:
        confidence, discovered_from = PAGE_CONFIDENCE, page_url
    else:
        # A careers link to a hiring channel (Notion, LinkedIn, ...) says where the company
        # hires; slug guesses could then only find another company's board.
        probe = (
            None
            if scan.channels and not scan.hits
            else _guess(client, normalized, budget=budget, sleep=sleep)
        )
        if probe is None:
            marker = "checked:" + ",".join(scan.pages_checked)
            if scan.channels:
                marker += ";via:" + ",".join(scan.channels)
            return _Finding(
                normalized,
                scan.pages_checked,
                None,
                discovered_from=marker,
                channels=scan.channels,
            )
        hit = BoardHit(probe.vendor, probe.identifier, probe.url, "guess")
        confidence, discovered_from, probe_name = GUESS_CONFIDENCE, probe.url, probe.board_name
    name: str | None = None
    if hit.vendor not in UNSUPPORTED_VENDORS:
        name = probe_name or board_name(
            client, hit.vendor, hit.identifier, budget=budget, sleep=sleep
        )
    return _Finding(
        normalized,
        scan.pages_checked,
        hit,
        confidence,
        discovered_from,
        name,
        scan.channels,
    )


def _record(
    session: Session,
    company: Company,
    finding: _Finding,
    company_name: str | None,
    now: datetime,
) -> DiscoveryResult:
    found = finding.hit
    if found is None:
        return _record_no_board(session, company, finding, now)
    normalized = finding.domain
    confidence, discovered_from = finding.confidence, finding.discovered_from

    def result(
        outcome: Outcome,
        board_id: int | None,
        *,
        name: str | None = None,
        ratio: float | None = None,
        review_id: int | None = None,
    ) -> DiscoveryResult:
        return DiscoveryResult(
            domain=normalized,
            outcome=outcome,
            vendor=found.vendor,
            identifier=found.identifier,
            confidence=confidence,
            discovered_from=discovered_from,
            pages_checked=finding.pages_checked,
            company_id=company.id,
            board_id=board_id,
            board_name=name,
            ratio=ratio,
            review_id=review_id,
            channels=finding.channels,
        )

    if found.vendor in UNSUPPORTED_VENDORS:
        board = _write_board(
            session,
            company,
            vendor=found.vendor,
            identifier=found.identifier,
            discovered_from=discovered_from,
            confidence=confidence,
            status="unsupported",
            now=now,
        )
        _drop_no_board_marker(session, normalized)
        return result("unsupported", board.id)

    name = finding.board_name
    ratio = name_ratio(name, [domain_label(normalized), company_name, company.name])
    existing = _find_board(session, found.vendor, found.identifier)
    owned_elsewhere = existing is not None and existing.company_id not in (None, company.id)
    if owned_elsewhere or ratio < MATCH_THRESHOLD:
        payload: dict[str, Any] = {
            "domain": normalized,
            "vendor": found.vendor,
            "identifier": found.identifier,
            "board_name": name,
            "ratio": ratio,
            "discovered_from": discovered_from,
            "reason": "owned_by_other_company" if owned_elsewhere else "name_mismatch",
        }
        item = _queue_review(session, payload, now)
        if existing is None:
            # Not activated: stored paused (never polled) until the review item is resolved.
            existing = AtsBoard(
                company_id=company.id,
                vendor=found.vendor,
                identifier=found.identifier,
                discovered_from=discovered_from,
                confidence=confidence,
                status="paused",
            )
            session.add(existing)
            session.flush()
        if not owned_elsewhere:
            _drop_no_board_marker(session, normalized)
        return result("review", existing.id, name=name, ratio=ratio, review_id=item.id)

    board = _write_board(
        session,
        company,
        vendor=found.vendor,
        identifier=found.identifier,
        discovered_from=discovered_from,
        confidence=confidence,
        status="active",
        now=now,
    )
    _drop_no_board_marker(session, normalized)
    _resolve_reviews(session, normalized, found.vendor, found.identifier, now)
    return result("board", board.id, name=name, ratio=ratio)


def _record_no_board(
    session: Session, company: Company, finding: _Finding, now: datetime
) -> DiscoveryResult:
    board = _write_board(
        session,
        company,
        vendor=NO_BOARD_VENDOR,
        identifier=finding.domain,
        discovered_from=finding.discovered_from,
        confidence=NO_BOARD_CONFIDENCE,
        status="no_board",
        now=now,
    )
    return DiscoveryResult(
        domain=finding.domain,
        outcome="no_board",
        vendor=NO_BOARD_VENDOR,
        identifier=finding.domain,
        confidence=NO_BOARD_CONFIDENCE,
        discovered_from=finding.discovered_from,
        pages_checked=finding.pages_checked,
        company_id=company.id,
        board_id=board.id,
        channels=finding.channels,
    )


# --- re-verification and batches -----------------------------------------------------------


def reverify_due(board: AtsBoard, now: datetime) -> bool:
    """``True`` when discovery should look at this board's domain again.

    * ``paused`` boards wait for a human (their ``board_verify`` item): never due.
    * Due when never verified or last verified 30 days ago or more.
    * ``dead`` boards (every ATS collector sets ``dead`` on the second consecutive 404,
      spec section 4's "404 twice in a row") are also due when they were polled after
      their last verification. Discovery stamps ``last_verified_at`` on dead boards
      after every outcome, so a board it could not revive waits for the next poll or
      30 days instead of being re-checked on every batch.
    """
    _require_aware(now)
    if board.status == "paused":
        return False
    verified = board.last_verified_at
    if verified is None or now - verified >= REVERIFY_AFTER:
        return True
    polled = board.last_polled_at
    return board.status == "dead" and polled is not None and verified < polled


def select_new_companies(session: Session, limit: int) -> list[Company]:
    """Companies with no ``ats_boards`` row and no open ``board_verify`` item, oldest first."""
    if limit < 1:
        return []
    has_board = exists().where(AtsBoard.company_id == Company.id)
    in_review = exists().where(
        ReviewQueueItem.kind == REVIEW_KIND,
        ReviewQueueItem.status == "open",
        ReviewQueueItem.payload.op("->>")("domain") == Company.domain,
    )
    stmt = (
        select(Company)
        .where(~has_board, ~in_review)
        .order_by(Company.created_at, Company.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def select_due_boards(session: Session, now: datetime, limit: int) -> list[AtsBoard]:
    """Boards :func:`reverify_due` selects, one per company, least recently verified first."""
    if limit < 1:
        return []
    cutoff = now - REVERIFY_AFTER
    stmt = (
        select(AtsBoard)
        .where(
            AtsBoard.company_id.is_not(None),
            AtsBoard.status != "paused",
            or_(
                AtsBoard.last_verified_at.is_(None),
                AtsBoard.last_verified_at <= cutoff,
                and_(
                    AtsBoard.status == "dead",
                    AtsBoard.last_polled_at.is_not(None),
                    AtsBoard.last_verified_at < AtsBoard.last_polled_at,
                ),
            ),
        )
        .order_by(AtsBoard.last_verified_at.asc().nulls_first(), AtsBoard.id)
    )
    due: list[AtsBoard] = []
    companies: set[int] = set()
    for board in session.scalars(stmt):
        if board.company_id is None or board.company_id in companies:
            continue
        if not reverify_due(board, now):
            continue
        companies.add(board.company_id)
        due.append(board)
        if len(due) >= limit:
            break
    return due


Commit = Callable[[Session], None]
"""Makes everything the batch wrote so far durable; the CLI passes ``Session.commit``."""

_OUTCOMES: tuple[Outcome, ...] = ("board", "unsupported", "review", "no_board")


def _outcome_counts(results: Iterable[DiscoveryResult]) -> dict[str, int]:
    counts = Counter(result.outcome for result in results)
    return {name: counts.get(name, 0) for name in _OUTCOMES}


@dataclass(frozen=True)
class BatchResult:
    """What one :func:`discover_batch` run did."""

    run_id: str
    results: list[DiscoveryResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    new_domains: int = 0
    reverified: int = 0
    deferred: int = 0
    """Selected domains never started because the wall-clock budget ran out."""
    budget_exhausted: bool = False
    elapsed_s: float = 0.0
    concurrency: int = 1

    @property
    def outcomes(self) -> dict[str, int]:
        """Count per outcome: the coverage numbers (``board`` over all is the hit rate)."""
        return _outcome_counts(self.results)

    @property
    def processed(self) -> int:
        """Domains finished, with an outcome or an error."""
        return len(self.results) + len(self.errors)


def _batch_targets(
    session: Session, now: datetime, new_limit: int, due_limit: int
) -> tuple[list[tuple[str, str]], int, int]:
    """``(domain, name)`` to visit: new companies first, then boards due for re-verification."""
    new = select_new_companies(session, new_limit)
    targets: list[tuple[str, str]] = [(company.domain, company.name) for company in new]
    seen = {company.id for company in new}
    reverify = 0
    for board in select_due_boards(session, now, due_limit):
        company = session.get(Company, board.company_id)
        if company is None or company.id in seen:
            continue
        seen.add(company.id)
        targets.append((company.domain, company.name))
        reverify += 1
    return targets, len(new), reverify


def _run_counts(results: Sequence[DiscoveryResult], errors: int) -> dict[str, int]:
    outcomes = _outcome_counts(results)
    return {
        "fetched": len(results) + errors,
        "inserted": outcomes["board"] + outcomes["unsupported"],
        "skipped": outcomes["review"] + outcomes["no_board"],
        "errors": errors,
    }


def _progress(
    session: Session, run_id: str, results: Sequence[DiscoveryResult], errors: int
) -> None:
    """Keep the open ``runs`` row's counters current, so a killed run still shows its work."""
    run = session.get(Run, run_id)
    if run is None:
        return
    counts = _run_counts(results, errors)
    run.fetched = counts["fetched"]
    run.inserted = counts["inserted"]
    run.skipped = counts["skipped"]
    run.errors = counts["errors"]


def _durable(session: Session, commit: Commit | None) -> None:
    session.flush()
    if commit is not None:
        commit(session)


def _drive(
    count: int,
    workers: int,
    *,
    investigate: Callable[[int], _Finding],
    record: Callable[[int, Future[_Finding]], None],
    out_of_time: Callable[[], bool],
) -> int:
    """Run ``investigate(i)`` for ``i < count`` on up to ``workers`` threads.

    Each finished item is handed to ``record`` on the calling thread (the single database
    writer) as soon as it completes. No new item starts once ``out_of_time()``; in-flight
    ones still finish. Returns how many items were started.
    """
    submitted = 0
    pending: dict[Future[_Finding], int] = {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tell-discovery")
    try:
        while True:
            while submitted < count and len(pending) < workers:
                if out_of_time():
                    count = submitted
                    break
                pending[pool.submit(investigate, submitted)] = submitted
                submitted += 1
            if not pending:
                return submitted
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(finished, key=pending.__getitem__):
                record(pending.pop(future), future)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def discover_batch(
    session: Session,
    client: httpx.Client,
    *,
    now: datetime,
    limit: int | None = None,
    rps: float | None = None,
    reverify_limit: int | None = None,
    budget: RateBudget | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
    concurrency: int | None = None,
    max_minutes: float | None = None,
    commit: Commit | None = None,
) -> BatchResult:
    """One discovery pass under the spec section 11 budget.

    ``limit`` (default ``TELL_DISCOVERY_BUDGET``, 300) caps the *new* domains: companies
    with no ``ats_boards`` row, oldest first. Boards due for re-verification are taken
    afterwards, capped separately by ``reverify_limit`` (default: ``limit``).

    Up to ``concurrency`` (default ``TELL_DISCOVERY_CONCURRENCY``, 6) domains are
    investigated at once on worker threads, and every request of the run (robots, pages,
    vendor APIs) still draws from the ONE :class:`RateBudget` at ``rps`` (default
    ``TELL_DISCOVERY_RPS``, 4): the request rate is unchanged, only the waiting on slow
    or dead hosts overlaps. Workers never touch the database; this thread is the single
    writer and records each domain as it finishes, in its own savepoint (a failure is
    counted in ``errors`` and the run continues), then calls ``commit`` so a killed job
    keeps every finished domain. ``commit=None`` only flushes (the caller owns the
    transaction, as the tests do).

    No new domain starts once ``max_minutes`` (default ``TELL_DISCOVERY_MAX_MINUTES``,
    22) of ``clock`` time have passed; in-flight domains finish and the rest are deferred
    to the next run (``notes.budget_exhausted``, ``notes.deferred``).

    One ``runs`` row with ``source='discovery'``, committed at the start and kept current
    after every domain: ``fetched`` = domains processed, ``inserted`` = boards found
    (``board`` + ``unsupported``), ``skipped`` = ``review`` + ``no_board``, outcome counts
    in ``notes``. ``finished_at`` is ``now`` plus the elapsed ``clock`` time.
    """
    _require_aware(now)
    started = clock()
    settings = get_settings()
    new_limit = settings.tell_discovery_budget if limit is None else limit
    due_limit = new_limit if reverify_limit is None else reverify_limit
    rate = settings.tell_discovery_rps if rps is None else rps
    workers = settings.tell_discovery_concurrency if concurrency is None else concurrency
    minutes = settings.tell_discovery_max_minutes if max_minutes is None else max_minutes
    if workers < 1:
        msg = f"concurrency must be >= 1, got {workers}"
        raise ValueError(msg)
    if minutes <= 0:
        msg = f"max_minutes must be > 0, got {minutes}"
        raise ValueError(msg)
    bucket = budget or RateBudget(rate, clock=clock, sleep=sleep)
    robots = RobotsCache(client, user_agent=_user_agent(client), budget=bucket)
    run_id = start_run(session, DISCOVERY_SOURCE, now=now)
    targets, new_count, reverify = _batch_targets(session, now, new_limit, due_limit)
    _durable(session, commit)

    def investigate(domain: str) -> _Finding:
        return _investigate(client, _normalized(domain), budget=bucket, sleep=sleep, robots=robots)

    done_results: list[tuple[int, DiscoveryResult]] = []
    done_errors: list[tuple[int, str]] = []

    def record(index: int, future: Future[_Finding]) -> None:
        domain, name = targets[index]
        try:
            finding = future.result()
            with session.begin_nested():
                company = _ensure_company(session, finding.domain, name)
                result = _store(session, company, finding, name, now)
        except Exception:
            _logger.exception("discovery failed for %s", domain)
            done_errors.append((index, domain))
        else:
            done_results.append((index, _log(result)))
        _progress(session, run_id, [r for _, r in done_results], len(done_errors))
        _durable(session, commit)

    def out_of_time() -> bool:
        return clock() - started >= minutes * 60.0

    submitted = _drive(
        len(targets),
        workers,
        investigate=lambda index: investigate(targets[index][0]),
        record=record,
        out_of_time=out_of_time,
    )
    if submitted < len(targets):
        _logger.warning(
            "discovery: %s-minute budget used up; %d domains deferred",
            minutes,
            len(targets) - submitted,
        )

    elapsed = max(0.0, clock() - started)
    deferred = len(targets) - submitted
    results = [result for _, result in sorted(done_results, key=lambda item: item[0])]
    errors = [domain for _, domain in sorted(done_errors)]
    batch = BatchResult(
        run_id=run_id,
        results=results,
        errors=errors,
        new_domains=new_count,
        reverified=reverify,
        deferred=deferred,
        budget_exhausted=deferred > 0,
        elapsed_s=elapsed,
        concurrency=workers,
    )
    finish_run(
        session,
        run_id,
        status="ok",
        **_run_counts(results, len(errors)),
        notes={
            "outcomes": batch.outcomes,
            "new_domains": new_count,
            "reverified": reverify,
            "concurrency": workers,
            "budget_exhausted": batch.budget_exhausted,
            "deferred": deferred,
        },
        now=now + timedelta(seconds=elapsed),
    )
    _durable(session, commit)
    return batch
