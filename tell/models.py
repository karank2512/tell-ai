"""SQLAlchemy 2.0 models: the DDL in ``docs/SPEC.md`` section 5, field for field.

Thirteen tables, one class each, in the order the spec lists them, plus one table
the spec lacks (``company_feeds``, below). Column names, types, nullability, server
defaults, primary keys, unique constraints, foreign keys and the four secondary
indexes mirror the DDL exactly; ``tests/test_schema.py`` proves parity against the
applied alembic migration with ``compare_metadata``.

Constraint and index names follow PostgreSQL's own defaults (``<table>_pkey``,
``<table>_<cols>_key``, ``<table>_<col>_fkey``, ``<table>_<cols>_idx``) so the
hand-written DDL in the spec and the migration name the same objects.

Nothing here goes beyond the DDL except the allowed-value constants below (the
spec documents them as comments next to the columns), a generic ``__repr__`` and
the recorded deviations below. ``postings.version_marker`` (migration ``0002``,
``docs/decisions/0013-postings-version-marker.md``) holds the provider change
marker last seen for a posting so ``job_updated`` does not depend on events that
the 180-day prune removes. The ``company_feeds`` table (migration ``0003``,
``docs/decisions/0014-company-feeds-table.md``) keeps the company blog feeds the
news collector discovers (spec section 3.7) and their conditional-request
validators, which section 5 has nowhere to keep. ``ats_boards.not_found_count`` and
``ats_boards.first_not_found_at`` (migration ``0004``,
``docs/decisions/0015-ats-404-and-reverification.md``) carry the 404 streak that
decides when a board is ``dead``. The ``posting_ghost_votes`` table (migration
``0005``, ``docs/decisions/0017-posting-ghost-votes.md``) stores the job-seeker "this
looks like a ghost job" votes spec section 7 counts; they stay inert until 500 votes.
``watchlists.slug`` and ``watchlists.is_public`` (migration ``0006``,
``docs/decisions/0022-public-watchlists.md``) mark the lists spec section 9 publishes.
The ``api_keys`` and ``api_usage`` tables (migration ``0007``,
``docs/decisions/0024-api-keys-tables.md``) hold the FastAPI v1 service's bearer keys
(SHA-256 at rest, never the raw key) and per-key daily call counts (spec section 10).
``companies.yc_status`` / ``companies.yc_batch`` (migration ``0008``, ADR 0034) carry the
YC directory facts the Form D guards read. ``companies.source`` / ``companies.cik_source``
and the ``issuer_discovery`` table (migration ``0009``,
``docs/decisions/0035-issuer-first-discovery.md``) record where a company and its CIK came
from and what issuer-first discovery found per Form D issuer. The ``nudges`` table
(migration ``0010``, ``docs/decisions/0036-per-event-slack-nudges.md``) records which
``score_crossed_threshold`` event has been nudged to which watchlist's own Slack webhook.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- allowed values (documented in the spec's DDL comments) ------------------

AtsVendor = Literal[
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workday",
    "oracle",
    "workable",
    "dover",
    "gem",
    "comeet",
    "recruitee",
    "personio",
    "teamtailor",
    "bamboohr",
    "rippling",
    "breezy",
    "wellfound",
    "jobvite",
    "jazzhr",
    "pinpoint",
    "icims",
    "homerun",
    "join",
    "zohorecruit",
    "polymer",
    "yc_waas",
    "none",
]
ATS_VENDORS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workday",
    "oracle",
    "workable",
    "dover",
    "gem",
    "comeet",
    "recruitee",
    "personio",
    "teamtailor",
    "bamboohr",
    "rippling",
    "breezy",
    "wellfound",
    "jobvite",
    "jazzhr",
    "pinpoint",
    "icims",
    "homerun",
    "join",
    "zohorecruit",
    "polymer",
    "yc_waas",
    "none",
)
"""``ats_boards.vendor``. The spec's DDL comment lists the first six; ``workable`` is
added per PLAN "Decisions locked" (``docs/decisions/0003``): Workday, Oracle and Workable
are detected by discovery but never polled (recorded with ``status='unsupported'``).
``dover`` to ``polymer`` (more ATS vendors) and ``yc_waas`` (YC Work at a Startup, a
listing site) are recorded the same way since the 2026-10-14 recall audit
(``docs/decisions/0030``), and ``none`` is discovery's ``no_board`` coverage marker.
Mirrors ``tell.discovery.ats`` (``SUPPORTED_VENDORS``, ``UNSUPPORTED_VENDORS``,
``NO_BOARD_VENDOR``); ``tests/discovery/test_ats.py`` keeps the two in step. Nothing in
the database constrains the column."""

AtsBoardStatus = Literal["active", "no_board", "dead", "paused", "unsupported"]
ATS_BOARD_STATUSES: tuple[str, ...] = ("active", "no_board", "dead", "paused", "unsupported")
"""``ats_boards.status``: the spec's four values plus ``unsupported`` (PLAN Phase 1)."""

