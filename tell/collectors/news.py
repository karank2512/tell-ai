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

Politeness: 1 request per second per host (``TELL_RATE_NEWS_RPS``), no retries. A
429 or 503 from Google News is a block: the run stops at once and finishes
``failed`` with ``runs.notes["blocked"] = true``, so the SPEC section 11 dead-man
notices a block that persists (a block is a signal to slow down, not to retry). A
company feed is not re-polled before ``company_feeds.last_polled_at + 6h``.

Rotation (PLAN Phase 4, SPEC section 11), read from ``runs.notes["polled"]`` of the
news runs of the last 7 days: watchlist members and companies with any event in
the last 30 days are polled at most once per 24 hours; the long tail only on its
weekday (``company id % 7 == now.weekday()``, UTC) and at most once per 7 days. Both
intervals allow one hour of slack for cron start jitter. Explicit ``company_ids``
replace the rotation and only respect the 6-hour cache.

Budget per run: at most :data:`MAX_COMPANIES` companies (watchlist members first,
then the active companies, then the long tail; least recently polled first within
each group) and :data:`RUN_BUDGET` of wall-clock time. A run that hits either stops
cleanly, keeps what it wrote, finishes ``ok`` and says so in ``runs.notes``
(``deferred``, ``budget_exhausted``).

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
from tell.collectors.news_classify import classify, event_type, redact_leadership
from tell.config import get_settings
from tell.http import HostRateBudgets, RetryExhausted, fetch, make_client
from tell.models import Company, CompanyFeed, EventRow, Run, WatchlistItem

__all__ = [
    "COLLECTOR",
    "FALLBACK_PATHS",
    "MAX_COMPANIES",
    "RUN_BUDGET",
    "SOURCE",
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
    "google_news_url",
    "headline",
    "ingest",
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
"""Companies per run: 1,200 Google News queries at 1 rps is 20 minutes, inside the
30-minute ``filings`` job with margin."""
RUN_BUDGET: Final = timedelta(minutes=25)
"""Wall-clock budget per run; the run stops cleanly before the next company after it."""
DISCOVERY_RETRY: Final = timedelta(days=30)
"""A domain where discovery found no feed is not probed again for this long."""

BLOCK_STATUSES: Final = frozenset({429, 503})
"""Google News answers that mean "slow down": the run stops, nothing is retried."""
DEAD_STATUSES: Final = frozenset({404, 410})
"""Company-feed answers that mark the ``company_feeds`` row ``dead``."""

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


def google_news_url(company_name: str) -> str:
    """The Google News RSS search URL for ``"<company name>" when:7d`` (SPEC 3.7)."""
    query = quote_plus(f'"{company_name}" when:7d')
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
            "blocked": self.blocked is not None,
        }
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


def _polled_since(session: Session, now: datetime, since: datetime) -> dict[int, datetime]:
    polled: dict[int, datetime] = {}
    for notes in _news_runs_since(session, since):
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


def recently_polled(session: Session, now: datetime) -> dict[int, datetime]:
    """Company id -> last Google News poll, for polls less than 6 hours before ``now``."""
    return _polled_since(session, now, now - CACHE_TTL)


def _recent_discovery_misses(session: Session, now: datetime) -> set[int]:
    misses: set[int] = set()
    for notes in _news_runs_since(session, now - DISCOVERY_RETRY):
        entries = notes.get("no_feed")
        if isinstance(entries, list):
            misses.update(int(value) for value in entries if isinstance(value, int))
    return misses


def select_companies(
    session: Session, now: datetime, *, company_ids: Sequence[int] | None = None
) -> list[Company]:
    """The companies due this run, in priority order (PLAN Phase 4 rotation).

    Groups, in order: watchlist members, companies with any event in the last 30
    days, then the long-tail bucket ``id % 7 == now.weekday()`` (UTC). The first two
    are due when not polled in the last 24 hours, the long tail when not polled in
    the last 7 days (both less :data:`SCHEDULE_SLACK`). Within a group the least
    recently polled come first, then by id, so a run cut short by the budget resumes
    with the companies it left out. Explicit ``company_ids`` replace the rotation and
    only skip companies polled less than 6 hours ago.
    """
    if company_ids is not None:
        recent = recently_polled(session, now)
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


@dataclass(frozen=True)
class DiscoveredFeed:
    """A company feed found by :func:`discover_feed`, with the response that proved it."""

    feed_url: str
    discovered_from: str
    content: bytes
    etag: str | None
    last_modified: str | None


