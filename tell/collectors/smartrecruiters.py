"""SmartRecruiters Posting API collector (spec section 3.4).

One board is one SmartRecruiters ``companyIdentifier``. A poll lists every public
posting with ``GET /v1/companies/{companyIdentifier}/postings?limit=100&offset=N``,
paginating while ``offset < totalFound``, then fetches the posting detail
(``/postings/{id}``) only for ids not yet in ``postings`` for that board, so the
job description can be hashed into ``content_hash``. Everything else (state
machine, events, idempotency) is :mod:`tell.collectors.base`.

* Idempotency key: ``smartrecruiters:{companyIdentifier}:{id}:{releasedDate}``; the
  version marker is ``releasedDate`` rendered with :func:`base.iso_utc`.
* Public URL: built as ``https://jobs.smartrecruiters.com/{companyIdentifier}/{id}``
  (the list response carries no public URL; the detail's slugged ``postingUrl`` is
  not used so the URL stays the same whether or not a detail was fetched).
* Never kept: ``creator`` (a recruiter's name) and the description text.
* Optional in practice (2026-10-14, 30 postings on 10 boards): ``location.region`` is
  absent on 4 postings (2 boards) and the list items never carry ``postingUrl``; the
  model already allows both. One bad posting never fails a board: each list page goes
  through :func:`validate_page`, which skips and logs (WARNING, board and field path)
  items that break :class:`SmartRecruitersPosting` and counts them in
  :attr:`SmartRecruitersPostingList.invalid_jobs` and ``BoardRunResult.errors`` (added
  to ``runs.errors`` by ``tell ingest``); their ids go to ``sync_board_postings`` so a
  listed but invalid posting is never closed. Only when every item of a non-empty page
  fails does the board fail, with the strict error.
* Board liveness is the shared ADR 0015 rule: a 200 calls
  :func:`~tell.collectors.base.record_board_ok`, a list-endpoint 404 calls
  :func:`~tell.collectors.base.record_board_not_found` (``dead`` after two 404s at
  least 12 hours apart, plus one ``board_verify`` review item). A 404 never advances
  ``last_polled_at``; this module never writes ``last_verified_at``. A 404 on a
  posting *detail* only costs that posting's content hash.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, override

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    CollectorSpec,
    ObservedPosting,
    RunResult,
    iso_utc,
    listed_id,
    record_board_not_found,
    record_board_ok,
    sync_board_postings,
    write_events,
)
from tell.config import get_settings
from tell.http import RateBudget, Sleeper, fetch, make_client
from tell.models import AtsBoard, Company, Posting

__all__ = [
    "API_BASE",
    "COLLECTOR",
    "PAGE_LIMIT",
    "PUBLIC_BASE",
    "SOURCE",
    "BoardNotFoundError",
    "BoardRunResult",
    "SmartRecruitersPosting",
    "SmartRecruitersPostingDetail",
    "SmartRecruitersPostingList",
    "board_url",
    "default_budget",
    "description_text",
    "fetch_board",
    "fetch_posting_detail",
    "html_to_text",
    "ingest_board",
    "parse",
    "posting_url",
    "run_fixture",
    "validate_page",
]

SOURCE = "smartrecruiters"
API_BASE = "https://api.smartrecruiters.com/v1/companies"
PUBLIC_BASE = "https://jobs.smartrecruiters.com"
PAGE_LIMIT = 100
MAX_PAGES = 200
"""Hard stop for pagination (20,000 postings) in case ``totalFound`` misbehaves."""

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE
FIXTURE_IDENTIFIER = "tell-fixture"
FIXTURE_DOMAIN = "smartrecruiters-fixture.test"

_logger = logging.getLogger(__name__)


# --- response models (the contract) ----------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class SmartRecruitersLabel(_Model):
    """An ``{id, label}`` pair (``department``, ``function``, ``typeOfEmployment``...)."""

    id: str | None = None
    label: str | None = None


class SmartRecruitersLocation(_Model):
    city: str | None = None
    region: str | None = None
    country: str
    remote: bool | None = None
    hybrid: bool | None = None
    full_location: str | None = Field(default=None, alias="fullLocation")


class SmartRecruitersCompany(_Model):
    identifier: str | None = None
    name: str | None = None


class SmartRecruitersPosting(_Model):
    """One ``content[]`` item of the list response; unknown keys (``creator``) are dropped."""

    id: str
    name: str
    released_date: datetime = Field(alias="releasedDate")
    location: SmartRecruitersLocation
    ref: str
    department: SmartRecruitersLabel | None = None
    function: SmartRecruitersLabel | None = None
    company: SmartRecruitersCompany | None = None
    ref_number: str | None = Field(default=None, alias="refNumber")
    type_of_employment: SmartRecruitersLabel | None = Field(default=None, alias="typeOfEmployment")
    experience_level: SmartRecruitersLabel | None = Field(default=None, alias="experienceLevel")
    industry: SmartRecruitersLabel | None = None
    posting_url: str | None = Field(default=None, alias="postingUrl")


class SmartRecruitersPostingList(_Model):
    """``GET /v1/companies/{companyIdentifier}/postings``: one page of postings."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    offset: int
    limit: int
    total_found: int = Field(alias="totalFound")
    content: list[SmartRecruitersPosting]
    _invalid_ids: tuple[str | None, ...] = PrivateAttr(default=())

    @property
    def invalid_jobs(self) -> int:
        """Items :func:`validate_page` skipped because they broke the posting model."""
        return len(self._invalid_ids)

    @property
    def invalid_ids(self) -> tuple[str | None, ...]:
        """The skipped ids (``None`` when unusable), for ``sync_board_postings``."""
        return self._invalid_ids