AliasSource = Literal["ats_board", "edgar", "news", "manual"]
ALIAS_SOURCES: tuple[str, ...] = ("ats_board", "edgar", "news", "manual")
"""``company_aliases.source``."""

SignalType = Literal[
    "hiring_velocity", "funding", "leadership", "launch", "expansion", "tech_adoption"
]
SIGNAL_TYPES: tuple[str, ...] = (
    "hiring_velocity",
    "funding",
    "leadership",
    "launch",
    "expansion",
    "tech_adoption",
)
"""``signals.signal_type``: the spec section 8 signal table, which includes ``expansion``
(the section 5 DDL comment omits it). Plain text column, no CHECK constraint."""

FeedbackVerdict = Literal["useful", "not_useful", "already_knew", "worked", "booked", "won"]
FEEDBACK_VERDICTS: tuple[str, ...] = (
    "useful",
    "not_useful",
    "already_knew",
    "worked",
    "booked",
    "won",
)
"""``feedback.verdict``."""

RunStatus = Literal["running", "ok", "failed"]
RUN_STATUSES: tuple[str, ...] = ("running", "ok", "failed")
"""``runs.status``."""

ReviewQueueKind = Literal["entity_match", "board_verify", "news_classify"]
REVIEW_QUEUE_KINDS: tuple[str, ...] = ("entity_match", "board_verify", "news_classify")
"""``review_queue.kind``."""

CompanyFeedStatus = Literal["active", "dead", "paused"]
COMPANY_FEED_STATUSES: tuple[str, ...] = ("active", "dead", "paused")
"""``company_feeds.status`` (ADR 0014)."""

ReviewQueueStatus = Literal["open", "resolved", "rejected"]
REVIEW_QUEUE_STATUSES: tuple[str, ...] = ("open", "resolved", "rejected")
"""``review_queue.status``."""

CompanySource = Literal["yc", "issuer_first", "board_first", "manual"]
COMPANY_SOURCES: tuple[str, ...] = ("yc", "issuer_first", "board_first", "manual")
"""``companies.source`` (ADR 0035, migration 0009): where the company came from. ``yc`` is
the YC directory seed (and the column default, so every row before 0009 is ``yc``);
``issuer_first`` a company created from a Form D issuer on site evidence; ``board_first``
one found through a public job board; ``manual`` one added by hand (a review verdict,
``tell discover`` on a domain, an intake list)."""

CikSource = Literal["issuer_first_a", "issuer_first_b", "r1", "guarded_r2", "manual"]
CIK_SOURCES: tuple[str, ...] = ("issuer_first_a", "issuer_first_b", "r1", "guarded_r2", "manual")
"""``companies.cik_source`` (ADR 0035, migration 0009): the trusted path that set
``companies.cik``. Issuer-first discovery on tier A or B site evidence, an R1 link, an R2
link that passed the Form D guards, or a review verdict. Only a CIK with one of these
values feeds rule R0 (``tell.resolve.match``); ``NULL`` means the CIK's origin is unknown
and it is not trusted."""

IssuerDiscoveryStatus = Literal["linked", "tier_c", "no_domain", "conflict", "rejected"]
ISSUER_DISCOVERY_STATUSES: tuple[str, ...] = (
    "linked",
    "tier_c",
    "no_domain",
    "conflict",
    "rejected",
)
"""``issuer_discovery.status`` (ADR 0035)."""

IssuerEvidenceTier = Literal["A", "B", "C"]
ISSUER_EVIDENCE_TIERS: tuple[str, ...] = ("A", "B", "C")
"""``issuer_discovery.tier`` (ADR 0035): A is the full legal name on the site, B the full
brand plus the Form D city or state, C weaker (review or log only)."""

TRUSTED_EVIDENCE_TIERS: frozenset[str] = frozenset({"A", "B"})
"""Tiers whose ``linked`` record counts as site evidence for guard (f) (ADR 0035)."""


# --- base -----------------------------------------------------------------------

NAMING_CONVENTION: dict[str, str] = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
}
"""PostgreSQL's default object names, made explicit so migrations and the DDL agree."""


class Base(DeclarativeBase):
    """Declarative base for every ``tell`` table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """``<ClassName pk=value ...>`` using the mapper's primary-key columns."""
        mapper = self.__mapper__
        keys = [mapper.get_property_by_column(column).key for column in mapper.primary_key]
        parts = " ".join(f"{key}={getattr(self, key)!r}" for key in keys)
        return f"<{type(self).__name__} {parts}>"


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


