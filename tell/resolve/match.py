"""Match rules R1-R6 (spec section 6) and the resolver that links unlinked events.

Rules are evaluated top to bottom; the first that fires sets ``confidence``:

======  ===========================================================  ==========  =========
Rule    Condition                                                    Confidence  Action
======  ===========================================================  ==========  =========
R1      the source URL is on the watched domain (``on_domain_company_id``)  0.98  auto_link
R2      normalized name (or its alt form) equals exactly one company's alias  0.95  auto_link
R3      ``token_set_ratio >= 92``, issuer state == ``hq_state`` (city when no state), the
        geography singles out exactly one company, and the superset guard holds  0.90  auto_link
R4      ratio ``>= 95``, exactly one candidate company (ratio >= 85) in the whole table,
        and the superset guard holds                                 0.85  auto_link
R5      ratio ``>= 85`` with two or more candidate companies, or one not auto-linked  0.60  review
R6      otherwise                                                    0           unlinked
======  ===========================================================  ==========  =========

Interpretation notes (recorded in ``docs/methods/entity-resolution.md`` and ADR 0018):

* R2 with an alias shared by two companies is ambiguous and falls through (R3 may
  disambiguate by geography; otherwise R5).
* R3 with two companies at ratio >= 92 whose geography both match is a tie; the rule
  does not fire and the event goes to review (R5) with both candidates logged.
* R4's "exactly one candidate company in the whole table" is evaluated over every alias
  (not a truncated extract pool), so a company holding many aliases cannot hide another.
* Superset guard: ``token_set_ratio`` scores any token superset 100 ("Blue Stripe Bakery"
  vs "stripe"), so R3 and R4 additionally require a guard ratio of at least 85
  (:data:`GUARD_RATIO`) against one of the company's aliases:
  ``min(token_sort_ratio(norm, alias), 100 * matched_chars / len(norm))``, i.e. the
  plain sorted-token ratio, capped by the share of the *query* the alias explains (plain
  ``token_sort_ratio`` alone dilutes with length: "Domu Technologies Press" vs "domu
  technologies" scores 85). A candidate failing the guard falls to R5 review. This only
  removes auto-links, so it is consistent with the spec table.
* The spec words R5 as "85 to 91 with one candidate"; a single candidate at 92-94 whose
  geography does not match is also sent to review rather than dropped.
* Alias hygiene (ADR 0033, :mod:`tell.resolve.aliases`): an alias failing the alias rule
  (under 3 letters and digits, all digits, or only filler tokens such as ``of`` / ``ai``)
  is never written and is ignored by the index even when present. A **single-token**
  alias is scored only against a name that is itself a single token (whole word against
  whole word, so a one-letter typo such as "QuikNode" still finds ``quicknode``) or is
  found by exact equality; it never scores against a longer name, so ``token_set_ratio``
  cannot score "Arboretum Apple a Series of CGF2026 LLC" 100 against ``apple``.
  Multi-token aliases are scored as before.
* Form D guards (ADR 0034, :mod:`tell.resolve.guards`), applied when ``match_name`` gets
  the issuer's facts (the resolver passes them for ``form_d_filed`` / ``form_d_amendment``
  events; news, ATS and other EDGAR forms are matched as before): (e) a pooled investment
  vehicle is no candidate for an operating company and goes straight to R6, with no review
  item; (a) an R3 link becomes R5 review (``r3_review_only``); (b)-(d) an R2 or R4 link
  whose issuer is not a Delaware corporation, whose company's YC status is known and not
  Active, or whose incorporation year does not fit the YC batch becomes R5 review. A
  demoted decision records ``guard_failed`` (and ``demoted_from``); a guard never demotes
  to R6.
* Distinctive-token floor (ADR 0034): an R5 review candidate must share a distinctive
  token with the name (:func:`tell.resolve.aliases.shares_distinctive_token`; not filler,
  not a descriptor such as ``technologies`` / ``therapeutics``). A name whose only
  candidates share a descriptor is R6. Auto-link rules and guard demotions are unaffected.
* Enrichment (ADR 0034): only a trusted link writes ``edgar`` aliases, ``legal_name`` and
  ``cik``: R1, R2 (after its guards), or a human review verdict. An R4 link of an EDGAR
  event links the event and writes nothing else, so a fuzzy link can never mint the exact
  alias that makes the next filing an R2.

Every decision is written into ``events.payload["match"]`` as
``{rule, ratio, candidates[:5], confidence, resolved_at}`` so a wrong link can be audited.
R1 links made by the collectors (``board.company_id`` copied onto job and tech events)
carry :data:`tell.collectors.base.R1_MATCH` for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.db import upsert
from tell.models import ALIAS_SOURCES, AtsBoard, Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve.aliases import (
    alias_problem,
    is_single_token,
    label_alias_problem,
    shares_distinctive_token,
)
from tell.resolve.guards import (
    FORM_D_TYPES,
    R3_REVIEW_ONLY,
    CompanyFacts,
    IssuerFacts,
    fund_signals,
    guard_failures,
)
from tell.resolve.normalize import domain_label, normalize_name, normalize_name_alt, slug_words

__all__ = [
    "AUTO_LINK_RULES",
    "CANDIDATE_LIMIT",
    "CONFIDENCE",
    "GEO_RATIO",
    "GUARD_RATIO",
    "REVIEW_KIND",
    "REVIEW_RATIO",
    "RULES",
    "TRUSTED_RULES",
    "UNIQUE_RATIO",
    "AliasIndex",
    "Candidate",
    "MatchDecision",
    "ResolveStats",
    "add_aliases",
    "backfill_aliases",
    "guard_ratio",
    "issuer_facts",
    "link_event",
    "match_name",
    "resolve_events",
    "review_alias_pairs",
    "stored_alias_problem",
]

_log = logging.getLogger(__name__)

Rule = Literal["R1", "R2", "R3", "R4", "R5", "R6"]
Action = Literal["auto_link", "review", "unlinked"]

RULES: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5", "R6")
AUTO_LINK_RULES: frozenset[str] = frozenset({"R1", "R2", "R3", "R4"})
TRUSTED_RULES: frozenset[str] = frozenset({"R1", "R2", "manual"})
"""Links that write enrichment (``edgar`` aliases, ``legal_name``, ``cik``; ADR 0034): R1,
R2 after its guards, and a human review verdict (``manual``)."""
CONFIDENCE: dict[str, float] = {
    "R1": 0.98,
    "R2": 0.95,
    "R3": 0.90,
    "R4": 0.85,
    "R5": 0.60,
    "R6": 0.0,
}
REVIEW_KIND = "entity_match"
CANDIDATE_LIMIT = 5
"""Candidates kept per decision (the spec's ``candidates[:5]``)."""

GEO_RATIO = 92
UNIQUE_RATIO = 95
REVIEW_RATIO = 85
GUARD_RATIO = 85
"""Superset guard for R3 / R4: :func:`guard_ratio` against one of the company's aliases must
reach this too."""

NAME_SOURCES: frozenset[str] = frozenset({"edgar", "news"})
"""Sources whose events carry a company *name* the resolver may match (ADR 0018).

Job and GitHub payloads carry ``title`` / repository ``name`` values that are not company
names; an event from any other source is skipped (counted as ``no_name``).
"""

SUPPORTED_BOARD_VENDORS: frozenset[str] = frozenset(
    {"greenhouse", "lever", "ashby", "smartrecruiters"}
)
"""Boards whose identifiers become ``ats_board`` aliases in :func:`backfill_aliases`."""

US_STATES: dict[str, str] = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "district of columbia": "dc",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}
"""Full state names folded to the two-letter codes Form D uses in ``stateOrCountry``."""


