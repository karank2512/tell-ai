"""Alias hygiene maintenance (ADR 0033): prune bad aliases, close junk review items.

Both operations are dry runs unless ``apply`` is set, and both are idempotent.

* :func:`prune_aliases` (``tell resolve prune-aliases``) deletes ``company_aliases`` rows
  of source ``manual`` that fail the alias rule (:mod:`tell.resolve.aliases`): written by
  ``backfill-aliases`` before the rule existed (``of`` for ``of.one``, ``apple`` for a
  company keyed by ``apple.com``). A ``manual`` alias a human wrote through a review
  verdict is kept, and so is every ``edgar`` / ``news`` / ``ats_board`` row (counted, not
  touched). The index ignores failing aliases anyway; pruning keeps the table honest.
* :func:`close_junk_reviews` (``tell resolve close-junk-reviews``) re-runs the matcher on
  every open ``entity_match`` item. An item that no longer has any candidate at or above
  ``REVIEW_RATIO`` (its candidates were all failing or single-token-superset aliases) is
  closed as ``rejected`` with ``payload.resolved_by = "alias_hygiene"``; nothing is
  deleted and no event is linked. Items that still have a real candidate stay open for a
  human, even when the new rules would now auto-link them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from tell.models import Company, CompanyAlias, ReviewQueueItem
from tell.resolve.aliases import alias_problem, is_single_token
from tell.resolve.match import (
    REVIEW_KIND,
    REVIEW_RATIO,
    AliasIndex,
    match_name,
    review_alias_pairs,
    stored_alias_problem,
)
from tell.resolve.normalize import normalize_name

__all__ = [
    "EXAMPLES",
    "RESOLVED_BY",
    "AliasVerdict",
    "close_junk_reviews",
    "failing_aliases",
    "prune_aliases",
]

RESOLVED_BY = "alias_hygiene"
"""``payload.resolved_by`` on review items closed by :func:`close_junk_reviews`."""

EXAMPLES = 25
"""Examples listed in a report (the counts cover everything)."""

_DELETE_BATCH = 500


@dataclass(frozen=True)
class AliasVerdict:
    """One ``company_aliases`` row that fails the alias rule, and what pruning does with it."""

    company_id: int
    alias: str
    source: str
    reason: str
    action: str
    """``delete`` (manual), ``keep_human_review`` (manual, written by a review verdict) or
    ``keep_source`` (any other source)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "alias": self.alias,
            "source": self.source,
            "reason": self.reason,
            "action": self.action,
        }


def failing_aliases(session: Session) -> list[AliasVerdict]:
    """Every ``company_aliases`` row failing the alias rule, with the action pruning takes."""
    companies = {
        cid: (name, domain)
        for cid, name, domain in session.execute(select(Company.id, Company.name, Company.domain))
    }
    human = review_alias_pairs(session)
    found: list[AliasVerdict] = []
    rows = session.execute(
        select(CompanyAlias.company_id, CompanyAlias.alias, CompanyAlias.source).order_by(
            CompanyAlias.company_id, CompanyAlias.alias
        )
    )
    for company_id, alias, source in rows:
        name, domain = companies[company_id]  # company_aliases.company_id is a foreign key
        reason = stored_alias_problem(alias, name=name, domain=domain)
        if reason is None:
            continue
        if source != "manual":
            action = "keep_source"
        elif (company_id, alias) in human:
            action = "keep_human_review"
        else:
            action = "delete"
        found.append(AliasVerdict(company_id, alias, source, reason, action))
    return found


def prune_aliases(session: Session, *, apply: bool = False) -> dict[str, Any]:
    """Report (and with ``apply`` delete) ``manual`` aliases failing the alias rule.

    Flushes; the caller owns the transaction.
    """
    found = failing_aliases(session)
    doomed = [v for v in found if v.action == "delete"]
    if apply:
        for start in range(0, len(doomed), _DELETE_BATCH):
            chunk = doomed[start : start + _DELETE_BATCH]
            session.execute(
                delete(CompanyAlias).where(
                    CompanyAlias.source == "manual",
                    or_(
                        *(
                            and_(
                                CompanyAlias.company_id == v.company_id,
                                CompanyAlias.alias == v.alias,
                            )
                            for v in chunk
                        )
                    ),
                )
            )
        session.flush()
    actions = Counter(v.action for v in found)
    return {
        "dry_run": not apply,
        "failing": len(found),
        "to_delete": len(doomed),
        "deleted": len(doomed) if apply else 0,
        "kept_human_review": actions.get("keep_human_review", 0),
        "kept_other_sources": dict(
            sorted(Counter(v.source for v in found if v.action == "keep_source").items())
        ),
        "by_reason": dict(sorted(Counter(v.reason for v in doomed).items())),
        "examples": [v.to_dict() for v in doomed[:EXAMPLES]],
    }


def _old_aliases(payload: dict[str, Any]) -> list[str]:
    candidates = payload.get("candidates") or []
    return [
        str(c["alias"])
        for c in candidates
        if isinstance(c, dict)
        and isinstance(c.get("alias"), str)
        and float(c.get("ratio") or 0) >= REVIEW_RATIO
    ]


def _junk_reason(alias: str, name: str) -> str:
    """Why an old candidate alias no longer counts: the rule's reason, or a one-word superset."""
    problem = alias_problem(alias)
    if problem is not None:
        return problem
    if is_single_token(alias) and normalize_name(name) != alias:
        return "single_token_superset"
    return "below_review_ratio"


def close_junk_reviews(session: Session, *, now: datetime, apply: bool = False) -> dict[str, Any]:
    """Re-evaluate open ``entity_match`` items; close those with no real candidate left.

    An item is junk when :func:`tell.resolve.match.match_name`, run on its name and
    geography with the current rules, finds no candidate at or above ``REVIEW_RATIO``
    (rule R6). With ``apply`` the item becomes ``rejected`` with ``verdict = "reject"``
    and ``resolved_by = "alias_hygiene"``, so the resolver never reopens the pair. Flushes;
    the caller owns the transaction.
    """
    index = AliasIndex.from_session(session)
    stmt = (
        select(ReviewQueueItem)
        .where(ReviewQueueItem.kind == REVIEW_KIND, ReviewQueueItem.status == "open")
        .order_by(ReviewQueueItem.id)
    )
    scanned = 0
    junk: list[ReviewQueueItem] = []
    kept: Counter[str] = Counter()
    aliases: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for item in session.scalars(stmt):
        scanned += 1
        payload = item.payload
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            kept["no_name"] += 1
            continue
        state = payload.get("state")
        city = payload.get("city")
        decision = match_name(
            session,
            name,
            state=state if isinstance(state, str) else None,
            city=city if isinstance(city, str) else None,
            index=index,
        )
        if decision.rule != "R6":
            kept[decision.rule] += 1
            continue
        junk.append(item)
        for alias in _old_aliases(payload):
            aliases[alias] += 1
            reasons[_junk_reason(alias, name)] += 1
    if apply:
        for item in junk:
            item.status = "rejected"
            item.resolved_at = now
            item.payload = {**item.payload, "verdict": "reject", "resolved_by": RESOLVED_BY}
        session.flush()
    return {
        "dry_run": not apply,
        "open_scanned": scanned,
        "junk": len(junk),
        "closed": len(junk) if apply else 0,
        "kept_open_by_rule": dict(sorted(kept.items())),
        "junk_aliases": dict(aliases.most_common(EXAMPLES)),
        "junk_alias_reasons": dict(sorted(reasons.items())),
        "examples": [{"id": item.id, "name": item.payload.get("name")} for item in junk[:EXAMPLES]],
    }
