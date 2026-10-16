"""Enrichment gate v0 (ADR 0038): should the person spend an enrichment credit on this crossing?

tell never enriches and never holds an enrichment result (no personal data). The gate only
advises: its verdict rides on the nudge (``Worth enriching: ...``), and the person's own
tool (Clay, Hunter, Apollo, their CRM) spends the credit. The verdict and its reasons are
written to the ``enrichment_verdicts`` ledger, one row per (crossing event, watchlist), so
the advice is auditable, the two history rules below have something to read, and the
acted-on rate has a denominator once feedback arrives.

The five rules of the 2026-10-15 research report, section 5, as implemented in v0:

1. **Two families.** At least two distinct signal families (``signal_type``) with a
   receipt URL contributed. One family is the failure mode the workflow lane found most
   often (a lone Form D, a lone press release): verdict ``skip``. The score being at or
   above the profile's ``alert_threshold`` and the company inside its ``size_band`` are
   already guaranteed by the crossing itself (:func:`tell.signals.scoring.emit_crossings`).
2. **Coverage is not thin.** The company's coverage note names a board tell cannot read
   (``dead:<vendor>:<id>``, ``unsupported:<vendor>``): tell is blind to hiring although the
   company has a board, so the picture is partial: verdict ``hold``. ``no_board`` passes:
   a seed company without a public job board is normal, and rule 1 already demands a
   second family.
3. **Not already active in the CRM.** When the watchlist's ``tell-private`` record holds a
   HubSpot token (ADR 0040), the company is looked up by domain: present with a sales
   activity (``hs_last_sales_activity_timestamp`` or ``notes_last_contacted``) in the last
   90 days is ``in_crm_active`` and verdict ``skip`` (someone is already working it);
   present without one is ``in_crm_quiet`` and absent is ``not_in_crm``, both pass. Without
   a token the reason ``crm_unchecked`` is recorded so the ledger says what was not looked
   at. tell's own note never counts as activity (it sets ``notes_last_updated``, which is
   not read).
4. **No credit on this company in the last 90 days** for this watchlist (an ``enrich``
   verdict in the ledger): verdict ``skip``. Per watchlist, never across customers.
5. **The watchlist's monthly credit budget has headroom.** ``enrich`` verdicts this UTC
   month under ``credit_budget_monthly`` from the watchlist's ``tell-private`` record
   (default :data:`DEFAULT_MONTHLY_CREDITS`, the free allowances the report found): else
   verdict ``hold``.

``skip`` wins over ``hold``; ``enrich`` only when every rule passes. Every rule is
evaluated and recorded even after one fails, so a reader sees the whole picture.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.models import EnrichmentVerdict, Watchlist

__all__ = [
    "CRM_STATUSES",
    "DEFAULT_MONTHLY_CREDITS",
    "LEDGER_WINDOW",
    "MIN_FAMILIES",
    "THIN_COVERAGE_PREFIXES",
    "VERDICTS",
    "Verdict",
    "credits_used_this_month",
    "decide",
    "enriched_recently",
    "families_with_receipts",
    "judge",
    "record_verdict",
    "verdict_row",
]

VERDICTS: tuple[str, ...] = ("enrich", "skip", "hold")

MIN_FAMILIES = 2
"""Distinct ``signal_type`` values a crossing needs before the gate says ``enrich``."""

THIN_COVERAGE_PREFIXES: tuple[str, ...] = ("dead:", "unsupported:")
"""Coverage notes that mean tell knows of a board it cannot read (rule 2)."""

LEDGER_WINDOW = timedelta(days=90)
"""Rule 4: no second credit on the same company for the same watchlist inside this window."""

DEFAULT_MONTHLY_CREDITS = 100
"""Rule 5 default: the free Clay data-credit allowance the report found (unverified); a
watchlist overrides it with ``credit_budget_monthly`` in its ``tell-private`` record."""

CRM_STATUSES: tuple[str, ...] = ("unchecked", "absent", "quiet", "active")
"""Rule 3 inputs: not looked up, not in the CRM, in it without recent sales activity, in it
with sales activity in the last 90 days."""

_CRM_REASON = {
    "unchecked": "crm_unchecked",
    "absent": "not_in_crm",
    "quiet": "in_crm_quiet",
    "active": "in_crm_active",
}
_SKIP_REASONS = frozenset({"one_family", "enriched_recently", "in_crm_active"})
_HOLD_REASONS = frozenset({"budget_spent"})
_THIN_PREFIX = "thin_coverage:"


@dataclass(frozen=True)
class Verdict:
    """What the gate decided and why."""

    verdict: str
    """``enrich``, ``skip`` or ``hold``."""
    reasons: list[str] = field(default_factory=list)
    """One token per rule, in rule order: ``two_families`` / ``one_family``,
    ``coverage_ok`` / ``thin_coverage:<note>``, ``crm_unchecked`` / ``not_in_crm`` /
    ``in_crm_quiet`` / ``in_crm_active``, ``no_recent_credit`` / ``enriched_recently``,
    ``budget_ok`` / ``budget_spent``."""
    families: list[str] = field(default_factory=list)
    """Distinct signal families with a receipt, largest contribution first."""

    @property
    def line(self) -> str:
        """The nudge's ``Worth enriching:`` line."""
        if self.verdict == "enrich":
            return "Worth enriching: yes · " + " + ".join(self.families[:MIN_FAMILIES])
        return f"Worth enriching: {self.verdict} · {_explain(self.reasons)}"


