"""Match rules R1-R6 (spec section 6) and the resolver that links unlinked events.

Rules are evaluated top to bottom; the first that fires sets ``confidence``:

======  ===========================================================  ==========  =========
Rule    Condition                                                    Confidence  Action
======  ===========================================================  ==========  =========
R1      the source URL is on the watched domain (``on_domain_company_id``)  0.98  auto_link
R2      normalized name (or its alt form) equals exactly one company's alias  0.95  auto_link
R3      ``token_set_ratio >= 92`` and issuer state == ``hq_state`` (city when no state)  0.90  auto_link
R4      ratio ``>= 95`` and exactly one candidate company in the whole table  0.85  auto_link
R5      ratio ``>= 85`` with two or more candidate companies, or one not auto-linked  0.60  review
R6      otherwise                                                    0           unlinked
======  ===========================================================  ==========  =========

Interpretation notes (recorded in ``docs/methods/entity-resolution.md``):

* R2 with an alias shared by two companies is ambiguous and falls through (R3 may
  disambiguate by geography; otherwise R5).
* The spec words R5 as "85 to 91 with one candidate"; a single candidate at 92-94 whose
  geography does not match is also sent to review rather than dropped.

Every decision is written into ``events.payload["match"]`` as
``{rule, ratio, candidates[:5], confidence, resolved_at}`` so a wrong link can be audited.
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
from tell.resolve.normalize import domain_label, normalize_name, normalize_name_alt, slug_words

__all__ = [
    "AUTO_LINK_RULES",
    "CANDIDATE_LIMIT",
    "CONFIDENCE",
    "REVIEW_KIND",
    "RULES",
    "AliasIndex",
    "Candidate",
    "MatchDecision",
    "ResolveStats",
    "backfill_aliases",
    "match_name",
    "resolve_events",
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
    """One company considered for a name: its best-scoring alias and that ratio."""

    company_id: int
    alias: str
    ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {"company_id": self.company_id, "alias": self.alias, "ratio": self.ratio}


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


class AliasIndex:
    """Every alias (plus each company's normalized name) with geography, ready for scoring.

    Built once per :func:`resolve_events` run (or per :func:`match_name` call without
    one) and scanned with ``rapidfuzz.process.extract``, which handles ~10k aliases in
    milliseconds. :meth:`add` keeps the index current as a run links events and writes
    new aliases.
    """

    def __init__(
        self,
        entries: Iterable[tuple[str, int]] = (),
        geography: Mapping[int, tuple[str | None, str | None]] | None = None,
    ) -> None:
        self._companies: dict[str, set[int]] = {}
        self._aliases: list[str] = []
        self._alts: list[str] = []
        self._geo: dict[int, tuple[str | None, str | None]] = {}
        if geography:
            for company_id, (state, city) in geography.items():
                self._geo[company_id] = (_geo_key(state), _city_key(city))
        for alias, company_id in entries:
            self.add(alias, company_id)

    @classmethod
    def from_session(cls, session: Session) -> AliasIndex:
        """Load ``company_aliases`` and ``companies`` (name normalized, ``hq_state``/``hq_city``)."""
        companies = session.execute(
            select(Company.id, Company.name, Company.hq_state, Company.hq_city)
        ).all()
        index = cls(geography={cid: (state, city) for cid, _, state, city in companies})
        for cid, name, _, _ in companies:
            index.add(normalize_name(name), cid)
        for alias, cid in session.execute(select(CompanyAlias.alias, CompanyAlias.company_id)):
            index.add(alias, cid)
        return index

    def __len__(self) -> int:
        return len(self._aliases)

    def add(self, alias: str, company_id: int) -> None:
        """Register ``alias`` (already normalized) for ``company_id``; empty aliases are ignored."""
        if not alias:
            return
        holders = self._companies.get(alias)
        if holders is None:
            self._companies[alias] = {company_id}
            self._aliases.append(alias)
            self._alts.append(normalize_name_alt(alias))
        else:
            holders.add(company_id)

    def set_geography(self, company_id: int, state: str | None, city: str | None) -> None:
        self._geo[company_id] = (_geo_key(state), _city_key(city))

    def exact(self, norm: str, alt: str) -> set[int]:
        """Companies holding an alias equal to ``norm`` or ``alt`` (or whose alias's alt form is)."""
        found: set[int] = set()
        for form in {norm, alt}:
            if form:
                found |= self._companies.get(form, set())
        if alt:
            for alias, alias_alt in zip(self._aliases, self._alts, strict=True):
                if alias_alt == alt:
                    found |= self._companies[alias]
        return found

    def candidates(self, norm: str, alt: str, *, limit: int = CANDIDATE_LIMIT) -> list[Candidate]:
        """Top ``limit`` companies by ``token_set_ratio`` against every alias (plain and alt form)."""
        if not norm or not self._aliases:
            return []
        scored: dict[int, float] = {}
        pool = max(limit * 4, 20)
        for query, choices in ((norm, self._aliases), (alt, self._alts)):
            if not query:
                continue
            for _, score, index in process.extract(
                query, choices, scorer=fuzz.token_set_ratio, limit=pool
            ):
                position = int(index)
                scored[position] = max(scored.get(position, 0.0), float(score))
        best: dict[int, Candidate] = {}
        for position, ratio in scored.items():
            alias = self._aliases[position]
            for company_id in self._companies[alias]:
                current = best.get(company_id)
                if (
                    current is None
                    or ratio > current.ratio
                    or (ratio == current.ratio and alias < current.alias)
                ):
                    best[company_id] = Candidate(company_id, alias, round(ratio, 2))
        ranked = sorted(best.values(), key=lambda c: (-c.ratio, c.company_id))
        return ranked[:limit]

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
    for candidate in strong:
        if candidate.ratio >= GEO_RATIO and idx.geography_matches(
            candidate.company_id, state, city
        ):
            return _decision("R3", candidate.company_id, candidate.ratio, candidates)

    companies = {c.company_id for c in strong}
    if strong and strong[0].ratio >= UNIQUE_RATIO and len(companies) == 1:
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
    payload = event.payload
    name: Any = payload.get("issuer_name") or payload.get("company_name") or payload.get("name")
    if not isinstance(name, str) or not name.strip():
        name = _news_name(payload) if event.source == "news" else None
    if not name:
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
    """Insert normalized aliases for ``company_id`` (``ON CONFLICT DO NOTHING``); return how many were new."""
    if source not in ALIAS_SOURCES:
        msg = f"alias source must be one of {sorted(ALIAS_SOURCES)}, not {source!r}"
        raise ValueError(msg)
    rows = {
        normalized: {"company_id": company_id, "alias": normalized, "source": source}
        for normalized in (normalize_name(n) for n in names)
        if normalized
    }
    if not rows:
        return 0
    return upsert(
        session, CompanyAlias, list(rows.values()), conflict_columns=["company_id", "alias"]
    )


def _cik_taken(session: Session, cik: str, company_id: int) -> bool:
    stmt = select(Company.id).where(Company.cik == cik, Company.id != company_id).limit(1)
    return session.scalar(stmt) is not None


def _apply_link(
    session: Session, event: EventRow, subject: _Subject, company_id: int, index: AliasIndex
) -> int:
    """Set ``company_id``, fill ``cik`` / ``legal_name`` when empty, add aliases; return aliases added."""
    event.company_id = company_id
    company = session.get(Company, company_id)
    if company is not None and event.source == "edgar":
        if company.cik is None and subject.cik and not _cik_taken(session, subject.cik, company_id):
            company.cik = subject.cik
        if company.legal_name is None and subject.legal_name:
            company.legal_name = subject.legal_name
    names = (subject.name, *subject.previous_names)
    added = add_aliases(session, company_id, names, source=_alias_source(event.source))
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
            stats.aliases_added += _apply_link(session, event, subject, decision.company_id, index)
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
    """
    added = 0
    for company in session.scalars(select(Company).order_by(Company.id)):
        names = [company.name, domain_label(company.domain)]
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
