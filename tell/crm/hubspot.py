"""HubSpot write-back (ADR 0037): company upsert by domain, ``tell_*`` properties, a note.

What tell writes, and nothing else:

* **Company** (``POST`` / ``PATCH /crm/v3/objects/companies``), found first by
  ``POST /crm/v3/objects/companies/search`` on the ``domain`` property. A new company gets
  ``name`` and ``domain``; an existing one keeps everything it has and only the ``tell_*``
  properties below move.
* **Properties** (:data:`PROPERTIES`, created once by :func:`ensure_properties` in the
  ``companyinformation`` group): ``tell_score`` (number), ``tell_top_signal`` (text),
  ``tell_receipt_url`` (text), ``tell_scored_at`` (date), ``tell_profile`` (text).
* **Note** (``POST /crm/v3/objects/notes``, associated to the company with the
  HubSpot-defined type :data:`NOTE_TO_COMPANY`): the receipt as plain text, built by
  :func:`note_body`: the score line, one ``summary · occurred_at · url`` line per
  contribution, the ``Why now`` sentence of the Slack digest and the account page link.
* **Task** (optional, ``POST /crm/v3/objects/tasks``, type :data:`TASK_TO_COMPANY`):
  ``tell: {name} crossed {threshold}``, due in :data:`TASK_DUE_DAYS` days, ``NOT_STARTED``,
  assigned to ``hubspot_owner_id`` when the caller passes one.

Idempotency without a table: the push is skipped as ``unchanged`` when the HubSpot
company already carries the same ``tell_scored_at`` and ``tell_score`` (the day's score
was pushed before), so a rerun never writes a second note; ``force`` overrides.

Politeness: one :class:`~tell.http.RateBudget` at :data:`RPS` requests per second (a
Free or Starter private app allows 100 per 10 seconds; search allows fewer), the project
User-Agent, and ``Authorization: Bearer`` with the customer's private-app token. Every
error from HubSpot is a :class:`HubSpotError` with the HTTP status, HubSpot's
``category`` and, for a 403 ``MISSING_SCOPES`` body, the scopes it names. The token is
never logged or echoed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.config import get_settings
from tell.http import RateBudget, fetch, make_client
from tell.models import Company, Score
from tell.publish.json_api import round_half_up
from tell.signals.profiles import Profile

__all__ = [
    "ACTIVITY_PROPERTIES",
    "CRM_ACTIVITY_WINDOW",
    "LIST_PAGE_CAP",
    "LIST_PAGE_SIZE",
    "NOTE_TO_COMPANY",
    "PROPERTIES",
    "PROPERTY_GROUP",
    "RPS",
    "SCOPES",
    "TASK_DUE_DAYS",
    "TASK_TO_COMPANY",
    "HubSpotClient",
    "HubSpotError",
    "Lookup",
    "PropertySpec",
    "PushOutcome",
    "Receipt",
    "check",
    "company_properties",
    "crm_status",
    "ensure_properties",
    "load_receipt",
    "note_body",
    "push_company",
    "why_now",
]

PROPERTY_GROUP = "companyinformation"
"""HubSpot's default company property group; the ``tell_*`` properties live in it."""

NOTE_TO_COMPANY = 190
"""HubSpot-defined association type: note to company (associations v4 reference)."""

TASK_TO_COMPANY = 192
"""HubSpot-defined association type: task to company (associations v4 reference)."""

TASK_DUE_DAYS = 2
LIST_PAGE_SIZE = 100
"""Companies per page of ``GET /crm/v3/objects/companies`` (HubSpot's maximum)."""

LIST_PAGE_CAP = 10
"""Pages the lookup reads from the object store when the search index misses, so a company
created seconds ago is still found (HubSpot's search index lags creates by seconds).
1,000 companies covers a seed-stage portal; past the cap a push may create a duplicate."""

RPS = 4.0
"""Requests per second against HubSpot: under the search endpoint's limit and far under the
private-app limit of 100 per 10 seconds on Free and Starter."""

SCOPES: tuple[str, ...] = (
    "crm.objects.companies.read",
    "crm.objects.companies.write",
    "crm.schemas.companies.write",
    "crm.objects.contacts.read",
    "crm.objects.contacts.write",
)
"""Private-app scopes the three commands need. The two ``contacts`` scopes are what
HubSpot's notes and tasks guides list as the engagement endpoints' requirement; tell never
calls a contacts endpoint (``docs/crm/hubspot.md``)."""

