"""Seed ``companies`` from the public YC company directory (``docs/sources/yc-directory.md``).

Source: the community mirror ``https://yc-oss.github.io/api/companies/all.json``, a
daily JSON export of the public Algolia index behind ``ycombinator.com/companies``
(launched companies only). Only four fields are read, ``name``, ``website``,
``batch`` and ``all_locations``; the pydantic model drops everything else on parse,
so nothing about people (founders, descriptions) is ever held, let alone stored.

Normalisation:

* ``domain`` is the registrable domain of ``website`` (``tldextract``, private
  suffixes included), lowercase, no ``www``; rows without a usable website are
  skipped and counted.
* ``name`` is the YC name, whitespace-collapsed.
* ``hq_city`` / ``hq_state`` / ``hq_country`` come from the first location when it
  looks like ``City, ST, USA`` (two-letter state); ``hq_country`` is then ``US``.
  Any other shape leaves the three columns ``NULL`` rather than guessing.

Writes are ``INSERT ... ON CONFLICT (domain) DO UPDATE`` on ``companies``: ``name``
takes the YC value, the ``hq_*`` columns keep an existing value when YC has none, and
the row is touched only when something changes, so a re-seed is a no-op. Within one
seed the first record for a domain wins and later ones count as duplicates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from tell.discovery import registrable_domain
from tell.http import RateBudget, get
from tell.models import Company

__all__ = [
    "YC_DIRECTORY_URL",
    "SeedStats",
    "YcCompany",
    "YcRecord",
    "fetch_directory",
    "parse_directory",
    "parse_location",
    "seed_companies",
    "seed_from_file",
]

YC_DIRECTORY_URL = "https://yc-oss.github.io/api/companies/all.json"
"""Daily JSON mirror of the public YC directory (all launched companies)."""

UPSERT_BATCH = 500

_WHITESPACE = re.compile(r"\s+")
_STATE = re.compile(r"[A-Z]{2}")
_US_COUNTRIES = frozenset({"USA", "United States", "United States of America", "US"})


class YcRecord(BaseModel):
    """The four directory fields tell reads; every other field is dropped on parse."""

    model_config = ConfigDict(extra="ignore")

    name: str
    website: str | None = None
    batch: str | None = None
    all_locations: str | None = None


class YcCompany(TypedDict):
    """One normalised directory entry."""

    name: str
    domain: str
    batch: str | None
    locations: str | None


@dataclass(frozen=True)
class SeedStats:
    """Counts from one seed: ``read`` = records in, ``inserted`` + ``updated`` + ``unchanged``
    = distinct domains written, plus records skipped for a missing website or a duplicate."""

    read: int
    inserted: int
    updated: int
    unchanged: int
    skipped_no_website: int
    duplicates: int


_RECORDS = TypeAdapter(list[YcRecord])


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return collapsed or None


def parse_location(locations: str | None) -> tuple[str | None, str | None, str | None]:
    """``"San Francisco, CA, USA; Remote"`` -> ``("San Francisco", "CA", "US")``.

    Only the first ``;``-separated location is read, and only the US shape
    ``City, ST, USA`` is parsed; anything else is ``(None, None, None)``.
    """
    first = _clean((locations or "").split(";", 1)[0])
    if first is None:
        return None, None, None
    parts = [part.strip() for part in first.split(",")]
    if len(parts) != 3 or not all(parts):
        return None, None, None
    city, state, country = parts
    if country not in _US_COUNTRIES or not _STATE.fullmatch(state):
        return None, None, None
    return city, state, "US"


def parse_directory(raw: Any) -> tuple[list[YcCompany], int]:
    """Validate a directory JSON array; return the usable companies and the no-website count."""
    records = _RECORDS.validate_python(raw)
    companies: list[YcCompany] = []
    skipped = 0
    for record in records:
        name = _clean(record.name)
        domain = registrable_domain(record.website) if record.website else None
        if name is None or domain is None:
            skipped += 1
            continue
        companies.append(
            YcCompany(
                name=name,
                domain=domain,
                batch=_clean(record.batch),
                locations=_clean(record.all_locations),
            )
        )
    return companies, skipped


def fetch_directory(
    client: httpx.Client, *, url: str = YC_DIRECTORY_URL, budget: RateBudget | None = None
) -> list[YcCompany]:
    """Download and normalise the public directory (one GET, retried per ``tell.http``)."""
    response = get(url, client=client, budget=budget)
    companies, _ = parse_directory(response.json())
    return companies


def _rows(companies: Iterable[YcCompany]) -> tuple[list[dict[str, Any]], int]:
    rows: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for company in companies:
        if company["domain"] in rows:
            duplicates += 1
            continue
        city, state, country = parse_location(company["locations"])
        rows[company["domain"]] = {
            "domain": company["domain"],
            "name": company["name"],
            "hq_city": city,
            "hq_state": state,
            "hq_country": country,
        }
    return list(rows.values()), duplicates


def _upsert_chunk(session: Session, chunk: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """Upsert one chunk; return (inserted, updated)."""
    domains = [row["domain"] for row in chunk]
    existing = set(session.scalars(select(Company.domain).where(Company.domain.in_(domains))))
    stmt = pg_insert(Company).values(list(chunk))
    excluded = stmt.excluded
    merged = {
        column: func.coalesce(excluded[column], getattr(Company, column))
        for column in ("hq_city", "hq_state", "hq_country")
    }
    changed = or_(
        Company.name.is_distinct_from(excluded["name"]),
        *(getattr(Company, column).is_distinct_from(value) for column, value in merged.items()),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["domain"],
        set_={"name": excluded["name"], **merged, "updated_at": func.now()},
        where=changed,
    )
    written = session.execute(stmt.returning(Company.domain)).scalars().all()
    inserted = sum(1 for domain in written if domain not in existing)
    return inserted, len(written) - inserted


def seed_companies(
    session: Session, companies: Iterable[YcCompany], *, read: int | None = None
) -> SeedStats:
    """Upsert normalised directory entries into ``companies`` (flushes, does not commit)."""
    listed = list(companies)
    rows, duplicates = _rows(listed)
    inserted = updated = 0
    for start in range(0, len(rows), UPSERT_BATCH):
        chunk_inserted, chunk_updated = _upsert_chunk(session, rows[start : start + UPSERT_BATCH])
        inserted += chunk_inserted
        updated += chunk_updated
    session.flush()
    total = len(listed) if read is None else read
    return SeedStats(
        read=total,
        inserted=inserted,
        updated=updated,
        unchanged=len(rows) - inserted - updated,
        skipped_no_website=total - len(listed),
        duplicates=duplicates,
    )


def seed_from_file(session: Session, path: Path | str) -> SeedStats:
    """Seed from a saved directory JSON array (the ``all.json`` shape, or a trimmed copy)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    companies, _ = parse_directory(raw)
    return seed_companies(session, companies, read=len(raw))