class _SmartRecruitersPage(_Model):
    """One list page with each item left raw, so one bad item cannot fail the board."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    offset: int
    limit: int
    total_found: int = Field(alias="totalFound")
    content: list[Any]


@dataclass(frozen=True)
class BoardRunResult(RunResult):
    """One board poll; ``errors`` counts postings skipped because they failed validation."""


def _error_paths(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<posting>'} ({err['type']})"
        for err in exc.errors()
    )


def _listing(
    items: list[SmartRecruitersPosting],
    *,
    total_found: int,
    invalid: Sequence[str | None],
    offset: int = 0,
    limit: int = PAGE_LIMIT,
) -> SmartRecruitersPostingList:
    listing = SmartRecruitersPostingList(
        offset=offset, limit=limit, total_found=total_found, content=items
    )
    listing._invalid_ids = tuple(invalid)  # set once, right after construction
    return listing


def validate_page(content: str | bytes, *, identifier: str) -> SmartRecruitersPostingList:
    """Validate one list page item by item; invalid items are skipped, logged and counted.

    ``offset``, ``limit``, ``totalFound`` and ``content`` are strict. An item failing
    :class:`SmartRecruitersPosting` is dropped with one WARNING naming ``identifier``,
    the posting id and the field paths, and counted in
    :attr:`SmartRecruitersPostingList.invalid_jobs`. When every item of a non-empty
    page fails, the strict :class:`SmartRecruitersPostingList` error is raised instead.
    """
    page = _SmartRecruitersPage.model_validate_json(content)
    valid: list[SmartRecruitersPosting] = []
    invalid_ids: list[str | None] = []
    for index, item in enumerate(page.content):
        try:
            valid.append(SmartRecruitersPosting.model_validate(item))
        except ValidationError as exc:
            posting_id = listed_id(item)
            invalid_ids.append(posting_id)
            _logger.warning(
                "smartrecruiters board %s: skipped invalid posting %s (content.%d): %s",
                identifier,
                posting_id,
                page.offset + index,
                _error_paths(exc),
            )
    if page.content and not valid:
        SmartRecruitersPostingList.model_validate_json(content)  # raises the page-level error
    return _listing(
        valid,
        total_found=page.total_found,
        invalid=invalid_ids,
        offset=page.offset,
        limit=page.limit,
    )


class SmartRecruitersSection(_Model):
    title: str | None = None
    text: str | None = None


class SmartRecruitersSections(_Model):
    job_description: SmartRecruitersSection | None = Field(default=None, alias="jobDescription")


class SmartRecruitersJobAd(_Model):
    sections: SmartRecruitersSections


class SmartRecruitersPostingDetail(_Model):
    """``GET /v1/companies/{companyIdentifier}/postings/{id}``: the fields we read."""

    id: str
    job_ad: SmartRecruitersJobAd = Field(alias="jobAd")
    posting_url: str | None = Field(default=None, alias="postingUrl")


# --- HTML to plain text ----------------------------------------------------------------

_BLOCK_TAGS = frozenset(
    {"p", "br", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    @override
    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip tags, decode entities, collapse whitespace; block tags become word breaks."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


def description_text(detail: SmartRecruitersPostingDetail) -> str | None:
    """Plain text of ``jobAd.sections.jobDescription.text`` (hashed, never stored)."""
    section = detail.job_ad.sections.job_description
    if section is None or not section.text:
        return None
    return html_to_text(section.text) or None


# --- parse ------------------------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _label(value: SmartRecruitersLabel | None) -> str | None:
    if value is None or value.label is None:
        return None
    return value.label.strip() or None


def _location_text(location: SmartRecruitersLocation) -> str | None:
    if location.full_location and location.full_location.strip():
        # A posting without ``region`` reads "Jakarta, , Indonesia": drop the empty part.
        pieces = (piece.strip() for piece in location.full_location.split(","))
        return ", ".join(piece for piece in pieces if piece) or None
    parts = [location.city, location.region, location.country.upper() or None]
    text = ", ".join(part.strip() for part in parts if part and part.strip())
    return text or None


def _remote(item: SmartRecruitersPosting) -> bool | None:
    """SmartRecruiters' own ``location.remote`` flag; else ``True`` when the text says remote."""
    if item.location.remote is not None:
        return item.location.remote
    haystack = " ".join(
        part for part in (item.location.full_location, item.location.city, item.name) if part
    )
    return True if "remote" in haystack.lower() else None


