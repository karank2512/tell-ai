"""Form D linking guards (ADR 0034): what an issuer's own filing says about who it is.

Production on 2026-10-14 linked 41 Form D filings to YC companies; a hand check against
SEC filings and company sites found 20 right, 20 wrong and 1 unclear. Every right link
was a Delaware corporation linked to an Active YC company whose incorporation year fit
its batch; the wrong ones were one-word near misses through fuzzy R3, common-word brands
through exact R2 (``Payload``, ``Hub``, ``Echo``), non-Delaware or non-corporation
issuers (a Cayman LP, a Wyoming LLC, Canadian corporations), issuers incorporated after
the company's YC batch, and links to Inactive or Acquired YC companies. The filing
carries the facts that tell them apart, so the matcher now reads them:

* :class:`IssuerFacts` is what a Form D payload says about the issuer (entity type,
  jurisdiction, year of incorporation, industry group) and :class:`CompanyFacts` what the
  YC directory says about the company (status, batch).
* :func:`fund_signals` names the reasons an issuer is a pooled investment vehicle (guard
  e): such an issuer is never a candidate for an operating company, so it goes straight
  to R6 without a review item.
* :func:`guard_failures` lists the guards (b)-(d) an auto-link fails; a failure demotes the
  link to R5 review with ``payload.match.guard_failed``, never to R6. Guard (a), R3
  review-only, is applied by :func:`tell.resolve.match.match_name` itself
  (:data:`R3_REVIEW_ONLY`).

Everything here is pure: no database, no network.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = [
    "FORM_D_TYPES",
    "FUND_INDUSTRY",
    "FUND_PHRASES",
    "FUND_TOKENS",
    "GUARD_REASONS",
    "OVER_FIVE_YEARS_FALLBACK_BATCH",
    "OVER_FIVE_YEARS_RECENT_BATCHES",
    "R3_REVIEW_ONLY",
    "REQUIRED_ENTITY_TYPE",
    "REQUIRED_JURISDICTION",
    "REQUIRED_YC_STATUS",
    "CompanyFacts",
    "IssuerFacts",
    "batch_year",
    "fund_signals",
    "guard_failures",
]

FORM_D_TYPES: frozenset[str] = frozenset({"form_d_filed", "form_d_amendment"})
"""Event types the guards apply to (``tell.collectors.edgar_formd``)."""

R3_REVIEW_ONLY = "r3_review_only"
"""Guard (a): a Form D R3 link (fuzzy name + state) goes to review instead (ADR 0034,
proposed; deviates from SPEC section 6 R3)."""

REQUIRED_ENTITY_TYPE = "corporation"
REQUIRED_JURISDICTION = "delaware"
REQUIRED_YC_STATUS = "active"

OVER_FIVE_YEARS_RECENT_BATCHES = 2
"""Guard (d): an issuer incorporated "over five years" before its filing is inconsistent with
a YC batch from ``filing year - 2`` on (for 2026 filings: batches from 2024 on). Relative to
the filing year so the rule does not rot as batches age."""

OVER_FIVE_YEARS_FALLBACK_BATCH = 2024
"""The batch year guard (d) starts from when a payload has no ``date_filed``."""

FUND_INDUSTRY = "pooled investment fund"
"""Form D ``industryGroupType`` of funds (compared casefolded)."""

FUND_TOKENS: frozenset[str] = frozenset({"fund", "funds", "lp", "spv", "master", "feeder", "trust"})
"""Legal-form words of pooled vehicles, matched as whole tokens after dots are dropped
(``L.P.`` -> ``lp``). Two YC names contain one (``Meter Feeder``, ``AYC Fund``); a Form D
under those names is never auto-linked, which is the price of the rule."""

FUND_PHRASES: tuple[str, ...] = ("a series of",)
"""Multi-word fund forms ("X Fund I, a series of Y LLC")."""

GUARD_REASONS: tuple[str, ...] = (
    R3_REVIEW_ONLY,
    "entity_type_not_corporation",
    "jurisdiction_not_delaware",
    "yc_status_not_active",
    "incorporated_after_batch",
    "over_five_years_for_recent_batch",
)
"""Every value ``payload.match.guard_failed`` can hold."""

_BATCH_YEAR = re.compile(r"(?:19|20)\d{2}")
_NOT_WORD = re.compile(r"[^\w\s]")


def _fold(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    folded = " ".join(value.split()).casefold()
    return folded or None


@dataclass(frozen=True)
class IssuerFacts:
    """What a Form D says about its issuer (``tell.collectors.edgar_formd.formd_payload``).

    ``year_of_inc`` is set only when the filer reported incorporation within the last five
    years; the collector stores ``null`` for "over five years" (and the rare "yet to be
    formed"), so ``over_five_years`` is true when the payload has the key with a ``null``
    value. A payload without the key says nothing either way.
    """

    name: str
    entity_type: str | None = None
    jurisdiction: str | None = None
    year_of_inc: int | None = None
    over_five_years: bool = False
    industry: str | None = None
    filed_year: int | None = None

    @classmethod
    def from_payload(cls, name: str, payload: Mapping[str, Any]) -> IssuerFacts:
        year = payload.get("year_of_inc")
        filed = payload.get("date_filed")
        filed_year: int | None = None
        if isinstance(filed, str):
            try:
                filed_year = date.fromisoformat(filed[:10]).year
            except ValueError:
                filed_year = None
        entity_type = payload.get("entity_type")
        jurisdiction = payload.get("jurisdiction_of_inc")
        industry = payload.get("industry_group_type")
        return cls(
            name=name,
            entity_type=entity_type if isinstance(entity_type, str) else None,
            jurisdiction=jurisdiction if isinstance(jurisdiction, str) else None,
            year_of_inc=year if isinstance(year, int) and not isinstance(year, bool) else None,
            over_five_years="year_of_inc" in payload and year is None,
            industry=industry if isinstance(industry, str) else None,
            filed_year=filed_year,
        )


@dataclass(frozen=True)
class CompanyFacts:
    """What the YC directory says about a company (``companies.yc_status`` / ``yc_batch``)."""

    yc_status: str | None = None
    yc_batch: str | None = None


def batch_year(batch: str | None) -> int | None:
    """``"Summer 2021"`` -> ``2021``; ``None`` for ``"Unspecified"`` or no batch."""
    if not batch:
        return None
    found = _BATCH_YEAR.search(batch)
    return int(found.group(0)) if found else None


def _name_tokens(name: str) -> list[str]:
    return _NOT_WORD.sub("", name.casefold().replace("&", " ")).split()


def fund_signals(issuer: IssuerFacts) -> list[str]:
    """Why ``issuer`` is a pooled investment vehicle (guard e); empty for an operating company.

    ``industry`` (``industryGroupType`` is "Pooled Investment Fund"), ``token:<word>`` for a
    :data:`FUND_TOKENS` word in the name, ``phrase:<text>`` for a :data:`FUND_PHRASES` match.
    """
    signals: list[str] = []
    if _fold(issuer.industry) == FUND_INDUSTRY:
        signals.append("industry")
    tokens = _name_tokens(issuer.name)
    signals.extend(f"token:{t}" for t in sorted(set(tokens) & FUND_TOKENS))
    padded = f" {' '.join(tokens)} "
    signals.extend(f"phrase:{p}" for p in FUND_PHRASES if f" {p} " in padded)
    return signals


def guard_failures(issuer: IssuerFacts, company: CompanyFacts | None) -> list[str]:
    """The guards (b)-(d) a Form D auto-link of ``issuer`` to ``company`` fails, in order.

    (b) the issuer is a corporation incorporated in Delaware (missing values fail: a Form D
    always states both); (c) the company's YC status, when known, is Active; (d) the
    issuer's incorporation year is not later than the YC batch year, and an issuer
    incorporated "over five years" ago is not linked to a batch from ``filing year - 2`` on.
    The same guards apply to R2 and R4 auto-links (ADR 0034). A YC status other than
    Active (Inactive, Acquired, Public) fails (c).
    """
    failures: list[str] = []
    if _fold(issuer.entity_type) != REQUIRED_ENTITY_TYPE:
        failures.append("entity_type_not_corporation")
    if _fold(issuer.jurisdiction) != REQUIRED_JURISDICTION:
        failures.append("jurisdiction_not_delaware")
    if company is None:
        return failures
    status = _fold(company.yc_status)
    if status is not None and status != REQUIRED_YC_STATUS:
        failures.append("yc_status_not_active")
    year = batch_year(company.yc_batch)
    if year is not None:
        if issuer.year_of_inc is not None and issuer.year_of_inc > year:
            failures.append("incorporated_after_batch")
        if issuer.over_five_years:
            recent_from = (
                issuer.filed_year - OVER_FIVE_YEARS_RECENT_BATCHES
                if issuer.filed_year is not None
                else OVER_FIVE_YEARS_FALLBACK_BATCH
            )
            if year >= recent_from:
                failures.append("over_five_years_for_recent_batch")
    return failures
