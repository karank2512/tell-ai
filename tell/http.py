"""One shared, polite HTTP layer for every collector.

* a single :func:`make_client` factory (20 s timeout, project User-Agent);
* :func:`fetch` / :func:`get` with retries on 429 and 5xx using the full
  exponential backoff schedule 1 s, 2 s, 4 s, 8 s (a first attempt plus up to
  four retries, five attempts in total), honouring ``Retry-After`` given as
  seconds or as an HTTP-date, and never retrying any other 4xx; transport
  errors are retried only for idempotent methods (GET, HEAD, OPTIONS);
* :class:`RateBudget`, a thread-safe token bucket (``rps`` + ``burst``) with an
  injectable clock and sleeper, plus :class:`HostRateBudgets` keyed by host.

Sleep and clock are injectable everywhere so tests run instantly with a fake
clock and ``respx``.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from tell.config import get_settings

Sleeper = Callable[[float], None]
"""Blocks for the given number of seconds (``time.sleep`` in production)."""

Clock = Callable[[], float]
"""Returns a monotonic timestamp in seconds (``time.monotonic`` in production)."""

WallClock = Callable[[], datetime]
"""Returns the current wall-clock time, timezone-aware (used for HTTP-date parsing)."""

DEFAULT_TIMEOUT_S = 20.0
BACKOFF_SCHEDULE_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
"""Sleep before retry 1, 2, 3 and 4; the SPEC's backoff schedule, used in full."""
MAX_RETRIES = len(BACKOFF_SCHEDULE_S)
MAX_ATTEMPTS = 1 + MAX_RETRIES
"""First attempt plus one retry per backoff step: five attempts in total."""
MAX_RETRY_AFTER_S = 120.0
RETRY_STATUS_CODES = frozenset({429, *range(500, 600)})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods whose transport errors (connect/read failures) are safe to retry blindly."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RetryExhausted(httpx.HTTPError):
    """Raised when every retry attempt ended in a retryable status or transport error."""

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response | None,
        attempts: int,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.response = response
        self.attempts = attempts


def default_user_agent() -> str:
    """The project-wide User-Agent from settings (``TELL_HTTP_USER_AGENT``)."""
    return get_settings().tell_http_user_agent