_NOTE_BODY_LIMIT = 65_000
"""HubSpot caps ``hs_note_body`` at 65,536 characters; keep a margin."""
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_ERROR = 400
_ERROR_CHARS = 300
_ISO_DATE_WIDTH = 10


@dataclass(frozen=True)
class PropertySpec:
    """One ``tell_*`` company property as HubSpot's properties API creates it."""

    name: str
    label: str
    type: str
    field_type: str
    description: str

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "fieldType": self.field_type,
            "groupName": PROPERTY_GROUP,
            "description": self.description,
        }


PROPERTIES: tuple[PropertySpec, ...] = (
    PropertySpec("tell_score", "tell score", "number", "number", "tell buying-signal score."),
    PropertySpec(
        "tell_top_signal", "tell top signal", "string", "text", "tell's largest contribution."
    ),
    PropertySpec(
        "tell_receipt_url",
        "tell receipt URL",
        "string",
        "text",
        "Source of the largest contribution.",
    ),
    PropertySpec("tell_scored_at", "tell scored at", "date", "date", "Day the score was computed."),
    PropertySpec("tell_profile", "tell profile", "string", "text", "ICP profile of the score."),
)


class HubSpotError(RuntimeError):
    """HubSpot answered with an error; carries the status and HubSpot's category."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        category: str | None = None,
        missing_scopes: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        self.missing_scopes = tuple(missing_scopes)

    @property
    def hint(self) -> str:
        """What to do about it, for the command line."""
        if self.status == _HTTP_UNAUTHORIZED:
            return (
                "HubSpot rejected the token (401): check HUBSPOT_PRIVATE_APP_TOKEN "
                "(docs/crm/hubspot.md)."
            )
        if self.status == _HTTP_FORBIDDEN:
            scopes = ", ".join(self.missing_scopes) or ", ".join(SCOPES)
            return f"the private app lacks a scope (403); grant: {scopes} (docs/crm/hubspot.md)."
        return str(self)


def _error_from(response: httpx.Response) -> HubSpotError:
    category: str | None = None
    scopes: list[str] = []
    message = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        category = str(body.get("category")) if body.get("category") else None
        if isinstance(body.get("message"), str):
            message = f"HTTP {response.status_code}: {body['message'][:_ERROR_CHARS]}"
        for err in body.get("errors") or []:
            context = err.get("context") if isinstance(err, dict) else None
            if isinstance(context, dict):
                required = context.get("requiredScopes") or context.get("requiredGranularScopes")
                if isinstance(required, list):
                    scopes.extend(str(s) for s in required)
    return HubSpotError(
        message, status=response.status_code, category=category, missing_scopes=scopes
    )


ACTIVITY_PROPERTIES: tuple[str, ...] = ("hs_last_sales_activity_timestamp", "notes_last_contacted")
"""HubSpot's "Last Engagement Date" and "Last Contacted": set by calls, emails, meetings
and logged sales activity. Read for the gate's rule 3 (ADR 0040). ``notes_last_updated``
("Last Activity Date") is deliberately not read: tell's own note sets it, so a company tell
pushed would look worked on by a person (observed on the first live portal, 2026-10-16)."""

CRM_ACTIVITY_WINDOW = timedelta(days=90)
"""Rule 3: sales activity inside this window means someone is already working the account."""

_LOOKUP_PROPERTIES: list[str] = [
    "name",
    "domain",
    *(p.name for p in PROPERTIES),
    *ACTIVITY_PROPERTIES,
]


def crm_status(company: Mapping[str, Any] | None, *, now: datetime) -> str:
    """Rule 3's input from a looked-up company: ``absent``, ``quiet`` or ``active``."""
    if company is None:
        return "absent"
    props = company.get("properties") or {}
    for name in ACTIVITY_PROPERTIES:
        raw = props.get(name)
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if now - CRM_ACTIVITY_WINDOW <= stamp <= now + timedelta(days=1):
            return "active"
    return "quiet"


@dataclass(frozen=True)
class Lookup:
    """How a company was (or was not) found before an upsert."""

    company: dict[str, Any] | None
    via: str
    """``search`` (the index had it), ``list`` (found or exhausted in the object store) or
    ``list_capped`` (not found within :data:`LIST_PAGE_CAP` pages)."""
    pages: int
    """Object-store pages read; 0 when the search index answered."""


