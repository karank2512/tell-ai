"""News collector: Google News RSS per company plus the company's own blog feed (SPEC 3.7).

Two feeds per company, both parsed with ``feedparser`` and then validated with
:class:`NewsFeed` (the contract model):

* **Google News RSS**, ``GET https://news.google.com/rss/search?q=%22{name}%22+when%3A7d
  &hl=en-US&gl=US&ceid=US:en``. An item is kept only when the company's name
  (casefolded, punctuation stripped, trailing legal suffix dropped) appears in its
  headline; the rest are counted as skipped. Kept items carry
  ``payload.match = {"rule": "news_query", ...}``.
* **The company's own feed**, discovered once (:func:`discover_feed`) from
  ``https://{domain}/blog`` and the homepage (``<link rel="alternate">`` with an RSS or
  Atom type), then the fallbacks ``/feed``, ``/rss``, ``/blog/rss.xml``,
  ``/blog/feed``; the first answer that ``feedparser`` reads with at least one entry
  wins and is stored in ``company_feeds`` (ADR 0014). Polls send ``If-None-Match`` /
  ``If-Modified-Since`` from the stored validators. Items from a feed on the
  company's own domain are linked with ``payload.match = {"rule": "R1",
  "confidence": 0.98}`` (SPEC section 6).

Every kept headline goes through :func:`tell.collectors.news_classify.classify`;
unclassified items are not stored (counted as skipped). One event per article URL:
``external_id = url``, ``version = "1"``, so ``event_id = sha256("news|<url>|1")``;
the SPEC key ``news:{sha256(url)}`` is in ``payload.key``. The payload is exactly
``title, title_redacted, source, matched_regex, category, query, match, key``: never
article text. Leadership headlines are stored redacted
(:func:`tell.collectors.news_classify.redact_leadership`, ADR 0016, provisional):
``title`` has the person's name replaced with ``[person]`` (or is only the role
phrase) and ``title_redacted`` is ``true``; every other category stores the title
unchanged with ``title_redacted = false``.

Politeness, two separate budgets, no retries anywhere. Google News is queried one
company at a time at ``TELL_RATE_NEWS_RPS`` (1 request/second). A 429 or 503 from
Google News is a block: the run stops at once and finishes ``failed`` with
``runs.notes["blocked"] = true``, so the SPEC section 11 dead-man notices a block that
persists (a block is a signal to slow down, not to retry). ``tell ingest --source news``
still exits 0 when the block is the run's only failure (:func:`is_block_only_failure`),
so one block does not turn the scheduled ingest job red. Company sites (discovery,
feed polls) are worked on by :data:`SITE_WORKERS` threads at :data:`SITE_RPS` per host
with a :data:`SITE_TIMEOUT_S` timeout, redirects charged hop by hop to each host's
budget; the first transport error or 429/503 ends a
site's discovery. A company feed is not re-polled before
``company_feeds.last_polled_at + 6h``; discovery runs at most once per company per
:data:`DISCOVERY_RETRY` (30 days): a miss, including a site that never answered, is
recorded in ``runs.notes["no_feed"]`` (ADR 0014, ADR 0032) and is never an error.

Transactions: :func:`ingest` commits the run row at once and then after every company
(Google News answer and finished site work) and before every wait for a site worker,
keeping the open run row's counters and notes current, so no transaction is held across network I/O and a killed run keeps its
work. Worker threads never touch the database.

Rotation (PLAN Phase 4, SPEC section 11), read from ``runs.notes["polled"]`` of the
news runs of the last 7 days: watchlist members and companies with any event in
the last 30 days are polled at most once per 24 hours; the long tail only on its
weekday (``company id % 7 == now.weekday()``, UTC) and at most once per 7 days. Both
intervals allow one hour of slack for cron start jitter. Explicit ``company_ids``
replace the rotation and only respect the 6-hour cache.

Budget per run: at most :data:`MAX_COMPANIES` companies (watchlist members first,
then the active companies, then the long tail; least recently polled first within
each group), :data:`MAX_GOOGLE_REQUESTS` (600) Google News requests and
:data:`RUN_BUDGET` of wall-clock time. A run that hits any of them stops cleanly, keeps
what it wrote, finishes ``ok`` and says so in ``runs.notes`` (``deferred``,
``request_cap_reached``, ``budget_exhausted``).

Cohort runs (SPEC 13.1): ``when_days`` widens the Google News window (``when:120d``)
and ``cohort="formd"`` polls the companies linked to a ``form_d_filed`` event in the
last 90 days (:func:`formd_cohort`) instead of the rotation, under the same cache,
budgets, block policy and redaction. ``runs.notes["when_days"]`` records the window, and
a run only counts as "recently polled" for a later run asking for an equal or narrower
window, so a scheduled 7-day poll never hides a company from the 120-day cohort run.

:func:`ingest` is the entry point for ``tell ingest --source news``: it opens the
run, calls :func:`collect` and closes the run with the notes the next run reads.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote_plus, urljoin, urlsplit

import feedparser
import httpx
import lxml.html
from lxml import etree
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tell.collectors.base import (
    CollectorSpec,
    Event,
    RunResult,
    finish_run,
    iso_utc,
    parse_iso,
    sha256_hex,
    start_run,
    utcnow,
    write_events,
)
from tell.collectors.edgar_formd import FORM_D_FILED
from tell.collectors.news_classify import classify, event_type, redact_leadership
from tell.config import get_settings
from tell.http import HostRateBudgets, RateBudget, RetryExhausted, fetch, make_client
from tell.models import Company, CompanyFeed, EventRow, Run, WatchlistItem

__all__ = [
    "COHORTS",
    "COHORT_DAYS",
    "COHORT_WHEN_DAYS",
    "COHORT_WINDOW",
    "COLLECTOR",
    "DEFAULT_WHEN_DAYS",
    "FALLBACK_PATHS",
    "MAX_COMPANIES",
    "MAX_GOOGLE_REQUESTS",
    "MAX_WHEN_DAYS",
    "RUN_BUDGET",
    "SITE_BACKLOG",
    "SITE_MAX_REDIRECTS",
    "SITE_RPS",
    "SITE_TIMEOUT_S",
    "SITE_WORKERS",
    "SOURCE",
    "Commit",
    "CompanyRef",
    "DiscoveredFeed",
    "NewsFeed",
    "NewsItem",
    "NewsRunResult",
    "collect",
    "company_key",
    "derive_events",
    "discover_feed",
    "feed_links",
    "formd_cohort",
    "google_news_url",
    "headline",
    "ingest",
    "is_block_only_failure",
    "last_polled",
    "name_in_title",
    "news_key",
    "on_domain",
    "parse_feed_items",
    "recently_polled",
    "run_fixture",
    "select_companies",
]

SOURCE: Final = "news"
GOOGLE_NEWS_HOST: Final = "news.google.com"
FIXTURE_DIR: Final = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE

CACHE_TTL: Final = timedelta(hours=6)
"""A company feed (or an explicitly requested company) polled less than this long ago
is skipped."""
ACTIVE_WINDOW: Final = timedelta(days=30)
"""Companies with any event in this window are priority companies (SPEC section 11)."""
ROTATION_DAYS: Final = 7
"""The long tail is polled once a week: ``company id % 7 == now.weekday()``."""
PRIORITY_INTERVAL: Final = timedelta(hours=24)
"""Watchlist members and active companies are polled at most once per this interval."""
LONG_TAIL_INTERVAL: Final = timedelta(days=ROTATION_DAYS)
"""Long-tail companies are polled at most once per this interval."""
SCHEDULE_SLACK: Final = timedelta(hours=1)
"""Taken off both intervals so cron start jitter does not push a poll a whole run later."""
POLL_LOOKBACK: Final = timedelta(days=ROTATION_DAYS)
"""How far back the ``runs.notes["polled"]`` history is read."""
MAX_COMPANIES: Final = 1200
"""Companies per run. Each costs one Google News query, so :data:`MAX_GOOGLE_REQUESTS`
(600) is the tighter bound on a scheduled run; this cap still applies when a caller
raises the request cap (a manual cohort rerun)."""
MAX_GOOGLE_REQUESTS: Final = 600
"""Google News requests per run (one per company polled; no retries). Four scheduled runs
a day stay at or under 2,400 requests, inside the SPEC section 14 guidance of under
3,000 a day. A run that reaches it stops cleanly like :data:`MAX_COMPANIES` and says so in
``runs.notes`` (``google_requests``, ``request_cap_reached``)."""
RUN_BUDGET: Final = timedelta(minutes=25)
"""Wall-clock budget per run; the run stops cleanly before the next company after it."""
DISCOVERY_RETRY: Final = timedelta(days=30)
"""A company whose discovery found no feed (or whose site never answered) is not probed
again for this long; read from ``runs.notes["no_feed"]``."""

SITE_RPS: Final = 1.0
"""Requests per second per company host (discovery and feed polls), independent of the
Google News budget (``TELL_RATE_NEWS_RPS``)."""
SITE_TIMEOUT_S: Final = 10.0
"""Timeout of every company-site request; the first one that fails ends that site's
discovery."""
SITE_MAX_REDIRECTS: Final = 5
"""Redirect hops one company-site request follows; each hop is charged to its host's
:data:`SITE_RPS` budget."""
SITE_WORKERS: Final = 6
"""Company sites worked on at once (worker threads; the database writer stays single)."""
SITE_BACKLOG: Final = 2
"""Outstanding site jobs per worker before the Google News loop waits for one to finish."""
PROGRESS_EVERY: Final = 20
"""The open run row's notes are refreshed every this many companies (counters every one)."""