def posting_url(company_identifier: str, posting_id: str) -> str:
    """Public posting page: ``https://jobs.smartrecruiters.com/{companyIdentifier}/{id}``."""
    return f"{PUBLIC_BASE}/{company_identifier}/{posting_id}"


def _extra(item: SmartRecruitersPosting) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "ref": item.ref,
        "ref_number": item.ref_number,
        "city": item.location.city,
        "region": item.location.region,
        "country": item.location.country,
        "hybrid": item.location.hybrid,
        "function": _label(item.function),
        "type_of_employment": _label(item.type_of_employment),
        "experience_level": _label(item.experience_level),
        "industry": _label(item.industry),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _as_listing(
    raw: SmartRecruitersPostingList | Mapping[str, Any] | str | bytes,
) -> SmartRecruitersPostingList:
    if isinstance(raw, SmartRecruitersPostingList):
        return raw
    if isinstance(raw, (str, bytes)):
        return SmartRecruitersPostingList.model_validate_json(raw)
    return SmartRecruitersPostingList.model_validate(raw)


def parse(
    raw: SmartRecruitersPostingList | Mapping[str, Any] | str | bytes,
    *,
    identifier: str | None = None,
    descriptions: Mapping[str, str] | None = None,
) -> list[ObservedPosting]:
    """Map a list response to :class:`ObservedPosting` objects.

    ``identifier`` is the board's ``companyIdentifier``, used for the public URL when
    an item carries no ``company.identifier``. ``descriptions`` maps posting id to the
    plain-text job description fetched from the detail endpoint; it is hashed into
    ``content_hash`` and discarded. Department is ``department.label``, falling back
    to ``function.label`` (SmartRecruiters often sends an empty ``department``).
    """
    listing = _as_listing(raw)
    texts = descriptions or {}
    observed: list[ObservedPosting] = []
    for item in listing.content:
        company_identifier = (
            item.company.identifier if item.company and item.company.identifier else identifier
        )
        if item.posting_url:
            url = item.posting_url
        elif company_identifier:
            url = posting_url(company_identifier, item.id)
        else:
            msg = f"posting {item.id!r} has no company identifier to build its URL from"
            raise ValueError(msg)
        released = _aware(item.released_date)
        observed.append(
            ObservedPosting(
                external_id=item.id,
                title=item.name.strip(),
                url=url,
                version=iso_utc(released),
                department=_label(item.department) or _label(item.function),
                location=_location_text(item.location),
                remote=_remote(item),
                posted_at=released,
                description_plain=texts.get(item.id),
                extra=_extra(item),
            )
        )
    return observed