# --- client ------------------------------------------------------------------------------------


class HubSpotClient:
    """A thin, paced client over the handful of HubSpot endpoints tell uses."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        settings = get_settings()
        self._base = (base_url or settings.hubspot_api_base).rstrip("/")
        self._sleep = sleep if sleep is not None else time.sleep
        self._budget = RateBudget(rps=RPS, burst=1, sleep=self._sleep)
        self._own_client = client is None
        self._client = client if client is not None else make_client()
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def __enter__(self) -> HubSpotClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> dict[str, Any] | None:
        response = fetch(
            f"{self._base}{path}",
            method=method,
            headers=self._headers,
            json=json,
            params=params,
            client=self._client,
            sleep=self._sleep,
            budget=self._budget,
        )
        if response.status_code == _HTTP_NOT_FOUND and not_found_ok:
            return None
        if response.status_code >= _HTTP_ERROR:
            raise _error_from(response)
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {"value": data}

    # properties

    def get_property(self, name: str) -> dict[str, Any] | None:
        """The company property ``name``, or ``None`` when HubSpot has no such property."""
        return self._request("GET", f"/crm/v3/properties/companies/{name}", not_found_ok=True)

    def create_property(self, spec: PropertySpec) -> dict[str, Any]:
        created = self._request("POST", "/crm/v3/properties/companies", json=spec.payload())
        return created or {}

    # companies

    def probe(self) -> dict[str, Any]:
        """One cheap authenticated read (``GET /crm/v3/objects/companies?limit=1``)."""
        return self._request("GET", "/crm/v3/objects/companies", params={"limit": 1}) or {}

    def find_company(self, domain: str) -> dict[str, Any] | None:
        """The first company whose ``domain`` equals ``domain`` in the search index."""
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}
            ],
            "properties": _LOOKUP_PROPERTIES,
            "limit": 1,
        }
        found = self._request("POST", "/crm/v3/objects/companies/search", json=body) or {}
        results = found.get("results") or []
        first = results[0] if results else None
        return first if isinstance(first, dict) else None

    def list_company(self, domain: str, *, page_cap: int = LIST_PAGE_CAP) -> Lookup:
        """Scan the object store page by page for ``domain`` (the search index lags creates).

        Reads at most ``page_cap`` pages of :data:`LIST_PAGE_SIZE`; stops at the first
        match or the end of the list.
        """
        after: str | None = None
        pages = 0
        wanted = domain.strip().lower()
        while pages < page_cap:
            params: dict[str, Any] = {
                "limit": LIST_PAGE_SIZE,
                "properties": ",".join(_LOOKUP_PROPERTIES),
            }
            if after:
                params["after"] = after
            page = self._request("GET", "/crm/v3/objects/companies", params=params) or {}
            pages += 1
            for item in page.get("results") or []:
                props = item.get("properties") or {} if isinstance(item, dict) else {}
                if str(props.get("domain") or "").strip().lower() == wanted:
                    return Lookup(company=item, via="list", pages=pages)
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return Lookup(company=None, via="list", pages=pages)
        return Lookup(company=None, via="list_capped", pages=pages)

    def lookup_company(self, domain: str) -> Lookup:
        """Search first; when the index has no match, scan the object store (issue #5)."""
        found = self.find_company(domain)
        if found is not None:
            return Lookup(company=found, via="search", pages=0)
        return self.list_company(domain)

    def create_company(self, properties: Mapping[str, Any]) -> dict[str, Any]:
        created = self._request(
            "POST", "/crm/v3/objects/companies", json={"properties": dict(properties)}
        )
        return created or {}

    def update_company(self, company_id: str, properties: Mapping[str, Any]) -> dict[str, Any]:
        updated = self._request(
            "PATCH",
            f"/crm/v3/objects/companies/{company_id}",
            json={"properties": dict(properties)},
        )
        return updated or {}

    # engagements

    def create_note(self, company_id: str, body: str, at: datetime) -> dict[str, Any]:
        payload = _note_payload(company_id, body, at)
        return self._request("POST", "/crm/v3/objects/notes", json=payload) or {}

    def create_task(
        self,
        company_id: str,
        *,
        subject: str,
        body: str,
        due: datetime,
        owner_id: str | None,
    ) -> dict[str, Any]:
        payload = _task_payload(company_id, subject=subject, body=body, due=due, owner_id=owner_id)
        return self._request("POST", "/crm/v3/objects/tasks", json=payload) or {}


def _association(company_id: str, type_id: int) -> list[dict[str, Any]]:
    return [
        {
            "to": {"id": company_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}],
        }
    ]