def make_client(
    *,
    user_agent: str | None = None,
    timeout_s: float | None = None,
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """Build the shared :class:`httpx.Client` with the project's politeness defaults.

    ``user_agent`` defaults to ``TELL_HTTP_USER_AGENT``; collectors talking to the
    SEC pass ``SEC_USER_AGENT`` instead. Extra ``kwargs`` go straight to
    :class:`httpx.Client` (tests pass ``transport=``).
    """
    settings = get_settings()
    merged = {
        "User-Agent": user_agent or settings.tell_http_user_agent,
        "Accept": "application/json, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    }
    if headers:
        merged.update(headers)
    return httpx.Client(
        timeout=httpx.Timeout(timeout_s if timeout_s is not None else settings.tell_http_timeout_s),
        headers=merged,
        follow_redirects=True,
        **kwargs,
    )


def parse_retry_after(value: str | None, *, now: WallClock = _utc_now) -> float | None:
    """Turn a ``Retry-After`` header into a non-negative delay in seconds.

    Accepts an integer/float number of seconds or an HTTP-date. Returns ``None``
    when the header is absent, unparseable or not a finite number (``nan``,
    ``inf``), in which case the caller falls back to the normal backoff. The
    result is capped at :data:`MAX_RETRY_AFTER_S` so a hostile header cannot
    stall a run.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now()).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


def backoff_delay(attempt: int) -> float:
    """Delay before retry number ``attempt`` (1-based): 1 s, 2 s, 4 s, 8 s."""
    index = min(max(attempt, 1), len(BACKOFF_SCHEDULE_S)) - 1
    return BACKOFF_SCHEDULE_S[index]


def should_retry(response: httpx.Response) -> bool:
    """429 and every 5xx are retried; every other status is final."""
    return response.status_code in RETRY_STATUS_CODES


def is_idempotent(method: str) -> bool:
    """``True`` for the methods whose transport errors :func:`fetch` retries."""
    return method.upper() in IDEMPOTENT_METHODS


def fetch(
    url: str,
    *,
    client: httpx.Client | None = None,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    user_agent: str | None = None,
    content: bytes | str | None = None,
    json: Any | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Sleeper = time.sleep,
    now: WallClock = _utc_now,
    budget: RateBudget | None = None,
) -> httpx.Response:
    """Perform one HTTP request with the project retry policy and return the response.

    Retries on 429 and 5xx with the exponential backoff schedule 1, 2, 4, 8 s (a
    first attempt plus up to four retries, :data:`MAX_ATTEMPTS` in total), honouring
    ``Retry-After`` when the server sends a usable one. Other 4xx responses are
    returned as-is (never retried) so callers can treat 404 as a business signal.
    Raises :class:`RetryExhausted` after ``max_attempts`` failed attempts.

    Transport errors (connection refused, read timeout, ...) are retried on the same
    schedule only for idempotent methods (GET, HEAD, OPTIONS), because a request that
    timed out may still have reached the server. For every other method (POST, PUT,
    PATCH, DELETE) the :class:`httpx.TransportError` propagates unchanged after the
    first attempt and the caller decides whether a repeat is safe.

    ``user_agent`` overrides the client's header for this call only (SEC traffic).
    ``budget`` is a :class:`RateBudget` acquired before every attempt.
    """
    if max_attempts < 1:
        msg = "max_attempts must be >= 1"
        raise ValueError(msg)
    own_client = client is None
    http = client if client is not None else make_client(user_agent=user_agent)
    call_headers: dict[str, str] = dict(headers or {})
    if user_agent is not None:
        call_headers["User-Agent"] = user_agent
    retry_transport_errors = is_idempotent(method)

    last_response: httpx.Response | None = None
    last_error: httpx.TransportError | None = None
    request = http.build_request(method, url, params=params, headers=call_headers)
    try:
        for attempt in range(1, max_attempts + 1):
            if budget is not None:
                budget.acquire()
            request = http.build_request(
                method, url, params=params, headers=call_headers, content=content, json=json
            )
            try:
                response = http.send(request)
            except httpx.TransportError as exc:
                if not retry_transport_errors:
                    raise
                last_error = exc
                last_response = None
            else:
                if not should_retry(response):
                    return response
                last_response = response
                last_error = None
            if attempt == max_attempts:
                break
            delay = None
            if last_response is not None:
                delay = parse_retry_after(last_response.headers.get("Retry-After"), now=now)
            sleep(delay if delay is not None else backoff_delay(attempt))
    finally:
        if own_client:
            http.close()

    detail = (
        f"HTTP {last_response.status_code}"
        if last_response is not None
        else f"{type(last_error).__name__}: {last_error}"
    )
    raise RetryExhausted(
        f"{method} {url} failed after {max_attempts} attempts ({detail})",
        request=request,
        response=last_response,
        attempts=max_attempts,
    )


def get(url: str, **kwargs: Any) -> httpx.Response:
    """``fetch`` with ``GET`` that also raises :class:`httpx.HTTPStatusError` on 4xx."""
    response = fetch(url, method="GET", **kwargs)
    response.raise_for_status()
    return response


class RateBudget:
    """Token bucket: at most ``rps`` requests per second with bursts up to ``burst``.

    :meth:`acquire` blocks (via the injected ``sleep``) until a token is available
    and returns the number of seconds it waited. Both ``clock`` and ``sleep`` are
    injectable so tests use a fake clock.

    Thread-safe: the slot is reserved under a lock (the token count may go negative,
    which is the queue of callers already promised a slot) and the sleep happens
    outside it, so ``TELL_POLL_CONCURRENCY`` workers sharing one bucket never admit
    more than ``rps`` requests per second between them.
    """

    def __init__(
        self,
        rps: float,
        burst: int = 1,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        if rps <= 0:
            msg = "rps must be > 0"
            raise ValueError(msg)
        if burst < 1:
            msg = "burst must be >= 1"
            raise ValueError(msg)
        self.rps = float(rps)
        self.burst = int(burst)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._last = clock()

    def _refill(self) -> None:
        """Credit the tokens earned since the last call; the caller holds the lock."""
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rps)

    def _wait_locked(self) -> float:
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self.rps

    def wait_time(self) -> float:
        """Seconds until the next token is available, without consuming one."""
        with self._lock:
            self._refill()
            return self._wait_locked()

    def acquire(self) -> float:
        """Reserve one token, sleeping first if necessary; returns seconds waited.

        The reservation is made under the lock before sleeping, so concurrent callers
        each get their own slot in the schedule and never the same one.
        """
        with self._lock:
            self._refill()
            waited = self._wait_locked()
            self._tokens -= 1.0
        if waited > 0.0:
            self._sleep(waited)
        return waited


class HostRateBudgets:
    """A :class:`RateBudget` per host, created lazily with shared ``rps``/``burst``.

    ``overrides`` maps a host to a specific ``(rps, burst)`` pair, e.g. a lower
    budget for ``www.sec.gov``. Bucket creation is serialised, so threads racing on
    a new host share one bucket.
    """

    def __init__(
        self,
        rps: float,
        burst: int = 1,
        *,
        overrides: Mapping[str, tuple[float, int]] | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self.rps = rps
        self.burst = burst
        self._overrides = dict(overrides or {})
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._buckets: dict[str, RateBudget] = {}

    def for_host(self, host: str) -> RateBudget:
        key = host.lower()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                rps, burst = self._overrides.get(key, (self.rps, self.burst))
                bucket = RateBudget(rps, burst, clock=self._clock, sleep=self._sleep)
                self._buckets[key] = bucket
        return bucket

    def for_url(self, url: str) -> RateBudget:
        host = httpx.URL(url).host
        return self.for_host(host)

    def acquire(self, url: str) -> float:
        """Acquire a token for ``url``'s host; returns seconds waited."""
        return self.for_url(url).acquire()
