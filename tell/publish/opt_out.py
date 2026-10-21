"""The public-site opt-out (ADR 0044): companies that asked to be left off tell's site.

A company that would rather not appear on the site opens a GitHub issue on the code
repository titled ``Opt out: <domain>`` (``.github/ISSUE_TEMPLATE/opt-out.md``, linked from
the footer of every account page and of the Form D filings page). Once the operator has
checked that the request comes from the company, ``tell publish opt-out add DOMAIN`` sets
``companies.public_opt_out`` and ``companies.public_opted_out_at`` (migration 0014), and the
next publish (:func:`tell.publish.site.render_site`) leaves the company out of everything it
writes:

* no account page and no ``api/v0/companies/{domain}.json``: the company drops out of
  :func:`tell.publish.site.published_company_ids`, so the size budget's own clean-up deletes
  the files an earlier publish wrote;
* no row in the weekly top lists (``weekly/{ISO-week}.html``, ``api/v0/weekly/latest.json``),
  in the Form D filings list, in a public watchlist's JSON or among the index's top movers;
* its rows are removed from the archived weekly pages, which are never rendered again.

Nothing private changes: the Slack nudges and digests, the HubSpot push and the CSV export
belong to the customer whose watchlist holds the company and read neither column; nor does
the keyed v1 API. The aggregate counts (weekly coverage, run health, the methods reports)
still count the company, and the ``tell-data`` system of record (``events/*.jsonl``, board
snapshots) and the repository's git history keep what they hold.

``tell publish opt-out remove DOMAIN`` undoes it from the next publish on (rows already
removed from archived pages stay removed); ``list`` prints every opted-out company. The
request itself, and how it was verified, stays on the GitHub issue: nothing else about it is
stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.config import get_settings
from tell.models import Company

__all__ = [
    "OPT_OUT_TEMPLATE",
    "OptOutChange",
    "add_opt_out",
    "normalize_domain",
    "opt_out_link",
    "opted_out",
    "opted_out_domains",
    "remove_opt_out",
]

OPT_OUT_TEMPLATE = "opt-out.md"
"""The issue template under ``.github/ISSUE_TEMPLATE/`` (title ``Opt out: <domain>``)."""


@dataclass(frozen=True)
class OptOutChange:
    """A company's opt-out state after ``add`` or ``remove``."""

    domain: str
    public_opt_out: bool
    public_opted_out_at: datetime | None
    changed: bool
    """``False`` when the company already was in the state asked for (the call is a no-op)."""

    def as_record(self) -> dict[str, Any]:
        at = self.public_opted_out_at
        return {
            "domain": self.domain,
            "public_opt_out": self.public_opt_out,
            "public_opted_out_at": iso_utc(at) if at is not None else None,
            "changed": self.changed,
        }


def normalize_domain(value: str) -> str:
    """``https://www.Acme.com/`` -> ``acme.com``: the ``companies.domain`` an issue names."""
    text = value.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].rstrip(".")
    return text.removeprefix("www.")


def _company(session: Session, domain: str) -> Company:
    key = normalize_domain(domain)
    company = session.scalars(select(Company).where(Company.domain == key)).one_or_none()
    if company is None:
        msg = f"no company with domain {key!r}: tell publishes nothing for it"
        raise LookupError(msg)
    return company


def add_opt_out(session: Session, domain: str, *, now: datetime) -> OptOutChange:
    """Leave the company off the public site from the next publish on (idempotent).

    Raises :class:`LookupError` when no company has the domain. A second ``add`` keeps the
    first ``public_opted_out_at``.
    """
    company = _company(session, domain)
    changed = not company.public_opt_out
    if changed:
        company.public_opt_out = True
        company.public_opted_out_at = now
        company.updated_at = now
        session.flush()
    return OptOutChange(company.domain, True, company.public_opted_out_at, changed)


def remove_opt_out(session: Session, domain: str, *, now: datetime) -> OptOutChange:
    """Put the company back on the public site from the next publish on (idempotent)."""
    company = _company(session, domain)
    changed = company.public_opt_out
    if changed:
        company.public_opt_out = False
        company.public_opted_out_at = None
        company.updated_at = now
        session.flush()
    return OptOutChange(company.domain, False, None, changed)


def opted_out(session: Session) -> list[Company]:
    """Every company that opted out of the public site, by domain."""
    stmt = select(Company).where(Company.public_opt_out.is_(True)).order_by(Company.domain)
    return list(session.scalars(stmt))


def opted_out_domains(session: Session) -> frozenset[str]:
    """The domains of :func:`opted_out`."""
    stmt = select(Company.domain).where(Company.public_opt_out.is_(True))
    return frozenset(str(domain) for domain in session.scalars(stmt))


def opt_out_link(domain: str | None = None) -> str:
    """The new-issue link of the opt-out template, its title prefilled for ``domain``."""
    repo = get_settings().tell_repo_url.rstrip("/")
    query = {"template": OPT_OUT_TEMPLATE}
    if domain:
        query["title"] = f"Opt out: {domain}"
    return f"{repo}/issues/new?{urlencode(query)}"
