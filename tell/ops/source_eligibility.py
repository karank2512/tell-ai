"""Source eligibility: a pure CANDIDATE for the news source exclusion, partly wired.

Review candidate (``docs/review/source-exclusion-retention-design.md``). First local
slice: the JSONL publish/archive writer and the prune delete hold excluded rows; the
Slack crossing list and HubSpot receipts fail closed; ``tell publish``/``prune``/
``notify`` refuse to run while :data:`WIRED` is ``False`` (it is). Scoring inputs, the
site, the JSON API, the weekly page and the CSV export are NOT filtered yet, which is
why :data:`WIRED` stays ``False``. This module itself reads nothing, writes nothing and
opens no connection; every function here takes plain values and returns a decision.

It keeps four concerns apart:

1. **Exclusion** (:func:`classify_event`, :func:`classify_crossing`): whether a stored row
   may feed scores, exports, notifications or reports. A read-time decision. It never
   changes or deletes the row.
2. **Retention** (:func:`archive_disposition`): what the size-budget prune may do with a
   row. An excluded row is HELD: it is never archived to the public ``tell-data`` JSONL
   and never becomes eligible for deletion. That is a holding state, not a retention
   policy; the owner's choice is still open (see the design doc).
3. **Lineage and audit** (:class:`Decision`, :class:`ExclusionAudit`): every decision
   names its provenance, its reason and the policy version that produced it.
4. **Log hygiene** (:meth:`ExclusionAudit.to_log_line`): counts and enum values only.
   No URL, headline, query string, payload value or secret ever reaches a log line.

Provenance is identified from three markers the news collector has written since its
first commit (``tell/collectors/news.py``, ``derive_events``): ``payload.match.rule``,
the host of ``payload.query`` (the request URL) and the host of the item ``url``. A row
counts as Google News only when all three agree. A row where they disagree, or where one
is missing, is ``unknown`` and excluded. The publisher label (``payload.source``) is never
used: it is the feed's own text, not provenance.

Permission is separate from provenance. Google News is excluded whatever the permits
say; changing that needs a reviewed code change, not a setting. A company feed is
eligible only when a :class:`FeedPermit` for its exact feed URL is in :data:`PERMITS`, a
code constant that is empty. No environment variable or config file is read.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

__all__ = [
    "ELIGIBILITY_CUTOVER",
    "INDEPENDENT_SOURCES",
    "PERMITS",
    "POLICY_VERSION",
    "STAMP_TOP_N",
    "WIRED",
    "ArchiveDisposition",
    "Decision",
    "ExclusionAudit",
    "FeedPermit",
    "FetchPlan",
    "NewsCoverage",
    "Provenance",
    "Reason",
    "StoredScore",
    "archive_disposition",
    "cached_output_trusted",
    "classify_crossing",
    "classify_event",
    "classify_stamped_crossing",
    "eligible_signal_ids",
    "news_coverage",
    "news_fetch_plan",
    "outputs_blocked",
    "stamped_signal_ids",
]

WIRED: Final[bool] = False
"""Whether every output path (publish, prune, notify) reads only eligible rows.

A code constant, never a setting: no environment variable or config file can flip it.
While it is not ``True``, ``tell publish``, ``tell prune``, ``tell notify`` and the
``tell weekly`` alias exit 2 before opening any database session or HTTP client
(:func:`outputs_blocked`). Flipping it is a reviewed code change made only once the
remaining output paths (site, JSON API, weekly page, CSV, scoring inputs) filter through
this module; see the change summary for the next slice.
"""

STAMP_TOP_N: Final = 5
"""Contributions copied into a stamped crossing's ``payload.contributions`` (proposed N)."""

ELIGIBILITY_CUTOVER: Final[date | None] = None
"""First scored day whose stored ``scores`` row may be shown as a receipt (HubSpot).

``None`` means no cutover has been chosen, so no stored score is trusted and
``load_receipt`` fails closed. Setting a date is the owner's reviewed choice, made only
after scoring reads eligible signals alone.
"""