# --- tables -----------------------------------------------------------------------


class Company(Base):
    """One row per resolved company, keyed by domain."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(Text)
    cik: Mapped[str | None] = mapped_column(Text, unique=True)
    hq_city: Mapped[str | None] = mapped_column(Text)
    hq_state: Mapped[str | None] = mapped_column(Text)
    hq_country: Mapped[str | None] = mapped_column(Text)
    github_org: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )
    yc_status: Mapped[str | None] = mapped_column(Text)
    """YC directory status as listed (``Active``, ``Inactive``, ``Acquired``, ``Public``);
    ``NULL`` for companies not from the directory (migration 0008, ADR 0034)."""
    yc_batch: Mapped[str | None] = mapped_column(Text)
    """YC batch as listed, e.g. ``Summer 2021`` (migration 0008, ADR 0034)."""
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="yc", server_default=text("'yc'")
    )
    """Where the company came from (:data:`COMPANY_SOURCES`; migration 0009, ADR 0035)."""
    cik_source: Mapped[str | None] = mapped_column(Text)
    """The trusted path that set ``cik`` (:data:`CIK_SOURCES`), or ``NULL`` when unknown
    (migration 0009, ADR 0035). Only a CIK with a ``cik_source`` feeds rule R0."""


class CompanyAlias(Base):
    """Normalized alias → company (spec section 6)."""

    __tablename__ = "company_aliases"
    __table_args__ = (PrimaryKeyConstraint("company_id", "alias"),)

    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class AtsBoard(Base):
    """A discovered job board (vendor + identifier) and its polling state."""

    __tablename__ = "ats_boards"
    __table_args__ = (UniqueConstraint("vendor", "identifier"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("companies.id"))
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_from: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    last_verified_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    """Discovery's ownership verification (spec section 4 step 5); collectors never write it."""
    last_polled_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    """Last poll that answered 200; a 404 never advances it (ADR 0015)."""
    not_found_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """404 polls since the last 200 (ADR 0015, migration 0004)."""
    first_not_found_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    """First 404 of the current streak; ``NULL`` when the last poll answered 200."""


