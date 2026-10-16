"""HubSpot with the nudge (ADR 0040): rule 3 of the gate, and the push on ``enrich``.

A watchlist whose ``tell-private`` record holds ``hubspot_private_app_token`` gets two
things on every nudge:

1. **Rule 3 of the enrichment gate.** The company is looked up in that HubSpot portal by
   domain (:meth:`tell.crm.hubspot.HubSpotClient.lookup_company`) and the gate learns
   whether it is absent, present without recent sales activity (``quiet``) or present with
   a call, email, meeting or logged activity in the last 90 days (``active``, verdict
   ``skip``: a person is already on it). tell's own note never counts
   (:data:`tell.crm.hubspot.ACTIVITY_PROPERTIES`).
2. **The push on ``enrich``.** The company is created or updated with the five ``tell_*``
   properties, the receipt note and a task (ADR 0037's ``tell crm hubspot push --task``),
   and the nudge says so: ``HubSpot: added · note + task``, ``updated · note + task``,
   ``already current`` or ``push failed (401)``. The ledger row records ``crm_action``,
   ``crm_company_id`` and ``crm_pushed_at``; a resend after a failed Slack POST shows the
   stored line and never pushes twice, a failed push is retried on the next attempt.

Nothing personal moves: the push is company-level only (ADR 0037); the token is read from
``tell-private`` at run time and never stored or logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.crm.hubspot import (
    HubSpotClient,
    HubSpotError,
    Lookup,
    crm_status,
    load_receipt,
    push_company,
)
from tell.intake.reader import DEFAULT_PRIVATE_DIR, PrivateRecordError, load_private_watchlists
from tell.models import EnrichmentVerdict, Watchlist
from tell.signals.profiles import ProfileNotFoundError, get_profile

__all__ = ["CrmConfig", "CrmRun", "crm_line", "load_private_crm", "push_counts"]

log = logging.getLogger(__name__)

_LINES = {
    "created": "HubSpot: added · note + task",
    "updated": "HubSpot: updated · note + task",
    "unchanged": "HubSpot: already current",
}


@dataclass(frozen=True)
class CrmConfig:
    """One watchlist's HubSpot access, from its ``tell-private`` record."""

    token: str
    owner_id: str | None = None


def load_private_crm(private_dir: Path = DEFAULT_PRIVATE_DIR) -> dict[str, CrmConfig]:
    """``{watchlist slug: CrmConfig}`` for the records with a HubSpot token.

    Same reading and the same silence on a broken file as the webhook loader.
    """
    try:
        records = load_private_watchlists(private_dir)
    except (PrivateRecordError, yaml.YAMLError):
        return {}
    return {
        slug: CrmConfig(token=r.hubspot_private_app_token, owner_id=r.hubspot_owner_id)
        for slug, r in records.items()
        if r.hubspot_private_app_token
    }


def crm_line(row: EnrichmentVerdict | None) -> str | None:
    """The nudge's ``HubSpot:`` line for a ledger row, or ``None`` when nothing was pushed."""
    if row is None or not row.crm_action:
        return None
    if row.crm_action in _LINES:
        return _LINES[row.crm_action]
    detail = row.crm_company_id or "error"
    return f"HubSpot: push failed ({detail})"


class CrmRun:
    """One nudge run's HubSpot clients, one per watchlist with a token, and a lookup cache."""

    def __init__(
        self,
        configs: Mapping[str, CrmConfig],
        *,
        now: datetime,
        sleep: Callable[[float], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._configs = dict(configs)
        self._now = now
        self._sleep = sleep
        self._http = client
        self._clients: dict[str, HubSpotClient] = {}
        self._lookups: dict[tuple[str, str], Lookup | None] = {}

    def __enter__(self) -> CrmRun:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def configured(self, slug: str) -> bool:
        return slug in self._configs

    def _client(self, slug: str) -> HubSpotClient:
        if slug not in self._clients:
            self._clients[slug] = HubSpotClient(
                self._configs[slug].token, client=self._http, sleep=self._sleep
            )
        return self._clients[slug]

    def status(self, slug: str, domain: str) -> str:
        """Rule 3's input: ``absent`` / ``quiet`` / ``active``; ``unchecked`` when HubSpot fails."""
        if not self.configured(slug):
            return "unchecked"
        key = (slug, domain)
        if key not in self._lookups:
            try:
                self._lookups[key] = self._client(slug).lookup_company(domain)
            except (HubSpotError, httpx.HTTPError) as exc:
                log.warning("hubspot lookup failed for a watchlist (%s)", type(exc).__name__)
                self._lookups[key] = None
        lookup = self._lookups[key]
        if lookup is None:
            return "unchecked"
        return crm_status(lookup.company, now=self._now)

    def push(
        self,
        session: Session,
        *,
        slug: str,
        watchlist: Watchlist,
        domain: str,
        threshold: float | None,
        row: EnrichmentVerdict,
    ) -> str | None:
        """Push the company, note and task; record the outcome on ``row``; return the line."""
        settings = get_settings()
        action = "failed"
        company_id: str | None = None
        try:
            profile = get_profile(watchlist.icp_profile)
            receipt = load_receipt(session, domain, profile)
            outcome = push_company(
                self._client(slug),
                receipt,
                now=self._now,
                base_url=settings.tell_base_url,
                threshold=profile.alert_threshold if threshold is None else threshold,
                task=True,
                owner_id=self._configs[slug].owner_id,
            )
            action = outcome.action
            company_id = outcome.hubspot_company_id
        except HubSpotError as exc:
            company_id = str(exc.status)
            log.warning("hubspot push failed for a watchlist (HTTP %s)", exc.status)
        except (httpx.HTTPError, LookupError, ProfileNotFoundError) as exc:
            company_id = type(exc).__name__
            log.warning("hubspot push failed for a watchlist (%s)", type(exc).__name__)
        row.crm_action = action
        row.crm_company_id = company_id
        row.crm_pushed_at = self._now
        session.flush()
        self._lookups.pop((slug, domain), None)
        return crm_line(row)


def push_counts(rows: list[EnrichmentVerdict | None]) -> dict[str, int]:
    """``{created, updated, unchanged, failed}`` over the rows of one run."""
    counts = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
    for row in rows:
        if row is not None and row.crm_action in counts:
            counts[row.crm_action] += 1
    return counts
