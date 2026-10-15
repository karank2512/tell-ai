"""Quarterly Form D data sets: download, cache and write them (SPEC 3.6 backfill, 13.1, 13.2).

The SEC's Division of Economic and Risk Analysis publishes every Form D of a calendar
quarter as one ZIP of tab-separated files (:data:`FORMD_DATASETS_PAGE`). This module is
what ``tell backfill formd`` runs:

* :func:`download_dataset` fetches ``{year}q{n}_d.zip`` (:func:`dataset_urls`: the SEC
  moved the data sets from ``files/structureddata/`` to ``files/datastandardsinnovation/``
  from 2026 Q2 on, so both locations are tried) once into a local cache directory with the EDGAR politeness rules of
  :class:`tell.collectors.edgar.SecClient` (``SEC_USER_AGENT``, gzip, one request per
  ``TELL_RATE_EDGAR_RPS`` token, the shared retry policy). It refuses to touch sec.gov
  while ``SEC_USER_AGENT`` is still the placeholder default
  (:func:`require_sec_contact`): the SEC answers anonymous tools with 403 and its
  fair-access policy asks for a real contact.
* :func:`backfill_formd` loads each data set with
  :func:`tell.collectors.edgar_formd.load_formd_quarterly` (ZIP or unpacked directory),
  keeps the filings dated within ``since_days`` of ``now``, links an issuer whose CIK is
  already a company's ``cik`` with a trusted ``cik_source`` (as the daily collector does,
  ADR 0035) and writes the events with
  :func:`tell.collectors.base.write_events`. Events keep ``source = 'edgar'`` and the
  daily key ``edgar:{accession}``, so a rerun, or a filing the daily index already
  delivered, inserts nothing. Every other link is left to ``tell resolve``.

The related-persons file of the data set is never opened (see ``edgar_formd``). This
module has no ``COLLECTOR``; discovery skips it. The ``runs`` row is recorded by the CLI
under :data:`SOURCE` (``edgar_backfill``), which is not a scheduled or health source.
"""

from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
from sqlalchemy.orm import Session

from tell.collectors.base import Event, write_events
from tell.collectors.edgar import SecClient
from tell.collectors.edgar_formd import company_ids_by_cik, load_formd_quarterly
from tell.config import get_settings
from tell.http import RateBudget, Sleeper

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_SINCE_DAYS",
    "FORMD_DATASETS_PAGE",
    "FORMD_DATASET_URL",
    "FORMD_DATASET_URLS",
    "FORMD_MOVED_FROM",
    "PLACEHOLDER_CONTACT",
    "SOURCE",
    "BackfillResult",
    "DatasetNotPublishedError",
    "Quarter",
    "SecContactMissingError",
    "backfill_formd",
    "dataset_url",
    "dataset_urls",
    "download_dataset",
    "parse_quarter",
    "require_sec_contact",
]

SOURCE: Final = "edgar_backfill"
"""``runs.source`` of ``tell backfill formd``; the events themselves stay ``edgar``."""

FORMD_DATASETS_PAGE: Final = "https://www.sec.gov/dera/data/form-d"
"""The SEC page listing the quarterly Form D data sets (SPEC 3.6; it may redirect)."""

_FORMD_DATASET_PATH: Final = "data/form-d-data-sets/{year}q{quarter}_d.zip"

FORMD_DATASET_URLS: Final[tuple[str, ...]] = (
    f"https://www.sec.gov/files/datastandardsinnovation/{_FORMD_DATASET_PATH}",
    f"https://www.sec.gov/files/structureddata/{_FORMD_DATASET_PATH}",
)
"""Where one quarter's data set ZIP lives, as linked from :data:`FORMD_DATASETS_PAGE`.

Checked 2026-10-13: 2026 Q2 and Q3 are linked under ``datastandardsinnovation/``, 2026 Q1
and earlier under ``structureddata/`` (2008-2012 often with a ``_d_0.zip`` suffix, which
this module does not cover). :func:`dataset_urls` orders them by :data:`FORMD_MOVED_FROM`."""

FORMD_DATASET_URL: Final = FORMD_DATASET_URLS[0]
"""The current location (2026 Q2 on); see :data:`FORMD_DATASET_URLS`."""

FORMD_MOVED_FROM: Final = (2026, 2)
"""The first quarter the SEC links under ``datastandardsinnovation/``."""