# --- decisions ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One company considered for a name: its best-scoring alias and that ratio.

    ``sort_ratio`` is the best :func:`guard_ratio` over the company's scored aliases, the
    value the superset guard checks (R3 / R4 need ``sort_ratio >= GUARD_RATIO``).
    """

    company_id: int
    alias: str
    ratio: float
    sort_ratio: float = 0.0
    distinctive: bool = True
    """Whether one of the company's aliases at or above :data:`REVIEW_RATIO` shares a
    distinctive token with the name (ADR 0034); only such a candidate makes an R5 item.
    Not part of the audit record."""

    @property
    def guarded(self) -> bool:
        """Whether the superset guard allows an auto-link to this company."""
        return self.sort_ratio >= GUARD_RATIO

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "alias": self.alias,
            "ratio": self.ratio,
            "sort_ratio": self.sort_ratio,
        }


@dataclass
class MatchDecision:
    """The outcome of :func:`match_name` for one name."""

    rule: Rule
    confidence: float
    company_id: int | None
    ratio: float | None
    candidates: list[Candidate]
    action: Action
    guard_failed: list[str] = field(default_factory=list)
    """Form D guards the auto-link failed (ADR 0034); the decision is then R5."""
    demoted_from: str | None = None
    """The auto-link rule a failed guard demoted (``R2`` / ``R3`` / ``R4``)."""
    proposed_company_id: int | None = None
    """The company the demoted auto-link would have linked."""
    fund_signals: list[str] = field(default_factory=list)
    """Why a Form D issuer is a pooled investment vehicle (guard e); the decision is then R6."""

    def audit(self, *, now: datetime) -> dict[str, Any]:
        """The ``payload["match"]`` record: ``{rule, ratio, candidates[:5], confidence, resolved_at}``.

        Plus ``guard_failed`` / ``demoted_from`` / ``proposed_company_id`` for a demoted
        Form D link and ``excluded = "pooled_fund"`` / ``fund_signals`` for a fund issuer.
        """
        record: dict[str, Any] = {
            "rule": self.rule,
            "ratio": self.ratio,
            "candidates": [c.to_dict() for c in self.candidates[:CANDIDATE_LIMIT]],
            "confidence": self.confidence,
            "resolved_at": iso_utc(now),
        }
        if self.guard_failed:
            record["guard_failed"] = list(self.guard_failed)
            record["demoted_from"] = self.demoted_from
            record["proposed_company_id"] = self.proposed_company_id
        if self.fund_signals:
            record["excluded"] = "pooled_fund"
            record["fund_signals"] = list(self.fund_signals)
        return record


def _geo_key(state: str | None) -> str | None:
    if state is None:
        return None
    folded = normalize_name(state)
    if not folded:
        return None
    return US_STATES.get(folded, folded)


def _city_key(city: str | None) -> str | None:
    if city is None:
        return None
    folded = normalize_name(city)
    return folded or None


# --- alias index -----------------------------------------------------------------------------


def guard_ratio(query: str, alias: str) -> float:
    """The superset guard: ``token_sort_ratio`` capped by how much of ``query`` the alias covers.

    ``token_sort_ratio = 200 * M / (len(query) + len(alias))`` with ``M`` matching
    characters (Indel), so ``100 * M / len(query)`` is the share of the query's characters
    the alias explains. The minimum of the two is symmetric for a short query against a
    long alias ("acme" vs "acme robotics": 47) and does not dilute for a long query with
    one extra token ("domu technologies press" vs "domu technologies": sort 85, cover 74).
    A one-letter typo passes ("beakon health" vs "beacon health": 92).
    """
    if not query or not alias:
        return 0.0
    sort = float(fuzz.token_sort_ratio(query, alias))
    cover = sort * (len(query) + len(alias)) / (2 * len(query))
    return round(min(sort, cover), 2)


def _better_alias(new: tuple[float, float, str], current: tuple[float, float, str]) -> bool:
    """Higher ``token_set_ratio`` wins, then higher ``token_sort_ratio``, then alphabetical."""
    if new[0] != current[0]:
        return new[0] > current[0]
    if new[1] != current[1]:
        return new[1] > current[1]
    return new[2] < current[2]


class AliasIndex:
    """Every alias (plus each company's normalized name) with geography, ready for scoring.

    Built once per :func:`resolve_events` run (or per :func:`match_name` call without
    one) and scanned with ``rapidfuzz.process.extract``, which handles ~10k aliases in
    milliseconds. :meth:`add` keeps the index current as a run links events and writes
    new aliases.

    Alias hygiene (ADR 0033): :meth:`add` ignores an alias that fails
    :func:`tell.resolve.aliases.alias_problem`, and keeps single-token aliases in a pool of
    their own that is scored only for single-token names (plus exact equality), so a token
    superset of a one-word alias never becomes a candidate.
    """

    def __init__(
        self,
        entries: Iterable[tuple[str, int]] = (),
        geography: Mapping[int, tuple[str | None, str | None]] | None = None,
        facts: Mapping[int, CompanyFacts] | None = None,
    ) -> None:
        self._companies: dict[str, set[int]] = {}
        self._aliases: list[str] = []
        """The fuzzy pool for every name: multi-token aliases (``process.extract`` choices)."""
        self._alts: list[str] = []
        """Alt forms of :attr:`_aliases`, position for position."""
        self._singles: list[str] = []
        """The fuzzy pool for single-token names only: single-token aliases."""
        self._single_alts: list[str] = []
        """Alt forms of :attr:`_singles`, position for position."""
        self._alt_of: dict[str, str] = {}
        """alias -> its alt form, for every registered alias."""
        self._by_alt: dict[str, set[str]] = {}
        """alt form -> aliases that fold to it (so :meth:`exact` is a dict lookup)."""
        self._geo: dict[int, tuple[str | None, str | None]] = {}
        self._facts: dict[int, CompanyFacts] = dict(facts or {})
        """company -> YC status and batch, for the Form D guards."""
        if geography:
            for company_id, (state, city) in geography.items():
                self._geo[company_id] = (_geo_key(state), _city_key(city))
        for alias, company_id in entries:
            self.add(alias, company_id)

    @classmethod
    def from_session(
        cls, session: Session, *, skip_edgar: Iterable[tuple[int, str]] = ()
    ) -> AliasIndex:
        """Load ``company_aliases`` and ``companies`` (name normalized, ``hq_state``/``hq_city``,
        ``yc_status``/``yc_batch``).

        A stored ``manual`` alias that is the company's bare domain label and fails the
        label rule (``apple`` for a company keyed by ``apple.com``, ``ramp`` from
        ``ramp.com``) is skipped like any failing alias, unless a review verdict wrote it
        (:func:`review_alias_pairs`), so the matcher behaves the same before and after
        ``tell resolve prune-aliases`` (ADR 0033). ``skip_edgar`` names ``(company_id,
        alias)`` pairs whose ``edgar`` row is left out (``tell resolve relink-formd``
        re-earns them).
        """
        companies = session.execute(
            select(
                Company.id,
                Company.name,
                Company.domain,
                Company.hq_state,
                Company.hq_city,
                Company.yc_status,
                Company.yc_batch,
            )
        ).all()
        index = cls(
            geography={row[0]: (row[3], row[4]) for row in companies},
            facts={row[0]: CompanyFacts(yc_status=row[5], yc_batch=row[6]) for row in companies},
        )
        owners: dict[int, tuple[str, str]] = {}
        for cid, name, domain, *_ in companies:
            owners[cid] = (name, domain)
            index.add(normalize_name(name), cid)
        human = review_alias_pairs(session)
        skipped = set(skip_edgar)
        rows = session.execute(
            select(CompanyAlias.alias, CompanyAlias.company_id, CompanyAlias.source)
        )
        for alias, cid, source in rows:
            if source == "edgar" and (cid, alias) in skipped:
                continue
            owner = owners.get(cid)
            if (
                source == "manual"
                and owner is not None
                and (cid, alias) not in human
                and stored_alias_problem(alias, name=owner[0], domain=owner[1]) is not None
            ):
                continue
            index.add(alias, cid)
        return index

    def __len__(self) -> int:
        return len(self._companies)

    def add(self, alias: str, company_id: int) -> None:
        """Register ``alias`` (already normalized) for ``company_id``.

        Empty aliases and aliases failing the alias rule are ignored.
        """
        if not alias or alias_problem(alias) is not None:
            return
        holders = self._companies.get(alias)
        if holders is None:
            self._companies[alias] = {company_id}
            alt = normalize_name_alt(alias)
            self._alt_of[alias] = alt
            self._by_alt.setdefault(alt, set()).add(alias)
            if is_single_token(alias):
                self._singles.append(alias)
                self._single_alts.append(alt)
            else:
                self._aliases.append(alias)
                self._alts.append(alt)
        else:
            holders.add(company_id)

    def set_geography(self, company_id: int, state: str | None, city: str | None) -> None:
        self._geo[company_id] = (_geo_key(state), _city_key(city))

    def company_facts(self, company_id: int) -> CompanyFacts | None:
        """The company's YC status and batch, when the index was built with them."""
        return self._facts.get(company_id)

    def _exact_aliases(self, norm: str, alt: str) -> set[str]:
        """Aliases equal to ``norm`` or ``alt``, or whose alt form equals ``alt``."""
        found = {form for form in (norm, alt) if form and form in self._companies}
        if alt:
            found |= self._by_alt.get(alt, set())
        return found

    def exact(self, norm: str, alt: str) -> set[int]:
        """Companies holding an alias equal to ``norm`` or ``alt`` (or whose alias's alt form is)."""
        found: set[int] = set()
        for alias in self._exact_aliases(norm, alt):
            found |= self._companies[alias]
        return found

    def _scored_aliases(self, norm: str, alt: str, pool: int) -> dict[str, float]:
        """``alias -> best token_set_ratio`` over the plain and alt forms.

        Two passes per form and pool: the top ``pool`` aliases (so R6 decisions still log
        their nearest candidates) and *every* alias at or above :data:`REVIEW_RATIO`, so a
        company holding many aliases can never push another company's candidate alias out
        of view (R4's "exactly one candidate company in the whole table" is evaluated over
        the full index). The single-token pool is scored only when the query form is a
        single token itself; otherwise a single-token alias enters only on exact equality,
        at 100.
        """
        scored: dict[str, float] = {}
        passes = [(norm, self._aliases, self._aliases), (alt, self._alts, self._aliases)]
        if norm and is_single_token(norm):
            passes.append((norm, self._singles, self._singles))
        if alt and is_single_token(alt):
            passes.append((alt, self._single_alts, self._singles))
        for query, choices, names in passes:
            if not query or not choices:
                continue
            top = process.extract(query, choices, scorer=fuzz.token_set_ratio, limit=pool)
            strong = process.extract(
                query,
                choices,
                scorer=fuzz.token_set_ratio,
                limit=None,
                score_cutoff=REVIEW_RATIO - 0.01,
            )
            for _, score, index in (*top, *strong):
                alias = names[int(index)]
                scored[alias] = max(scored.get(alias, 0.0), float(score))
        for alias in self._exact_aliases(norm, alt):
            if is_single_token(alias):
                scored[alias] = 100.0
        return scored

    def candidates(self, norm: str, alt: str, *, limit: int = CANDIDATE_LIMIT) -> list[Candidate]:
        """Companies ranked by ``token_set_ratio`` against every alias (plain and alt form).

        Returns every company at or above :data:`REVIEW_RATIO` and at least the top
        ``limit``; callers truncate to ``candidates[:5]`` for the audit record. Each
        company carries its best-scoring alias (ties: higher ``token_sort_ratio``, then
        alphabetical) and ``sort_ratio``, the best ``token_sort_ratio`` over its scored
        aliases, for the superset guard.
        """
        if not norm or not self._companies:
            return []
        scored = self._scored_aliases(norm, alt, max(limit * 4, 20))
        best_alias: dict[int, tuple[float, float, str]] = {}
        """company -> (ratio, that alias's sort ratio, alias): the alias shown in the audit."""
        best_sort: dict[int, float] = {}
        distinctive: set[int] = set()
        names = [form for form in (norm, alt) if form]
        for alias, ratio in scored.items():
            sort_ratio = guard_ratio(norm, alias)
            if alt:
                sort_ratio = max(sort_ratio, guard_ratio(alt, self._alt_of[alias]))
            shares = ratio >= REVIEW_RATIO and shares_distinctive_token(
                names, (alias, self._alt_of[alias])
            )
            for company_id in self._companies[alias]:
                if shares:
                    distinctive.add(company_id)
                best_sort[company_id] = max(best_sort.get(company_id, 0.0), sort_ratio)
                current = best_alias.get(company_id)
                if current is None or _better_alias((ratio, sort_ratio, alias), current):
                    best_alias[company_id] = (ratio, sort_ratio, alias)
        ranked = sorted(
            (
                Candidate(
                    company_id,
                    alias,
                    round(ratio, 2),
                    round(best_sort[company_id], 2),
                    company_id in distinctive,
                )
                for company_id, (ratio, _, alias) in best_alias.items()
            ),
            key=lambda c: (-c.ratio, -c.sort_ratio, c.company_id),
        )
        strong = sum(1 for c in ranked if c.ratio >= REVIEW_RATIO)
        return ranked[: max(limit, strong)]

    def geography_matches(self, company_id: int, state: str | None, city: str | None) -> bool:
        """Issuer state equals ``hq_state``; city equals ``hq_city`` when either state is missing."""
        known = self._geo.get(company_id)
        if known is None:
            return False
        company_state, company_city = known
        issuer_state = _geo_key(state)
        if issuer_state is not None and company_state is not None:
            return issuer_state == company_state
        issuer_city = _city_key(city)
        return issuer_city is not None and issuer_city == company_city


def stored_alias_problem(alias: str, *, name: str, domain: str) -> str | None:
    """Why a stored alias of the company (``name``, ``domain``) fails the alias rule, or ``None``.

    Every alias gets :func:`tell.resolve.aliases.alias_problem`; an alias that is the
    company's domain label (and not also its normalized name) gets
    :func:`tell.resolve.aliases.label_alias_problem` too, the test ``backfill_aliases``
    applies before writing a label.
    """
    problem = alias_problem(alias)
    if problem is not None:
        return problem
    if alias == domain_label(domain) and alias != normalize_name(name):
        return label_alias_problem(alias, domain)
    return None


def review_alias_pairs(session: Session) -> set[tuple[int, str]]:
    """``(company_id, alias)`` pairs a review verdict wrote (``link`` / ``create`` items).

    A human decided those; alias hygiene never second-guesses them.
    """
    stmt = select(ReviewQueueItem.payload).where(
        ReviewQueueItem.kind == REVIEW_KIND, ReviewQueueItem.status == "resolved"
    )
    pairs: set[tuple[int, str]] = set()
    for payload in session.scalars(stmt):
        company_id = payload.get("company_id")
        name = payload.get("name")
        if isinstance(company_id, int) and isinstance(name, str):
            pairs.add((company_id, normalize_name(name)))
    return pairs


# --- the rules -------------------------------------------------------------------------------


def _decision(
    rule: Rule, company_id: int | None, ratio: float | None, candidates: list[Candidate]
) -> MatchDecision:
    action: Action
    if rule in AUTO_LINK_RULES:
        action = "auto_link"
    elif rule == "R5":
        action = "review"
    else:
        action = "unlinked"
    return MatchDecision(
        rule=rule,
        confidence=CONFIDENCE[rule],
        company_id=company_id,
        ratio=ratio,
        candidates=candidates[:CANDIDATE_LIMIT],
        action=action,
    )


def _demote(decision: MatchDecision, failed: list[str]) -> MatchDecision:
    """``decision`` as an R5 review item that records the guards it failed (ADR 0034)."""
    demoted = _decision("R5", None, decision.ratio, decision.candidates)
    demoted.guard_failed = failed
    demoted.demoted_from = decision.rule
    demoted.proposed_company_id = decision.company_id
    return demoted


def _guarded(
    decision: MatchDecision, issuer: IssuerFacts | None, index: AliasIndex
) -> MatchDecision:
    """Apply the Form D guards to an auto-link (no-op without issuer facts)."""
    if issuer is None or decision.action != "auto_link" or decision.company_id is None:
        return decision
    failed = guard_failures(issuer, index.company_facts(decision.company_id))
    if decision.rule == "R3":
        failed = [R3_REVIEW_ONLY, *failed]
    return _demote(decision, failed) if failed else decision


def _name_rules(
    idx: AliasIndex,
    norm: str,
    alt: str,
    candidates: list[Candidate],
    *,
    state: str | None,
    city: str | None,
    issuer: IssuerFacts | None,
) -> MatchDecision:
    """R2-R6 for a name (R1 and the fund exclusion are decided by :func:`match_name`)."""
    best = candidates[0].ratio if candidates else None
    exact = idx.exact(norm, alt)
    if len(exact) == 1:
        company_id = next(iter(exact))
        ratio = next((c.ratio for c in candidates if c.company_id == company_id), 100.0)
        return _guarded(_decision("R2", company_id, ratio, candidates), issuer, idx)

    strong = [c for c in candidates if c.ratio >= REVIEW_RATIO]

    # R3: name + geography. The geography has to single out exactly one company; a tie
    # (two companies at >= 92 in the issuer's state) is ambiguous and goes to review with
    # both candidates logged. The superset guard applies before the link is made.
    geo = [
        c
        for c in strong
        if c.ratio >= GEO_RATIO and idx.geography_matches(c.company_id, state, city)
    ]
    if len({c.company_id for c in geo}) == 1 and geo[0].guarded:
        return _guarded(_decision("R3", geo[0].company_id, geo[0].ratio, candidates), issuer, idx)

    # R4: name only, unique in the whole table (``strong`` holds every company >= 85).
    companies = {c.company_id for c in strong}
    if strong and strong[0].ratio >= UNIQUE_RATIO and len(companies) == 1 and strong[0].guarded:
        return _guarded(
            _decision("R4", strong[0].company_id, strong[0].ratio, candidates), issuer, idx
        )

    # R5 needs a candidate that shares a distinctive token with the name (ADR 0034): a
    # shared descriptor ("technologies", "therapeutics") is no evidence of identity.
    reviewable = [c for c in strong if c.distinctive]
    if reviewable:
        return _decision("R5", None, reviewable[0].ratio, candidates)

    return _decision("R6", None, best, candidates)


def match_name(
    session: Session,
    name: str,
    *,
    state: str | None = None,
    city: str | None = None,
    on_domain_company_id: int | None = None,
    index: AliasIndex | None = None,
    issuer: IssuerFacts | None = None,
) -> MatchDecision:
    """Apply R1-R6 to ``name`` and return the first rule that fires.

    ``index`` (an :class:`AliasIndex`) avoids reloading the alias table on every call;
    without one the table is read from ``session``. ``issuer`` (a Form D's
    :class:`~tell.resolve.guards.IssuerFacts`) turns on the Form D guards (ADR 0034): a
    pooled fund is R6, and an auto-link failing a guard is R5 with ``guard_failed``.
    """
    idx = index if index is not None else AliasIndex.from_session(session)
    norm = normalize_name(name)
    alt = normalize_name_alt(name)
    candidates = idx.candidates(norm, alt)
    best = candidates[0].ratio if candidates else None

    if on_domain_company_id is not None:
        return _decision("R1", on_domain_company_id, best, candidates)

    if issuer is not None:
        signals = fund_signals(issuer)
        if signals:
            excluded = _decision("R6", None, best, candidates)
            excluded.fund_signals = signals
            return excluded

    return _name_rules(idx, norm, alt, candidates, state=state, city=city, issuer=issuer)


# --- resolving events ------------------------------------------------------------------------


@dataclass
class ResolveStats:
    """Counts from one :func:`resolve_events` run."""

    scanned: int = 0
    auto_linked: int = 0
    review: int = 0
    unlinked: int = 0
    no_name: int = 0
    rejected_skipped: int = 0
    aliases_added: int = 0
    guard_demoted: int = 0
    """Form D auto-links a guard sent to review instead (ADR 0034)."""
    pooled_funds: int = 0
    """Form D issuers excluded as pooled investment vehicles (R6, no review item)."""
    by_rule: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "auto_linked": self.auto_linked,
            "review": self.review,
            "unlinked": self.unlinked,
            "no_name": self.no_name,
            "rejected_skipped": self.rejected_skipped,
            "aliases_added": self.aliases_added,
            "guard_demoted": self.guard_demoted,
            "pooled_funds": self.pooled_funds,
            "by_rule": dict(sorted(self.by_rule.items())),
        }


