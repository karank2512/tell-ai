"""The Slack digest (spec section 9 "Slack digest"): Block Kit, one message per watchlist.

Message shape (:func:`build_digest_blocks`):

1. a red ``Stale sources`` line first when any source has no successful run in 24 hours
   (spec section 12 "Alerts");
2. header ``tell · {watchlist} · {date}``;
3. context ``{n} new signals · {m} accounts crossed the threshold · freshness p95 {h}h``;
4. one section per account that crossed the threshold, at most 10, highest score first:
   ``*{name}* ({domain}) · score {score}``, one bullet per contribution with its receipt
   as a link, then ``Why now:`` one sentence from the top two contributions;
5. footer: links to the account pages and the weekly page, and a "reply with a domain to
   add it" line pointing at the intake form (a Google Form read by an Actions job, not
   Slack events).

Timing (:func:`should_send_now`): the ingest workflow runs four times a day and calls
``tell publish notify --slack`` every time; only the first run at or after
``TELL_SLACK_DIGEST_HOUR`` (default 7) local time (``TELL_SLACK_TZ``) on a local day with
no digest sent yet sends. Each send is recorded as a ``runs`` row with source
``slack_digest``; a run whose every send failed is recorded ``failed`` so the next run
retries. Messages go out sequentially with a 1.1 s sleep between them (incoming webhooks
allow about one message per second).

Routing (ADR 0036): a watchlist whose ``tell-private`` record carries a
``slack_webhook_url`` gets its digest on that webhook; every other watchlist goes to the
global ``SLACK_WEBHOOK_URL``; a watchlist with neither is counted and skipped. The
per-event nudges that go out between digests live in :mod:`tell.publish.nudge` and reuse
the receipt section built here.

Crossed accounts are the ``score_crossed_threshold`` events observed in the last 24 hours
for the watchlist's profile and companies; their contributions come from the stored
``scores`` row of the crossing's scored day.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, start_run
from tell.config import get_settings
from tell.http import fetch
from tell.models import Company, EventRow, Run, Score, Signal, Watchlist, WatchlistItem
from tell.publish.health import compute_health
from tell.publish.json_api import intake_link, round_half_up
from tell.publish.weekly import IsoWeek
from tell.signals.scoring import SCORE_CROSSED_THRESHOLD, SCORE_SOURCE

__all__ = [
    "DIGEST_SOURCE",
    "MAX_ACCOUNTS",
    "SECTION_LIMIT",
    "SEND_INTERVAL_S",
    "CrossedAccount",
    "DigestOutcome",
    "build_digest_blocks",
    "crossed_accounts",
    "escape",
    "link",
    "new_signals_count",
    "post_blocks",
    "receipt_section",
    "send_digests",
    "should_send_now",
    "watchlist_members",
    "why_now",
]

DIGEST_SOURCE = "slack_digest"
"""``runs.source`` of a digest send."""
SEND_INTERVAL_S = 1.1
MAX_ACCOUNTS = 10
_WINDOW = timedelta(hours=24)
SECTION_LIMIT = 2900
"""Slack caps a section's text at 3000 characters; keep a margin."""


@dataclass(frozen=True)
class CrossedAccount:
    """One account that crossed the threshold, with its receipts."""

    name: str
    domain: str
    score: float
    contributions: list[dict[str, Any]]
    """``[{signal_type, role_family, contribution, url, summary, ...}]``, largest first."""
    event_id: str = ""
    """The ``score_crossed_threshold`` event this account crossed on (nudge idempotency)."""
    company_id: int = 0
    threshold: float | None = None
    """The profile's alert threshold at the crossing, from the event payload."""


@dataclass(frozen=True)
class DigestOutcome:
    """What :func:`send_digests` did."""

    sent: int
    errors: int
    skipped: str | None = None
    """Why nothing was sent (``no webhook``, ``not the digest run``), else ``None``."""
    run_id: str | None = None
    without_webhook: int = 0
    """Watchlists skipped because neither a private nor the global webhook exists."""


# --- Block Kit ----------------------------------------------------------------------------------


def escape(text: str) -> str:
    """Escape the three characters Slack mrkdwn reserves."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url: str, text: str) -> str:
    """A Slack mrkdwn link, its label escaped (``|`` would end the label)."""
    return f"<{url}|{escape(text).replace('|', '/')}>"


def _hours(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}h"


def why_now(contributions: Sequence[Mapping[str, Any]]) -> str:
    """The ``Why now:`` sentence built from the top two contributions' summaries."""
    summaries = [str(c.get("summary") or "").strip().rstrip(".") for c in contributions[:2]]
    summaries = [s for s in summaries if s]
    if not summaries:
        return "Why now: the score crossed the alert threshold."
    return "Why now: " + " and ".join(summaries) + "."


