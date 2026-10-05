from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from tell import http as tell_http
from tell.http import (
    HostRateBudgets,
    RateBudget,
    RetryExhausted,
    backoff_delay,
    fetch,
    get,
    make_client,
    parse_retry_after,
)

URL = "https://boards.example.test/v1/boards/acme/jobs"


class FakeClock:
    """Manual clock: ``sleep`` advances time instead of blocking."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def wall(self) -> datetime:
        return datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=self.now - 1_000.0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_success_returns_response_and_sends_project_user_agent(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    route = mocked.get(URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
    with make_client() as client:
        response = fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert response.status_code == 200
    assert response.json() == {"jobs": []}
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["User-Agent"] == "tell/0.1 (+https://github.com/karankapur/tell)"
    assert clock.sleeps == []


def test_per_call_user_agent_override_for_sec(mocked: respx.MockRouter, clock: FakeClock) -> None:
    route = mocked.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    with make_client() as client:
        fetch(URL, client=client, user_agent="tell/0.1 (sec-contact)", sleep=clock.sleep)
        fetch(URL, client=client, sleep=clock.sleep)
    first, second = route.calls[0].request, route.calls[1].request
    assert first.headers["User-Agent"] == "tell/0.1 (sec-contact)"
    assert second.headers["User-Agent"] == "tell/0.1 (+https://github.com/karankapur/tell)"


def test_make_client_defaults() -> None:
    with make_client(user_agent="ua/1") as client:
        assert client.headers["User-Agent"] == "ua/1"
        assert client.timeout.read == 20.0
        assert client.timeout.connect == 20.0
        assert client.follow_redirects is True


def test_429_then_success_honours_retry_after_seconds(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    route = mocked.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    with make_client() as client:
        response = fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert response.status_code == 200
    assert route.call_count == 2
    assert clock.sleeps == [7.0]


def test_429_retry_after_http_date(mocked: respx.MockRouter, clock: FakeClock) -> None:
    when = clock.wall() + timedelta(seconds=30)
    http_date = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    mocked.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": http_date}),
            httpx.Response(200),
        ]
    )
    with make_client() as client:
        fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert clock.sleeps == [30.0]


def test_5xx_without_retry_after_uses_exponential_backoff(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    route = mocked.get(URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(200),
        ]
    )
    with make_client() as client:
        response = fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert response.status_code == 200
    assert route.call_count == 4
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_four_failures_then_success_uses_the_whole_schedule(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    route = mocked.get(URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(429),
            httpx.Response(200),
        ]
    )
    with make_client() as client:
        response = fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert response.status_code == 200
    assert route.call_count == 5
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0]


def test_503_five_times_exhausts_after_five_attempts(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    route = mocked.get(URL).mock(return_value=httpx.Response(503))
    with make_client() as client, pytest.raises(RetryExhausted) as excinfo:
        fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert tell_http.MAX_ATTEMPTS == 5
    assert route.call_count == 5
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0]  # no sleep after the final attempt
    err = excinfo.value
    assert err.attempts == 5
    assert err.response is not None
    assert err.response.status_code == 503
    assert err.request.url == URL
    assert "503" in str(err)


def test_404_is_not_retried(mocked: respx.MockRouter, clock: FakeClock) -> None:
    route = mocked.get(URL).mock(return_value=httpx.Response(404))
    with make_client() as client:
        response = fetch(URL, client=client, sleep=clock.sleep)
    assert response.status_code == 404
    assert route.call_count == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 410, 422])
def test_other_4xx_not_retried(mocked: respx.MockRouter, clock: FakeClock, status: int) -> None:
    route = mocked.get(URL).mock(return_value=httpx.Response(status))
    with make_client() as client:
        assert fetch(URL, client=client, sleep=clock.sleep).status_code == status
    assert route.call_count == 1


def test_get_raises_on_4xx(mocked: respx.MockRouter, clock: FakeClock) -> None:
    mocked.get(URL).mock(return_value=httpx.Response(404))
    with make_client() as client, pytest.raises(httpx.HTTPStatusError):
        get(URL, client=client, sleep=clock.sleep)


def test_transport_errors_are_retried(mocked: respx.MockRouter, clock: FakeClock) -> None:
    route = mocked.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, text="fine")]
    )
    with make_client() as client:
        response = fetch(URL, client=client, sleep=clock.sleep)
    assert response.text == "fine"
    assert route.call_count == 2
    assert clock.sleeps == [1.0]


def test_transport_errors_exhaust(mocked: respx.MockRouter, clock: FakeClock) -> None:
    mocked.get(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with make_client() as client, pytest.raises(RetryExhausted) as excinfo:
        fetch(URL, client=client, sleep=clock.sleep, max_attempts=2)
    assert excinfo.value.response is None
    assert "ConnectTimeout" in str(excinfo.value)
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "get"])
def test_transport_errors_retried_for_idempotent_methods(
    mocked: respx.MockRouter, clock: FakeClock, method: str
) -> None:
    route = mocked.route(method=method.upper(), url=URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)]
    )
    with make_client() as client:
        assert fetch(URL, client=client, method=method, sleep=clock.sleep).status_code == 200
    assert route.call_count == 2
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_transport_errors_propagate_for_non_idempotent_methods(
    mocked: respx.MockRouter, clock: FakeClock, method: str
) -> None:
    route = mocked.route(method=method, url=URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)]
    )
    with make_client() as client, pytest.raises(httpx.ReadTimeout):
        fetch(URL, client=client, method=method, json={"a": 1}, sleep=clock.sleep)
    assert route.call_count == 1  # the request is never repeated
    assert clock.sleeps == []


def test_post_still_retries_retryable_statuses(mocked: respx.MockRouter, clock: FakeClock) -> None:
    # A 503 means the server answered, so repeating a POST is the server's own advice.
    route = mocked.post(URL).mock(side_effect=[httpx.Response(503), httpx.Response(201)])
    with make_client() as client:
        assert fetch(URL, client=client, method="POST", sleep=clock.sleep).status_code == 201
    assert route.call_count == 2
    assert clock.sleeps == [1.0]


def test_fetch_without_client_builds_and_closes_one(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    route = mocked.get(URL).mock(return_value=httpx.Response(200))
    assert fetch(URL, sleep=clock.sleep).status_code == 200
    assert route.calls.last.request.headers["User-Agent"].startswith("tell/0.1")


def test_fetch_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        fetch(URL, max_attempts=0)


def test_fetch_passes_params_and_json(mocked: respx.MockRouter, clock: FakeClock) -> None:
    route = mocked.post(URL).mock(return_value=httpx.Response(200))
    with make_client() as client:
        fetch(
            URL,
            client=client,
            method="POST",
            params={"content": "true"},
            json={"a": 1},
            sleep=clock.sleep,
        )
    sent = route.calls.last.request
    assert sent.url.params["content"] == "true"
    assert sent.content == b'{"a":1}'


def test_parse_retry_after_variants(clock: FakeClock) -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("not a date or number") is None
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("99999") == tell_http.MAX_RETRY_AFTER_S
    past = (clock.wall() - timedelta(seconds=60)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(past, now=clock.wall) == 0.0


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity", "+inf"])
def test_parse_retry_after_rejects_non_finite_numbers(value: str) -> None:
    assert parse_retry_after(value) is None


def test_non_finite_retry_after_falls_back_to_backoff(
    mocked: respx.MockRouter, clock: FakeClock
) -> None:
    mocked.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "nan"}),
            httpx.Response(429, headers={"Retry-After": "inf"}),
            httpx.Response(200),
        ]
    )
    with make_client() as client:
        fetch(URL, client=client, sleep=clock.sleep, now=clock.wall)
    assert clock.sleeps == [1.0, 2.0]


def test_backoff_schedule() -> None:
    assert [backoff_delay(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert backoff_delay(0) == 1.0
    assert backoff_delay(9) == 8.0
    assert len(tell_http.BACKOFF_SCHEDULE_S) + 1 == tell_http.MAX_ATTEMPTS


# --- RateBudget ---------------------------------------------------------------


def test_rate_budget_spacing(clock: FakeClock) -> None:
    budget = RateBudget(rps=2.0, burst=1, clock=clock, sleep=clock.sleep)
    waits = [budget.acquire() for _ in range(4)]
    assert waits == [0.0, 0.5, 0.5, 0.5]
    assert clock.sleeps == [0.5, 0.5, 0.5]
    assert clock.now == pytest.approx(1_001.5)


def test_rate_budget_burst_then_throttle(clock: FakeClock) -> None:
    budget = RateBudget(rps=1.0, burst=3, clock=clock, sleep=clock.sleep)
    assert [budget.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert budget.wait_time() == pytest.approx(1.0)
    assert budget.acquire() == pytest.approx(1.0)
    clock.now += 10.0  # idle: bucket refills but never beyond burst
    assert [budget.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert budget.acquire() == pytest.approx(1.0)


def test_rate_budget_refills_with_elapsed_time(clock: FakeClock) -> None:
    budget = RateBudget(rps=4.0, burst=1, clock=clock, sleep=clock.sleep)
    budget.acquire()
    clock.now += 0.25
    assert budget.acquire() == 0.0
    clock.now += 0.1
    assert budget.acquire() == pytest.approx(0.15)


def test_rate_budget_validates_arguments() -> None:
    with pytest.raises(ValueError, match="rps"):
        RateBudget(rps=0)
    with pytest.raises(ValueError, match="burst"):
        RateBudget(rps=1, burst=0)


class FrozenClock:
    """A clock that never moves and a sleeper that only records, safe to share across threads.

    With time frozen every caller sees the same bucket state, so the waits a bucket
    hands out are exactly the schedule it reserved: no two callers may receive the
    same slot.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)


