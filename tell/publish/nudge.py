"""Per-event Slack nudges (ADR 0036): one message per crossing, to the watchlist's own webhook.

The digest (:mod:`tell.publish.slack`) is a once-a-day summary. A nudge goes out on the
ingest run that scored the crossing, so a design partner hears about an account within
hours instead of the next morning. It is sent only to a watchlist that has its own
``slack_webhook_url`` in ``tell-private/watchlists/<slug>.yaml`` (ADR 0027, read back by
:func:`load_private_webhooks`); the global ``SLACK_WEBHOOK_URL`` never receives nudges.

Selection (:func:`pending_nudges`): the watchlist's ``score_crossed_threshold`` events of
the last 24 hours (the same accounts the digest lists, :func:`crossed_accounts`) minus
those with a ``nudges`` row of status ``ok`` for this watchlist. A ``failed`` row (non-2xx
or a transport error) is updated and retried on the next run while the event is still in
the window; after 24 hours a crossing is never nudged, so a backfill or a long outage
cannot flood a channel.

Message shape (:func:`build_nudge_blocks`): one section with
``*{name}* ({domain}) · score {score} · crossed {threshold}``, one receipt bullet per
contribution, the ``Why now:`` sentence and a ``Worth enriching:`` line, then a context
line with the account page and, when ``TELL_FEEDBACK_FORM_URL`` is set, prefilled
``useful`` / ``not useful`` links for the top contribution's signal.

``Worth enriching`` is the enrichment gate's verdict (:mod:`tell.signals.gate`, ADR
0038): ``yes · {family a} + {family b}``, ``skip · {reason}`` or ``hold · {reason}``,
decided once per (crossing, watchlist) and written to the ``enrichment_verdicts`` ledger
before the message is built, so a resend after a failed POST shows the same verdict and
never charges the watchlist's monthly credit budget twice. The watchlist's budget comes
from ``credit_budget_monthly`` in its ``tell-private`` record (:func:`load_private_budgets`).

Each run with at least one candidate is recorded as a ``runs`` row with source
``slack_nudge`` (``fetched`` = candidates, ``inserted`` = sent, ``errors``); a run whose
every send failed is ``failed``. Sends are paced 1.1 s apart like the digest.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, start_run
from tell.config import get_settings
from tell.intake.reader import DEFAULT_PRIVATE_DIR, PrivateRecordError, load_private_watchlists
from tell.models import Company, Nudge, Watchlist
from tell.publish.json_api import company_facts_many, feedback_link
from tell.publish.slack import (
    SEND_INTERVAL_S,
    CrossedAccount,
    crossed_accounts,
    escape,
    link,
    post_blocks,
    receipt_section,
    why_now,
)
from tell.signals.gate import Verdict, decide, families_with_receipts, judge

__all__ = [
    "NUDGE_SOURCE",
    "NudgeOutcome",
    "build_nudge_blocks",
    "load_private_budgets",
    "load_private_webhooks",
    "pending_nudges",
    "send_nudges",
    "worth_enriching",
]

log = logging.getLogger(__name__)

NUDGE_SOURCE = "slack_nudge"
"""``runs.source`` of a nudge run."""


@dataclass(frozen=True)
class NudgeOutcome:
    """What :func:`send_nudges` did."""

    candidates: int
    sent: int
    errors: int
    skipped: str | None = None
    """Why nothing was attempted (``no private webhooks``, ``nothing to nudge``)."""
    run_id: str | None = None
    verdicts: dict[str, int] | None = None
    """How many candidates the gate judged ``enrich`` / ``skip`` / ``hold``."""


# --- private webhooks ----------------------------------------------------------------------------


def load_private_webhooks(private_dir: Path = DEFAULT_PRIVATE_DIR) -> dict[str, str]:
    """``{watchlist slug: webhook}`` from ``tell-private``; ``{}`` when absent or unreadable.

    A record that does not parse or validate is logged by error type only (its message
    could quote the file and its name is a customer's slug, ADR 0027) and yields no
    webhooks at all, so a broken file never silently drops one list's nudges while
    sending another's.
    """
    try:
        records = load_private_watchlists(private_dir)
    except (PrivateRecordError, yaml.YAMLError) as exc:
        log.warning(
            "private watchlist records unreadable (%s); no private webhooks", type(exc).__name__
        )
        return {}
    return {slug: r.slack_webhook_url for slug, r in records.items() if r.slack_webhook_url}


def load_private_budgets(private_dir: Path = DEFAULT_PRIVATE_DIR) -> dict[str, int]:
    """``{watchlist slug: credit_budget_monthly}`` for the records that set one (ADR 0038).

    Same reading and the same silence on a broken file as :func:`load_private_webhooks`;
    a watchlist without a budget takes the gate's default.
    """
    try:
        records = load_private_watchlists(private_dir)
    except (PrivateRecordError, yaml.YAMLError):
        return {}
    return {
        slug: r.credit_budget_monthly
        for slug, r in records.items()
        if r.credit_budget_monthly is not None
    }


# --- Block Kit -----------------------------------------------------------------------------------


def worth_enriching(contributions: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    """Rule 1 alone, as the first gate shipped it: ``(two families?, families)``.

    Kept for callers that have no session (the full verdict is :func:`tell.signals.gate.judge`).
    """
    families = families_with_receipts(contributions)
    return decide(contributions).verdict == "enrich", families


def build_nudge_blocks(
    account: CrossedAccount,
    *,
    pages_base_url: str,
    feedback_form_url: str | None = None,
    verdict: Verdict | None = None,
    watchlist_slug: str | None = None,
) -> list[dict[str, Any]]:
    """The Block Kit ``blocks`` of one nudge (see the module docstring).

    ``verdict`` is the ledger's; without one (no session) the pure gate runs over the
    contributions alone, as if coverage, history and budget were all fine.
    """
    base = pages_base_url.rstrip("/")
    crossed = "" if account.threshold is None else f" · crossed {account.threshold:g}"
    head = (
        f"*{escape(account.name)}* ({escape(account.domain)}) · score {account.score:.1f}{crossed}"
    )
    gate = verdict if verdict is not None else decide(account.contributions)
    tail = [escape(why_now(account.contributions)), escape(gate.line)]
    section = receipt_section(head, account.contributions, tail)
    elements = [
        {"type": "mrkdwn", "text": link(f"{base}/companies/{account.domain}.html", "Account page")}
    ]
    if feedback_form_url:
        top = account.contributions[0] if account.contributions else None
        signal_id = int(top["signal_id"]) if top and top.get("signal_id") is not None else None
        votes = " · ".join(
            link(
                feedback_link(
                    signal_id=signal_id,
                    domain=account.domain,
                    verdict=verdict,
                    form_url=feedback_form_url,
                    watchlist=watchlist_slug,
                ),
                label,
            )
            for verdict, label in (("useful", "useful"), ("not_useful", "not useful"))
        )
        elements.append({"type": "mrkdwn", "text": f"Rate it: {votes}"})
    return [section, {"type": "context", "elements": elements}]


def _text(account: CrossedAccount) -> str:
    crossed = "" if account.threshold is None else f" crossed {account.threshold:g}:"
    return f"tell · {account.name} ({account.domain}){crossed} score {account.score:.1f}"


# --- data ----------------------------------------------------------------------------------------


def pending_nudges(session: Session, watchlist: Watchlist, now: datetime) -> list[CrossedAccount]:
    """Crossings of the last 24 hours not yet nudged (status ``ok``) to ``watchlist``."""
    crossed = crossed_accounts(session, watchlist, now)
    if not crossed:
        return []
    done = set(
        session.scalars(
            select(Nudge.event_id).where(
                Nudge.watchlist_id == watchlist.id,
                Nudge.status == "ok",
                Nudge.event_id.in_([a.event_id for a in crossed]),
            )
        )
    )
    return [a for a in crossed if a.event_id not in done]


def _record(
    session: Session,
    watchlist: Watchlist,
    account: CrossedAccount,
    *,
    now: datetime,
    http_status: int | None,
) -> bool:
    ok = http_status is not None and 200 <= http_status < 300
    row = session.scalars(
        select(Nudge).where(Nudge.watchlist_id == watchlist.id, Nudge.event_id == account.event_id)
    ).one_or_none()
    if row is None:
        row = Nudge(event_id=account.event_id, watchlist_id=watchlist.id, sent_at=now, status="ok")
        session.add(row)
    row.sent_at = now
    row.status = "ok" if ok else "failed"
    row.http_status = http_status
    session.flush()
    return ok


# --- sending -------------------------------------------------------------------------------------


def send_nudges(
    session: Session,
    *,
    now: datetime,
    private_webhooks: Mapping[str, str],
    sleep: Callable[[float], None] | None = None,
    client: httpx.Client | None = None,
    credit_budgets: Mapping[str, int] | None = None,
) -> NudgeOutcome:
    """Send every pending nudge of every watchlist in ``private_webhooks`` (slug -> webhook).

    ``credit_budgets`` maps a slug to its monthly enrichment budget (gate rule 5); a slug
    without one takes the default. Flushes the ``nudges``, ``enrichment_verdicts`` and
    ``runs`` rows; never commits. Runs on every ingest run; with no candidate nothing is
    recorded.
    """
    pause = sleep if sleep is not None else time.sleep
    if not private_webhooks:
        return NudgeOutcome(candidates=0, sent=0, errors=0, skipped="no private webhooks")
    settings = get_settings()
    watchlists = list(
        session.scalars(
            select(Watchlist)
            .where(Watchlist.slug.in_(list(private_webhooks)))
            .order_by(Watchlist.id)
        )
    )
    work: list[tuple[Watchlist, str, CrossedAccount]] = []
    for watchlist in watchlists:
        assert watchlist.slug is not None  # selected by slug
        webhook = private_webhooks[watchlist.slug]
        work.extend((watchlist, webhook, a) for a in pending_nudges(session, watchlist, now))
    if not work:
        return NudgeOutcome(candidates=0, sent=0, errors=0, skipped="nothing to nudge")
    coverage = _coverage_notes(session, [a.company_id for _, _, a in work], now)
    budgets = dict(credit_budgets or {})
    run_id = start_run(session, NUDGE_SOURCE, now=now)
    sent = errors = 0
    verdicts = {"enrich": 0, "skip": 0, "hold": 0}
    for index, (watchlist, webhook, account) in enumerate(work):
        if index:
            pause(SEND_INTERVAL_S)
        assert watchlist.slug is not None  # selected by slug
        verdict = judge(
            session,
            watchlist,
            event_id=account.event_id,
            company_id=account.company_id,
            contributions=account.contributions,
            coverage_note=coverage.get(account.company_id),
            now=now,
            credit_budget=budgets.get(watchlist.slug),
        )
        verdicts[verdict.verdict] = verdicts.get(verdict.verdict, 0) + 1
        blocks = build_nudge_blocks(
            account,
            pages_base_url=settings.tell_base_url,
            feedback_form_url=settings.tell_feedback_form_url,
            verdict=verdict,
            watchlist_slug=watchlist.slug,
        )
        status = post_blocks(webhook, _text(account), blocks, client=client, sleep=pause)
        if _record(session, watchlist, account, now=now, http_status=status):
            sent += 1
        else:
            errors += 1
    finish_run(
        session,
        run_id,
        status="failed" if sent == 0 else "ok",
        fetched=len(work),
        inserted=sent,
        skipped=0,
        errors=errors,
        notes={"watchlists": len({w.id for w, _, _ in work}), "verdicts": verdicts},
        now=now,
    )
    return NudgeOutcome(
        candidates=len(work), sent=sent, errors=errors, run_id=run_id, verdicts=verdicts
    )


def _coverage_notes(session: Session, company_ids: Sequence[int], now: datetime) -> dict[int, str]:
    """``{company_id: coverage_note}`` for the companies about to be nudged (gate rule 2)."""
    ids = sorted(set(company_ids))
    if not ids:
        return {}
    companies = list(session.scalars(select(Company).where(Company.id.in_(ids))))
    facts = company_facts_many(session, companies, now)
    return {cid: f.coverage_note for cid, f in facts.items()}