POLICY_VERSION: Final = "source-exclusion/1"
"""Stamped on every decision. A cached output made under another version (or none) is untrusted."""

INDEPENDENT_SOURCES: Final[frozenset[str]] = frozenset(
    {"edgar", "greenhouse", "lever", "ashby", "smartrecruiters", "github"}
)
"""``events.source`` values written by collectors that never read news (verified 2026-11-17)."""

NEWS_SOURCE: Final = "news"
TELL_SOURCE: Final = "tell"
HIRING_VELOCITY: Final = "hiring_velocity"
"""``source='tell'`` type derived only from ATS postings (``tell/signals/velocity.py``)."""
SCORE_CROSSED_THRESHOLD: Final = "score_crossed_threshold"
"""``source='tell'`` type derived from stored scores; eligible only through its lineage."""

GOOGLE_RULE: Final = "news_query"
COMPANY_FEED_RULES: Final[frozenset[str]] = frozenset({"R1", "company_feed"})

_SCORE_TOLERANCE: Final = 1e-9


class Provenance(StrEnum):
    INDEPENDENT = "independent"
    GOOGLE_NEWS = "google_news"
    COMPANY_FEED = "company_feed"
    DERIVED = "derived"
    UNKNOWN = "unknown"


class Reason(StrEnum):
    INDEPENDENT_SOURCE = "independent_source"
    GOOGLE_NEWS_EXCLUDED = "google_news_excluded"
    NO_FEED_PERMIT = "no_feed_permit"
    FEED_PERMITTED = "feed_permitted"
    PROVENANCE_CONFLICT = "provenance_conflict"
    PROVENANCE_UNKNOWN = "provenance_unknown"
    UNRECOGNIZED_SOURCE = "unrecognized_source"
    LINEAGE_REQUIRED = "lineage_required"
    LINEAGE_INCOMPLETE = "lineage_incomplete"
    LINEAGE_RESTRICTED = "lineage_restricted"
    LINEAGE_CLEAN = "lineage_clean"


_ELIGIBLE_REASONS: Final[frozenset[Reason]] = frozenset(
    {Reason.INDEPENDENT_SOURCE, Reason.FEED_PERMITTED, Reason.LINEAGE_CLEAN}
)


@dataclass(frozen=True)
class Decision:
    """One row's eligibility. Holds no payload, URL or headline (safe to log by design)."""

    provenance: Provenance
    reason: Reason
    policy_version: str = POLICY_VERSION

    @property
    def eligible(self) -> bool:
        return self.reason in _ELIGIBLE_REASONS


@dataclass(frozen=True)
class FeedPermit:
    """A recorded, feed-specific permission to store and republish one company feed.

    ``evidence_ref`` is a repo-relative path under ``docs/`` to the reviewed evidence. A
    URL is refused, so a permit can never carry a credential or point at unreviewed text.
    """

    feed_url: str
    evidence_ref: str
    recorded_on: date

    def __post_init__(self) -> None:
        host = _host(self.feed_url)
        parts = urlsplit(self.feed_url)
        if parts.scheme != "https" or host is None or parts.username or parts.password:
            msg = "feed_url must be an https URL with a host and no credentials"
            raise ValueError(msg)
        if _is_google_host(host):
            msg = "a FeedPermit cannot cover a Google host; Google News needs a code change"
            raise ValueError(msg)
        ref = self.evidence_ref
        if not ref.startswith("docs/") or "://" in ref or ".." in ref.split("/"):
            msg = "evidence_ref must be a repo-relative path under docs/"
            raise ValueError(msg)


PERMITS: Final[tuple[FeedPermit, ...]] = ()
"""No company feed has recorded permission (source-safety review, 2026-11-16). Empty on purpose."""


# --- provenance -----------------------------------------------------------------------------