DEFAULT_SINCE_DAYS: Final = 120
"""Filings older than this many days before ``now`` are not written (the 13.1 cohort is
90 days; 30 days of margin cover a report dated a few weeks later)."""

PLACEHOLDER_CONTACT: Final = "example.invalid"
"""Marker of the placeholder ``SEC_USER_AGENT`` default in :mod:`tell.config`."""

DEFAULT_CACHE_DIR: Final = Path(__file__).resolve().parents[3] / "tell-private" / "cache" / "formd"
"""``../tell-private/cache/formd`` next to the checkout, beside the Wayback cache."""

_log = logging.getLogger(__name__)

_QUARTER = re.compile(r"^\s*(\d{4})\s*-?\s*q\s*([1-4])\s*$", re.IGNORECASE)


class SecContactMissingError(RuntimeError):
    """``SEC_USER_AGENT`` is still the placeholder: no request may go to sec.gov."""


class DatasetNotPublishedError(LookupError):
    """The SEC answered 404 for a quarter's data set (not posted yet, or the URL moved)."""


@dataclass(frozen=True, order=True)
class Quarter:
    """A calendar quarter, ``2026Q2``."""

    year: int
    quarter: int

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.quarter}"

    @property
    def filename(self) -> str:
        """The SEC's file name, also the cache file name: ``2026q2_d.zip``."""
        return f"{self.year}q{self.quarter}_d.zip"

    @property
    def url(self) -> str:
        """The location tried first; :attr:`urls` lists every candidate."""
        return dataset_url(self.year, self.quarter)

    @property
    def urls(self) -> tuple[str, ...]:
        return dataset_urls(self.year, self.quarter)


def parse_quarter(text: str) -> Quarter:
    """``"2026Q2"`` (also ``2026q2``, ``2026-Q2``) -> :class:`Quarter`; ``ValueError`` else."""
    match = _QUARTER.match(text)
    if match is None:
        msg = f"not a quarter: {text!r} (expected YYYYQn, e.g. 2026Q2)"
        raise ValueError(msg)
    return Quarter(int(match.group(1)), int(match.group(2)))


def dataset_urls(year: int, quarter: int) -> tuple[str, ...]:
    """Every candidate URL of one quarter's ZIP, the likelier location first.

    Quarters from :data:`FORMD_MOVED_FROM` on try ``datastandardsinnovation/`` first,
    older ones ``structureddata/`` first; the other location is the fallback.
    """
    if quarter not in (1, 2, 3, 4):
        msg = f"quarter must be 1-4, got {quarter}"
        raise ValueError(msg)
    templates = FORMD_DATASET_URLS
    if (year, quarter) < FORMD_MOVED_FROM:
        templates = tuple(reversed(templates))
    return tuple(t.format(year=year, quarter=quarter) for t in templates)


def dataset_url(year: int, quarter: int) -> str:
    """The first of :func:`dataset_urls` for one quarter."""
    return dataset_urls(year, quarter)[0]


def require_sec_contact(user_agent: str | None = None) -> str:
    """Return the SEC User-Agent, or raise :class:`SecContactMissingError` for the placeholder."""
    agent = user_agent if user_agent is not None else get_settings().sec_user_agent
    if PLACEHOLDER_CONTACT in agent.lower() or not agent.strip():
        msg = (
            f"SEC_USER_AGENT is still the placeholder ({agent!r}); set it to "
            "'tell/0.1 (<your contact email>)' before downloading from sec.gov "
            "(docs/CUTOVER_CHECKLIST.md step 5), or pass an already-downloaded ZIP with --file"
        )
        raise SecContactMissingError(msg)
    return agent