class Posting(Base):
    """Current state of every posting ever seen."""

    __tablename__ = "postings"
    __table_args__ = (
        UniqueConstraint("board_id", "external_id"),
        Index(None, "board_id", "fingerprint"),
        Index(None, "role_family", "posted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    board_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ats_boards.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)
    role_family: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    remote: Mapped[bool | None] = mapped_column(Boolean)
    posted_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    first_seen_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    repost_of: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("postings.id"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text)
    version_marker: Mapped[str | None] = mapped_column(Text)
    """Provider change marker last seen (ADR 0013); ``NULL`` only for rows older than 0002."""


class PostingObservation(Base):
    """One row per poll per posting; the audit trail for velocity."""

    __tablename__ = "posting_observations"
    __table_args__ = (PrimaryKeyConstraint("posting_id", "observed_at"),)

    posting_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("postings.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)


class EventRow(Base):
    """The one shape every source becomes (spec section 8); ``collectors.base.Event`` writes it."""

    __tablename__ = "events"
    __table_args__ = (
        # Named by hand: the naming convention cannot see a column inside text("... DESC").
        Index("events_company_id_occurred_at_idx", "company_id", text("occurred_at DESC")),
        Index("events_type_occurred_at_idx", "type", text("occurred_at DESC")),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("companies.id"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    magnitude: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("1"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)


class Signal(Base):
    """A scored signal derived from one event."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("event_id", "signal_type", "role_family"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(Text, ForeignKey("events.event_id"), nullable=False)
    signal_type: Mapped[str] = mapped_column(Text, nullable=False)
    role_family: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    magnitude: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    half_life_days: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)


class Score(Base):
    """Daily score per company and ICP profile, with its contributions."""

    __tablename__ = "scores"
    __table_args__ = (PrimaryKeyConstraint("company_id", "icp_profile", "as_of"),)

    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    icp_profile: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    contributions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


class Watchlist(Base):
    """A named list of companies for one owner and ICP profile.

    ``slug`` and ``is_public`` (migration ``0006``, ADR 0022) decide whether and where
    the list is published as ``api/v0/watchlists/{slug}.json``; private lists (the
    default) are never published. ``owner`` is never published.
    """

    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    icp_profile: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    slug: Mapped[str | None] = mapped_column(Text, unique=True)
    """URL-safe name of a published list (ADR 0022); ``NULL`` for lists never published."""
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Publish this list as ``api/v0/watchlists/{slug}.json`` (ADR 0022)."""


class WatchlistItem(Base):
    """Membership of a company in a watchlist."""

    __tablename__ = "watchlist_items"
    __table_args__ = (PrimaryKeyConstraint("watchlist_id", "company_id"),)

    watchlist_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("watchlists.id"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)


class Feedback(Base):
    """A human verdict on one signal from one watchlist."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("watchlists.id"), nullable=False
    )
    signal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("signals.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )


class Run(Base):
    """One collector run; the run-health page and the dead-man switch read these."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    fetched: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    inserted: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    skipped: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    errors: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ReviewQueueItem(Base):
    """A human-review item (entity match, board verification, news classification)."""

    __tablename__ = "review_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(_timestamptz())


class CompanyFeed(Base):
    """An RSS/Atom feed discovered on a company's own site (ADR 0014, migration 0003).

    The news collector (spec section 3.7) discovers the feed once, then polls it with
    ``If-None-Match`` / ``If-Modified-Since`` built from ``etag`` / ``last_modified``
    and skips it while ``last_polled_at`` is younger than 6 hours.
    """

    __tablename__ = "company_feeds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    discovered_from: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    last_polled_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )


class PostingGhostVote(Base):
    """One job-seeker "this looks like a ghost job" vote on a posting (ADR 0017, migration 0005).

    Anonymous by design: no voter identity is stored, only when and through which
    surface (``source``, e.g. ``site``) the vote arrived. Spec section 7 keeps the
    count inert (it changes no weight) until there are 500 votes.
    """

    __tablename__ = "posting_ghost_votes"
    __table_args__ = (Index(None, "posting_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    posting_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("postings.id"), nullable=False)
    voted_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class ApiKey(Base):
    """A bearer key for the FastAPI v1 service (spec section 10, ADR 0024, migration 0007).

    Only ``sha256(key)`` is stored (``key_hash``), plus the first 12 characters
    (``key_prefix``) so an operator can tell keys apart. ``owner`` is a handle, never an
    email. A key with ``revoked_at`` set never authenticates again.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    daily_quota: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1000, server_default=text("1000")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(_timestamptz())


class ApiUsage(Base):
    """Calls per key per UTC day: the metering row upserted before each handler runs."""

    __tablename__ = "api_usage"
    __table_args__ = (PrimaryKeyConstraint("key_id", "day"),)

    key_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("api_keys.id"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class IssuerDiscovery(Base):
    """One Form D issuer that issuer-first discovery has tried (ADR 0035, migration 0009).

    Keyed by the issuer's CIK (10 digits). ``status`` is one of
    :data:`ISSUER_DISCOVERY_STATUSES`, ``tier`` one of :data:`ISSUER_EVIDENCE_TIERS` (or
    ``NULL`` when nothing qualified), ``domain`` the company domain the evidence was found
    on. ``evidence`` holds only ``{"path": ..., "span": ...}``: the page path and the
    matched legal-name string, never surrounding page text (it can hold a phone number or a
    street address). ``attempts`` / ``last_tried_at`` / ``next_retry_at`` schedule the
    retries of a ``no_domain`` issuer. A ``linked`` record at tier A or B is the stored
    site evidence that lets guard (f) pass a Form D auto-link to a company without YC
    facts (``tell.resolve.guards``).
    """

    __tablename__ = "issuer_discovery"
    __table_args__ = (Index(None, "status", "next_retry_at"),)

    cik: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_tried_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    next_retry_at: Mapped[datetime | None] = mapped_column(_timestamptz())
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=text("now()")
    )


class Nudge(Base):
    """One per-event Slack nudge: a crossing event sent to a watchlist's webhook (ADR 0036).

    One row per (event, watchlist). ``status`` is ``ok`` once the message was accepted
    (2xx) or ``failed`` (``http_status`` holds the response code, ``NULL`` on a transport
    error); a ``failed`` row is updated in place when the next run retries. Nothing here
    names a person: the webhook itself stays in ``tell-private``.
    """

    __tablename__ = "nudges"
    __table_args__ = (UniqueConstraint("event_id", "watchlist_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, ForeignKey("events.event_id"), nullable=False)
    watchlist_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("watchlists.id"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)


ALL_MODELS: tuple[type[Base], ...] = (
    Company,
    CompanyAlias,
    AtsBoard,
    Posting,
    PostingObservation,
    EventRow,
    Signal,
    Score,
    Watchlist,
    WatchlistItem,
    Feedback,
    Run,
    ReviewQueueItem,
    CompanyFeed,
    PostingGhostVote,
    ApiKey,
    ApiUsage,
    IssuerDiscovery,
    Nudge,
)
"""Every mapped class, in DDL order then the recorded additions (tests, TRUNCATE lists)."""