def receipt_section(
    head: str, contributions: Sequence[Mapping[str, Any]], tail: Sequence[str] = ()
) -> dict[str, Any]:
    """One mrkdwn section: ``head``, a receipt bullet per contribution, then ``tail`` lines.

    ``head`` and ``tail`` are already escaped. Bullets stop with ``• …`` before the text
    would pass :data:`SECTION_LIMIT`; the tail lines are always kept.
    """
    lines = [head]
    budget = SECTION_LIMIT - len(head) - sum(len(t) + 1 for t in tail) - 1
    for item in contributions:
        summary = str(item.get("summary") or item.get("signal_type") or "signal")
        bullet = f"• {link(str(item['url']), summary)}"
        if len(bullet) + 1 > budget:
            lines.append("• …")
            break
        budget -= len(bullet) + 1
        lines.append(bullet)
    lines.extend(tail)
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _account_section(account: CrossedAccount) -> dict[str, Any]:
    head = f"*{escape(account.name)}* ({escape(account.domain)}) · score {account.score:.1f}"
    return receipt_section(head, account.contributions, [escape(why_now(account.contributions))])


def build_digest_blocks(  # noqa: PLR0917 - the positional order is the documented call shape
    watchlist_name: str,
    date: date,
    crossed: Sequence[CrossedAccount],
    new_signals: int,
    freshness_p95_h: float | None,
    pages_base_url: str,
    stale_sources: Sequence[str],
    *,
    weekly_url: str | None = None,
    intake_url: str | None = None,
) -> list[dict[str, Any]]:
    """The Block Kit ``blocks`` of one digest message (see the module docstring)."""
    base = pages_base_url.rstrip("/")
    blocks: list[dict[str, Any]] = []
    if stale_sources:
        names = ", ".join(sorted(stale_sources))
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":red_circle: *Stale sources:* {escape(names)}"
                        " (no successful run in 24 hours)"
                    ),
                },
            }
        )
    header = f"tell · {watchlist_name} · {date.isoformat()}"
    blocks.append(
        {"type": "header", "text": {"type": "plain_text", "text": header[:150], "emoji": False}}
    )
    ranked = sorted(crossed, key=lambda a: (-a.score, a.domain))
    context = (
        f"{new_signals} new signals · {len(ranked)} accounts crossed the threshold"
        f" · freshness p95 {_hours(freshness_p95_h)}"
    )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    shown = ranked[:MAX_ACCOUNTS]
    if shown:
        blocks.append({"type": "divider"})
        blocks.extend(_account_section(account) for account in shown)
    weekly = weekly_url or f"{base}/weekly/{IsoWeek.containing(date).previous().label}.html"
    pages = " · ".join(link(f"{base}/companies/{a.domain}.html", a.domain) for a in shown)
    footer = f"{pages} · " if pages else ""
    footer += link(weekly, "Weekly page")
    intake = intake_url or intake_link()
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": footer},
                {
                    "type": "mrkdwn",
                    "text": f"Reply with a domain to add it: {link(intake, 'watchlist intake form')}",
                },
            ],
        }
    )
    return blocks


# --- data -------------------------------------------------------------------------------------


def watchlist_members(session: Session, watchlist: Watchlist) -> list[int]:
    """The company ids on ``watchlist``, ascending."""
    stmt = select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == watchlist.id)
    return sorted(int(r[0]) for r in session.execute(stmt))


def crossed_accounts(session: Session, watchlist: Watchlist, now: datetime) -> list[CrossedAccount]:
    """Accounts of ``watchlist`` with a ``score_crossed_threshold`` in the last 24 hours."""
    members = watchlist_members(session, watchlist)
    if not members:
        return []
    stmt = (
        select(EventRow, Company)
        .join(Company, Company.id == EventRow.company_id)
        .where(
            EventRow.source == SCORE_SOURCE,
            EventRow.type == SCORE_CROSSED_THRESHOLD,
            EventRow.company_id.in_(members),
            EventRow.observed_at > now - _WINDOW,
            EventRow.observed_at <= now,
            EventRow.payload.contains({"profile": watchlist.icp_profile}),
        )
        .order_by(EventRow.observed_at, EventRow.event_id)
    )
    latest: dict[int, tuple[EventRow, Company]] = {}
    for event, company in session.execute(stmt):
        latest[company.id] = (event, company)
    accounts: list[CrossedAccount] = []
    for event, company in latest.values():
        payload = event.payload or {}
        as_of_text = payload.get("as_of")
        stored = None
        if as_of_text:
            stored = session.get(
                Score, (company.id, watchlist.icp_profile, date.fromisoformat(str(as_of_text)))
            )
        if stored is not None:
            score_value = float(stored.score)
            contributions = list(stored.contributions)
        else:
            score_value = float(payload.get("score") or event.magnitude)
            contributions = list(payload.get("top_contributions") or [])
        threshold = payload.get("threshold")
        accounts.append(
            CrossedAccount(
                name=company.name,
                domain=company.domain,
                score=round_half_up(score_value),
                contributions=[c for c in contributions if c.get("url")],
                event_id=event.event_id,
                company_id=company.id,
                threshold=None if threshold is None else float(threshold),
            )
        )
    return sorted(accounts, key=lambda a: (-a.score, a.domain))


