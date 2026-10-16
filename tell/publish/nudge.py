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

``Worth enriching`` is v0 of the enrichment gate: ``yes`` when at least two distinct
signal families (``signal_type``) contributed, else ``not yet · one signal family``. A
crossing already implies the score is at or above the profile threshold and the company
is inside the profile's size band (:func:`tell.signals.scoring.emit_crossings`).

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
from tell.models import Nudge, Watchlist
from tell.publish.json_api import feedback_link
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

__all__ = [
    "NUDGE_SOURCE",
    "NudgeOutcome",
    "build_nudge_blocks",
    "load_private_webhooks",
    "pending_nudges",
    "send_nudges",
    "worth_enriching",
]

log = logging.getLogger(__name__)

NUDGE_SOURCE = "slack_nudge"
"""``runs.source`` of a nudge run."""
MIN_FAMILIES = 2
"""Distinct ``signal_type`` values a crossing needs before the gate says ``yes`` (v0)."""


@dataclass(frozen=True)
class NudgeOutcome:
    """What :func:`send_nudges` did."""

    candidates: int
    sent: int
    errors: int
    skipped: str | None = None
    """Why nothing was attempted (``no private webhooks``, ``nothing to nudge``)."""
    run_id: str | None = None


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


# --- Block Kit -----------------------------------------------------------------------------------


def worth_enriching(contributions: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    """The v0 gate: ``(yes, families)`` where families are the distinct signal types seen."""
    families: list[str] = []
    for item in contributions:
        family = str(item.get("signal_type") or "").strip()
        if family and item.get("url") and family not in families:
            families.append(family)
    return len(families) >= MIN_FAMILIES, families


def _gate_line(contributions: Sequence[Mapping[str, Any]]) -> str:
    yes, families = worth_enriching(contributions)
    if yes:
        return "Worth enriching: yes · " + " + ".join(families[:MIN_FAMILIES])
    return "Worth enriching: not yet · one signal family"


def build_nudge_blocks(
    account: CrossedAccount,
    *,
    pages_base_url: str,
    feedback_form_url: str | None = None,
) -> list[dict[str, Any]]:
    """The Block Kit ``blocks`` of one nudge (see the module docstring)."""
    base = pages_base_url.rstrip("/")
    crossed = "" if account.threshold is None else f" · crossed {account.threshold:g}"
    head = (
        f"*{escape(account.name)}* ({escape(account.domain)}) · score {account.score:.1f}{crossed}"
    )
    tail = [escape(why_now(account.contributions)), escape(_gate_line(account.contributions))]
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
) -> NudgeOutcome:
    """Send every pending nudge of every watchlist in ``private_webhooks`` (slug -> webhook).

    Flushes the ``nudges`` and ``runs`` rows; never commits. Runs on every ingest run;
    with no candidate nothing is recorded.
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
    run_id = start_run(session, NUDGE_SOURCE, now=now)
    sent = errors = 0
    for index, (watchlist, webhook, account) in enumerate(work):
        if index:
            pause(SEND_INTERVAL_S)
        blocks = build_nudge_blocks(
            account,
            pages_base_url=settings.tell_base_url,
            feedback_form_url=settings.tell_feedback_form_url,
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
        notes={"watchlists": len({w.id for w, _, _ in work})},
        now=now,
    )
    return NudgeOutcome(candidates=len(work), sent=sent, errors=errors, run_id=run_id)
