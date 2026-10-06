"""Pydantic request and response models of the v1 service (spec section 10).

The company object is the static ``api/v0/companies/{domain}.json`` contract
(``docs/api/company.schema.json``) field for field; optional fields (``heat``,
``role_family``, ``postings``) are left out of a response when the static builder
leaves them out, never sent as ``null``. Request bodies forbid unknown fields.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CompanyOut",
    "ContributionOut",
    "DecayedOut",
    "EventOut",
    "EventsOut",
    "FeedbackCreated",
    "FeedbackIn",
    "HealthOut",
    "InMarketCompany",
    "InMarketOut",
    "PostingOut",
    "Problem",
    "SourceHealthOut",
    "SourcesOut",
    "WatchlistCreated",
    "WatchlistIn",
    "WatchlistRef",
]

Verdict = Literal["useful", "not_useful", "already_knew", "worked", "booked", "won"]
"""``feedback.verdict`` (spec section 5); mirrors :data:`tell.models.FEEDBACK_VERDICTS`."""

MAX_WATCHLIST_DOMAINS = 1000
MAX_NOTE_LENGTH = 1000


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- errors ---------------------------------------------------------------------------------


class Problem(_Out):
    """An RFC 9457 problem body (``application/problem+json``)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Validation errors (422 only)."
    )


# --- company (the static v0 object) -------------------------------------------------------------


class PostingOut(_Out):
    title: str
    posted_at: str
    url: str


class ContributionOut(_Out):
    type: str
    role_family: str | None = None
    contribution: float
    occurred_at: str
    summary: str
    url: str
    postings: list[PostingOut] | None = None


class DecayedOut(_Out):
    type: str
    role_family: str | None = None
    decayed_at: str
    url: str
    summary: str


class SourcesOut(_Out):
    ats: str
    edgar_cik: str | None
    news: bool


class CompanyOut(_Out):
    """``api/v0/companies/{domain}.json`` (spec section 9)."""

    domain: str
    name: str
    as_of: str
    profile: str
    score: float
    heat: int | None = None
    contributions: list[ContributionOut]
    open_postings: int
    long_open_roles: int
    sources: SourcesOut
    generated_by: str
    why_not_higher: list[DecayedOut]


# --- in-market ---------------------------------------------------------------------------------


class WatchlistRef(_Out):
    id: int
    name: str
    slug: str | None
    icp_profile: str
    is_public: bool


class InMarketCompany(_Out):
    domain: str
    name: str
    score: float
    heat: int
    top_contributions: list[ContributionOut]


class InMarketOut(_Out):
    watchlist: WatchlistRef
    profile: str
    as_of: date
    since: date
    min_score: float
    families: list[str]
    companies: list[InMarketCompany]


# --- events --------------------------------------------------------------------------------------


class EventOut(_Out):
    event_id: str
    source: str
    type: str
    occurred_at: datetime
    observed_at: datetime
    magnitude: float
    url: str
    payload: dict[str, Any]


class EventsOut(_Out):
    domain: str
    since: date | None
    types: list[str]
    events: list[EventOut]


# --- watchlists ------------------------------------------------------------------------------------


class WatchlistIn(_In):
    name: str = Field(min_length=1, max_length=200)
    icp_profile: str = Field(default="default", min_length=1, max_length=100)
    domains: list[str] = Field(default_factory=list, max_length=MAX_WATCHLIST_DOMAINS)


class WatchlistCreated(_Out):
    id: int
    name: str
    icp_profile: str
    added: list[str] = Field(description="Known domains now on the list.")
    unknown: list[str] = Field(description="Domains tell has no company for (not added).")


# --- feedback ------------------------------------------------------------------------------------


class FeedbackIn(_In):
    signal_id: int = Field(gt=0)
    verdict: Verdict
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)
    watchlist_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "One of your watchlists holding the signal's company; when omitted, your "
            "lowest-id watchlist that holds it."
        ),
    )


class FeedbackCreated(_Out):
    id: int
    watchlist_id: int
    signal_id: int
    verdict: Verdict
    note: str | None
    created_at: datetime


# --- health ----------------------------------------------------------------------------------------


class SourceHealthOut(_Out):
    source: str
    kind: str
    last_ok_at: str | None
    success_rate_7d: float | None
    fetched_24h: int
    inserted_24h: int
    freshness_p95_h: float | None
    stale: bool
    status: str


class HealthOut(_Out):
    """``api/v0/health.json``."""

    generated_at: str
    stale_sources: list[str]
    freshness_p95_h: float | None
    sources: list[SourceHealthOut]
    generated_by: str