def new_signals_count(session: Session, company_ids: Sequence[int], now: datetime) -> int:
    """Signals of ``company_ids`` whose event was observed in the last 24 hours."""
    if not company_ids:
        return 0
    stmt = (
        select(func.count())
        .select_from(Signal)
        .join(EventRow, EventRow.event_id == Signal.event_id)
        .where(
            Signal.company_id.in_(list(company_ids)),
            EventRow.observed_at > now - _WINDOW,
            EventRow.observed_at <= now,
        )
    )
    return int(session.execute(stmt).scalar_one())


# --- timing and sending ---------------------------------------------------------------------------


def should_send_now(session: Session, now: datetime, tz: str, hour: int) -> bool:
    """True on the first run at or after ``hour`` local time with no digest sent that local day."""
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be timezone-aware, got naive {now!r}"
        raise ValueError(msg)
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    if local.hour < hour:
        return False
    day_start = datetime.combine(local.date(), dtime(0), tzinfo=zone)
    sent = session.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.source == DIGEST_SOURCE,
            Run.status == "ok",
            Run.started_at >= day_start,
            Run.started_at <= now,
        )
    ).scalar_one()
    return int(sent) == 0


def post_blocks(
    webhook_url: str,
    text: str,
    blocks: list[dict[str, Any]],
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """POST one Block Kit message to an incoming webhook; returns the HTTP status.

    ``None`` means a transport error. Goes through :func:`tell.http.fetch` (retried on
    429/5xx, never on transport errors since POST is not idempotent).
    """
    try:
        response = fetch(
            webhook_url,
            method="POST",
            json={"text": text, "blocks": blocks},
            client=client,
            sleep=sleep,
        )
    except httpx.HTTPError:
        return None
    return response.status_code


def send_digests(
    session: Session,
    *,
    now: datetime,
    webhook_url: str | None,
    sleep: Callable[[float], None] | None = None,
    force: bool = False,
    tz: str | None = None,
    hour: int | None = None,
    client: httpx.Client | None = None,
    private_webhooks: Mapping[str, str] | None = None,
) -> DigestOutcome:
    """Send one digest per watchlist when this is the digest run (or ``force``).

    ``private_webhooks`` maps a watchlist slug to its own webhook (ADR 0036); a watchlist
    with one gets its digest there, the rest go to ``webhook_url``, and a watchlist with
    neither is skipped and counted in ``without_webhook``. Flushes the ``runs`` row;
    never commits. POSTs go through :func:`post_blocks`. ``sleep`` (default
    ``time.sleep``) paces the sends and the retries.
    """
    pause = sleep if sleep is not None else time.sleep
    private = dict(private_webhooks or {})
    if not webhook_url and not private:
        return DigestOutcome(sent=0, errors=0, skipped="no webhook")
    settings = get_settings()
    zone = tz or settings.tell_slack_tz
    at = settings.tell_slack_digest_hour if hour is None else hour
    if not force and not should_send_now(session, now, zone, at):
        return DigestOutcome(sent=0, errors=0, skipped="not the digest run")
    health = compute_health(session, now)
    local_day = now.astimezone(ZoneInfo(zone)).date()
    watchlists = list(session.scalars(select(Watchlist).order_by(Watchlist.id)))
    run_id = start_run(session, DIGEST_SOURCE, now=now)
    sent = errors = without = attempted = 0
    for watchlist in watchlists:
        destination = (private.get(watchlist.slug) if watchlist.slug else None) or webhook_url
        if not destination:
            without += 1
            continue
        if attempted:
            pause(SEND_INTERVAL_S)
        attempted += 1
        crossed = crossed_accounts(session, watchlist, now)
        blocks = build_digest_blocks(
            watchlist.name,
            local_day,
            crossed,
            new_signals_count(session, watchlist_members(session, watchlist), now),
            health.freshness_p95_h,
            settings.tell_base_url,
            health.stale_sources,
        )
        text = f"tell · {watchlist.name} · {local_day.isoformat()}: {len(crossed)} accounts crossed"
        status = post_blocks(destination, text, blocks, client=client, sleep=pause)
        if status is not None and 200 <= status < 300:
            sent += 1
        else:
            errors += 1
    failed = attempted > 0 and sent == 0
    finish_run(
        session,
        run_id,
        status="failed" if failed else "ok",
        fetched=len(watchlists),
        inserted=sent,
        skipped=without,
        errors=errors,
        notes={"local_date": local_day.isoformat(), "tz": zone},
        now=now,
    )
    return DigestOutcome(sent=sent, errors=errors, run_id=run_id, without_webhook=without)
