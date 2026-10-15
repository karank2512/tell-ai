"""``tell resolve relink-formd``: re-earn every Form D link under the ADR 0034 rules.

Production's Form D links were made before the guards existed, and every one of them
wrote enrichment: the issuer name and previous names as ``edgar`` aliases,
``companies.legal_name`` and ``companies.cik``. A wrong link therefore also minted the
exact alias that linked the issuer's next filing through R2 (4 of the 15 wrong R2 links on
2026-10-14 were such knock-ons) and handed the backtest cohort a CIK it trusts.

:func:`relink_formd` re-evaluates every linked ``form_d_filed`` / ``form_d_amendment``
event (dry run unless ``apply``):

1. **Re-earn.** The alias index is built without the ``edgar`` aliases those events could
   have written (an alias of the linked company equal to an event's normalized issuer name
   or previous name), unless a review verdict (``rule = manual``) or a linked non-Form D
   EDGAR event of the same company also carries the name. Each event is then matched with
   :func:`tell.resolve.match.match_name` and the Form D guards. A link stays when the
   decision is an auto-link to the same company, or when the event's CIK is the company's
   CIK and that CIK was not written only by links that fail now. A kept trusted link
   (R2, or kept by CIK) puts its names back into the index, and the pass repeats until
   nothing changes, so a renamed company's filings still find each other through their
   previous names but a wrong link can never support itself.
2. **Unlink.** For a link that fails: ``company_id`` becomes ``NULL`` (or the other company,
   when the new decision is an auto-link to one), ``payload.match`` becomes the new
   decision with the old one under ``payload.match.previous`` (plus the old
   ``company_id``), and an R5 decision opens one review item.
3. **Clean up what it wrote.** An ``edgar`` alias of the old company is deleted when its
   text is the normalized issuer name or a previous name of an unlinked event and no event
   still linked to that company carries that name (the company's own normalized name and
   domain label are never touched). ``companies.legal_name`` is reset to ``NULL`` when it is
   an unlinked event's issuer name and no still-linked event of the company has that name;
   ``companies.cik`` (with its ``cik_source``) when it is an unlinked event's CIK and no
   still-linked event has it.

Running it twice changes nothing the second time: unlinked events are no longer scanned,
and every kept event is decided the same way.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from tell.models import Company, CompanyAlias, EventRow, ReviewQueueItem
from tell.resolve.guards import FORM_D_TYPES
from tell.resolve.match import (
    REVIEW_KIND,
    RULES,
    TRUSTED_RULES,
    AliasIndex,
    MatchDecision,
    _queue_review,
    _rejected_pairs,
    _review_key,
    _Subject,
    _subject,
    _was_rejected,
    link_event,
    match_name,
)
from tell.resolve.normalize import domain_label, normalize_name

__all__ = ["RELINKED_BY", "relink_formd"]

RELINKED_BY = "relink_formd"
"""``payload.match.relinked_by`` on every event this command changed."""

_DELETE_BATCH = 500


def _cik(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return f"{int(text):010d}" if text.isdigit() else None


@dataclass
class _Linked:
    """One linked EDGAR event as the repair sees it."""

    event: EventRow
    company_id: int
    subject: _Subject
    rule: str | None
    """The stored ``payload.match.rule`` (``None`` for a collector CIK link)."""

    @property
    def names(self) -> set[str]:
        return {n for n in (normalize_name(name) for name in self.subject.names) if n}

    @property
    def issuer(self) -> str:
        return normalize_name(self.subject.name)

    @property
    def cik(self) -> str | None:
        return _cik(self.subject.cik)


@dataclass
class _Outcome:
    linked: _Linked
    decision: MatchDecision
    reasons: list[str]

    @property
    def moved_to(self) -> int | None:
        d = self.decision
        if d.action == "auto_link" and d.company_id not in (None, self.linked.company_id):
            return d.company_id
        return None


def _rule(event: EventRow) -> str | None:
    match = event.payload.get("match")
    rule = match.get("rule") if isinstance(match, dict) else None
    return rule if isinstance(rule, str) else None


def _linked(session: Session, *, form_d: bool) -> list[_Linked]:
    kind = EventRow.type.in_(sorted(FORM_D_TYPES))
    stmt = (
        select(EventRow)
        .where(
            EventRow.source == "edgar",
            EventRow.company_id.is_not(None),
            kind if form_d else ~kind,
        )
        .order_by(EventRow.occurred_at, EventRow.event_id)
    )
    found: list[_Linked] = []
    for event in session.scalars(stmt):
        subject = _subject(event)
        if subject is None or event.company_id is None:
            continue
        found.append(_Linked(event, event.company_id, subject, _rule(event)))
    return found


class _Relinker:
    """The fixed-point re-evaluation of step 1."""

    def __init__(self, session: Session, linked: list[_Linked], anchors: list[_Linked]) -> None:
        self.session = session
        self.pending = [e for e in linked if e.rule != "manual"]
        manual = [e for e in linked if e.rule == "manual"]
        supported = {(e.company_id, n) for e in (*manual, *anchors) for n in e.names}
        skip = {(e.company_id, n) for e in self.pending for n in e.names} - supported
        self.index = AliasIndex.from_session(session, skip_edgar=skip)
        self.company_cik = {
            cid: _cik(cik) for cid, cik in session.execute(select(Company.id, Company.cik))
        }
        self.trusted_ciks: dict[int, set[str]] = defaultdict(set)
        for e in (*manual, *anchors):
            if e.cik:
                self.trusted_ciks[e.company_id].add(e.cik)
        self.establishing: dict[tuple[int, str], set[str]] = defaultdict(set)
        """(company, cik) -> events whose own match wrote that CIK (not collector CIK links,
        and not R0 links: R0 fires only on a CIK some trusted path already wrote)."""
        for e in linked:
            if e.cik and e.rule != "R0" and (e.rule in RULES or e.rule == "manual"):
                self.establishing[(e.company_id, e.cik)].add(e.event.event_id)
        self.kept: dict[str, _Linked] = {e.event.event_id: e for e in manual}
        self.decisions: dict[str, MatchDecision] = {}

    def _cik_trusted(self, e: _Linked) -> bool:
        """The event's CIK is the company's, and no failing link is all that wrote it."""
        if e.cik is None or self.company_cik.get(e.company_id) != e.cik:
            return False
        if not self.establishing.get((e.company_id, e.cik)):
            return True  # no linked filing wrote it: it came from a seed or by hand
        return e.cik in self.trusted_ciks[e.company_id]

    def _decide(self, e: _Linked) -> MatchDecision:
        s = e.subject
        return match_name(
            self.session,
            s.name,
            state=s.state,
            city=s.city,
            index=self.index,
            issuer=s.facts,
            edgar=True,
        )

    def _keep(self, e: _Linked, *, trusted: bool) -> None:
        self.kept[e.event.event_id] = e
        if trusted:
            for name in e.names:
                self.index.add(name, e.company_id)
            if e.cik:
                self.trusted_ciks[e.company_id].add(e.cik)

    def run(self) -> list[_Outcome]:
        remaining = list(self.pending)
        progress = True
        while progress and remaining:
            progress = False
            still: list[_Linked] = []
            for e in remaining:
                decision = self._decide(e)
                self.decisions[e.event.event_id] = decision
                if decision.action == "auto_link" and decision.company_id == e.company_id:
                    self._keep(e, trusted=decision.rule in TRUSTED_RULES)
                    progress = True
                elif self._cik_trusted(e):
                    self._keep(e, trusted=True)
                    progress = True
                else:
                    still.append(e)
            remaining = still
        return [
            _Outcome(
                e, self.decisions[e.event.event_id], _reasons(e, self.decisions[e.event.event_id])
            )
            for e in remaining
        ]


def _reasons(e: _Linked, decision: MatchDecision) -> list[str]:
    """Why a link fails now, most specific first."""
    if decision.fund_signals:
        return ["pooled_fund"]
    reasons: list[str] = []
    if e.rule == "R2" and (decision.demoted_from or decision.rule) != "R2":
        reasons.append("exact_alias_untrusted")
    reasons.extend(decision.guard_failed)
    if decision.action == "auto_link":
        reasons.append("matches_other_company")
    if e.rule not in RULES and e.rule != "manual":
        reasons.append("cik_untrusted")
    return reasons or [f"now_{decision.rule}"]


@dataclass
class _Cleanup:
    aliases: list[tuple[int, str]]
    legal_names: list[tuple[int, str]]
    ciks: list[tuple[int, str]]


def _cleanup(session: Session, outcomes: list[_Outcome], remaining: list[_Linked]) -> _Cleanup:
    """Step 3: the enrichment the failing links wrote that nothing still linked supports."""
    support_names: dict[int, set[str]] = defaultdict(set)
    support_issuers: dict[int, set[str]] = defaultdict(set)
    support_ciks: dict[int, set[str]] = defaultdict(set)
    for e in remaining:
        support_names[e.company_id] |= e.names
        support_issuers[e.company_id].add(e.issuer)
        if e.cik:
            support_ciks[e.company_id].add(e.cik)
    for o in outcomes:
        if o.moved_to is not None:
            support_names[o.moved_to] |= o.linked.names
            support_issuers[o.moved_to].add(o.linked.issuer)
            if o.linked.cik:
                support_ciks[o.moved_to].add(o.linked.cik)
    affected = sorted({o.linked.company_id for o in outcomes})
    companies = {c.id: c for c in session.scalars(select(Company).where(Company.id.in_(affected)))}
    wanted: set[tuple[int, str]] = set()
    legal: dict[int, str] = {}
    ciks: dict[int, str] = {}
    for o in outcomes:
        cid = o.linked.company_id
        company = companies.get(cid)
        if company is None:
            continue
        own = {normalize_name(company.name), domain_label(company.domain)}
        wanted |= {(cid, n) for n in o.linked.names - support_names[cid] - own}
        if company.legal_name is not None:
            legal_norm = normalize_name(company.legal_name)
            if legal_norm == o.linked.issuer and legal_norm not in support_issuers[cid]:
                legal[cid] = company.legal_name
        company_cik = _cik(company.cik)
        if company_cik and company_cik == o.linked.cik and company_cik not in support_ciks[cid]:
            ciks[cid] = company.cik or company_cik
    existing: set[tuple[int, str]] = set()
    if wanted:
        rows = session.execute(
            select(CompanyAlias.company_id, CompanyAlias.alias).where(
                CompanyAlias.source == "edgar",
                CompanyAlias.company_id.in_(sorted({cid for cid, _ in wanted})),
            )
        )
        existing = {(cid, alias) for cid, alias in rows} & wanted
    return _Cleanup(
        aliases=sorted(existing),
        legal_names=sorted(legal.items()),
        ciks=sorted(ciks.items()),
    )


def _apply(session: Session, outcomes: list[_Outcome], cleanup: _Cleanup, *, now: datetime) -> int:
    """Write steps 2 and 3; return the number of review items opened."""
    rejected = _rejected_pairs(session)
    opened = 0
    for o in outcomes:
        event, decision = o.linked.event, o.decision
        previous = event.payload.get("match")
        audit = decision.audit(now=now)
        audit["previous"] = {
            **(previous if isinstance(previous, dict) else {}),
            "company_id": o.linked.company_id,
        }
        audit["relinked_by"] = RELINKED_BY
        audit["relink_reasons"] = list(o.reasons)
        event.company_id = None  # a moved link is re-made after the clean-up below
        if decision.action == "review":
            norm = normalize_name(o.linked.subject.name)
            if _was_rejected(rejected, norm, _review_key(decision)):
                audit["rejected"] = True
            elif _queue_review(session, event, o.linked.subject, decision, now):
                opened += 1
        event.payload = {**event.payload, "match": audit}
    session.flush()
    for start in range(0, len(cleanup.aliases), _DELETE_BATCH):
        chunk = cleanup.aliases[start : start + _DELETE_BATCH]
        session.execute(
            delete(CompanyAlias)
            .where(
                CompanyAlias.source == "edgar",
                or_(
                    *(
                        and_(CompanyAlias.company_id == cid, CompanyAlias.alias == alias)
                        for cid, alias in chunk
                    )
                ),
            )
            # The relink session holds every Form D event it re-checked; "fetch" syncs only
            # the matched aliases instead of walking that identity map per chunk.
            .execution_options(synchronize_session="fetch")
        )
    for cid, _ in cleanup.legal_names:
        company = session.get(Company, cid)
        if company is not None:
            company.legal_name = None
    for cid, _ in cleanup.ciks:
        company = session.get(Company, cid)
        if company is not None:
            company.cik = None
            company.cik_source = None  # the source described the CIK just removed
    session.flush()
    for o in outcomes:
        if o.moved_to is not None:
            trusted = o.decision.rule in TRUSTED_RULES
            link_event(
                session,
                o.linked.event,
                o.moved_to,
                trusted=trusted,
                cik_source=o.decision.cik_source if trusted else None,
            )
    session.flush()
    return opened


def _would_open(session: Session, outcomes: list[_Outcome]) -> int:
    rejected = _rejected_pairs(session)
    open_events = {
        str(payload.get("event_id"))
        for payload in session.scalars(
            select(ReviewQueueItem.payload).where(
                ReviewQueueItem.kind == REVIEW_KIND, ReviewQueueItem.status == "open"
            )
        )
    }
    return sum(
        1
        for o in outcomes
        if o.decision.action == "review"
        and o.linked.event.event_id not in open_events
        and not _was_rejected(
            rejected, normalize_name(o.linked.subject.name), _review_key(o.decision)
        )
    )


def relink_formd(session: Session, *, now: datetime, apply: bool = False) -> dict[str, Any]:
    """Re-evaluate every linked Form D event under the ADR 0034 rules; report, and with
    ``apply`` repair. Flushes; the caller owns the transaction."""
    linked = _linked(session, form_d=True)
    anchors = _linked(session, form_d=False)
    relinker = _Relinker(session, linked, anchors)
    outcomes = relinker.run()
    failed = {o.linked.event.event_id for o in outcomes}
    remaining = [e for e in linked if e.event.event_id not in failed] + anchors
    cleanup = _cleanup(session, outcomes, remaining)
    ids = sorted({e.company_id for e in linked})
    rows = session.execute(select(Company.id, Company.name).where(Company.id.in_(ids))).all()
    names: dict[int, str] = {row[0]: row[1] for row in rows}
    kept_by_rule = Counter(e.rule or "cik" for e in linked if e.event.event_id not in failed)
    report_changes = [
        {
            "event_id": o.linked.event.event_id,
            "company_id": o.linked.company_id,
            "company": names.get(o.linked.company_id),
            "old_rule": o.linked.rule,
            "new_rule": o.decision.rule,
            "new_company_id": o.moved_to,
            "reason": o.reasons,
        }
        for o in outcomes
    ]
    opened = (
        _apply(session, outcomes, cleanup, now=now) if apply else _would_open(session, outcomes)
    )
    return {
        "dry_run": not apply,
        "scanned": len(linked),
        "kept": len(linked) - len(outcomes),
        "kept_by_rule": dict(sorted(kept_by_rule.items())),
        "unlinked": sum(1 for o in outcomes if o.moved_to is None),
        "moved": sum(1 for o in outcomes if o.moved_to is not None),
        "review_items_opened": opened,
        "by_new_rule": dict(sorted(Counter(o.decision.rule for o in outcomes).items())),
        "by_reason": dict(sorted(Counter(r for o in outcomes for r in o.reasons).items())),
        "aliases_deleted": len(cleanup.aliases),
        "legal_names_reset": len(cleanup.legal_names),
        "ciks_reset": len(cleanup.ciks),
        "changes": report_changes,
        "aliases": [{"company_id": cid, "alias": alias} for cid, alias in cleanup.aliases],
        "legal_names": [
            {"company_id": cid, "company": names.get(cid), "legal_name": legal}
            for cid, legal in cleanup.legal_names
        ],
        "ciks": [
            {"company_id": cid, "company": names.get(cid), "cik": cik} for cid, cik in cleanup.ciks
        ],
    }
