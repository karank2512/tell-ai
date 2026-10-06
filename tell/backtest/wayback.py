"""Wayback Machine client for the 13.2 retrospective reconstruction (spec section 13.2 (b)).

Two endpoints, both public and keyless:

* CDX listing: ``https://web.archive.org/cdx/search/cdx?url={board_url}&output=json
  &from={year}&filter=statuscode:200``. The JSON answer is a list of rows whose first
  row is the header (``urlkey, timestamp, original, mimetype, statuscode, digest,
  length``); an empty body or ``[]`` means no snapshot.
* Raw snapshot: ``https://web.archive.org/web/{timestamp}id_/{url}`` (the ``id_`` flag
  returns the archived bytes without the Wayback toolbar).

Only job identifiers are kept from a snapshot (:func:`extract_job_ids`): Greenhouse job
ids from ``/jobs/{digits}`` links and Lever posting UUIDs from
``jobs.lever.co/{token}/{uuid}`` links. Ashby and SmartRecruiters render their boards in
the browser, so their archived HTML lists no jobs and they are not reconstructed.

Politeness: one request per second (:data:`WAYBACK_RPS`) through
:class:`tell.http.RateBudget`, the project retry policy from :func:`tell.http.fetch`, and
an optional cap on requests per run. Results are cached as JSON under
``tell-private/cache/wayback/`` (:data:`DEFAULT_CACHE_DIR`): ``cdx-{sha1}.json`` per
board URL and start year, ``snap-{sha1}.json`` per snapshot with its job ids. A client
built with ``network=False`` (the default) reads only the cache; tests never touch the
network (respx).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tell.backtest.common import PRIVATE_ROOT
from tell.collectors.base import iso_utc, utcnow
from tell.http import RateBudget, Sleeper, fetch, make_client

__all__ = [
    "CDX_URL",
    "DEFAULT_CACHE_DIR",
    "SNAPSHOT_URL",
    "SUPPORTED_VENDORS",
    "WAYBACK_RPS",
    "Snapshot",
    "WaybackClient",
    "cdx_params",
    "extract_job_ids",
    "parse_cdx",
    "snapshot_url",
]

CDX_URL = "https://web.archive.org/cdx/search/cdx"
SNAPSHOT_URL = "https://web.archive.org/web/{timestamp}id_/{url}"
WAYBACK_RPS = 1.0
DEFAULT_CACHE_DIR = PRIVATE_ROOT / "cache" / "wayback"
SUPPORTED_VENDORS: frozenset[str] = frozenset({"greenhouse", "lever"})
"""Vendors whose archived board HTML lists the jobs."""

_GREENHOUSE_JOB = re.compile(r"/jobs/(\d{4,})")
_LEVER_JOB = r"jobs\.(?:eu\.)?lever\.co/{token}/([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}})"
_TIMESTAMP = re.compile(r"^\d{14}$")


@dataclass(frozen=True)
class Snapshot:
    """One archived capture of a board page."""

    timestamp: str
    """``YYYYMMDDhhmmss`` in UTC, as the CDX API returns it."""
    original: str

    @property
    def at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def cdx_params(url: str, from_year: int) -> dict[str, str]:
    """Query parameters of the CDX listing for ``url`` from ``from_year``."""
    return {
        "url": url,
        "output": "json",
        "from": str(from_year),
        "filter": "statuscode:200",
    }


def snapshot_url(snapshot: Snapshot) -> str:
    """The raw (``id_``) archived copy of ``snapshot``."""
    return SNAPSHOT_URL.format(timestamp=snapshot.timestamp, url=snapshot.original)


def parse_cdx(payload: Any) -> list[Snapshot]:
    """Snapshots from a CDX JSON answer (header row first), oldest first, de-duplicated."""
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    if not isinstance(header, list) or "timestamp" not in header or "original" not in header:
        msg = f"unexpected CDX header: {header!r}"
        raise ValueError(msg)
    ts_at = header.index("timestamp")
    url_at = header.index("original")
    seen: dict[str, Snapshot] = {}
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) <= max(ts_at, url_at):
            continue
        stamp = str(row[ts_at])
        if _TIMESTAMP.match(stamp) and stamp not in seen:
            seen[stamp] = Snapshot(timestamp=stamp, original=str(row[url_at]))
    return [seen[k] for k in sorted(seen)]


def extract_job_ids(html: str, vendor: str, identifier: str) -> frozenset[str]:
    """Job identifiers linked from an archived board page (see the module docstring)."""
    if vendor == "greenhouse":
        return frozenset(_GREENHOUSE_JOB.findall(html))
    if vendor == "lever":
        token = re.escape(identifier.split(":", 1)[-1])
        return frozenset(re.findall(_LEVER_JOB.format(token=token), html, flags=re.IGNORECASE))
    return frozenset()


def _key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()


class WaybackClient:
    """Cached, rate-limited access to the CDX listing and raw snapshots.

    ``network=False`` reads only the cache and answers ``None`` for anything not cached.
    ``max_requests`` caps the network requests of this client (``None``: no cap); once it
    is reached, uncached lookups answer ``None`` and :attr:`exhausted` is set. ``sleep`` is
    used for the rate budget and the retry backoff (tests inject a fake).
    """

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        *,
        network: bool = False,
        client: httpx.Client | None = None,
        budget: RateBudget | None = None,
        max_requests: int | None = None,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self.cache_dir = cache_dir
        self.network = network
        self._client = client
        self._own_client = client is None
        self._budget = budget
        self.max_requests = max_requests
        self.requests_made = 0
        self.exhausted = False
        self.cache_hits = 0
        self._sleep = sleep

    def close(self) -> None:
        if self._client is not None and self._own_client:
            self._client.close()
        self._client = None

    def __enter__(self) -> WaybackClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- cache --------------------------------------------------------------------------------

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self.cache_dir / name
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        self.cache_hits += 1
        return data

    def _write(self, name: str, data: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # --- network ------------------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response | None:
        if not self.network:
            return None
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            self.exhausted = True
            return None
        if self._client is None:
            self._client = make_client()
        if self._budget is None:
            self._budget = RateBudget(WAYBACK_RPS, sleep=self._sleep)
        self.requests_made += 1
        try:
            response = fetch(
                url, client=self._client, params=params, budget=self._budget, sleep=self._sleep
            )
        except httpx.HTTPError:
            return None
        return response if response.status_code == httpx.codes.OK else None

    # --- public API ---------------------------------------------------------------------------

    def snapshots(self, url: str, *, from_year: int) -> list[Snapshot] | None:
        """Snapshots of ``url`` from ``from_year``; ``None`` when unknown (not cached, no network)."""
        name = f"cdx-{_key(url, str(from_year))}.json"
        cached = self._read(name)
        if cached is not None:
            return [Snapshot(str(ts), str(orig)) for ts, orig in cached.get("snapshots", ())]
        response = self._get(CDX_URL, cdx_params(url, from_year))
        if response is None:
            return None
        body = response.text.strip()
        found = parse_cdx(json.loads(body)) if body else []
        self._write(
            name,
            {
                "url": url,
                "from": from_year,
                "fetched_at": iso_utc(utcnow()),
                "snapshots": [[s.timestamp, s.original] for s in found],
            },
        )
        return found

    def job_ids(self, snapshot: Snapshot, vendor: str, identifier: str) -> frozenset[str] | None:
        """Job ids on ``snapshot``; ``None`` when unknown (not cached, no network, error)."""
        name = f"snap-{_key(snapshot.timestamp, snapshot.original, vendor)}.json"
        cached = self._read(name)
        if cached is not None:
            return frozenset(str(i) for i in cached.get("job_ids", ()))
        response = self._get(snapshot_url(snapshot))
        if response is None:
            return None
        ids = extract_job_ids(response.text, vendor, identifier)
        self._write(
            name,
            {
                "timestamp": snapshot.timestamp,
                "url": snapshot.original,
                "vendor": vendor,
                "fetched_at": iso_utc(utcnow()),
                "job_ids": sorted(ids),
            },
        )
        return ids