def download_dataset(
    quarter: Quarter,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    *,
    client: httpx.Client | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
    refresh: bool = False,
) -> Path:
    """The cached ZIP of ``quarter``, downloading it first when it is not cached.

    A cached file that is a readable ZIP is returned without any request (and without
    the contact check). Otherwise the contact is checked and the candidates of
    :func:`dataset_urls` are fetched in order through :class:`SecClient` until one answers
    200; the body is written atomically to ``cache_dir/{year}q{n}_d.zip``. A 404 or 403
    moves on to the next candidate (sec.gov answers a missing file with 403 as often as
    with 404). When every candidate answered 404, :class:`DatasetNotPublishedError` is
    raised; when one answered 403, its :class:`httpx.HTTPStatusError` is (a wrong
    User-Agent also gets 403); any other error status raises at once; a body that is not
    a ZIP raises :class:`ValueError` and nothing is cached.
    """
    target = cache_dir / quarter.filename
    if not refresh and target.is_file() and zipfile.is_zipfile(target):
        _log.info("edgar backfill: %s cached at %s", quarter.label, target)
        return target
    require_sec_contact()
    response: httpx.Response | None = None
    forbidden: httpx.HTTPStatusError | None = None
    with SecClient(client, budget=budget, sleep=sleep) as sec:
        for url in quarter.urls:
            try:
                response = sec.get(url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != httpx.codes.FORBIDDEN:
                    raise
                forbidden = exc
                continue
            if response is not None:
                break
    if response is None:
        if forbidden is not None:
            raise forbidden
        msg = (
            f"{' and '.join(quarter.urls)} answered 404: the {quarter.label} Form D data set "
            f"is not published (yet), or the URL moved; check {FORMD_DATASETS_PAGE} and pass "
            "a downloaded ZIP with --file"
        )
        raise DatasetNotPublishedError(msg)
    content = response.content
    if not zipfile.is_zipfile(io.BytesIO(content)):
        kind = response.headers.get("Content-Type", "unknown type")
        msg = f"{response.request.url} did not return a ZIP ({kind}, {len(content)} bytes)"
        raise ValueError(msg)
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")
    partial.write_bytes(content)
    partial.replace(target)
    _log.info("edgar backfill: %s downloaded (%d bytes)", quarter.label, len(content))
    return target


@dataclass
class BackfillResult:
    """What :func:`backfill_formd` did; :attr:`notes` is what ``runs.notes`` stores."""

    since_days: int
    cutoff: str
    fetched: int = 0
    """Events the data sets produced (before the window filter)."""
    inserted: int = 0
    skipped: int = 0
    """Events in the window already present (a rerun, or the daily index got them)."""
    outside_window: int = 0
    linked_by_cik: int = 0
    datasets: list[str] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def notes(self) -> dict[str, Any]:
        return {
            "datasets": list(self.datasets),
            "since_days": self.since_days,
            "cutoff": self.cutoff,
            "outside_window": self.outside_window,
            "linked_by_cik": self.linked_by_cik,
            "by_type": dict(sorted(self.by_type.items())),
        }


def _link_by_cik(events: Sequence[Event], ids: dict[str, int]) -> list[Event]:
    return [
        replace(event, company_id=ids[str(event.payload.get("cik"))])
        if event.company_id is None and str(event.payload.get("cik")) in ids
        else event
        for event in events
    ]


def backfill_formd(
    session: Session,
    paths: Sequence[Path],
    *,
    run_id: str,
    now: datetime,
    since_days: int = DEFAULT_SINCE_DAYS,
) -> BackfillResult:
    """Write the Form D events of each data set in ``paths`` filed within ``since_days``.

    ``paths`` are data-set ZIPs or unpacked directories. A filing is kept when its filing
    date is on or after ``now``'s UTC date minus ``since_days``. Idempotent: events carry
    the daily collector's keys, so a rerun inserts nothing. Flushes, never commits.
    """
    if since_days < 1:
        msg = f"since_days must be >= 1, got {since_days}"
        raise ValueError(msg)
    first_day = now.date() - timedelta(days=since_days)
    result = BackfillResult(since_days=since_days, cutoff=first_day.isoformat())
    for path in paths:
        events = load_formd_quarterly(path, run_id=run_id, now=now)
        kept = [e for e in events if e.occurred_at.date() >= first_day]
        ids = company_ids_by_cik(session, [str(e.payload.get("cik")) for e in kept])
        linked = _link_by_cik(kept, ids)
        stats = write_events(session, linked)
        session.flush()
        result.datasets.append(Path(path).name)
        result.fetched += len(events)
        result.outside_window += len(events) - len(kept)
        result.inserted += stats.inserted
        result.skipped += stats.skipped
        result.linked_by_cik += sum(1 for e in linked if e.company_id is not None)
        for event in kept:
            result.by_type[event.type] = result.by_type.get(event.type, 0) + 1
    return result