def _explain(reasons: Sequence[str]) -> str:
    for reason in reasons:
        if reason == "one_family":
            return "one signal family"
        if reason == "in_crm_active":
            return "already in HubSpot with sales activity in the last 90 days"
        if reason == "enriched_recently":
            return "a credit went to this company in the last 90 days"
    for reason in reasons:
        if reason.startswith(_THIN_PREFIX):
            return f"board unreadable ({reason[len(_THIN_PREFIX) :]})"
        if reason == "budget_spent":
            return "this month's credit budget is spent"
    return "no reason recorded"


def families_with_receipts(contributions: Sequence[Mapping[str, Any]]) -> list[str]:
    """Distinct ``signal_type`` values among contributions that carry a ``url``, in order."""
    families: list[str] = []
    for item in contributions:
        family = str(item.get("signal_type") or "").strip()
        if family and item.get("url") and family not in families:
            families.append(family)
    return families


def decide(
    contributions: Sequence[Mapping[str, Any]],
    *,
    coverage_note: str | None = None,
    crm_status: str = "unchecked",
    enriched_recently: bool = False,
    credits_used: int = 0,
    credit_budget: int = DEFAULT_MONTHLY_CREDITS,
) -> Verdict:
    """The pure gate: the five rules over already-gathered facts (see the module docstring)."""
    if crm_status not in CRM_STATUSES:
        msg = f"crm_status must be one of {CRM_STATUSES}, not {crm_status!r}"
        raise ValueError(msg)
    families = families_with_receipts(contributions)
    reasons: list[str] = []
    reasons.append("two_families" if len(families) >= MIN_FAMILIES else "one_family")
    note = (coverage_note or "").strip()
    thin = any(note.startswith(prefix) for prefix in THIN_COVERAGE_PREFIXES)
    reasons.append(f"{_THIN_PREFIX}{note}" if thin else "coverage_ok")
    reasons.append(_CRM_REASON[crm_status])
    reasons.append("enriched_recently" if enriched_recently else "no_recent_credit")
    reasons.append("budget_spent" if credits_used >= max(credit_budget, 0) else "budget_ok")
    if any(r in _SKIP_REASONS for r in reasons):
        verdict = "skip"
    elif thin or any(r in _HOLD_REASONS for r in reasons):
        verdict = "hold"
    else:
        verdict = "enrich"
    return Verdict(verdict=verdict, reasons=reasons, families=families)


# --- the ledger ----------------------------------------------------------------------------------