# --- fetch ------------------------------------------------------------------------------


class BoardNotFoundError(Exception):
    """The list endpoint answered 404: the company identifier is unknown or renamed."""

    def __init__(self, identifier: str, url: str) -> None:
        super().__init__(f"SmartRecruiters board {identifier!r} not found (404 at {url})")
        self.identifier = identifier
        self.url = url


_budget_lock = threading.Lock()
_budget: RateBudget | None = None


def default_budget() -> RateBudget:
    """The process-wide token bucket for ``api.smartrecruiters.com`` (``TELL_RATE_ATS_RPS``)."""
    global _budget  # noqa: PLW0603 -- one lazily built bucket shared by every board poll
    with _budget_lock:
        if _budget is None:
            rps = get_settings().tell_rate_ats_rps
            _budget = RateBudget(rps, burst=max(1, int(rps)))
        return _budget


def board_url(identifier: str) -> str:
    """List endpoint for one company (query parameters are added per page)."""
    return f"{API_BASE}/{identifier}/postings"


def fetch_board(
    client: httpx.Client,
    identifier: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
    max_pages: int = MAX_PAGES,
) -> SmartRecruitersPostingList:
    """Fetch every page of one company's postings and return them as one listing.

    Requests ``?limit=100&offset=0``, then ``offset=100``, ... while
    ``offset < totalFound`` (stopping early on an empty page or after ``max_pages``).
    429 and 5xx are retried by :func:`tell.http.fetch` (backoff 1, 2, 4, 8 s, honouring
    ``Retry-After``). Raises :class:`BoardNotFoundError` on 404 and
    :class:`httpx.HTTPStatusError` on any other 4xx. Items that break the contract are
    skipped and counted (:func:`validate_page`); the returned listing's ``invalid_jobs``
    is the total over all pages.
    """
    url = board_url(identifier)
    bucket = budget if budget is not None else default_budget()
    items: list[SmartRecruitersPosting] = []
    total_found = 0
    invalid: list[str | None] = []
    offset = 0
    for _ in range(max_pages):
        response = fetch(
            url,
            client=client,
            params={"limit": PAGE_LIMIT, "offset": offset},
            sleep=sleep,
            budget=bucket,
        )
        if response.status_code == 404:
            raise BoardNotFoundError(identifier, str(response.request.url))
        response.raise_for_status()
        page = validate_page(response.content, identifier=identifier)
        items.extend(page.content)
        invalid.extend(page.invalid_ids)
        total_found = page.total_found
        offset += PAGE_LIMIT
        if offset >= page.total_found or not (page.content or page.invalid_jobs):
            break
    return _listing(items, total_found=total_found, invalid=invalid)