def _note_payload(company_id: str, body: str, at: datetime) -> dict[str, Any]:
    return {
        "properties": {"hs_timestamp": iso_utc(at), "hs_note_body": body[:_NOTE_BODY_LIMIT]},
        "associations": _association(company_id, NOTE_TO_COMPANY),
    }


def _task_payload(
    company_id: str, *, subject: str, body: str, due: datetime, owner_id: str | None
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "hs_timestamp": iso_utc(due),
        "hs_task_subject": subject,
        "hs_task_body": body,
        "hs_task_status": "NOT_STARTED",
        "hs_task_priority": "MEDIUM",
        "hs_task_type": "TODO",
    }
    if owner_id:
        properties["hubspot_owner_id"] = owner_id
    return {"properties": properties, "associations": _association(company_id, TASK_TO_COMPANY)}


# --- the receipt -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Receipt:
    """A company's latest stored score for one profile, as the push needs it."""

    company_id: int
    domain: str
    name: str
    profile: str
    as_of: date
    score: float
    contributions: list[dict[str, Any]]
    """Largest first; only entries with a ``url`` (the receipt rule)."""

    @property
    def top(self) -> dict[str, Any] | None:
        return self.contributions[0] if self.contributions else None


def load_receipt(session: Session, domain: str, profile: Profile) -> Receipt:
    """The newest ``scores`` row of ``domain`` under ``profile``; :class:`LookupError` if none."""
    company = session.scalar(select(Company).where(Company.domain == domain))
    if company is None:
        msg = f"no company with domain {domain!r}; run tell discover domain first"
        raise LookupError(msg)
    stored = session.scalar(
        select(Score)
        .where(Score.company_id == company.id, Score.icp_profile == profile.name)
        .order_by(Score.as_of.desc())
        .limit(1)
    )
    if stored is None:
        msg = f"no {profile.name!r} score for {domain!r}; run tell score --all-profiles first"
        raise LookupError(msg)
    contributions = sorted(
        (dict(c) for c in stored.contributions if c.get("url")),
        key=lambda c: -float(c.get("contribution") or 0.0),
    )
    return Receipt(
        company_id=company.id,
        domain=company.domain,
        name=company.name,
        profile=profile.name,
        as_of=stored.as_of,
        score=round_half_up(float(stored.score)),
        contributions=contributions,
    )


def why_now(contributions: Sequence[Mapping[str, Any]]) -> str:
    """The Slack digest's ``Why now`` sentence: the top two summaries joined by ``and``."""
    summaries = [str(c.get("summary") or "").strip().rstrip(".") for c in contributions[:2]]
    summaries = [s for s in summaries if s]
    if not summaries:
        return "Why now: the score crossed the alert threshold."
    return "Why now: " + " and ".join(summaries) + "."


def _day(value: Any) -> str:
    text = str(value or "")
    return text[:_ISO_DATE_WIDTH] if len(text) >= _ISO_DATE_WIDTH else text


def note_body(receipt: Receipt, *, base_url: str) -> str:
    """The receipt as plain text: score line, one line per contribution, why now, page."""
    lines = [
        f"tell score {receipt.score:.1f} ({receipt.profile}) as of {receipt.as_of.isoformat()}"
    ]
    for item in receipt.contributions:
        summary = str(item.get("summary") or item.get("signal_type") or "signal")
        lines.append(f"{summary} · {_day(item.get('occurred_at'))} · {item['url']}")
    lines.append(why_now(receipt.contributions))
    lines.append(f"Account page: {base_url.rstrip('/')}/companies/{receipt.domain}.html")
    return "\n".join(lines)


def company_properties(receipt: Receipt) -> dict[str, Any]:
    """The five ``tell_*`` values for the company upsert."""
    top = receipt.top or {}
    return {
        "tell_score": f"{receipt.score:.1f}",
        "tell_top_signal": str(top.get("summary") or "")[:255],
        "tell_receipt_url": str(top.get("url") or "")[:255],
        "tell_scored_at": receipt.as_of.isoformat(),
        "tell_profile": receipt.profile,
    }