def _get(
    client: httpx.Client,
    url: str,
    budgets: HostRateBudgets,
    *,
    accept: str,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """One GET, rate-limited per host, never retried.

    Raises :class:`tell.http.RetryExhausted` for 429/5xx (carrying the response) and
    for transport errors (no response).
    """
    call_headers = {"Accept": accept, **(headers or {})}
    return fetch(
        url, client=client, headers=call_headers, max_attempts=1, budget=budgets.for_url(url)
    )


def _status_of(exc: RetryExhausted) -> int | None:
    return exc.response.status_code if exc.response is not None else None


@dataclass
class _Probe:
    """Discovery bookkeeping: whether every request got a definitive answer."""

    clean: bool = True
    tried: list[str] = field(default_factory=list)


def _probe(
    client: httpx.Client, url: str, budgets: HostRateBudgets, probe: _Probe, *, accept: str
) -> httpx.Response | None:
    probe.tried.append(url)
    try:
        response = _get(client, url, budgets, accept=accept)
    except httpx.HTTPError as exc:
        probe.clean = False
        _logger.info("feed discovery: %s failed: %s", url, type(exc).__name__)
        return None
    return response


def _try_feed(
    client: httpx.Client,
    url: str,
    domain: str,
    budgets: HostRateBudgets,
    probe: _Probe,
    *,
    discovered_from: str,
) -> DiscoveredFeed | None:
    response = _probe(client, url, budgets, probe, accept=FEED_ACCEPT)
    if response is None or response.status_code != 200:
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


def _discover(
    client: httpx.Client, domain: str, budgets: HostRateBudgets
) -> tuple[DiscoveredFeed | None, _Probe]:
    probe = _Probe()
    base = f"https://{domain}"
    checked: set[str] = set()
    for page in (f"{base}/blog", f"{base}/"):
        response = _probe(client, page, budgets, probe, accept=HTML_ACCEPT)
        if response is None or response.status_code != 200:
            continue
        for href in feed_links(response.content, str(response.url)):
            if href in checked or not on_domain(href, domain):
                continue
            checked.add(href)
            found = _try_feed(client, href, domain, budgets, probe, discovered_from=page)
            if found is not None:
                return found, probe
    for path in FALLBACK_PATHS:
        url = f"{base}{path}"
        if url in checked:
            continue
        checked.add(url)
        found = _try_feed(client, url, domain, budgets, probe, discovered_from=url)
        if found is not None:
            return found, probe
    return None, probe


def discover_feed(
    client: httpx.Client, domain: str, *, budgets: HostRateBudgets | None = None
) -> DiscoveredFeed | None:
    """Find the company's RSS/Atom feed on ``domain`` (SPEC 3.7), or ``None``.

    Order: ``https://{domain}/blog`` then the homepage, each scanned for
    ``<link rel="alternate" type="application/rss+xml|application/atom+xml">`` (every
    advertised feed is fetched to confirm it); then ``/feed``, ``/rss``,
    ``/blog/rss.xml``, ``/blog/feed``. The first 200 that ``feedparser`` reads with at
    least one entry wins. Feeds off the company's domain are ignored, so a match is
    always rule R1.
    """
    found, _ = _discover(client, domain, budgets or _default_budgets())
    return found


def _default_budgets() -> HostRateBudgets:
    return HostRateBudgets(get_settings().tell_rate_news_rps)


# --- one company ------------------------------------------------------------------------------


@dataclass
class _Run:
    """Everything one run's per-company steps share."""

    session: Session
    client: httpx.Client
    budgets: HostRateBudgets
    result: NewsRunResult
    run_id: str
    now: datetime
    misses: set[int] = field(default_factory=set)


def _ref(company: Company) -> CompanyRef:
    return CompanyRef(name=company.name, domain=company.domain)


def _poll_google_news(run: _Run, company: Company) -> bool:
    """Query Google News for one company; ``True`` when the poll got an answer.

    Raises :class:`_BlockedError` on 429/503 and :class:`pydantic.ValidationError`
    when the response no longer fits :class:`NewsFeed`.
    """
    url = google_news_url(company.name)
    try:
        response = _get(run.client, url, run.budgets, accept=FEED_ACCEPT)
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


def _company_feed(content: bytes, feed_url: str, company: Company) -> tuple[NewsFeed, int, int]:
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
        {
            "origin": "company_feed",
            "feed_url": feed_url,
            "company": _ref(company),
            "items": complete,
        }
    )
    return feed, len(raw), len(raw) - len(complete)


def _record_company_feed(run: _Run, company: Company, feed_url: str, content: bytes) -> None:
    feed, fetched, incomplete = _company_feed(content, feed_url, company)
    events, dropped = derive_events(feed, company_id=company.id, run_id=run.run_id, now=run.now)
    run.result.record(run.session, fetched, events, dropped + incomplete)


