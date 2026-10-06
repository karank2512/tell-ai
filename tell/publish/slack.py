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
    "SEND_INTERVAL_S",
    "CrossedAccount",
    "DigestOutcome",
    "build_digest_blocks",
    "crossed_accounts",
    "new_signals_count",
    "send_digests",
    "should_send_now",
]

DIGEST_SOURCE = "slack_digest"
"""``runs.source`` of a digest send."""
SEND_INTERVAL_S = 1.1
MAX_ACCOUNTS = 10
_WINDOW = timedelta(hours=24)
_SECTION_LIMIT = 2900
"""Slack caps a section's text at 3000 characters; keep a margin."""


@dataclass(frozen=True)
class CrossedAccount:
    """One account that crossed the threshold, with its receipts."""

    name: str
    domain: str
    score: float
    contributions: list[dict[str, Any]]
    """``[{signal_type, role_family, contribution, url, summary, ...}]``, largest first."""


@dataclass(frozen=True)
class DigestOutcome:
    """What :func:`send_digests` did."""

    sent: int
    errors: int
    skipped: str | None = None
    """Why nothing was sent (``no webhook``, ``not the digest run``), else ``None``."""
    run_id: str | None = None


# --- Block Kit ----------------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(url: str, text: str) -> str:
    return f"<{url}|{_escape(text).replace('|', '/')}>"


def _hours(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}h"


def _why_now(contributions: Sequence[Mapping[str, Any]]) -> str:
    summaries = [str(c.get("summary") or "").strip().rstrip(".") for c in contributions[:2]]
    summaries = [s for s in summaries if s]
    if not summaries:
        return "Why now: the score crossed the alert threshold."
    return "Why now: " + " and ".join(summaries) + "."


def _account_section(account: CrossedAccount) -> dict[str, Any]:
    head = f"*{_escape(account.name)}* ({_escape(account.domain)}) · score {account.score:.1f}"
    why = _escape(_why_now(account.contributions))
    lines = [head]
    budget = _SECTION_LIMIT - len(head) - len(why) - 2
    for item in account.contributions:
        summary = str(item.get("summary") or item.get("signal_type") or "signal")
        bullet = f"• {_link(str(item['url']), summary)}"
        if len(bullet) + 1 > budget:
            lines.append("• …")
            break
        budget -= len(bullet) + 1
        lines.append(bullet)
    lines.append(why)
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


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
                        f":red_circle: *Stale sources:* {_escape(names)}"
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
    pages = " · ".join(_link(f"{base}/companies/{a.domain}.html", a.domain) for a in shown)
    footer = f"{pages} · " if pages else ""
    footer += _link(weekly, "Weekly page")
    intake = intake_url or intake_link()
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": footer},
                {
                    "type": "mrkdwn",
                    "text": f"Reply with a domain to add it: {_link(intake, 'watchlist intake form')}",
                },
            ],
        }
    )
    return blocks


# --- data -------------------------------------------------------------------------------------


def _members(session: Session, watchlist: Watchlist) -> list[int]:
    stmt = select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == watchlist.id)
    return sorted(int(r[0]) for r in session.execute(stmt))


def crossed_accounts(session: Session, watchlist: Watchlist, now: datetime) -> list[CrossedAccount]:
    """Accounts of ``watchlist`` with a ``score_crossed_threshold`` in the last 24 hours."""
    members = _members(session, watchlist)
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
        accounts.append(
            CrossedAccount(
                name=company.name,
                domain=company.domain,
                score=round_half_up(score_value),
                contributions=[c for c in contributions if c.get("url")],
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
) -> DigestOutcome:
    """Send one digest per watchlist when this is the digest run (or ``force``).

    Flushes the ``runs`` row; never commits. POSTs go through :func:`tell.http.fetch`
    (retried on 429/5xx, never on transport errors since POST is not idempotent).
    ``sleep`` (default ``time.sleep``) paces the sends and the retries.
    """
    pause = sleep if sleep is not None else time.sleep
    if not webhook_url:
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
    sent = errors = 0
    for index, watchlist in enumerate(watchlists):
        if index:
            pause(SEND_INTERVAL_S)
        crossed = crossed_accounts(session, watchlist, now)
        blocks = build_digest_blocks(
            watchlist.name,
            local_day,
            crossed,
            new_signals_count(session, _members(session, watchlist), now),
            health.freshness_p95_h,
            settings.tell_base_url,
            health.stale_sources,
        )
        text = f"tell · {watchlist.name} · {local_day.isoformat()}: {len(crossed)} accounts crossed"
        try:
            response = fetch(
                webhook_url,
                method="POST",
                json={"text": text, "blocks": blocks},
                client=client,
                sleep=pause,
            )
        except httpx.HTTPError:
            errors += 1
            continue
        if 200 <= response.status_code < 300:
            sent += 1
        else:
            errors += 1
    failed = bool(watchlists) and sent == 0
    finish_run(
        session,
        run_id,
        status="failed" if failed else "ok",
        fetched=len(watchlists),
        inserted=sent,
        skipped=0,
        errors=errors,
        notes={"local_date": local_day.isoformat(), "tz": zone},
        now=now,
    )
    return DigestOutcome(sent=sent, errors=errors, run_id=run_id)
