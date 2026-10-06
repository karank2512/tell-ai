"""ATS discovery: domain to job board (spec section 4).

Given ``acme.com``, find the company's job board or record that none was found.

1. Fetch ``https://acme.com/careers``, ``/jobs``, ``/join``, ``/company/careers`` and the
   homepage, in that order, stopping at the first 200. Redirects are followed by hand,
   at most :data:`MAX_REDIRECTS`; a redirect whose target is itself a board URL is a hit
   and is not fetched. ``robots.txt`` is honoured per origin (RFC 9309 status rules); a
   disallowed candidate is skipped and recorded as ``robots_blocked:<path>``.
2. Scan the page: ``<iframe src>``, ``<script src>``, ``<a href>`` (via ``lxml.html``),
   then the raw HTML text (inline scripts, data attributes), against :data:`PATTERNS`.
   Supported vendors win over unsupported ones; within a class the first hit wins.
   ``grnh.se`` short links rank last: they are per-posting links with an opaque code,
   not board tokens. A chosen short link is resolved with one GET (redirects not
   followed) whose ``Location`` must name ``boards.greenhouse.io/{token}`` or
   ``job-boards.greenhouse.io/{token}``; otherwise discovery falls through to step 3.
3. Nothing on the page (or an unresolvable short link): guess slugs from the domain label (``acme``, ``acmeinc``,
   ``acme-inc``) against each documented public API in turn; a 200 with at least one
   job counts. Page hits get confidence 0.95, guesses 0.6.
4. Verify ownership: the board's company name (Greenhouse ``/v1/boards/{token}`` ->
   ``name``; Lever and Ashby: the ``<title>`` of the hosted board page; SmartRecruiters:
   ``content[0].company.name`` of the postings API) must reach
   ``rapidfuzz.fuzz.token_set_ratio >= 85`` against the domain label or the company
   name. Otherwise a ``review_queue`` item (``kind='board_verify'``) is written and the
   board is not activated (a new row is stored with ``status='paused'``, which no
   collector polls, until a human resolves the item).
5. Write ``ats_boards``: ``status='active'`` for a verified supported vendor,
   ``'unsupported'`` for Workday / Oracle / Workable (never fetched further, never
   polled; ``docs/decisions/0003``) and, when nothing is found, one row
   ``vendor='none', identifier=<domain>, status='no_board'`` whose ``discovered_from``
   is ``checked:`` plus the comma-joined candidate paths, so coverage is reportable.

Re-verification (:func:`reverify_due`): every 30 days, and whenever a board has
answered 404 twice in a row. ``ats_boards`` has no 404 counter; every ATS collector
already marks a board ``status='dead'`` on its second consecutive 404 (Greenhouse,
Ashby and SmartRecruiters compare ``last_polled_at`` with ``last_verified_at``; Lever
keeps the streak in an open ``review_queue`` item), so ``dead`` *is* the
"two consecutive 404s" marker. Every discovery outcome stamps ``last_verified_at = now``
on the company's ``dead`` boards and its ``no_board`` marker, so a dead board is due
again only after 30 days or after a poll newer than that stamp, not on every batch.

Every result is logged as one JSON line on the ``tell.discovery`` logger:
``{domain, outcome, vendor, identifier, confidence, pages_checked}``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
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
from tell.models import AtsBoard, Company, ReviewQueueItem

__all__ = [
    "CANDIDATE_PATHS",
    "GREENHOUSE_SHORT_LINK",
    "GUESS_CONFIDENCE",
    "MATCH_THRESHOLD",
    "MAX_REDIRECTS",
    "NO_BOARD_VENDOR",
    "PAGE_CONFIDENCE",
    "PATTERNS",
    "REVERIFY_AFTER",
    "SUPPORTED_VENDORS",
    "UNSUPPORTED_VENDORS",
    "BatchResult",
    "BoardHit",
    "BoardPattern",
    "DiscoveryResult",
    "Outcome",
    "RobotsCache",
    "board_name",
    "choose_hit",
    "discover_batch",
    "discover_domain",
    "find_boards",
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
"""Pages fetched in this order; the first 200 is scanned and the rest are not fetched."""

MAX_REDIRECTS = 2
"""Redirects followed per candidate page (spec section 4 step 1)."""

ROBOTS_MAX_REDIRECTS = 5
"""Redirects followed for ``robots.txt`` (RFC 9309 asks for at least five)."""

PAGE_CONFIDENCE = Decimal("0.95")
GUESS_CONFIDENCE = Decimal("0.60")
NO_BOARD_CONFIDENCE = Decimal("0.00")

MATCH_THRESHOLD = 85.0
"""Minimum ``token_set_ratio`` between the board's company name and the domain label / name."""

REVERIFY_AFTER = timedelta(days=30)

SUPPORTED_VENDORS: tuple[str, ...] = ("greenhouse", "lever", "ashby", "smartrecruiters")
"""Vendors with a documented public API; slug guesses try them in this order."""