def fetch_posting_detail(
    client: httpx.Client,
    identifier: str,
    posting_id: str,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> SmartRecruitersPostingDetail | None:
    """One posting's detail, or ``None`` when it 404s (withdrawn between list and detail)."""
    response = fetch(
        f"{board_url(identifier)}/{posting_id}",
        client=client,
        sleep=sleep,
        budget=budget if budget is not None else default_budget(),
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return SmartRecruitersPostingDetail.model_validate_json(response.content)


# --- ingest -----------------------------------------------------------------------------


def _known_hashes(session: Session, board: AtsBoard) -> dict[str, str | None]:
    """``external_id -> content_hash`` for every posting already stored for ``board``."""
    board_id: int | None = board.id  # None while the board is still transient
    if board_id is None:
        return {}
    rows = session.execute(
        select(Posting.external_id, Posting.content_hash).where(Posting.board_id == board_id)
    )
    return {row.external_id: row.content_hash for row in rows}


def _keep_known_hash(obs: ObservedPosting, known: str | None) -> ObservedPosting:
    # base._apply_observation overwrites postings.content_hash with the observation's
    # hash on every poll. A known id gets no detail call (spec 3.4), so its observation
    # would carry None and wipe the stored hash; carry the stored one instead. Set the
    # same way ObservedPosting.__post_init__ sets it (the field is init=False).
    if obs.content_hash is None and known is not None:
        object.__setattr__(obs, "content_hash", known)
    return obs


def _descriptions(
    client: httpx.Client,
    identifier: str,
    posting_ids: Iterable[str],
    *,
    budget: RateBudget | None,
    sleep: Sleeper,
) -> dict[str, str]:
    texts: dict[str, str] = {}
    for posting_id in posting_ids:
        try:
            detail = fetch_posting_detail(
                client, identifier, posting_id, budget=budget, sleep=sleep
            )
        except httpx.HTTPError as exc:
            # A failed detail costs only the content hash; the posting is still listed.
            _logger.warning("smartrecruiters detail %s/%s failed: %s", identifier, posting_id, exc)
            continue
        text = description_text(detail) if detail is not None else None
        if text:
            texts[posting_id] = text
    return texts


def ingest_board(
    session: Session,
    board: AtsBoard,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> BoardRunResult:
    """Poll one board: list, detail for new ids, sync ``postings``, write events.

    On success calls ``record_board_ok`` (``last_polled_at = now``, 404 streak reset,
    a ``dead`` board revived). On a list 404 writes no events and calls
    ``record_board_not_found`` (ADR 0015). Other HTTP failures propagate and leave the
    board untouched. Items that fail validation are skipped and counted in ``errors``.
    """
    own_client = client is None
    http = client if client is not None else make_client()
    try:
        try:
            listing = fetch_board(http, board.identifier, budget=budget, sleep=sleep)
        except BoardNotFoundError as exc:
            _logger.warning("%s", exc)
            record_board_not_found(session, board, now=now)
            return BoardRunResult(fetched=0, inserted=0, skipped=0, events=[])
        known = _known_hashes(session, board)
        new_ids = list(dict.fromkeys(item.id for item in listing.content if item.id not in known))
        texts = _descriptions(http, board.identifier, new_ids, budget=budget, sleep=sleep)
    finally:
        if own_client:
            http.close()

    observed = [
        _keep_known_hash(obs, known.get(obs.external_id))
        for obs in parse(listing, identifier=board.identifier, descriptions=texts)
    ]
    events = sync_board_postings(
        session, board, observed, run_id=run_id, now=now, invalid=listing.invalid_ids
    )
    stats = write_events(session, events)
    record_board_ok(session, board, now=now)
    return BoardRunResult(
        fetched=len(listing.content) + listing.invalid_jobs,
        inserted=stats.inserted,
        skipped=stats.skipped,
        events=events,
        errors=listing.invalid_jobs,
    )


# --- fixture replay ---------------------------------------------------------------------


def _fixture_board(session: Session) -> AtsBoard:
    board = session.scalars(
        select(AtsBoard).where(AtsBoard.vendor == SOURCE, AtsBoard.identifier == FIXTURE_IDENTIFIER)
    ).one_or_none()
    if board is not None:
        return board
    company = session.scalars(select(Company).where(Company.domain == FIXTURE_DOMAIN)).one_or_none()
    if company is None:
        company = Company(domain=FIXTURE_DOMAIN, name="SmartRecruiters fixture")
        session.add(company)
        session.flush()
    board = AtsBoard(
        company_id=company.id,
        vendor=SOURCE,
        identifier=FIXTURE_IDENTIFIER,
        discovered_from=None,
        confidence=Decimal("1.00"),
        status="active",
    )
    session.add(board)
    session.flush()
    return board


def run_fixture(session: Session, case_path: Path, /, *, run_id: str, now: datetime) -> RunResult:
    """Replay one list-response fixture through the poll path (``validate_page``), no network."""
    listing = validate_page(case_path.read_text(encoding="utf-8"), identifier=FIXTURE_IDENTIFIER)
    board = _fixture_board(session)
    observed = parse(listing, identifier=FIXTURE_IDENTIFIER)
    events = sync_board_postings(
        session, board, observed, run_id=run_id, now=now, invalid=listing.invalid_ids
    )
    stats = write_events(session, events)
    record_board_ok(session, board, now=now)
    return BoardRunResult(
        fetched=len(listing.content) + listing.invalid_jobs,
        inserted=stats.inserted,
        skipped=stats.skipped,
        events=events,
        errors=listing.invalid_jobs,
    )


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="ats",
    response_model=SmartRecruitersPostingList,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="releasedDate",
    run_fixture=run_fixture,
)
