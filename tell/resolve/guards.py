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
* :func:`guard_failures` lists the guards (b)-(d) and (f) an auto-link fails; a failure
  demotes the link to R5 review with ``payload.match.guard_failed``, never to R6. Guard
  (a), R3 review-only, is applied by :func:`tell.resolve.match.match_name` itself
  (:data:`R3_REVIEW_ONLY`).
* Guard (f) ``no_site_evidence`` (ADR 0035): guards (c) and (d) read YC facts, so a company
  without a YC status had no guard beyond (b), and "Marble, Inc." (a Delaware corporation)
  would auto-link through an exact R2 alias to an unrelated marble.studio. A Form D
  auto-link to a company with no YC status now passes only when the issuer's CIK is the
  company's ``cik`` and that CIK has a trusted ``cik_source``, or when issuer-first
  discovery stored tier A/B site evidence for the (CIK, company domain) pair
  (:func:`site_evidence`). Otherwise the link is R5 review, which writes no ``edgar``
  aliases. An EDGAR filing other than a Form D (an S-1, an 8-K) carries no issuer facts,
  so guards (b)-(d) cannot run on it, but guard (f) still does
  (:func:`filing_guard_failures`): without it an exact name on an S-1 would link to a
  company with no YC status and hand it a CIK that later filings follow.

Everything here is pure: no database, no network.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from tell.models import CIK_SOURCES

__all__ = [
    "FORM_D_TYPES",
    "FUND_INDUSTRY",
    "FUND_PHRASES",
    "FUND_TOKENS",
    "GUARD_REASONS",
    "NO_SITE_EVIDENCE",
    "OVER_FIVE_YEARS_FALLBACK_BATCH",
    "OVER_FIVE_YEARS_RECENT_BATCHES",
    "R3_REVIEW_ONLY",
    "REQUIRED_ENTITY_TYPE",
    "REQUIRED_JURISDICTION",
    "REQUIRED_YC_STATUS",
    "TRUSTED_CIK_SOURCES",
    "CompanyFacts",
    "IssuerFacts",
    "batch_year",
    "cik10",
    "filing_guard_failures",
    "fund_signals",
    "guard_failures",
    "site_evidence",
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

NO_SITE_EVIDENCE = "no_site_evidence"
"""Guard (f), ADR 0035: a Form D auto-link to a company without a YC status that has neither
a trusted CIK match nor stored site evidence for the issuer."""

TRUSTED_CIK_SOURCES: frozenset[str] = frozenset(CIK_SOURCES)
"""``companies.cik_source`` values whose CIK is trusted (rule R0 and guard f): every allowed
value (``tell.models.CIK_SOURCES``); ``NULL`` (origin unknown) is not."""

GUARD_REASONS: tuple[str, ...] = (
    R3_REVIEW_ONLY,
    "entity_type_not_corporation",
    "jurisdiction_not_delaware",
    "yc_status_not_active",
    "incorporated_after_batch",
    "over_five_years_for_recent_batch",
    NO_SITE_EVIDENCE,
)
"""Every value ``payload.match.guard_failed`` can hold."""

_BATCH_YEAR = re.compile(r"(?:19|20)\d{2}")
_NOT_WORD = re.compile(r"[^\w\s]")


def cik10(value: Any) -> str | None:
    """A CIK as the 10-digit string ``companies.cik`` stores, or ``None`` when not a CIK."""
    text = (
        str(value).strip() if isinstance(value, str | int) and not isinstance(value, bool) else ""
    )
    return f"{int(text):010d}" if text.isdigit() and len(text) <= 10 else None


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
    cik: str | None = None
    """The issuer's CIK, 10 digits (guard f)."""

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
            cik=cik10(payload.get("cik")),
        )


@dataclass(frozen=True)
class CompanyFacts:
    """What the YC directory says about a company (``companies.yc_status`` / ``yc_batch``),
    plus what guard (f) reads for a company without YC facts: its ``cik`` (10 digits) and
    ``cik_source``, and ``evidence_ciks``, the CIKs with a stored tier A/B ``linked``
    ``issuer_discovery`` record on the company's domain (ADR 0035)."""

    yc_status: str | None = None
    yc_batch: str | None = None
    cik: str | None = None
    cik_source: str | None = None
    evidence_ciks: frozenset[str] = frozenset()


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


def _cik_evidence(cik: str | None, company: CompanyFacts) -> bool:
    if cik is None:
        return False
    if (
        cik10(company.cik) == cik
        and company.cik_source is not None
        and company.cik_source in TRUSTED_CIK_SOURCES
    ):
        return True
    return cik in company.evidence_ciks


def site_evidence(issuer: IssuerFacts, company: CompanyFacts) -> bool:
    """Guard (f)'s test: ``issuer`` is ``company`` by a trusted CIK or by stored site evidence.

    True when the issuer's CIK equals the company's ``cik`` and that CIK has a trusted
    ``cik_source`` (:data:`TRUSTED_CIK_SOURCES`), or when the issuer's CIK is one of the
    company's ``evidence_ciks``. An issuer without a CIK has no evidence.
    """
    return _cik_evidence(issuer.cik, company)


def filing_guard_failures(cik: Any, company: CompanyFacts | None) -> list[str]:
    """Guard (f) for an auto-link of an EDGAR filing that is not a Form D (ADR 0035).

    Such a filing (S-1, 8-K, ...) states no entity type, jurisdiction or incorporation year,
    so guards (b)-(d) have nothing to read; only (f) applies: a company with no YC status
    needs the filer's ``cik`` to be its trusted CIK or to carry stored site evidence
    (:func:`site_evidence`). ``company`` is ``None`` when the caller has no company facts;
    then nothing is checked, as in :func:`guard_failures`.
    """
    if company is None or _fold(company.yc_status) is not None:
        return []
    return [] if _cik_evidence(cik10(cik), company) else [NO_SITE_EVIDENCE]


def guard_failures(issuer: IssuerFacts, company: CompanyFacts | None) -> list[str]:
    """The guards (b)-(d) and (f) a Form D auto-link of ``issuer`` to ``company`` fails, in order.

    (b) the issuer is a corporation incorporated in Delaware (missing values fail: a Form D
    always states both); (c) the company's YC status, when known, is Active; (d) the
    issuer's incorporation year is not later than the YC batch year, and an issuer
    incorporated "over five years" ago is not linked to a batch from ``filing year - 2`` on;
    (f) a company with no YC status needs :func:`site_evidence` (ADR 0035). The same guards
    apply to R2 and R4 auto-links (ADR 0034). A YC status other than Active (Inactive,
    Acquired, Public) fails (c). ``company`` is ``None`` when the caller has no company
    facts at all (an index built by hand); then only (b) is checked.
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
    if status is None and not site_evidence(issuer, company):
        failures.append(NO_SITE_EVIDENCE)
    return failures
