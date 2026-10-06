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
three recorded deviations. ``postings.version_marker`` (migration ``0002``,
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
``api_keys`` / ``api_usage`` arrive with Phase 9 in their own migration.
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
    "greenhouse", "lever", "ashby", "smartrecruiters", "workday", "oracle", "workable"
]
ATS_VENDORS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workday",
    "oracle",
    "workable",
)
"""``ats_boards.vendor``. The spec's DDL comment lists the first six; ``workable`` is
added per PLAN "Decisions locked" (``docs/decisions/0003``): Workday, Oracle and Workable
are detected by discovery but never polled (recorded with ``status='unsupported'``)."""

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
    """A named list of companies for one owner and ICP profile."""

    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    icp_profile: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))


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
)
"""Every mapped class, in DDL order then the recorded additions (tests, TRUNCATE lists)."""