UNSUPPORTED_VENDORS: tuple[str, ...] = ("workday", "oracle", "workable")
"""Detected and recorded with ``status='unsupported'``; never fetched further or polled."""

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


def _pattern(
    vendor: str, name: str, regex: str, identifier: Callable[[re.Match[str]], str]
) -> BoardPattern:
    return BoardPattern(vendor, name, re.compile(regex, re.IGNORECASE), identifier)


PATTERNS: tuple[BoardPattern, ...] = (
    _pattern(
        "greenhouse",
        "greenhouse_embed",
        _START
        + r"(?:job-boards|boards)\.greenhouse\.io/embed/job_board(?:/js)?/?[^\s\"'<>]*?[?&;]for=("
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
)
"""Every vendor URL shape of spec section 4 step 2, in priority order within a vendor."""


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


def find_boards(content: bytes | str, *, redirects: Sequence[str] = ()) -> list[BoardHit]:
    """Every distinct board reference in a page, in scan order (redirects, attributes, text)."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    sources: list[tuple[str, str]] = [("redirect", url) for url in redirects]
    sources += _attribute_values(raw)
    sources.append(("text", raw.decode("utf-8", errors="replace")))
    hits: list[BoardHit] = []
    seen: set[tuple[str, str]] = set()
    for source, text in sources:
        for hit in _hits_in(text, source):
            key = (hit.vendor, hit.identifier)
            if key not in seen:
                seen.add(key)
                hits.append(hit)
    return hits


def _hit_rank(hit: BoardHit) -> int:
    if hit.pattern == GRNH_SE_PATTERN:
        return 2
    return 0 if hit.vendor in SUPPORTED_VENDORS else 1


def choose_hit(hits: Sequence[BoardHit]) -> BoardHit | None:
    """The first supported-vendor hit, else the first unsupported one, else ``None``.

    ``grnh.se`` short links rank after everything else: the code is a per-posting
    link, not a board token, and only becomes a board through :func:`resolve_short_link`.
    """
    return min(hits, key=_hit_rank) if hits else None


def _matches_board(url: str) -> bool:
    return any(pattern.regex.search(url) for pattern in PATTERNS)


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


def _get_page(
    client: httpx.Client,
    url: str,
    *,
    budget: RateBudget | None,
    max_redirects: int = MAX_REDIRECTS,
    allowed: Callable[[str], bool] | None = None,
    stop_at: Callable[[str], bool] | None = None,
) -> _Page:
    """GET ``url`` following at most ``max_redirects`` redirects by hand; one attempt each.

    Company sites are not retried (a flaky homepage is a miss, not a reason to wait 15 s).
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
            response = client.get(current, follow_redirects=False)
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
    """

    def __init__(
        self, client: httpx.Client, *, user_agent: str, budget: RateBudget | None = None
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._budget = budget
        self._parsers: dict[str, RobotFileParser] = {}

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
        origin = _origin(url)
        parser = self._parsers.get(origin)
        if parser is None:
            parser = self._load(origin)
            self._parsers[origin] = parser
        return parser.can_fetch(self._user_agent, url)


@dataclass(frozen=True)
class _Scan:
    hits: list[BoardHit]
    page_url: str | None
    pages_checked: tuple[str, ...]


def _scan_pages(
    client: httpx.Client, domain: str, *, robots: RobotsCache, budget: RateBudget | None
) -> _Scan:
    checked: list[str] = []
    for path in CANDIDATE_PATHS:
        url = f"https://{domain}{path}"
        if not robots.allowed(url):
            checked.append(f"robots_blocked:{path}")
            continue
        checked.append(path)
        page = _get_page(client, url, budget=budget, allowed=robots.allowed, stop_at=_matches_board)
        if page.note == "redirect_to_board":
            return _Scan(find_boards(b"", redirects=page.redirects), page.url, tuple(checked))
        if page.status == httpx.codes.OK:
            hits = find_boards(page.content, redirects=page.redirects)
            return _Scan(hits, page.url, tuple(checked))
    return _Scan([], None, tuple(checked))


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

    def log_record(self) -> dict[str, Any]:
        """The one JSON line logged per result."""
        return {
            "domain": self.domain,
            "outcome": self.outcome,
            "vendor": self.vendor,
            "identifier": self.identifier,
            "confidence": float(self.confidence),
            "pages_checked": list(self.pages_checked),
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
    normalized = registrable_domain(domain)
    if normalized is None:
        msg = f"not a registrable domain: {domain!r}"
        raise ValueError(msg)
    company = _ensure_company(session, normalized, company_name)
    robots = robots or RobotsCache(client, user_agent=_user_agent(client), budget=budget)

    result = _discover(
        session,
        client,
        company,
        normalized,
        company_name,
        now=now,
        budget=budget,
        sleep=sleep,
        robots=robots,
    )
    _mark_verified(session, company, normalized, now)
    return _log(result)


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


def _discover(
    session: Session,
    client: httpx.Client,
    company: Company,
    normalized: str,
    company_name: str | None,
    *,
    now: datetime,
    budget: RateBudget | None,
    sleep: Sleeper,
    robots: RobotsCache,
) -> DiscoveryResult:
    scan = _scan_pages(client, normalized, robots=robots, budget=budget)
    hit = choose_hit(scan.hits)
    if hit is not None and hit.pattern == GRNH_SE_PATTERN:
        hit = resolve_short_link(client, hit, budget=budget)
    probe_name: str | None = None
    if hit is not None and scan.page_url is not None:
        confidence, discovered_from = PAGE_CONFIDENCE, scan.page_url
    else:
        probe = _guess(client, normalized, budget=budget, sleep=sleep)
        if probe is None:
            return _record_no_board(session, company, normalized, scan.pages_checked, now)
        hit = BoardHit(probe.vendor, probe.identifier, probe.url, "guess")
        confidence, discovered_from, probe_name = GUESS_CONFIDENCE, probe.url, probe.board_name

    found = hit

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
            pages_checked=scan.pages_checked,
            company_id=company.id,
            board_id=board_id,
            board_name=name,
            ratio=ratio,
            review_id=review_id,
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

    name = probe_name or board_name(
        client, found.vendor, found.identifier, budget=budget, sleep=sleep
    )
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
    session: Session,
    company: Company,
    domain: str,
    pages_checked: tuple[str, ...],
    now: datetime,
) -> DiscoveryResult:
    discovered_from = "checked:" + ",".join(pages_checked)
    board = _write_board(
        session,
        company,
        vendor=NO_BOARD_VENDOR,
        identifier=domain,
        discovered_from=discovered_from,
        confidence=NO_BOARD_CONFIDENCE,
        status="no_board",
        now=now,
    )
    return DiscoveryResult(
        domain=domain,
        outcome="no_board",
        vendor=NO_BOARD_VENDOR,
        identifier=domain,
        confidence=NO_BOARD_CONFIDENCE,
        discovered_from=discovered_from,
        pages_checked=pages_checked,
        company_id=company.id,
        board_id=board.id,
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


@dataclass(frozen=True)
class BatchResult:
    """What one :func:`discover_batch` run did."""

    run_id: str
    results: list[DiscoveryResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    new_domains: int = 0
    reverified: int = 0

    @property
    def outcomes(self) -> dict[str, int]:
        """Count per outcome: the coverage numbers (``board`` over all is the hit rate)."""
        counts = Counter(result.outcome for result in self.results)
        return {
            name: counts.get(name, 0) for name in ("board", "unsupported", "review", "no_board")
        }


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
) -> BatchResult:
    """One discovery pass under the spec section 11 budget.

    ``limit`` (default ``TELL_DISCOVERY_BUDGET``, 300) caps the *new* domains: companies
    with no ``ats_boards`` row, oldest first. Boards due for re-verification are taken
    afterwards, capped separately by ``reverify_limit`` (default: ``limit``). Every
    request of the run (robots, pages, vendor APIs) shares one :class:`RateBudget` at
    ``rps`` (default ``TELL_DISCOVERY_RPS``, 4). Each domain runs in a savepoint, so one
    failure is counted in ``errors`` and the run continues. One ``runs`` row with
    ``source='discovery'``: ``fetched`` = domains processed, ``inserted`` = boards found
    (``board`` + ``unsupported``), ``skipped`` = ``review`` + ``no_board``, outcome
    counts in ``notes``.
    """
    _require_aware(now)
    settings = get_settings()
    new_limit = settings.tell_discovery_budget if limit is None else limit
    due_limit = new_limit if reverify_limit is None else reverify_limit
    rate = settings.tell_discovery_rps if rps is None else rps
    bucket = budget or RateBudget(rate, clock=clock, sleep=sleep)
    robots = RobotsCache(client, user_agent=_user_agent(client), budget=bucket)
    run_id = start_run(session, DISCOVERY_SOURCE, now=now)

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

    results: list[DiscoveryResult] = []
    errors: list[str] = []
    for domain, name in targets:
        try:
            with session.begin_nested():
                result = discover_domain(
                    session,
                    client,
                    domain,
                    name,
                    now=now,
                    budget=bucket,
                    sleep=sleep,
                    robots=robots,
                )
        except Exception:
            _logger.exception("discovery failed for %s", domain)
            errors.append(domain)
            continue
        results.append(result)

    batch = BatchResult(
        run_id=run_id,
        results=results,
        errors=errors,
        new_domains=len(new),
        reverified=reverify,
    )
    outcomes = batch.outcomes
    finish_run(
        session,
        run_id,
        status="ok",
        fetched=len(targets),
        inserted=outcomes["board"] + outcomes["unsupported"],
        skipped=outcomes["review"] + outcomes["no_board"],
        errors=len(errors),
        notes={"outcomes": outcomes, "new_domains": len(new), "reverified": reverify},
        now=now,
    )
    return batch