@dataclass(frozen=True)
class _Subject:
    """What a payload offers the matcher."""

    name: str
    state: str | None
    city: str | None
    previous_names: tuple[str, ...]
    cik: str | None
    legal_name: str | None
    facts: IssuerFacts | None = None
    """The issuer's facts for a Form D event (turns on the guards); ``None`` otherwise."""

    @property
    def names(self) -> tuple[str, ...]:
        """The name and every previous name: what a trusted link writes as aliases."""
        return (self.name, *self.previous_names)


def issuer_facts(event: EventRow) -> IssuerFacts | None:
    """The Form D guards' view of ``event``, or ``None`` for anything but a Form D."""
    if event.source != "edgar" or event.type not in FORM_D_TYPES:
        return None
    name = event.payload.get("issuer_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return IssuerFacts.from_payload(name.strip(), event.payload)


def _news_name(payload: Mapping[str, Any]) -> str | None:
    """The company name a Google News query URL carries (``q="Acme"``), when any."""
    query = payload.get("query")
    if not isinstance(query, str):
        return None
    try:
        values = parse_qs(urlparse(query).query).get("q") or []
    except ValueError:
        return None
    for value in values:
        text = value.strip().strip('"').strip()
        if text:
            return text
    return None


def _subject(event: EventRow) -> _Subject | None:
    """The name (and geography) an event offers the matcher, chosen by source.

    EDGAR events name the company in ``issuer_name`` (Form D) or ``company_name`` (other
    forms); news events in their Google News query. Any other source is skipped: job
    and GitHub payloads carry titles and repository names, not company names.
    """
    if event.source not in NAME_SOURCES:
        return None
    payload = event.payload
    name: Any = None
    if event.source == "edgar":
        name = payload.get("issuer_name") or payload.get("company_name")
    else:
        name = _news_name(payload)
    if not isinstance(name, str) or not name.strip():
        return None
    previous_raw = payload.get("previous_names")
    previous = tuple(
        p for p in (previous_raw if isinstance(previous_raw, list) else []) if isinstance(p, str)
    )
    state = payload.get("issuer_state")
    city = payload.get("issuer_city")
    cik = payload.get("cik")
    return _Subject(
        name=name.strip(),
        state=state if isinstance(state, str) else None,
        city=city if isinstance(city, str) else None,
        previous_names=previous,
        cik=cik if isinstance(cik, str) and cik.strip() else None,
        legal_name=name.strip() if event.source == "edgar" else None,
        facts=issuer_facts(event),
    )


def _alias_source(event_source: str) -> str:
    return event_source if event_source in ALIAS_SOURCES else "manual"


def add_aliases(session: Session, company_id: int, names: Iterable[str], *, source: str) -> int:
    """Insert normalized aliases for ``company_id`` (``ON CONFLICT DO NOTHING``); return how many were new.

    A name whose normalized form fails the alias rule
    (:func:`tell.resolve.aliases.alias_problem`: too short, all digits, only filler
    tokens) is skipped, whatever the source.
    """
    if source not in ALIAS_SOURCES:
        msg = f"alias source must be one of {sorted(ALIAS_SOURCES)}, not {source!r}"
        raise ValueError(msg)
    rows = {
        normalized: {"company_id": company_id, "alias": normalized, "source": source}
        for normalized in (normalize_name(n) for n in names)
        if normalized and alias_problem(normalized) is None
    }
    if not rows:
        return 0
    return upsert(
        session, CompanyAlias, list(rows.values()), conflict_columns=["company_id", "alias"]
    )


def _cik_taken(session: Session, cik: str, company_id: int) -> bool:
    stmt = select(Company.id).where(Company.cik == cik, Company.id != company_id).limit(1)
    return session.scalar(stmt) is not None


def link_event(
    session: Session,
    event: EventRow,
    company_id: int,
    *,
    index: AliasIndex | None = None,
    trusted: bool = True,
) -> int:
    """Link ``event`` to ``company_id`` with the enrichment a trusted link gets; return aliases added.

    Sets ``event.company_id``. For a trusted link (``trusted``: R1, R2 after its guards, a
    review verdict; ADR 0034) of an EDGAR event it also fills ``companies.cik`` (when empty
    and not held by another company) and ``companies.legal_name`` (when empty) and adds
    the event's name and every ``previous_names`` entry as ``edgar`` aliases, so a later
    filing under a former name resolves through R2 (spec section 6: "each previous name
    becomes an alias when a filing is linked"). An untrusted EDGAR link (R3, R4) writes
    nothing but ``company_id``. News links add their name as a ``news`` alias as before.
    ``index`` is kept current when given.
    """
    event.company_id = company_id
    subject = _subject(event)
    if subject is None:
        return 0
    if event.source == "edgar" and not trusted:
        return 0
    company = session.get(Company, company_id)
    if company is not None and event.source == "edgar":
        if company.cik is None and subject.cik and not _cik_taken(session, subject.cik, company_id):
            company.cik = subject.cik
        if company.legal_name is None and subject.legal_name:
            company.legal_name = subject.legal_name
    names = subject.names
    added = add_aliases(session, company_id, names, source=_alias_source(event.source))
    if index is not None:
        for name in names:
            index.add(normalize_name(name), company_id)
    return added


def _review_key(decision: MatchDecision) -> frozenset[int]:
    return frozenset(c.company_id for c in decision.candidates if c.ratio >= REVIEW_RATIO)


def _rejected_pairs(session: Session) -> set[tuple[str, frozenset[int]]]:
    """``(normalized name, candidate company ids)`` of every rejected ``entity_match`` item."""
    stmt = select(ReviewQueueItem.payload).where(
        ReviewQueueItem.kind == REVIEW_KIND, ReviewQueueItem.status == "rejected"
    )
    pairs: set[tuple[str, frozenset[int]]] = set()
    for payload in session.scalars(stmt):
        name = payload.get("name")
        candidates = payload.get("candidates") or []
        if not isinstance(name, str) or not isinstance(candidates, list):
            continue
        ids = frozenset(
            int(c["company_id"])
            for c in candidates
            if isinstance(c, dict) and isinstance(c.get("company_id"), int)
        )
        pairs.add((normalize_name(name), ids))
    return pairs


def _was_rejected(pairs: set[tuple[str, frozenset[int]]], norm: str, ids: frozenset[int]) -> bool:
    return any(name == norm and ids <= rejected for name, rejected in pairs)


def _open_review_exists(session: Session, event_id: str) -> bool:
    stmt = (
        select(ReviewQueueItem.id)
        .where(
            ReviewQueueItem.kind == REVIEW_KIND,
            ReviewQueueItem.status == "open",
            ReviewQueueItem.payload.contains({"event_id": event_id}),
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def _queue_review(
    session: Session, event: EventRow, subject: _Subject, decision: MatchDecision, now: datetime
) -> bool:
    """One open ``entity_match`` item per event; a rerun never duplicates it.

    A guard-demoted decision adds ``guard_failed`` and ``proposed_company_id`` so the
    reviewer sees why it was not linked. Returns whether an item was opened.
    """
    if _open_review_exists(session, event.event_id):
        return False
    payload: dict[str, Any] = {
        "event_id": event.event_id,
        "name": subject.name,
        "state": subject.state,
        "city": subject.city,
        "candidates": [c.to_dict() for c in decision.candidates[:CANDIDATE_LIMIT]],
    }
    if decision.guard_failed:
        payload["guard_failed"] = list(decision.guard_failed)
        payload["proposed_company_id"] = decision.proposed_company_id
    session.add(ReviewQueueItem(kind=REVIEW_KIND, payload=payload, created_at=now))
    return True


def _unresolved_events(session: Session, *, limit: int | None, retry: bool) -> list[EventRow]:
    stmt = select(EventRow).where(EventRow.company_id.is_(None))
    if not retry:
        rule = EventRow.payload.op("->")("match").op("->>")("rule")
        stmt = stmt.where((rule.is_(None)) | (rule.not_in(RULES)))
    stmt = stmt.order_by(EventRow.occurred_at, EventRow.event_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def resolve_events(
    session: Session, *, now: datetime, limit: int | None = None, retry: bool = False
) -> ResolveStats:
    """Match every event with ``company_id NULL`` and record the decision in its payload.

    Form D events are matched on ``issuer_name`` with ``issuer_state`` / ``issuer_city``
    and the Form D guards (ADR 0034); other EDGAR events on ``company_name``; news events
    on the company name in their Google News query. On a trusted auto-link (R1, R2) the
    company gets its ``cik`` and ``legal_name`` when those were empty, and the issuer name
    plus every previous name become aliases; an R4 link writes only ``company_id``. R5
    opens one ``review_queue`` item per event (never twice); a pair a human already
    rejected is left unlinked instead. R6 leaves the event unlinked and kept.

    Events whose payload already carries an R1-R6 decision are skipped unless ``retry``
    is set, so a second run changes nothing; ``retry`` re-evaluates unlinked events
    after aliases or companies were added.
    """
    stats = ResolveStats()
    index = AliasIndex.from_session(session)
    rejected = _rejected_pairs(session)
    for event in _unresolved_events(session, limit=limit, retry=retry):
        stats.scanned += 1
        subject = _subject(event)
        if subject is None:
            stats.no_name += 1
            continue
        decision = match_name(
            session,
            subject.name,
            state=subject.state,
            city=subject.city,
            index=index,
            issuer=subject.facts,
        )
        audit = decision.audit(now=now)
        previous = event.payload.get("match")
        if isinstance(previous, dict) and previous.get("rule") not in RULES:
            audit["source_rule"] = previous.get("rule")
        if isinstance(previous, dict) and isinstance(previous.get("previous"), dict):
            audit["previous"] = previous["previous"]  # a relinked event keeps its audit trail
        if decision.guard_failed:
            stats.guard_demoted += 1
        if decision.fund_signals:
            stats.pooled_funds += 1
        if decision.action == "auto_link" and decision.company_id is not None:
            stats.auto_linked += 1
            stats.aliases_added += link_event(
                session,
                event,
                decision.company_id,
                index=index,
                trusted=decision.rule in TRUSTED_RULES,
            )
        elif decision.action == "review":
            norm = normalize_name(subject.name)
            if _was_rejected(rejected, norm, _review_key(decision)):
                stats.rejected_skipped += 1
                stats.unlinked += 1
                audit["rejected"] = True
            else:
                stats.review += 1
                _queue_review(session, event, subject, decision, now)
        else:
            stats.unlinked += 1
        stats.by_rule[decision.rule] = stats.by_rule.get(decision.rule, 0) + 1
        event.payload = {**event.payload, "match": audit}
        _log.debug("resolve: %s %s -> %s", event.source, event.event_id[:12], decision.rule)
    session.flush()
    return stats


def backfill_aliases(session: Session) -> int:
    """Seed ``company_aliases`` so R2 has data; idempotent. Returns the number of new rows.

    Every company gets its normalized ``name`` and its domain label (source ``manual``);
    every linked, supported ATS board contributes its identifier as words (``acme-labs``
    -> ``acme``) with source ``ats_board``. Boards store no display name (discovery keeps
    it only in logs), so the identifier is the only board-side name available.

    Every alias passes the alias rule; the domain label also has to pass
    :func:`tell.resolve.aliases.label_alias_problem` (not a common English word of 4
    letters or fewer, not a platform's domain), so ``of.one`` adds no ``of`` and a
    company keyed by ``apple.com`` adds no ``apple`` (ADR 0033).
    """
    added = 0
    for company in session.scalars(select(Company).order_by(Company.id)):
        names = [company.name]
        label = domain_label(company.domain)
        if label_alias_problem(label, company.domain) is None:
            names.append(label)
        added += add_aliases(session, company.id, names, source="manual")
    boards = select(AtsBoard).where(
        AtsBoard.company_id.is_not(None),
        AtsBoard.vendor.in_(sorted(SUPPORTED_BOARD_VENDORS)),
        AtsBoard.status != "no_board",
    )
    for board in session.scalars(boards.order_by(AtsBoard.id)):
        if board.company_id is None:
            continue
        added += add_aliases(
            session, board.company_id, [slug_words(board.identifier)], source="ats_board"
        )
    session.flush()
    return added