def _host(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _is_google_host(host: str) -> bool:
    return host == "google.com" or host.endswith(".google.com")


def _news_provenance(url: object, payload: Mapping[str, Any]) -> tuple[Provenance, Reason | None]:
    match = payload.get("match")
    rule = match.get("rule") if isinstance(match, Mapping) else None
    query_host = _host(payload.get("query"))
    url_host = _host(url)
    if not isinstance(rule, str) or query_host is None or url_host is None:
        return Provenance.UNKNOWN, Reason.PROVENANCE_UNKNOWN
    google_markers = (
        rule == GOOGLE_RULE,
        _is_google_host(query_host),
        _is_google_host(url_host),
    )
    if all(google_markers):
        return Provenance.GOOGLE_NEWS, None
    if any(google_markers):
        return Provenance.UNKNOWN, Reason.PROVENANCE_CONFLICT
    if rule in COMPANY_FEED_RULES:
        return Provenance.COMPANY_FEED, None
    return Provenance.UNKNOWN, Reason.PROVENANCE_UNKNOWN


def classify_event(
    source: str,
    type_: str,
    url: object,
    payload: Mapping[str, Any] | None,
    *,
    permits: Sequence[FeedPermit] = PERMITS,
) -> Decision:
    """Classify one stored event. Never reads or changes anything.

    A ``score_crossed_threshold`` row is excluded here (``lineage_required``): only
    :func:`classify_crossing`, given its stored score and signal decisions, can clear it.
    """
    if source in INDEPENDENT_SOURCES:
        return Decision(Provenance.INDEPENDENT, Reason.INDEPENDENT_SOURCE)
    if source == TELL_SOURCE:
        return _TELL_TYPES.get(type_, _UNRECOGNIZED)
    if source == NEWS_SOURCE:
        return _news_decision(url, payload if isinstance(payload, Mapping) else {}, permits)
    return _UNRECOGNIZED


_UNRECOGNIZED: Final = Decision(Provenance.UNKNOWN, Reason.UNRECOGNIZED_SOURCE)
_TELL_TYPES: Final[Mapping[str, Decision]] = {
    HIRING_VELOCITY: Decision(Provenance.INDEPENDENT, Reason.INDEPENDENT_SOURCE),
    SCORE_CROSSED_THRESHOLD: Decision(Provenance.DERIVED, Reason.LINEAGE_REQUIRED),
}


def _news_decision(
    url: object, payload: Mapping[str, Any], permits: Sequence[FeedPermit]
) -> Decision:
    provenance, problem = _news_provenance(url, payload)
    if problem is not None:
        return Decision(provenance, problem)
    if provenance is Provenance.GOOGLE_NEWS:
        return Decision(provenance, Reason.GOOGLE_NEWS_EXCLUDED)
    permitted = any(p.feed_url == payload.get("query") for p in permits)
    return Decision(provenance, Reason.FEED_PERMITTED if permitted else Reason.NO_FEED_PERMIT)


# --- descendants --------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredScore:
    """The ``scores`` row a crossing cites: its value and every contributing signal id."""

    score: float
    signal_ids: tuple[int, ...]


def _signal_id(item: object) -> int | None:
    if not isinstance(item, Mapping):
        return None
    value = item.get("signal_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def classify_crossing(
    payload: Mapping[str, Any],
    stored: StoredScore | None,
    signal_decisions: Mapping[int, Decision],
) -> Decision:
    """Clear a ``score_crossed_threshold`` event only when its whole lineage is known and clean.

    The crossing payload keeps only its top two contributions, so the full set comes from
    the stored ``scores`` row for that day. That row is upserted on a rescore, so it is
    trusted only if its score still equals the crossing's and it still holds the cited top
    signals. Anything missing or inconsistent is ``lineage_incomplete``, never assumed clean.
    Any restricted contributor, however small, makes the crossing ``lineage_restricted``:
    without it the threshold might not have been crossed.
    """
    decisions = (
        [signal_decisions.get(i) for i in stored.signal_ids]
        if stored is not None and _consistent(payload, stored)
        else [None]
    )
    if any(d is None for d in decisions):
        reason = Reason.LINEAGE_INCOMPLETE
    elif any(d is not None and not d.eligible for d in decisions):
        reason = Reason.LINEAGE_RESTRICTED
    else:
        reason = Reason.LINEAGE_CLEAN
    return Decision(Provenance.DERIVED, reason)


def _consistent(payload: Mapping[str, Any], stored: StoredScore) -> bool:
    """Whether the stored score row is still the one the crossing was computed from."""
    crossed = _finite(payload.get("score"))
    recorded = _finite(stored.score)
    top = payload.get("top_contributions")
    if (
        not stored.signal_ids
        or crossed is None
        or recorded is None
        # Both finite here, so the difference is finite or +inf (fails), never NaN.
        or abs(crossed - recorded) > _SCORE_TOLERANCE
        or not isinstance(top, list)
        or not top
    ):
        return False
    top_ids = [_signal_id(item) for item in top]
    return all(i is not None for i in top_ids) and set(top_ids) <= set(stored.signal_ids)


def _finite(value: object) -> float | None:
    """``value`` as a finite float, or None for NaN, ±Inf, bool, non-numbers and huge ints.

    NaN compares false with everything and ``inf - inf`` is NaN, so without this a
    ``tolerance`` check on non-finite scores passed and a crossing was cleared.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def stamped_signal_ids(payload: object) -> tuple[int, ...] | None:
    """The full contributor list a stamped crossing carries, or None when it is malformed.

    Accepts only a non-empty list of distinct ints (bools refused). Never reads ``scores``.
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("signal_ids")
    if not isinstance(raw, list) or not raw:
        return None
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        ids.append(item)
    if len(set(ids)) != len(ids):
        return None
    return tuple(ids)


def classify_stamped_crossing(
    payload: object, signal_decisions: Mapping[int, Decision]
) -> Decision:
    """Clear a crossing from its own stamp: ``policy_version``, ``signal_ids``, ``contributions``.

    Unlike :func:`classify_crossing` this never reads the ``scores`` row, which a
    same-day rescore upserts; the stamp is written once with the crossing and the event
    is never rewritten. Clean only when: the stamp is this policy version; every cited
    signal id is known and eligible *now*; every stamped contribution cites one of those
    ids with a finite value; and the crossing's score is finite. An unstamped (legacy)
    crossing is always ``lineage_incomplete``: it is dropped from outputs, not repaired.
    """
    ids = _intact_stamp(payload)
    decisions = [signal_decisions.get(i) for i in ids] if ids is not None else [None]
    if any(d is None for d in decisions):
        return Decision(Provenance.DERIVED, Reason.LINEAGE_INCOMPLETE)
    if any(d is not None and not d.eligible for d in decisions):
        return Decision(Provenance.DERIVED, Reason.LINEAGE_RESTRICTED)
    return Decision(Provenance.DERIVED, Reason.LINEAGE_CLEAN)


def _intact_stamp(payload: object) -> tuple[int, ...] | None:
    """The stamped ``signal_ids`` when the whole stamp is present and consistent, else None."""
    if not isinstance(payload, Mapping):
        return None
    version = payload.get("policy_version")
    ids = stamped_signal_ids(payload)
    top = payload.get("contributions")
    if (
        not cached_output_trusted(version if isinstance(version, str) else None)
        or ids is None
        or _finite(payload.get("score")) is None
        or not isinstance(top, list)
        or not top
    ):
        return None
    for item in top:
        cited = _signal_id(item)
        if cited is None or cited not in ids:
            return None
        if isinstance(item, Mapping) and _finite(item.get("contribution")) is None:
            return None
    return ids


def outputs_blocked() -> bool:
    """True unless :data:`WIRED` is exactly ``True`` (read at call time, never from settings)."""
    return WIRED is not True


def eligible_signal_ids(signal_decisions: Mapping[int, Decision]) -> frozenset[int]:
    """Signal ids a fresh score may read: those whose source event is eligible."""
    return frozenset(i for i, d in signal_decisions.items() if d.eligible)


def cached_output_trusted(policy_version: str | None) -> bool:
    """A stored score, crossing or export is trusted only if made under this policy version.

    Everything stored before this candidate has no version, so none of it is trusted:
    it must be recomputed from eligible inputs or cleared through its lineage.
    """
    return policy_version == POLICY_VERSION


# --- retention (separate from exclusion) ----------------------------------------------------


class ArchiveDisposition(StrEnum):
    LIVE = "live"
    """Eligible and newer than the cutoff: nothing to do."""
    ARCHIVE_THEN_PRUNE = "archive_then_prune"
    """Eligible and older than the cutoff: today's prune behavior, unchanged."""
    HOLD = "hold"
    """Excluded: kept unchanged in the database, never archived publicly, never deleted."""


def archive_disposition(
    decision: Decision, observed_at: datetime, cutoff: datetime
) -> ArchiveDisposition:
    """What the size-budget prune may do with one row. Exclusion never implies deletion.

    HOLD has no age limit. How long a held row stays, and whether it ever leaves, is the
    owner's open retention choice; this function does not decide it. Both datetimes must
    be timezone-aware (``ValueError`` otherwise), so a naive value can never shift a row
    across the cutoff.
    """
    for name, value in (("observed_at", observed_at), ("cutoff", cutoff)):
        if value.tzinfo is None or value.utcoffset() is None:
            msg = f"{name} must be timezone-aware, got naive {value!r}"
            raise ValueError(msg)
    if not decision.eligible:
        return ArchiveDisposition.HOLD
    if observed_at < cutoff:
        return ArchiveDisposition.ARCHIVE_THEN_PRUNE
    return ArchiveDisposition.LIVE


# --- collector gate and coverage ------------------------------------------------------------


class NewsCoverage(StrEnum):
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


@dataclass(frozen=True)
class FetchPlan:
    """What a news run may fetch. Built from code constants only, never from settings."""

    google_news: bool
    company_feed_urls: frozenset[str]

    @property
    def runs(self) -> bool:
        return self.google_news or bool(self.company_feed_urls)


def news_fetch_plan(permits: Sequence[FeedPermit] = PERMITS) -> FetchPlan:
    """Google News is never fetched; only permitted feed URLs are; no permits, no run."""
    return FetchPlan(google_news=False, company_feed_urls=frozenset(p.feed_url for p in permits))


def news_coverage(permits: Sequence[FeedPermit] = PERMITS) -> tuple[NewsCoverage, str]:
    """Coverage follows from policy, not from row counts: zero news rows is not zero news."""
    if not permits:
        return NewsCoverage.UNAVAILABLE, "google_news_excluded;no_company_feed_permits"
    return NewsCoverage.PARTIAL, "google_news_excluded;company_feeds_permitted_only"


# --- audit ----------------------------------------------------------------------------------


@dataclass
class ExclusionAudit:
    """Counts of decisions for one evaluation pass, safe to log.

    Event ids of excluded rows are kept for a private audit artifact (:meth:`excluded_ids`)
    but are never written to the log line.
    """

    permits: Sequence[FeedPermit] = PERMITS
    _reasons: Counter[str] = field(default_factory=Counter)
    _provenance: Counter[str] = field(default_factory=Counter)
    _excluded: list[str] = field(default_factory=list)

    def add(self, event_id: str, decision: Decision) -> None:
        self._reasons[decision.reason.value] += 1
        self._provenance[decision.provenance.value] += 1
        if not decision.eligible:
            self._excluded.append(event_id)

    def extend(self, items: Iterable[tuple[str, Decision]]) -> None:
        for event_id, decision in items:
            self.add(event_id, decision)

    def excluded_ids(self) -> tuple[str, ...]:
        return tuple(self._excluded)

    def to_log_dict(self) -> dict[str, Any]:
        coverage, why = news_coverage(self.permits)
        evaluated = sum(self._reasons.values())
        return {
            "policy_version": POLICY_VERSION,
            "evaluated": evaluated,
            "eligible": evaluated - len(self._excluded),
            "excluded": len(self._excluded),
            "by_reason": dict(sorted(self._reasons.items())),
            "by_provenance": dict(sorted(self._provenance.items())),
            "news_coverage": coverage.value,
            "news_coverage_reason": why,
            "permits": len(self.permits),
        }

    def to_log_line(self) -> str:
        return json.dumps(self.to_log_dict(), sort_keys=True, separators=(",", ":"))