def _same_push(existing: Mapping[str, Any], receipt: Receipt) -> bool:
    props = existing.get("properties") or {}
    if _day(props.get("tell_scored_at")) != receipt.as_of.isoformat():
        return False
    try:
        return Decimal(str(props.get("tell_score"))) == Decimal(f"{receipt.score:.1f}")
    except ArithmeticError:
        return False


# --- the three operations ----------------------------------------------------------------------


@dataclass
class PushOutcome:
    """What :func:`push_company` did (or, on a dry run, would do)."""

    action: str
    """``created``, ``updated``, ``unchanged`` or ``dry_run``."""
    domain: str
    hubspot_company_id: str | None = None
    note_id: str | None = None
    task_id: str | None = None
    payloads: dict[str, Any] = field(default_factory=dict)
    """On a dry run: the request bodies that would have been sent."""
    lookup: Lookup | None = None
    """How the company was looked up; ``None`` on a dry run."""

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "action": self.action,
            "domain": self.domain,
            "hubspot_company_id": self.hubspot_company_id,
            "note_id": self.note_id,
            "task_id": self.task_id,
        }
        if self.lookup is not None and self.lookup.pages:
            record["listed"] = {
                "pages": self.lookup.pages,
                "capped": self.lookup.via == "list_capped",
            }
        if self.payloads:
            record["payloads"] = self.payloads
        return record


def ensure_properties(client: HubSpotClient) -> dict[str, list[str]]:
    """Create the ``tell_*`` company properties that do not exist yet. Idempotent."""
    created: list[str] = []
    existing: list[str] = []
    for spec in PROPERTIES:
        if client.get_property(spec.name) is None:
            client.create_property(spec)
            created.append(spec.name)
        else:
            existing.append(spec.name)
    return {"created": created, "existing": existing}


def check(client: HubSpotClient) -> dict[str, Any]:
    """One authenticated read plus which ``tell_*`` properties exist."""
    client.probe()
    present = {spec.name: client.get_property(spec.name) is not None for spec in PROPERTIES}
    return {"ok": True, "properties": present, "setup_needed": not all(present.values())}


def push_company(
    client: HubSpotClient | None,
    receipt: Receipt,
    *,
    now: datetime,
    base_url: str,
    threshold: float,
    task: bool = False,
    owner_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> PushOutcome:
    """Upsert the company, write the note (and the task) unless this score was pushed already.

    ``client`` may be ``None`` only on a dry run, which builds every payload and sends
    nothing.
    """
    properties = company_properties(receipt)
    body = note_body(receipt, base_url=base_url)
    subject = f"tell: {receipt.name} crossed {threshold:g}"
    task_body = f"{why_now(receipt.contributions)} Receipt in the note of {now.date().isoformat()}."
    due = now + timedelta(days=TASK_DUE_DAYS)
    if dry_run or client is None:
        payloads: dict[str, Any] = {
            "company": {
                "properties": {"name": receipt.name, "domain": receipt.domain, **properties}
            },
            "note": _note_payload("<company id>", body, now),
        }
        if task:
            payloads["task"] = _task_payload(
                "<company id>", subject=subject, body=task_body, due=due, owner_id=owner_id
            )
        return PushOutcome(action="dry_run", domain=receipt.domain, payloads=payloads)

    lookup = client.lookup_company(receipt.domain)
    existing = lookup.company
    if existing is not None:
        company_id = str(existing["id"])
        if _same_push(existing, receipt) and not force:
            return PushOutcome(
                action="unchanged",
                domain=receipt.domain,
                hubspot_company_id=company_id,
                lookup=lookup,
            )
        client.update_company(company_id, properties)
        action = "updated"
    else:
        created = client.create_company(
            {"name": receipt.name, "domain": receipt.domain, **properties}
        )
        company_id = str(created["id"])
        action = "created"
    note = client.create_note(company_id, body, now)
    outcome = PushOutcome(
        action=action,
        domain=receipt.domain,
        hubspot_company_id=company_id,
        note_id=str(note["id"]) if note.get("id") is not None else None,
        lookup=lookup,
    )
    if task:
        made = client.create_task(
            company_id, subject=subject, body=task_body, due=due, owner_id=owner_id
        )
        outcome.task_id = str(made["id"]) if made.get("id") is not None else None
    return outcome