DEFAULT_WHEN_DAYS: Final = 7
"""Google News ``when:`` window of the scheduled runs (SPEC 3.7)."""
COHORT_WHEN_DAYS: Final = 120
"""Google News ``when:`` window of the SPEC 13.1 cohort run (``--cohort formd``)."""
MAX_WHEN_DAYS: Final = 365
COHORT_DAYS: Final = 90
"""The ``formd`` cohort: companies linked to a ``form_d_filed`` event this many days old or
newer (the 13.1 cohort window)."""
COHORT_WINDOW: Final = timedelta(days=COHORT_DAYS)
COHORTS: Final[tuple[str, ...]] = ("formd",)

BLOCK_STATUSES: Final = frozenset({429, 503})
"""Google News answers that mean "slow down": the run stops, nothing is retried."""
DEAD_STATUSES: Final = frozenset({404, 410})
"""Company-feed answers that mark the ``company_feeds`` row ``dead``."""
SITE_STOP_STATUSES: Final = frozenset({429, 503})
"""Company-site answers that end that site's discovery at once (it asked us to slow down)."""

DiscoveryOutcome = Literal["found", "no_feed", "unreachable"]
"""How one company's feed discovery ended; ``no_feed`` and ``unreachable`` are not errors."""

Commit = Callable[[Session], None]
"""Makes everything written so far durable; :func:`ingest` passes ``Session.commit``."""

SITE_COUNTERS: Final[tuple[str, ...]] = (
    "discovered",
    "no_feed",
    "unreachable",
    "discovery_not_due",
    "feeds_polled",
    "not_modified",
    "dead",
    "feeds_cached",
)
"""``runs.notes["site"]`` keys: what the company-site work did this run."""

FEED_TYPES: Final = frozenset({"application/rss+xml", "application/atom+xml"})
FALLBACK_PATHS: Final[tuple[str, ...]] = ("/feed", "/rss", "/blog/rss.xml", "/blog/feed")
FEED_ACCEPT: Final = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
)
HTML_ACCEPT: Final = "text/html, application/xhtml+xml;q=0.9, */*;q=0.5"

NEWS_QUERY_MATCH: Final[Mapping[str, Any]] = {"rule": "news_query", "guard": "name_in_title"}
R1_MATCH: Final[Mapping[str, Any]] = {"rule": "R1", "confidence": 0.98}
OFF_DOMAIN_MATCH: Final[Mapping[str, Any]] = {"rule": "company_feed"}

_logger = logging.getLogger("tell.collectors.news")

# --- contract model -------------------------------------------------------------------


class CompanyRef(BaseModel):
    """The company a feed was fetched for (the fixture's context; no database id)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    domain: str = Field(min_length=1)


class NewsItem(BaseModel):
    """One feed item as ``feedparser`` reads it: the only fields tell keeps.

    ``source`` is Google News' ``<source>`` text (the publisher) or the company feed's
    title; it is required but may be ``null``. Article text is never read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1)
    link: str = Field(min_length=1)
    pub_date: AwareDatetime
    source: str | None


class NewsFeed(BaseModel):
    """One parsed feed: the ``COLLECTOR.response_model`` and the JSON fixture shape."""

    model_config = ConfigDict(extra="forbid")

    origin: Literal["google_news", "company_feed"]
    feed_url: str = Field(min_length=1)
    company: CompanyRef
    items: list[NewsItem]


# --- URLs, names, parsing ---------------------------------------------------------------


def google_news_url(company_name: str, when_days: int = DEFAULT_WHEN_DAYS) -> str:
    """The Google News RSS search URL for ``"<company name>" when:{N}d`` (SPEC 3.7).

    ``when_days`` is 7 for the scheduled runs and 120 for the SPEC 13.1 cohort run.
    """
    if not 1 <= when_days <= MAX_WHEN_DAYS:
        msg = f"when_days must be between 1 and {MAX_WHEN_DAYS}, got {when_days}"
        raise ValueError(msg)
    query = quote_plus(f'"{company_name}" when:{when_days}d')
    return f"https://{GOOGLE_NEWS_HOST}/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def news_key(url: str) -> str:
    """The SPEC idempotency key ``news:{sha256(url)}``, kept in ``payload.key``."""
    return f"news:{sha256_hex(url)}"


_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_LEGAL_SUFFIXES: Final = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh"}
)