def enriched_recently(
    session: Session,
    *,
    watchlist_id: int,
    company_id: int,
    now: datetime,
    exclude_event_id: str | None = None,
) -> bool:
    """Rule 4: an ``enrich`` verdict for this company and watchlist inside the window."""
    stmt = (
        select(func.count())
        .select_from(EnrichmentVerdict)
        .where(
            EnrichmentVerdict.watchlist_id == watchlist_id,
            EnrichmentVerdict.company_id == company_id,
            EnrichmentVerdict.verdict == "enrich",
            EnrichmentVerdict.decided_at > now - LEDGER_WINDOW,
            EnrichmentVerdict.decided_at <= now,
        )
    )
    if exclude_event_id is not None:
        stmt = stmt.where(EnrichmentVerdict.event_id != exclude_event_id)
    return int(session.scalar(stmt) or 0) > 0


def credits_used_this_month(session: Session, *, watchlist_id: int, now: datetime) -> int:
    """Rule 5: ``enrich`` verdicts of this watchlist since the first of the UTC month."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(EnrichmentVerdict)
        .where(
            EnrichmentVerdict.watchlist_id == watchlist_id,
            EnrichmentVerdict.verdict == "enrich",
            EnrichmentVerdict.decided_at >= start,
            EnrichmentVerdict.decided_at <= now,
        )
    )
    return int(session.scalar(stmt) or 0)


def verdict_row(session: Session, *, event_id: str, watchlist_id: int) -> EnrichmentVerdict | None:
    """The ledger row of (event, watchlist), if decided."""
    return session.scalars(
        select(EnrichmentVerdict).where(
            EnrichmentVerdict.event_id == event_id,
            EnrichmentVerdict.watchlist_id == watchlist_id,
        )
    ).one_or_none()


def record_verdict(
    session: Session,
    *,
    event_id: str,
    watchlist_id: int,
    company_id: int,
    verdict: Verdict,
    now: datetime,
) -> EnrichmentVerdict:
    """Write the ledger row for (event, watchlist); an existing row is returned unchanged."""
    row = session.scalars(
        select(EnrichmentVerdict).where(
            EnrichmentVerdict.event_id == event_id,
            EnrichmentVerdict.watchlist_id == watchlist_id,
        )
    ).one_or_none()
    if row is not None:
        return row
    row = EnrichmentVerdict(
        event_id=event_id,
        watchlist_id=watchlist_id,
        company_id=company_id,
        verdict=verdict.verdict,
        reasons=list(verdict.reasons),
        families=list(verdict.families),
        decided_at=now,
    )
    session.add(row)
    session.flush()
    return row


def judge(
    session: Session,
    watchlist: Watchlist,
    *,
    event_id: str,
    company_id: int,
    contributions: Sequence[Mapping[str, Any]],
    coverage_note: str | None,
    now: datetime,
    credit_budget: int | None = None,
    crm_status: str = "unchecked",
) -> Verdict:
    """Decide once per (event, watchlist) and remember it.

    A crossing whose verdict is already in the ledger (a nudge being resent after a failed
    POST) gets the stored verdict back, so a retry never charges the budget twice and the
    Slack line never changes between attempts.
    """
    stored = session.scalars(
        select(EnrichmentVerdict).where(
            EnrichmentVerdict.event_id == event_id,
            EnrichmentVerdict.watchlist_id == watchlist.id,
        )
    ).one_or_none()
    if stored is not None:
        return Verdict(
            verdict=stored.verdict, reasons=list(stored.reasons), families=list(stored.families)
        )
    budget = DEFAULT_MONTHLY_CREDITS if credit_budget is None else credit_budget
    verdict = decide(
        contributions,
        coverage_note=coverage_note,
        crm_status=crm_status,
        enriched_recently=enriched_recently(
            session,
            watchlist_id=watchlist.id,
            company_id=company_id,
            now=now,
            exclude_event_id=event_id,
        ),
        credits_used=credits_used_this_month(session, watchlist_id=watchlist.id, now=now),
        credit_budget=budget,
    )
    record_verdict(
        session,
        event_id=event_id,
        watchlist_id=watchlist.id,
        company_id=company_id,
        verdict=verdict,
        now=now,
    )
    return verdict