def _poll_feed(run: _Run, company: Company, row: CompanyFeed) -> None:
    """Conditional GET of one stored feed; 304 is a no-op, 404/410 marks it dead."""
    headers: dict[str, str] = {}
    if row.etag:
        headers["If-None-Match"] = row.etag
    if row.last_modified:
        headers["If-Modified-Since"] = row.last_modified
    try:
        response = _get(run.client, row.feed_url, run.budgets, accept=FEED_ACCEPT, headers=headers)
    except httpx.HTTPError as exc:
        run.result.errors += 1
        _logger.warning("news: feed %s failed: %s", row.id, type(exc).__name__)
        if isinstance(exc, RetryExhausted) and exc.response is not None:
            row.last_polled_at = run.now  # it answered (429/5xx): leave it alone for 6 hours
        return
    row.last_polled_at = run.now
    if response.status_code == 304:
        return
    if response.status_code in DEAD_STATUSES:
        row.status = "dead"
        return
    if response.status_code != 200:
        run.result.errors += 1
        return
    row.etag = response.headers.get("ETag") or row.etag
    row.last_modified = response.headers.get("Last-Modified") or row.last_modified
    _record_company_feed(run, company, row.feed_url, response.content)


def _store_discovered(
    session: Session, company: Company, found: DiscoveredFeed, now: datetime
) -> CompanyFeed:
    row = session.scalars(select(CompanyFeed).where(CompanyFeed.feed_url == found.feed_url)).first()
    if row is None:
        row = CompanyFeed(
            company_id=company.id,
            feed_url=found.feed_url,
            discovered_from=found.discovered_from,
            status="active",
        )
        session.add(row)
    row.etag = found.etag
    row.last_modified = found.last_modified
    row.last_polled_at = now
    session.flush()
    return row


def _company_feeds(run: _Run, company: Company) -> None:
    """Poll the company's stored feeds, or discover one when it has none."""
    rows = list(
        run.session.scalars(select(CompanyFeed).where(CompanyFeed.company_id == company.id))
    )
    if rows:
        for row in rows:
            fresh = row.last_polled_at is not None and run.now - row.last_polled_at < CACHE_TTL
            if row.status == "active" and not fresh:
                _poll_feed(run, company, row)
        return
    if company.id in run.misses:
        return
    found, probe = _discover(run.client, company.domain, run.budgets)
    if found is None:
        if probe.clean:
            run.result.no_feed.append(company.id)
        else:
            run.result.errors += 1
        return
    _store_discovered(run.session, company, found, run.now)
    _record_company_feed(run, company, found.feed_url, found.content)


# --- entry points -------------------------------------------------------------------------------


def collect(
    session: Session,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    company_ids: Sequence[int] | None = None,
    budgets: HostRateBudgets | None = None,
    max_companies: int = MAX_COMPANIES,
    budget: timedelta = RUN_BUDGET,
    clock: Callable[[], float] = time.monotonic,
) -> NewsRunResult:
    """Poll Google News and company feeds for this run's companies and write events.

    Polls at most ``max_companies`` companies in :func:`select_companies` order and
    starts no company once ``budget`` of ``clock`` time has passed
    (``budget_exhausted``); the companies left out are counted in ``deferred``.
    Stops at the first Google News block (429/503) or contract failure; everything
    written before that stays. Flushes, never commits. The caller (normally
    :func:`ingest`) closes the run with :attr:`NewsRunResult.notes`.
    """
    http = client if client is not None else make_client()
    run = _Run(
        session=session,
        client=http,
        budgets=budgets or _default_budgets(),
        result=NewsRunResult(),
        run_id=run_id,
        now=now,
    )
    started = clock()
    try:
        companies = select_companies(session, now, company_ids=company_ids)
        run.result.selected = len(companies)
        run.misses = _recent_discovery_misses(session, now)
        queue = companies[: max(max_companies, 0)]
        attempted = 0
        for company in queue:
            if clock() - started >= budget.total_seconds():
                run.result.budget_exhausted = True
                _logger.warning("news: run budget of %s used up; stopping cleanly", budget)
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
                run.result.polled[str(company.id)] = iso_utc(now)
            _company_feeds(run, company)
            session.flush()
        run.result.deferred = len(companies) - attempted
    finally:
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
    max_companies: int = MAX_COMPANIES,
    budget: timedelta = RUN_BUDGET,
    clock: Callable[[], float] = time.monotonic,
) -> Run:
    """One news run: ``start_run``, :func:`collect`, ``finish_run`` with the notes.

    A Google News block finishes ``failed`` (``notes.blocked = true``, one error) so
    the SPEC section 11 dead-man sees a block that persists; a contract failure also
    finishes ``failed``. A run cut short by its budget finishes ``ok``. Flushes, never
    commits.
    """
    started = now or utcnow()
    run_id = start_run(session, SOURCE, now=started)
    result = collect(
        session,
        run_id=run_id,
        now=started,
        client=client,
        company_ids=company_ids,
        budgets=budgets,
        max_companies=max_companies,
        budget=budget,
        clock=clock,
    )
    blocked = result.blocked is not None
    errors = result.errors + (1 if blocked else 0)
    return finish_run(
        session,
        run_id,
        status="failed" if blocked or result.drift is not None else "ok",
        fetched=result.fetched,
        inserted=result.inserted,
        skipped=result.skipped,
        errors=errors,
        notes=result.notes,
        now=now,
    )


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