def _normalize(text: str) -> str:
    """NFKC, casefold, punctuation to spaces, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NON_WORD.sub(" ", folded).split())


def company_key(name: str) -> str:
    """The company name as the name guard looks for it: normalized, legal suffix dropped.

    ``"Acme Robotics, Inc."`` becomes ``"acme robotics"``; a one-word name keeps its
    word even when it looks like a suffix.
    """
    words = _normalize(name).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def name_in_title(company_name: str, title: str) -> bool:
    """``True`` when the company's name appears in ``title`` as whole words."""
    key = company_key(company_name)
    if not key:
        return False
    return f" {key} " in f" {_normalize(title)} "


def headline(item: NewsItem) -> str:
    """The title without Google News' trailing `` - <source>`` publisher suffix."""
    if item.source:
        suffix = f" - {item.source}"
        if item.title.endswith(suffix) and len(item.title) > len(suffix):
            return item.title[: -len(suffix)].rstrip()
    return item.title


def on_domain(url: str, domain: str) -> bool:
    """``True`` when ``url``'s host is ``domain`` or one of its subdomains."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    target = domain.lower().strip().rstrip(".")
    return bool(target) and (host == target or host.endswith(f".{target}"))


def _struct_to_utc(value: time.struct_time | None) -> datetime | None:
    """``feedparser``'s ``*_parsed`` (a UTC ``struct_time``) to an aware datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def parse_feed_items(content: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Parse an RSS/Atom document; return ``(item dicts, feed title)``.

    Each item dict holds ``title``, ``link``, ``pub_date`` (ISO 8601, UTC) and
    ``source`` (Google News' ``<source>`` text, else ``None``). A missing title, link
    or date leaves that key out so :class:`NewsFeed` names the missing field.
    Descriptions and article text are never read.
    """
    parsed = feedparser.parse(content)
    items: list[dict[str, Any]] = []
    for entry in parsed.entries:
        item: dict[str, Any] = {}
        title = _text(entry.get("title"))
        link = _text(entry.get("link"))
        when = _struct_to_utc(entry.get("published_parsed") or entry.get("updated_parsed"))
        if title is not None:
            item["title"] = title
        if link is not None:
            item["link"] = link
        if when is not None:
            item["pub_date"] = iso_utc(when).replace("+00:00", "Z")
        source = entry.get("source")
        item["source"] = _text(source.get("title")) if isinstance(source, Mapping) else None
        items.append(item)
    feed_title = _text(parsed.feed.get("title")) if isinstance(parsed.feed, Mapping) else None
    return items, feed_title


def feed_links(html: bytes, base_url: str) -> list[str]:
    """Absolute URLs of ``<link rel="alternate">`` RSS/Atom feeds in an HTML page."""
    if not html.strip():
        return []
    try:
        document = lxml.html.document_fromstring(html)
    except (etree.ParserError, ValueError):
        return []
    found: list[str] = []
    for link in document.iter("link"):
        rel = (link.get("rel") or "").lower().split()
        type_ = (link.get("type") or "").split(";")[0].strip().lower()
        href = (link.get("href") or "").strip()
        if "alternate" in rel and type_ in FEED_TYPES and href:
            url = urljoin(base_url, href)
            if url not in found:
                found.append(url)
    return found


# --- events -------------------------------------------------------------------------------


def _match_for(feed: NewsFeed) -> Mapping[str, Any]:
    if feed.origin == "google_news":
        return NEWS_QUERY_MATCH
    return R1_MATCH if on_domain(feed.feed_url, feed.company.domain) else OFF_DOMAIN_MATCH


def derive_events(
    feed: NewsFeed, *, company_id: int | None, run_id: str, now: datetime
) -> tuple[list[Event], int]:
    """Turn one parsed feed into events; return ``(events, items dropped)``.

    Google News items must name the company in their headline (name guard); every
    item must classify. Dropped items are counted, never stored.
    """
    match = dict(_match_for(feed))
    events: list[Event] = []
    dropped = 0
    for item in feed.items:
        text = headline(item)
        if feed.origin == "google_news" and not name_in_title(feed.company.name, text):
            dropped += 1
            continue
        category, rule = classify(text)
        if category is None or rule is None:
            dropped += 1
            continue
        redacted = category == "leadership"
        payload: dict[str, Any] = {
            "title": redact_leadership(text) if redacted else item.title,
            "title_redacted": redacted,
            "source": item.source,
            "matched_regex": rule,
            "category": category,
            "query": feed.feed_url,
            "match": dict(match),
            "key": news_key(item.link),
        }
        events.append(
            Event(
                source=SOURCE,
                type=event_type(category),
                external_id=item.link,
                version="1",
                occurred_at=item.pub_date.astimezone(UTC),
                observed_at=now,
                url=item.link,
                payload=payload,
                company_id=company_id,
                run_id=run_id,
                magnitude=1,
            )
        )
    return events, dropped


# --- run bookkeeping --------------------------------------------------------------------


@dataclass
class NewsRunResult:
    """What :func:`collect` did; :attr:`notes` is what ``runs.notes`` stores."""

    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    events: list[Event] = field(default_factory=list)
    polled: dict[str, str] = field(default_factory=dict)
    no_feed: list[int] = field(default_factory=list)
    blocked: dict[str, Any] | None = None
    drift: str | None = None
    selected: int = 0
    deferred: int = 0
    budget_exhausted: bool = False
    google_requests: int = 0
    request_cap_reached: bool = False
    when_days: int = DEFAULT_WHEN_DAYS
    cohort: str | None = None
    cohort_size: int | None = None
    cohort_days: int | None = None
    site_workers: int = SITE_WORKERS
    site: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SITE_COUNTERS, 0))

    def record(self, session: Session, fetched: int, events: list[Event], dropped: int) -> None:
        stats = write_events(session, events)
        self.fetched += fetched
        self.inserted += stats.inserted
        self.skipped += dropped + stats.skipped
        self.events.extend(events)

    @property
    def notes(self) -> dict[str, Any]:
        notes: dict[str, Any] = {
            "polled": dict(self.polled),
            "polled_count": len(self.polled),
            "no_feed": list(self.no_feed),
            "selected": self.selected,
            "deferred": self.deferred,
            "budget_exhausted": self.budget_exhausted,
            "google_requests": self.google_requests,
            "request_cap_reached": self.request_cap_reached,
            "blocked": self.blocked is not None,
            "when_days": self.when_days,
            "site": dict(self.site),
            "site_workers": self.site_workers,
        }
        if self.cohort is not None:
            notes["cohort"] = self.cohort
            notes["cohort_size"] = self.cohort_size
            notes["cohort_days"] = self.cohort_days
        if self.blocked is not None:
            notes["blocked_by"] = dict(self.blocked)
        if self.drift is not None:
            notes["drift"] = self.drift
        return notes


def _news_runs_since(session: Session, since: datetime) -> Iterable[Mapping[str, Any]]:
    stmt = select(Run.notes).where(
        Run.source == SOURCE, Run.started_at >= since, Run.notes.is_not(None)
    )
    for notes in session.scalars(stmt):
        if isinstance(notes, Mapping):
            yield notes


def _run_when_days(notes: Mapping[str, Any]) -> int:
    """The ``when:`` window a news run queried; runs before the field existed used 7."""
    value = notes.get("when_days")
    return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_WHEN_DAYS


def _polled_since(
    session: Session, now: datetime, since: datetime, *, min_when_days: int = DEFAULT_WHEN_DAYS
) -> dict[int, datetime]:
    polled: dict[int, datetime] = {}
    for notes in _news_runs_since(session, since):
        if _run_when_days(notes) < min_when_days:
            continue
        entries = notes.get("polled")
        if not isinstance(entries, Mapping):
            continue
        for key, value in entries.items():
            try:
                company_id, when = int(key), parse_iso(str(value))
            except ValueError:
                continue
            if since < when <= now and when > polled.get(company_id, since):
                polled[company_id] = when
    return polled


def last_polled(session: Session, now: datetime) -> dict[int, datetime]:
    """Company id -> last Google News poll in the 7 days before ``now``.

    Read from ``runs.notes["polled"]`` of the news runs started in that window
    (whatever their status, so a blocked run's polls still count).
    """
    return _polled_since(session, now, now - POLL_LOOKBACK)


def recently_polled(
    session: Session, now: datetime, *, min_when_days: int = DEFAULT_WHEN_DAYS
) -> dict[int, datetime]:
    """Company id -> last Google News poll, for polls less than 6 hours before ``now``.

    Only runs that queried a ``when:`` window of at least ``min_when_days`` count: a
    scheduled 7-day poll an hour ago does not make a 120-day cohort query redundant.
    """
    return _polled_since(session, now, now - CACHE_TTL, min_when_days=min_when_days)


def formd_cohort(
    session: Session, now: datetime, *, window: timedelta = COHORT_WINDOW
) -> list[int]:
    """Ids of companies linked to a ``form_d_filed`` event filed in the ``window`` before ``now``.

    The SPEC 13.1 cohort's companies (the report narrows further to links by R1-R4 or
    CIK); ``--cohort formd`` queries Google News for them with ``when:120d``.
    """
    stmt = (
        select(EventRow.company_id)
        .where(
            EventRow.type == FORM_D_FILED,
            EventRow.company_id.is_not(None),
            EventRow.occurred_at >= now - window,
            EventRow.occurred_at <= now,
        )
        .distinct()
    )
    return sorted(int(company_id) for company_id in session.scalars(stmt) if company_id is not None)


def _recent_discovery_misses(session: Session, now: datetime) -> set[int]:
    misses: set[int] = set()
    for notes in _news_runs_since(session, now - DISCOVERY_RETRY):
        entries = notes.get("no_feed")
        if isinstance(entries, list):
            misses.update(int(value) for value in entries if isinstance(value, int))
    return misses


def select_companies(
    session: Session,
    now: datetime,
    *,
    company_ids: Sequence[int] | None = None,
    when_days: int = DEFAULT_WHEN_DAYS,
) -> list[Company]:
    """The companies due this run, in priority order (PLAN Phase 4 rotation).

    Groups, in order: watchlist members, companies with any event in the last 30
    days, then the long-tail bucket ``id % 7 == now.weekday()`` (UTC). The first two
    are due when not polled in the last 24 hours, the long tail when not polled in
    the last 7 days (both less :data:`SCHEDULE_SLACK`). Within a group the least
    recently polled come first, then by id, so a run cut short by the budget resumes
    with the companies it left out. Explicit ``company_ids`` replace the rotation and
    only skip companies polled less than 6 hours ago by a run whose ``when:`` window
    was at least ``when_days``.
    """
    if company_ids is not None:
        recent = recently_polled(session, now, min_when_days=when_days)
        chosen = session.scalars(
            select(Company).where(Company.id.in_(list(company_ids))).order_by(Company.id)
        ).all()
        return [company for company in chosen if company.id not in recent]

    watched = set(session.scalars(select(WatchlistItem.company_id).distinct()))
    active = set(
        session.scalars(
            select(EventRow.company_id)
            .where(
                EventRow.company_id.is_not(None),
                EventRow.occurred_at >= now - ACTIVE_WINDOW,
                EventRow.occurred_at <= now,
            )
            .distinct()
        )
    )
    last = last_polled(session, now)
    weekday = now.astimezone(UTC).weekday()
    stmt = select(Company).where(
        or_(Company.id.in_(sorted(watched | active)), Company.id % ROTATION_DAYS == weekday)
    )

    def group(company: Company) -> int:
        if company.id in watched:
            return 0
        return 1 if company.id in active else 2

    def due(company: Company) -> bool:
        previous = last.get(company.id)
        interval = PRIORITY_INTERVAL if group(company) < 2 else LONG_TAIL_INTERVAL
        return previous is None or now - previous >= interval - SCHEDULE_SLACK

    never = datetime.min.replace(tzinfo=UTC)
    chosen = [company for company in session.scalars(stmt) if due(company)]
    return sorted(
        chosen, key=lambda company: (group(company), last.get(company.id, never), company.id)
    )


# --- HTTP ------------------------------------------------------------------------------------


class _BlockedError(Exception):
    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"{url} answered {status}")
        self.url = url
        self.status = status


class _SiteUnreachableError(Exception):
    """A company site gave no usable answer (transport error, 429/503): stop probing it."""


@dataclass(frozen=True)
class DiscoveredFeed:
    """A company feed found by :func:`discover_feed`, with the response that proved it."""

    feed_url: str
    discovered_from: str
    content: bytes
    etag: str | None
    last_modified: str | None


def _google_get(client: httpx.Client, url: str, budget: RateBudget) -> httpx.Response:
    """One Google News GET under the Google News budget, never retried.

    Raises :class:`tell.http.RetryExhausted` for 429/5xx (carrying the response) and
    for transport errors (no response).
    """
    return fetch(url, client=client, headers={"Accept": FEED_ACCEPT}, max_attempts=1, budget=budget)


def _site_get(
    client: httpx.Client,
    url: str,
    budgets: HostRateBudgets,
    *,
    accept: str,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """One GET to a company site: 1 request/second per host, :data:`SITE_TIMEOUT_S`, no retry.

    Redirects are followed here, hop by hop, so every hop is charged to its own host's
    budget (a redirect to another host waits for that host's slot). At most
    :data:`SITE_MAX_REDIRECTS` hops; more raises :class:`httpx.TooManyRedirects`.
    :data:`SITE_TIMEOUT_S` applies to each phase (connect, read, ...) of each hop.

    Every final status is returned as is; transport failures raise
    :class:`httpx.HTTPError` (an unparseable domain raises :class:`httpx.InvalidURL`).
    """
    budgets.acquire(url)
    call_headers = {"Accept": accept, **(headers or {})}
    response = client.get(url, headers=call_headers, timeout=SITE_TIMEOUT_S, follow_redirects=False)
    hops = 0
    while response.next_request is not None:
        if hops >= SITE_MAX_REDIRECTS:
            msg = f"more than {SITE_MAX_REDIRECTS} redirects"
            raise httpx.TooManyRedirects(msg, request=response.request)
        hops += 1
        follow = response.next_request
        response.close()
        budgets.acquire(str(follow.url))
        response = client.send(follow, follow_redirects=False)
    return response


def _status_of(exc: RetryExhausted) -> int | None:
    return exc.response.status_code if exc.response is not None else None


def _probe(
    client: httpx.Client, url: str, budgets: HostRateBudgets, *, accept: str
) -> httpx.Response:
    try:
        response = _site_get(client, url, budgets, accept=accept)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        _logger.info("feed discovery: %s failed: %s", url, type(exc).__name__)
        raise _SiteUnreachableError(url) from exc
    if response.status_code in SITE_STOP_STATUSES:
        _logger.info("feed discovery: %s answered %s", url, response.status_code)
        raise _SiteUnreachableError(url)
    return response


def _try_feed(
    client: httpx.Client,
    url: str,
    domain: str,
    budgets: HostRateBudgets,
    *,
    discovered_from: str,
) -> DiscoveredFeed | None:
    response = _probe(client, url, budgets, accept=FEED_ACCEPT)
    if response.status_code != 200:
        return None
    if not on_domain(str(response.url), domain):
        return None
    items, _ = parse_feed_items(response.content)
    if not items:
        return None
    return DiscoveredFeed(
        feed_url=url,
        discovered_from=discovered_from,
        content=response.content,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


def _discover_steps(
    client: httpx.Client, domain: str, budgets: HostRateBudgets
) -> DiscoveredFeed | None:
    base = f"https://{domain}"
    checked: set[str] = set()
    for page in (f"{base}/blog", f"{base}/"):
        response = _probe(client, page, budgets, accept=HTML_ACCEPT)
        if response.status_code != 200:
            continue
        for href in feed_links(response.content, str(response.url)):
            if href in checked or not on_domain(href, domain):
                continue
            checked.add(href)
            found = _try_feed(client, href, domain, budgets, discovered_from=page)
            if found is not None:
                return found
    for path in FALLBACK_PATHS:
        url = f"{base}{path}"
        if url in checked:
            continue
        checked.add(url)
        found = _try_feed(client, url, domain, budgets, discovered_from=url)
        if found is not None:
            return found
    return None


def _discover(
    client: httpx.Client, domain: str, budgets: HostRateBudgets
) -> tuple[DiscoveredFeed | None, DiscoveryOutcome]:
    """Run discovery on one domain; the outcome is ``found``, ``no_feed`` or ``unreachable``.

    The first transport error (dead host, timeout, TLS failure) or 429/503 ends the
    domain at once: the remaining probes would wait on the same host for nothing.
    """
    try:
        found = _discover_steps(client, domain, budgets)
    except _SiteUnreachableError:
        return None, "unreachable"
    return found, ("found" if found is not None else "no_feed")


def discover_feed(
    client: httpx.Client, domain: str, *, budgets: HostRateBudgets | None = None
) -> DiscoveredFeed | None:
    """Find the company's RSS/Atom feed on ``domain`` (SPEC 3.7), or ``None``.

    Order: ``https://{domain}/blog`` then the homepage, each scanned for
    ``<link rel="alternate" type="application/rss+xml|application/atom+xml">`` (every
    advertised feed is fetched to confirm it); then ``/feed``, ``/rss``,
    ``/blog/rss.xml``, ``/blog/feed``. The first 200 that ``feedparser`` reads with at
    least one entry wins. Feeds off the company's domain are ignored, so a match is
    always rule R1. A site that does not answer (transport error, 429/503) ends the
    search with ``None`` at once. Requests go through the per-host company-site budget
    (:data:`SITE_RPS`, default) with a :data:`SITE_TIMEOUT_S` timeout.
    """
    found, _ = _discover(client, domain, budgets or _default_site_budgets())
    return found


def _default_google_budgets() -> HostRateBudgets:
    return HostRateBudgets(get_settings().tell_rate_news_rps)


def _default_site_budgets() -> HostRateBudgets:
    return HostRateBudgets(SITE_RPS)


# --- company-site work (worker threads, no database) -----------------------------------------


@dataclass(frozen=True)
class _FeedPlan:
    """A stored feed due for a conditional poll, copied out of its ``company_feeds`` row."""

    row_id: int
    feed_url: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class _SitePlan:
    """What one company's site work does: poll these feeds, or discover one."""

    company: CompanyRef
    feeds: tuple[_FeedPlan, ...] = ()
    discover: bool = False


@dataclass(frozen=True)
class _FeedPoll:
    """One conditional poll's answer; ``status is None`` means no answer at all."""

    plan: _FeedPlan
    status: int | None = None
    content: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    failure: str | None = None


@dataclass(frozen=True)
class _SiteOutcome:
    polls: tuple[_FeedPoll, ...] = ()
    found: DiscoveredFeed | None = None
    discovery: DiscoveryOutcome | None = None


def _poll_stored_feed(client: httpx.Client, plan: _FeedPlan, budgets: HostRateBudgets) -> _FeedPoll:
    """Conditional GET of one stored feed; the answer is applied later by the writer."""
    headers: dict[str, str] = {}
    if plan.etag:
        headers["If-None-Match"] = plan.etag
    if plan.last_modified:
        headers["If-Modified-Since"] = plan.last_modified
    try:
        response = _site_get(client, plan.feed_url, budgets, accept=FEED_ACCEPT, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return _FeedPoll(plan, failure=type(exc).__name__)
    ok = response.status_code == 200
    return _FeedPoll(
        plan,
        status=response.status_code,
        content=response.content if ok else b"",
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


def _site_work(client: httpx.Client, budgets: HostRateBudgets, plan: _SitePlan) -> _SiteOutcome:
    """Everything one company's site needs this run; runs on a worker, touches no database."""
    polls = tuple(_poll_stored_feed(client, feed, budgets) for feed in plan.feeds)
    if not plan.discover:
        return _SiteOutcome(polls=polls)
    found, outcome = _discover(client, plan.company.domain, budgets)
    return _SiteOutcome(polls=polls, found=found, discovery=outcome)


# --- the run (one writer thread) ---------------------------------------------------------------


@dataclass
class _Run:
    """Everything one run's per-company steps share."""

    session: Session
    client: httpx.Client
    google: RateBudget
    sites: HostRateBudgets
    result: NewsRunResult
    run_id: str
    now: datetime
    misses: set[int] = field(default_factory=set)
    feeds: dict[int, list[CompanyFeed]] = field(default_factory=dict)
    when_days: int = DEFAULT_WHEN_DAYS
    commit: Commit | None = None


def _ref(company: Company) -> CompanyRef:
    return CompanyRef(name=company.name, domain=company.domain)


def _poll_google_news(run: _Run, company: Company) -> bool:
    """Query Google News for one company; ``True`` when the poll got an answer.

    Raises :class:`_BlockedError` on 429/503 and :class:`pydantic.ValidationError`
    when the response no longer fits :class:`NewsFeed`.
    """
    url = google_news_url(company.name, run.when_days)
    run.result.google_requests += 1
    try:
        response = _google_get(run.client, url, run.google)
    except RetryExhausted as exc:
        status = _status_of(exc)
        if status in BLOCK_STATUSES:
            raise _BlockedError(url, status) from exc
        run.result.errors += 1
        _logger.warning("news: company %s query failed (%s)", company.id, status)
        return False
    except httpx.HTTPError as exc:
        run.result.errors += 1
        _logger.warning("news: company %s query failed: %s", company.id, type(exc).__name__)
        return False
    if response.status_code != 200:
        run.result.errors += 1
        _logger.warning("news: company %s query answered %s", company.id, response.status_code)
        return False
    items, _ = parse_feed_items(response.content)
    feed = NewsFeed.model_validate(
        {"origin": "google_news", "feed_url": url, "company": _ref(company), "items": items}
    )
    events, dropped = derive_events(feed, company_id=company.id, run_id=run.run_id, now=run.now)
    run.result.record(run.session, len(feed.items), events, dropped)
    return True


def _company_feed(content: bytes, feed_url: str, company: CompanyRef) -> tuple[NewsFeed, int, int]:
    """Parse a company feed; return ``(feed, entries fetched, incomplete entries)``.

    Blog feeds are not a provider contract: entries without a title, link or date are
    dropped (counted as skipped) instead of failing the whole feed. ``source`` is the
    feed's own title, else the company's domain.
    """
    raw, feed_title = parse_feed_items(content)
    complete = [item for item in raw if {"title", "link", "pub_date"} <= item.keys()]
    for item in complete:
        item["source"] = feed_title or company.domain
    feed = NewsFeed.model_validate(
        {"origin": "company_feed", "feed_url": feed_url, "company": company, "items": complete}
    )
    return feed, len(raw), len(raw) - len(complete)


def _record_company_feed(
    run: _Run, company_id: int, ref: CompanyRef, feed_url: str, content: bytes
) -> None:
    feed, fetched, incomplete = _company_feed(content, feed_url, ref)
    events, dropped = derive_events(feed, company_id=company_id, run_id=run.run_id, now=run.now)
    run.result.record(run.session, fetched, events, dropped + incomplete)


def _apply_poll(run: _Run, company_id: int, ref: CompanyRef, poll: _FeedPoll) -> None:
    """Write one stored feed's poll: 304 is a no-op, 404/410 marks it dead, other answers err."""
    row = run.session.get(CompanyFeed, poll.plan.row_id)
    if row is None:
        return
    run.result.site["feeds_polled"] += 1
    if poll.status is None:
        run.result.errors += 1
        _logger.warning("news: feed %s failed: %s", row.id, poll.failure)
        return
    row.last_polled_at = run.now  # it answered: leave it alone for 6 hours whatever it said
    if poll.status == 304:
        run.result.site["not_modified"] += 1
        return
    if poll.status in DEAD_STATUSES:
        row.status = "dead"
        run.result.site["dead"] += 1
        return
    if poll.status != 200:
        run.result.errors += 1
        _logger.warning("news: feed %s answered %s", row.id, poll.status)
        return
    row.etag = poll.etag or row.etag
    row.last_modified = poll.last_modified or row.last_modified
    _record_company_feed(run, company_id, ref, row.feed_url, poll.content)


def _store_discovered(
    session: Session, company_id: int, found: DiscoveredFeed, now: datetime
) -> CompanyFeed:
    row = session.scalars(select(CompanyFeed).where(CompanyFeed.feed_url == found.feed_url)).first()
    if row is None:
        row = CompanyFeed(
            company_id=company_id,
            feed_url=found.feed_url,
            discovered_from=found.discovered_from,
            status="active",
        )
        session.add(row)
    elif row.company_id == company_id and row.status == "dead":
        row.status = "active"  # the feed is back: discovery found it again
        row.discovered_from = found.discovered_from
    row.etag = found.etag
    row.last_modified = found.last_modified
    row.last_polled_at = now
    session.flush()
    return row


def _apply_discovery(run: _Run, company_id: int, ref: CompanyRef, outcome: _SiteOutcome) -> None:
    """Write one discovery: store the feed and its items, or remember the miss for 30 days.

    A miss (``no_feed``, or ``unreachable`` when the site never answered) is not an
    error: it goes to ``notes.no_feed``, which keeps discovery away for
    :data:`DISCOVERY_RETRY`. A feed already stored for another company (two companies
    on one site) is read for this company too, but this company has no feed of its own
    and is remembered as a miss.
    """
    site = run.result.site
    found = outcome.found
    if found is None:
        run.result.no_feed.append(company_id)
        site["unreachable" if outcome.discovery == "unreachable" else "no_feed"] += 1
        return
    row = _store_discovered(run.session, company_id, found, run.now)
    site["discovered"] += 1
    if row.company_id != company_id:
        run.result.no_feed.append(company_id)
    _record_company_feed(run, company_id, ref, found.feed_url, found.content)


def _apply_site(run: _Run, company_id: int, plan: _SitePlan, future: Future[_SiteOutcome]) -> None:
    """Record one company's finished site work in its own savepoint; a failure is one error."""
    try:
        outcome = future.result()
        with run.session.begin_nested():
            for poll in outcome.polls:
                _apply_poll(run, company_id, plan.company, poll)
            if outcome.discovery is not None:
                _apply_discovery(run, company_id, plan.company, outcome)
    except Exception:
        run.result.errors += 1
        _logger.exception("news: company %s site work failed", company_id)


def _site_plan(run: _Run, company: Company) -> _SitePlan | None:
    """The company's site work this run, or ``None`` when nothing is due.

    Stored ``active`` feeds are polled when their 6-hour cache has expired. Discovery
    runs only for a company with no ``active`` or ``paused`` feed, no discovery miss in
    the last :data:`DISCOVERY_RETRY` and no feed marked ``dead`` within that window.
    """
    rows = run.feeds.get(company.id, [])
    ref = _ref(company)
    if any(row.status in ("active", "paused") for row in rows):
        due = tuple(
            _FeedPlan(row.id, row.feed_url, row.etag, row.last_modified)
            for row in rows
            if row.status == "active"
            and (row.last_polled_at is None or run.now - row.last_polled_at >= CACHE_TTL)
        )
        if not due:
            run.result.site["feeds_cached"] += 1
            return None
        return _SitePlan(company=ref, feeds=due)
    recently_dead = any(
        row.status == "dead"
        and row.last_polled_at is not None
        and run.now - row.last_polled_at < DISCOVERY_RETRY
        for row in rows
    )
    if company.id in run.misses or recently_dead:
        run.result.site["discovery_not_due"] += 1
        return None
    return _SitePlan(company=ref, discover=True)


def _load_feeds(session: Session, company_ids: Sequence[int]) -> dict[int, list[CompanyFeed]]:
    feeds: dict[int, list[CompanyFeed]] = {}
    if not company_ids:
        return feeds
    stmt = select(CompanyFeed).where(CompanyFeed.company_id.in_(list(company_ids)))
    for row in session.scalars(stmt.order_by(CompanyFeed.id)):
        feeds.setdefault(row.company_id, []).append(row)
    return feeds


def _checkpoint(run: _Run, *, notes: bool) -> None:
    """Flush; with a ``commit``, keep the open run row current and commit (no idle transaction).

    The run row's counters follow every checkpoint and its notes (``polled``,
    ``no_feed``) every :data:`PROGRESS_EVERY` companies, so a run killed half way still
    tells the rotation and the discovery memory what it did.
    """
    run.session.flush()
    if run.commit is None:
        return
    row = run.session.get(Run, run.run_id)
    if row is not None:
        row.fetched = run.result.fetched
        row.inserted = run.result.inserted
        row.skipped = run.result.skipped
        row.errors = run.result.errors
        if notes:
            row.notes = {**(row.notes or {}), **run.result.notes}
    run.commit(run.session)


_Pending = dict[Future[_SiteOutcome], tuple[int, int, _SitePlan]]


def _settle(run: _Run, pending: _Pending, *, keep: int | None) -> None:
    """Record finished site work in submission order.

    ``keep=None`` records only what has finished; otherwise this waits until at most
    ``keep`` jobs are still running (``keep=0`` drains everything). Every wait starts
    with a checkpoint, so with a ``commit`` no transaction (this company's Google News
    events, or the previous batch's site writes) stays open while workers are on the
    network.
    """
    while pending:
        finished = {future for future in pending if future.done()}
        if not finished:
            if keep is None or len(pending) <= keep:
                return
            _checkpoint(run, notes=False)
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in sorted(finished, key=lambda f: pending[f][0]):
            _, company_id, plan = pending.pop(future)
            _apply_site(run, company_id, plan, future)


def _poll_companies(
    run: _Run,
    queue: Sequence[Company],
    pool: ThreadPoolExecutor,
    pending: _Pending,
    *,
    backlog: int,
    out_of_time: Callable[[], bool],
    max_requests: int = MAX_GOOGLE_REQUESTS,
) -> int:
    """The Google News loop: one company at a time, site work handed to ``pool``.

    Returns how many companies were started. Stops before the next company once
    ``out_of_time()`` (``budget_exhausted``) or once ``max_requests`` Google News
    requests were sent (``request_cap_reached``), and at a block or a contract failure.
    """
    attempted = 0
    for company in queue:
        if out_of_time():
            run.result.budget_exhausted = True
            break
        if run.result.google_requests >= max_requests:
            run.result.request_cap_reached = True
            break
        attempted += 1
        try:
            answered = _poll_google_news(run, company)
        except _BlockedError as blocked:
            run.result.blocked = {
                "status": blocked.status,
                "url": blocked.url,
                "company_id": company.id,
            }
            _logger.warning("news: blocked (%s); stopping this run", blocked.status)
            break
        except ValidationError as exc:
            run.result.errors += 1
            run.result.drift = f"google_news: {exc.error_count()} validation errors"
            _logger.error("news: Google News response failed the contract: %s", exc)
            break
        if answered:
            run.result.polled[str(company.id)] = iso_utc(run.now)
        plan = _site_plan(run, company)
        if plan is not None:
            _settle(run, pending, keep=backlog - 1)
            future = pool.submit(_site_work, run.client, run.sites, plan)
            pending[future] = (attempted, company.id, plan)
        _settle(run, pending, keep=None)
        _checkpoint(run, notes=attempted % PROGRESS_EVERY == 0)
    return attempted


# --- entry points -------------------------------------------------------------------------------


def _commit(session: Session) -> None:
    session.commit()


def collect(
    session: Session,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    company_ids: Sequence[int] | None = None,
    budgets: HostRateBudgets | None = None,
    site_budgets: HostRateBudgets | None = None,
    site_workers: int = SITE_WORKERS,
    max_companies: int = MAX_COMPANIES,
    budget: timedelta = RUN_BUDGET,
    clock: Callable[[], float] = time.monotonic,
    when_days: int = DEFAULT_WHEN_DAYS,
    cohort: str | None = None,
    cohort_days: int = COHORT_DAYS,
    commit: Commit | None = None,
    max_requests: int = MAX_GOOGLE_REQUESTS,
) -> NewsRunResult:
    """Poll Google News and company feeds for this run's companies and write events.

    Google News is queried on this thread, one company at a time, under its own budget
    (``budgets``' bucket for ``news.google.com``, default ``TELL_RATE_NEWS_RPS``).
    Company-site work (feed polls, discovery) runs on up to ``site_workers`` threads
    under per-host budgets (``site_budgets``, default :data:`SITE_RPS` per host; when
    only ``budgets`` is given it serves both) and never touches the database: this
    thread is the single writer and records each company's answers as they arrive. At
    most ``site_workers * SITE_BACKLOG`` companies' site work is outstanding; beyond
    that the Google News loop waits.

    Polls at most ``max_companies`` companies in :func:`select_companies` order, sends
    at most ``max_requests`` Google News requests (``request_cap_reached``) and starts no
    company once ``budget`` of ``clock`` time has passed (``budget_exhausted``); the
    companies left out are counted in ``deferred``.
    Stops at the first Google News block (429/503) or contract failure; everything
    written before that stays. Site work already started finishes and is recorded.

    ``commit`` (``ingest`` passes ``Session.commit``) is called after every company
    and before every wait for site work, so no transaction stays open across network I/O; ``None`` only flushes (the caller
    owns the transaction). The caller (normally :func:`ingest`) closes the run with
    :attr:`NewsRunResult.notes`.

    ``when_days`` sets the Google News ``when:`` window (7 for scheduled runs).
    ``cohort="formd"`` (SPEC 13.1) replaces the rotation with :func:`formd_cohort`'s
    companies (filings in the last ``cohort_days``), as explicit ``company_ids`` would;
    every other guard (the 6-hour cache, the company and wall-clock budgets, the block
    policy, redaction) is unchanged.
    """
    if cohort is not None and cohort not in COHORTS:
        msg = f"unknown cohort {cohort!r}; known: {', '.join(COHORTS)}"
        raise ValueError(msg)
    if cohort is not None and company_ids is not None:
        msg = "pass either company_ids or cohort, not both"
        raise ValueError(msg)
    if cohort_days < 1:
        msg = f"cohort_days must be >= 1, got {cohort_days}"
        raise ValueError(msg)
    if site_workers < 1:
        msg = f"site_workers must be >= 1, got {site_workers}"
        raise ValueError(msg)
    google_news_url("window check", when_days)  # rejects a bad window before any request
    http = client if client is not None else make_client()
    google = budgets or _default_google_budgets()
    run = _Run(
        session=session,
        client=http,
        google=google.for_host(GOOGLE_NEWS_HOST),
        sites=site_budgets or budgets or _default_site_budgets(),
        result=NewsRunResult(when_days=when_days, cohort=cohort, site_workers=site_workers),
        run_id=run_id,
        now=now,
        when_days=when_days,
        commit=commit,
    )
    started = clock()
    pending: _Pending = {}
    pool = ThreadPoolExecutor(max_workers=site_workers, thread_name_prefix="tell-news-site")
    try:
        if cohort is not None:
            company_ids = formd_cohort(session, now, window=timedelta(days=cohort_days))
            run.result.cohort_size = len(company_ids)
            run.result.cohort_days = cohort_days
        companies = select_companies(session, now, company_ids=company_ids, when_days=when_days)
        run.result.selected = len(companies)
        run.misses = _recent_discovery_misses(session, now)
        queue = companies[: max(max_companies, 0)]
        run.feeds = _load_feeds(session, [company.id for company in queue])
        _checkpoint(run, notes=False)  # nothing stays open while the network is in use
        attempted = _poll_companies(
            run,
            queue,
            pool,
            pending,
            backlog=site_workers * SITE_BACKLOG,
            out_of_time=lambda: clock() - started >= budget.total_seconds(),
            max_requests=max_requests,
        )
        if run.result.budget_exhausted:
            _logger.warning("news: run budget of %s used up; stopping cleanly", budget)
        if run.result.request_cap_reached:
            _logger.warning(
                "news: %s Google News requests sent (the per-run cap); stopping cleanly",
                run.result.google_requests,
            )
        run.result.deferred = len(companies) - attempted
        _settle(run, pending, keep=0)
        _checkpoint(run, notes=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        if client is None:
            http.close()
    return run.result


def ingest(
    session: Session,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    company_ids: Sequence[int] | None = None,
    budgets: HostRateBudgets | None = None,
    site_budgets: HostRateBudgets | None = None,
    site_workers: int = SITE_WORKERS,
    max_companies: int = MAX_COMPANIES,
    budget: timedelta = RUN_BUDGET,
    clock: Callable[[], float] = time.monotonic,
    when_days: int = DEFAULT_WHEN_DAYS,
    cohort: str | None = None,
    cohort_days: int = COHORT_DAYS,
    commit: Commit | None = _commit,
    max_requests: int = MAX_GOOGLE_REQUESTS,
) -> Run:
    """One news run: ``start_run``, :func:`collect`, ``finish_run`` with the notes.

    A Google News block finishes ``failed`` (``notes.blocked = true``, one error) so
    the SPEC section 11 dead-man sees a block that persists; a contract failure also
    finishes ``failed``. A run cut short by its budget finishes ``ok``. ``finished_at`` is
    the start (``now``) plus the real time the run took.

    ``commit`` (default ``Session.commit``) makes the run row durable at once and every
    company's writes as they land, so no transaction is held across network I/O and a
    killed run keeps its work. An unexpected exception then rolls back only the current
    company and closes the run ``failed`` with ``notes.error``. ``commit=None`` only
    flushes and lets the exception propagate (the caller owns the transaction).
    """
    started = now or utcnow()
    began = time.monotonic()
    run_id = start_run(session, SOURCE, now=started)
    if commit is not None:
        commit(session)
    try:
        result = collect(
            session,
            run_id=run_id,
            now=started,
            client=client,
            company_ids=company_ids,
            budgets=budgets,
            site_budgets=site_budgets,
            site_workers=site_workers,
            max_companies=max_companies,
            budget=budget,
            clock=clock,
            when_days=when_days,
            cohort=cohort,
            cohort_days=cohort_days,
            commit=commit,
            max_requests=max_requests,
        )
    except Exception as exc:
        if commit is None:
            raise
        return _close_crashed(session, run_id, exc, commit, now=_elapsed(started, began))
    blocked = result.blocked is not None
    errors = result.errors + (1 if blocked else 0)
    run = finish_run(
        session,
        run_id,
        status="failed" if blocked or result.drift is not None else "ok",
        fetched=result.fetched,
        inserted=result.inserted,
        skipped=result.skipped,
        errors=errors,
        notes=result.notes,
        now=_elapsed(started, began),
    )
    if commit is not None:
        commit(session)
    return run


def is_block_only_failure(run: Run) -> bool:
    """Whether ``run`` failed only because Google News blocked it (429/503).

    Such a run stays ``failed`` with ``notes.blocked = true`` (health and the dead-man
    see it), but ``tell ingest --source news`` exits 0 for it: a block is Google asking
    us to slow down, the work done before it is kept, and the next scheduled run tries
    again. A contract failure (``notes.drift``) or a crash (``notes.error``) is a real
    error and is not covered.
    """
    notes = run.notes or {}
    return (
        run.source == SOURCE
        and run.status == "failed"
        and notes.get("blocked") is True
        and "drift" not in notes
        and "error" not in notes
    )


def _elapsed(started: datetime, began: float) -> datetime:
    """``finished_at``: the run's start plus the real time it took."""
    return started + timedelta(seconds=max(0.0, time.monotonic() - began))


def _close_crashed(
    session: Session, run_id: str, exc: Exception, commit: Commit, *, now: datetime
) -> Run:
    """Close a run that raised: keep what earlier checkpoints committed, record the error."""
    _logger.error("news: run %s failed: %s: %s", run_id, type(exc).__name__, exc)
    session.rollback()
    row = session.get(Run, run_id)
    if row is None:  # the run row itself was never committed
        raise exc
    notes = dict(row.notes or {})
    notes["error"] = f"{type(exc).__name__}: {exc}"
    run = finish_run(
        session,
        run_id,
        status="failed",
        fetched=row.fetched or 0,
        inserted=row.inserted or 0,
        skipped=row.skipped or 0,
        errors=(row.errors or 0) + 1,
        notes=notes,
        now=now,
    )
    commit(session)
    return run


def _ensure_company(session: Session, ref: CompanyRef) -> Company:
    company = session.scalars(select(Company).where(Company.domain == ref.domain)).first()
    if company is None:
        company = Company(domain=ref.domain, name=ref.name)
        session.add(company)
        session.flush()
    return company


def run_fixture(session: Session, case_path: Path, *, run_id: str, now: datetime) -> RunResult:
    """Replay one JSON fixture (a :class:`NewsFeed`) through the real derive + write path.

    The fixture's company is created by domain when missing so events link to it.
    """
    feed = NewsFeed.model_validate_json(case_path.read_text(encoding="utf-8"))
    company = _ensure_company(session, feed.company)
    events, dropped = derive_events(feed, company_id=company.id, run_id=run_id, now=now)
    stats = write_events(session, events)
    session.flush()
    return RunResult(
        fetched=len(feed.items),
        inserted=stats.inserted,
        skipped=dropped + stats.skipped,
        events=events,
    )


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="news",
    response_model=NewsFeed,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="pub_date",
    run_fixture=run_fixture,
)