def test_rate_budget_is_thread_safe_and_never_over_admits() -> None:
    frozen = FrozenClock()
    budget = RateBudget(rps=2.0, burst=1, clock=frozen, sleep=frozen.sleep)
    workers = 16
    barrier = threading.Barrier(workers)
    waits: list[float] = []
    waits_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        waited = budget.acquire()
        with waits_lock:
            waits.append(waited)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # One slot every 0.5 s, each handed out exactly once: 0, 0.5, 1.0, ... 7.5.
    assert sorted(waits) == pytest.approx([i * 0.5 for i in range(workers)])
    assert len(set(waits)) == workers
    assert sorted(frozen.sleeps) == pytest.approx([i * 0.5 for i in range(1, workers)])
    # The bucket owes exactly the slots it promised.
    assert budget.wait_time() == pytest.approx(workers * 0.5)


def test_host_rate_budgets_create_one_bucket_per_host_under_contention(
    clock: FakeClock,
) -> None:
    budgets = HostRateBudgets(rps=1.0, burst=1, clock=clock, sleep=lambda _s: None)
    seen: list[RateBudget] = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        bucket = budgets.for_host("boards.example.test")
        with seen_lock:
            seen.append(bucket)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(b) for b in seen}) == 1


def test_rate_budget_is_applied_per_attempt(mocked: respx.MockRouter, clock: FakeClock) -> None:
    mocked.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    budget = RateBudget(rps=1.0, burst=1, clock=clock, sleep=clock.sleep)
    with make_client() as client:
        fetch(URL, client=client, sleep=clock.sleep, budget=budget)
    # attempt 1: token available; backoff 1s refills the bucket; attempt 2: no wait.
    assert clock.sleeps == [1.0]


def test_host_rate_budgets_are_independent_per_host(clock: FakeClock) -> None:
    budgets = HostRateBudgets(rps=1.0, burst=1, clock=clock, sleep=clock.sleep)
    assert budgets.acquire("https://a.example.test/x") == 0.0
    assert budgets.acquire("https://b.example.test/y") == 0.0
    assert budgets.acquire("https://A.example.test/z") == pytest.approx(1.0)
    assert budgets.for_host("a.example.test") is budgets.for_url("https://a.example.test/q")


def test_host_rate_budgets_overrides(clock: FakeClock) -> None:
    budgets = HostRateBudgets(
        rps=8.0, burst=2, overrides={"www.sec.gov": (5.0, 1)}, clock=clock, sleep=clock.sleep
    )
    sec = budgets.for_host("WWW.SEC.GOV")
    assert (sec.rps, sec.burst) == (5.0, 1)
    other = budgets.for_host("boards.greenhouse.io")
    assert (other.rps, other.burst) == (8.0, 2)
