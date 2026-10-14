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
from tell.resolve.aliases import alias_problem, is_single_token, label_alias_problem
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
    "UNIQUE_RATIO",
    "AliasIndex",
    "Candidate",
    "MatchDecision",
    "ResolveStats",
    "add_aliases",
    "backfill_aliases",
    "guard_ratio",
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

    def audit(self, *, now: datetime) -> dict[str, Any]:
        """The ``payload["match"]`` record: ``{rule, ratio, candidates[:5], confidence, resolved_at}``."""
        return {
            "rule": self.rule,
            "ratio": self.ratio,
            "candidates": [c.to_dict() for c in self.candidates[:CANDIDATE_LIMIT]],
            "confidence": self.confidence,
            "resolved_at": iso_utc(now),
        }


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
        if geography:
            for company_id, (state, city) in geography.items():
                self._geo[company_id] = (_geo_key(state), _city_key(city))
        for alias, company_id in entries:
            self.add(alias, company_id)

    @classmethod
    def from_session(cls, session: Session) -> AliasIndex:
        """Load ``company_aliases`` and ``companies`` (name normalized, ``hq_state``/``hq_city``).

        A stored ``manual`` alias that is the company's bare domain label and fails the
        label rule (``apple`` for a company keyed by ``apple.com``, ``ramp`` from
        ``ramp.com``) is skipped like any failing alias, unless a review verdict wrote it
        (:func:`review_alias_pairs`), so the matcher behaves the same before and after
        ``tell resolve prune-aliases`` (ADR 0033).
        """
        companies = session.execute(
            select(Company.id, Company.name, Company.domain, Company.hq_state, Company.hq_city)
        ).all()
        index = cls(geography={cid: (state, city) for cid, _, _, state, city in companies})
        owners: dict[int, tuple[str, str]] = {}
        for cid, name, domain, _, _ in companies:
            owners[cid] = (name, domain)
            index.add(normalize_name(name), cid)
        human = review_alias_pairs(session)
        rows = session.execute(
            select(CompanyAlias.alias, CompanyAlias.company_id, CompanyAlias.source)
        )
        for alias, cid, source in rows:
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
        for alias, ratio in scored.items():
            sort_ratio = guard_ratio(norm, alias)
            if alt:
                sort_ratio = max(sort_ratio, guard_ratio(alt, self._alt_of[alias]))
            for company_id in self._companies[alias]:
                best_sort[company_id] = max(best_sort.get(company_id, 0.0), sort_ratio)
                current = best_alias.get(company_id)
                if current is None or _better_alias((ratio, sort_ratio, alias), current):
                    best_alias[company_id] = (ratio, sort_ratio, alias)
        ranked = sorted(
            (
                Candidate(company_id, alias, round(ratio, 2), round(best_sort[company_id], 2))
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


def match_name(
    session: Session,
    name: str,
    *,
    state: str | None = None,
    city: str | None = None,
    on_domain_company_id: int | None = None,
    index: AliasIndex | None = None,
) -> MatchDecision:
    """Apply R1-R6 to ``name`` and return the first rule that fires.

    ``index`` (an :class:`AliasIndex`) avoids reloading the alias table on every call;
    without one the table is read from ``session``.
    """
    idx = index if index is not None else AliasIndex.from_session(session)
    norm = normalize_name(name)
    alt = normalize_name_alt(name)
    candidates = idx.candidates(norm, alt)
    best = candidates[0].ratio if candidates else None

    if on_domain_company_id is not None:
        return _decision("R1", on_domain_company_id, best, candidates)

    exact = idx.exact(norm, alt)
    if len(exact) == 1:
        company_id = next(iter(exact))
        ratio = next((c.ratio for c in candidates if c.company_id == company_id), 100.0)
        return _decision("R2", company_id, ratio, candidates)

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
        return _decision("R3", geo[0].company_id, geo[0].ratio, candidates)

    # R4: name only, unique in the whole table (``strong`` holds every company >= 85).
    companies = {c.company_id for c in strong}
    if strong and strong[0].ratio >= UNIQUE_RATIO and len(companies) == 1 and strong[0].guarded:
        return _decision("R4", strong[0].company_id, strong[0].ratio, candidates)

    if strong:
        return _decision("R5", None, strong[0].ratio, candidates)

    return _decision("R6", None, best, candidates)


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
    session: Session, event: EventRow, company_id: int, *, index: AliasIndex | None = None
) -> int:
    """Link ``event`` to ``company_id`` with the enrichment every link gets; return aliases added.

    Sets ``event.company_id``; for an EDGAR event fills ``companies.cik`` (when empty and
    not held by another company) and ``companies.legal_name`` (when empty); adds the
    event's name and every ``previous_names`` entry as aliases (source ``edgar`` /
    ``news``, so a later filing under a former name resolves through R2). Used by the
    resolver's auto-links and by review verdicts alike (spec section 6: "each previous
    name becomes an alias when a filing is linked"). ``index`` is kept current when given.
    """
    event.company_id = company_id
    subject = _subject(event)
    if subject is None:
        return 0
    company = session.get(Company, company_id)
    if company is not None and event.source == "edgar":
        if company.cik is None and subject.cik and not _cik_taken(session, subject.cik, company_id):
            company.cik = subject.cik
        if company.legal_name is None and subject.legal_name:
            company.legal_name = subject.legal_name
    names = (subject.name, *subject.previous_names)
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
) -> None:
    """One open ``entity_match`` item per event; a rerun never duplicates it."""
    if _open_review_exists(session, event.event_id):
        return
    session.add(
        ReviewQueueItem(
            kind=REVIEW_KIND,
            payload={
                "event_id": event.event_id,
                "name": subject.name,
                "state": subject.state,
                "city": subject.city,
                "candidates": [c.to_dict() for c in decision.candidates[:CANDIDATE_LIMIT]],
            },
            created_at=now,
        )
    )


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

    Form D events are matched on ``issuer_name`` with ``issuer_state`` / ``issuer_city``;
    other EDGAR events on ``company_name``; news events on the company name in their
    Google News query. On auto-link the company gets its ``cik`` and ``legal_name`` when
    those were empty, and the issuer name plus every previous name become aliases. R5
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
            session, subject.name, state=subject.state, city=subject.city, index=index
        )
        audit = decision.audit(now=now)
        previous = event.payload.get("match")
        if isinstance(previous, dict) and previous.get("rule") not in RULES:
            audit["source_rule"] = previous.get("rule")
        if decision.action == "auto_link" and decision.company_id is not None:
            stats.auto_linked += 1
            stats.aliases_added += link_event(session, event, decision.company_id, index=index)
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
