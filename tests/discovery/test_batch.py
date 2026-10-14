"""Production safety of ``discover_batch``: per-domain commits, wall-clock budget, threads.

All HTTP is mocked with ``respx``; time is a fake clock wherever the assertion is about
time, so nothing here waits for real.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tell.db import SessionLocal
from tell.discovery.ats import (
    CANDIDATE_PATHS,
    GREENHOUSE_API,
    SITE_TIMEOUT_S,
    RobotsCache,
    discover_batch,
    select_new_companies,
)
from tell.http import RateBudget, make_client
from tell.models import AtsBoard, Company, Run

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
UA = "tell/0.1 (+https://github.com/karank2512/tell-ai)"


def workday_page(domain: str) -> bytes:
    """A careers page linking the company's own Workday site (one board per company)."""
    tenant = domain.split(".", maxsplit=1)[0]
    return f'<a href="https://{tenant}.wd1.myworkdayjobs.com/en-US/careers">Jobs</a>'.encode()


def interrupt(_: httpx.Request) -> httpx.Response:
    raise KeyboardInterrupt


def no_sleep(_: float) -> None:
    return None


class FakeClock:
    """A monotonic fake clock; ``sleep`` advances it. Thread-safe."""

    def __init__(self) -> None:
        self.now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class ScriptedClock:
    """Returns the scripted readings in order, then the last one forever."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]


class SpyBudget(RateBudget):
    """A :class:`RateBudget` that records the clock time of every slot it hands out."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.slots: list[float] = []

    def _wait_locked(self) -> float:
        # Called under the bucket's lock right after the refill, so ``_last`` is "now".
        wait = super()._wait_locked()
        self.slots.append(self._last + wait)
        return wait


def free_budget() -> RateBudget:
    return RateBudget(1000.0, burst=100, clock=lambda: 0.0, sleep=no_sleep)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client() as http:
        yield http


def workday_site(router: respx.MockRouter, domain: str, **robots: Any) -> None:
    """robots.txt (404 unless overridden) and a /careers page linking a Workday site."""
    base = f"https://{domain}"
    route = router.get(f"{base}/robots.txt")
    if robots:
        route.mock(**robots)
    else:
        route.respond(404)
    for path in CANDIDATE_PATHS:
        router.get(f"{base}{path}").respond(404)
    router.get(f"{base}/careers").respond(200, content=workday_page(domain))


def seed(engine: Engine, domains: list[str]) -> None:
    """Committed companies, oldest first in list order."""
    with SessionLocal(bind=engine) as session:
        for age, domain in enumerate(reversed(domains)):
            session.add(
                Company(domain=domain, name=domain, created_at=NOW - timedelta(days=age + 1))
            )
        session.commit()


def board_domains(engine: Engine) -> set[str]:
    with SessionLocal(bind=engine) as session:
        stmt = select(Company.domain).join(AtsBoard, AtsBoard.company_id == Company.id)
        return set(session.scalars(stmt))


# --- per-domain commits ---------------------------------------------------------------------


def test_a_killed_batch_keeps_every_domain_finished_before_the_kill(
    engine: Engine, router: respx.MockRouter, client: httpx.Client
) -> None:
    """KeyboardInterrupt (the stand-in for SIGTERM / the job timeout) on domain 3 of 5."""
    domains = ["alpha.com", "bravo.com", "charlie.com", "delta.com", "echo.com"]
    seed(engine, domains)
    for domain in domains:
        if domain == "charlie.com":
            workday_site(router, domain, side_effect=interrupt)
        else:
            workday_site(router, domain)

    session = SessionLocal(bind=engine)
    try:
        with pytest.raises(KeyboardInterrupt):
            discover_batch(
                session,
                client,
                now=NOW,
                budget=free_budget(),
                concurrency=1,
                commit=Session.commit,
            )
        session.rollback()  # what the CLI's session_scope does on the way out
    finally:
        session.close()

    # The first two domains were committed as they finished; nothing after the kill.
    assert board_domains(engine) == {"alpha.com", "bravo.com"}
    with SessionLocal(bind=engine) as fresh:
        [run] = fresh.scalars(select(Run).where(Run.source == "discovery")).all()
        assert (run.status, run.fetched, run.inserted, run.finished_at) == (
            "running",
            2,
            2,
            None,
        )
        # The next run picks up exactly where this one stopped.
        assert [c.domain for c in select_new_companies(fresh, 10)] == domains[2:]


def test_commit_runs_after_the_run_row_and_after_every_domain(
    engine: Engine, router: respx.MockRouter, client: httpx.Client
) -> None:
    domains = ["alpha.com", "bravo.com", "charlie.com"]
    seed(engine, domains)
    for domain in domains:
        workday_site(router, domain)
    seen: list[set[str]] = []

    def commit(session: Session) -> None:
        session.commit()
        seen.append(board_domains(engine))  # what another connection can already see

    with SessionLocal(bind=engine) as session:
        batch = discover_batch(
            session, client, now=NOW, budget=free_budget(), concurrency=1, commit=commit
        )
    assert batch.outcomes["unsupported"] == 3
    # Start (run row), one per domain, finish.
    assert seen == [
        set(),
        {"alpha.com"},
        {"alpha.com", "bravo.com"},
        set(domains),
        set(domains),
    ]
    with SessionLocal(bind=engine) as fresh:
        run = fresh.get(Run, batch.run_id)
        assert run is not None
        assert (run.status, run.fetched, run.inserted) == ("ok", 3, 3)


# --- wall-clock budget ----------------------------------------------------------------------


def test_budget_stops_starting_new_domains(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """Two requests a domain at 4 rps (0.25 s apart); a 0.6 s budget fits two domains."""
    domains = ["alpha.com", "bravo.com", "charlie.com", "delta.com", "echo.com"]
    for age, domain in enumerate(reversed(domains)):
        db_session.add(Company(domain=domain, name=domain, created_at=NOW - timedelta(days=age)))
    db_session.flush()
    for domain in domains:
        workday_site(router, domain)
    clock = FakeClock()

    batch = discover_batch(
        db_session,
        client,
        now=NOW,
        rps=4.0,
        clock=clock,
        sleep=clock.sleep,
        concurrency=1,
        max_minutes=0.01,
    )

    assert [r.domain for r in batch.results] == ["alpha.com", "bravo.com"]
    assert (batch.budget_exhausted, batch.deferred, batch.new_domains) == (True, 3, 5)
    assert not [str(c.request.url) for c in router.calls if "charlie" in str(c.request.url)]
    run = db_session.get(Run, batch.run_id)
    assert run is not None
    assert run.notes is not None
    assert (run.fetched, run.notes["budget_exhausted"], run.notes["deferred"]) == (2, True, 3)
    assert run.finished_at == NOW + timedelta(seconds=0.75)
    # Deferred domains have no row, so the next run selects them first.
    assert [c.domain for c in select_new_companies(db_session, 10)] == domains[2:]


def test_budget_lets_in_flight_domains_finish(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """Three workers start three domains before the deadline; all three are recorded."""
    domains = ["alpha.com", "bravo.com", "charlie.com", "delta.com", "echo.com"]
    for age, domain in enumerate(reversed(domains)):
        db_session.add(Company(domain=domain, name=domain, created_at=NOW - timedelta(days=age)))
    db_session.flush()
    for domain in domains:
        workday_site(router, domain)
    # Readings: start, three admission checks inside the budget, then past it for good.
    clock = ScriptedClock(0.0, 0.0, 0.0, 0.0, 100.0)

    batch = discover_batch(
        db_session,
        client,
        now=NOW,
        budget=free_budget(),
        clock=clock,
        concurrency=3,
        max_minutes=1.0,
    )

    assert [r.domain for r in batch.results] == ["alpha.com", "bravo.com", "charlie.com"]
    assert (batch.budget_exhausted, batch.deferred) == (True, 2)
    run = db_session.get(Run, batch.run_id)
    assert run is not None
    assert run.finished_at == NOW + timedelta(seconds=100)


def test_no_budget_notes_when_everything_fits(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    db_session.add(Company(domain="alpha.com", name="Alpha"))
    db_session.flush()
    workday_site(router, "alpha.com")
    batch = discover_batch(db_session, client, now=NOW, budget=free_budget(), max_minutes=1.0)
    assert (batch.budget_exhausted, batch.deferred, batch.processed) == (False, 0, 1)


@pytest.mark.parametrize(("concurrency", "max_minutes"), [(0, 1.0), (1, 0.0)])
def test_bad_concurrency_or_budget(
    db_session: Session, client: httpx.Client, concurrency: int, max_minutes: float
) -> None:
    with pytest.raises(ValueError, match="must be"):
        discover_batch(
            db_session, client, now=NOW, concurrency=concurrency, max_minutes=max_minutes
        )


# --- concurrency ----------------------------------------------------------------------------


def test_workers_overlap_but_share_one_rate_budget(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """Eight domains on four workers: requests overlap, the slots stay 1/rps apart."""
    domains = [f"site{i}.com" for i in range(8)]
    for age, domain in enumerate(reversed(domains)):
        db_session.add(Company(domain=domain, name=domain, created_at=NOW - timedelta(days=age)))
    db_session.flush()
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def slow(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.02)  # real time, so the workers' requests overlap
        with lock:
            in_flight -= 1
        if request.url.path == "/careers":
            return httpx.Response(200, content=workday_page(request.url.host))
        return httpx.Response(404)

    router.route(host__regex=r"^site\d\.com$").mock(side_effect=slow)
    clock = FakeClock()
    rps = 4.0
    budget = SpyBudget(rps, clock=clock, sleep=clock.sleep)

    batch = discover_batch(db_session, client, now=NOW, budget=budget, clock=clock, concurrency=4)

    assert batch.outcomes["unsupported"] == 8
    assert [r.domain for r in batch.results] == domains  # reported in selection order
    assert 2 <= peak <= 4
    # Every request drew exactly one slot from the shared bucket...
    assert len(budget.slots) == len(router.calls) == 16
    # ...and no two slots are closer than 1/rps: the run never exceeds 4 requests a second.
    slots = sorted(budget.slots)
    gaps = [later - earlier for earlier, later in pairwise(slots)]
    assert min(gaps) >= 1 / rps - 1e-9
    assert slots[-1] - slots[0] >= (len(slots) - 1) / rps - 1e-9


def test_company_pages_and_robots_time_out_after_ten_seconds(
    db_session: Session, router: respx.MockRouter, client: httpx.Client
) -> None:
    """Company hosts get SITE_TIMEOUT_S; vendor APIs keep the client's default (20 s)."""
    db_session.add(Company(domain="acme.com", name="Acme"))
    db_session.flush()
    router.get("https://acme.com/robots.txt").respond(404)
    router.get("https://acme.com/careers").respond(
        200, html='<a href="https://boards.greenhouse.io/acme">Jobs</a>'
    )
    router.get(f"{GREENHOUSE_API}/acme").respond(200, json={"name": "Acme"})

    batch = discover_batch(db_session, client, now=NOW, budget=free_budget())

    assert batch.outcomes["board"] == 1
    timeouts = {str(c.request.url): c.request.extensions["timeout"] for c in router.calls}
    assert SITE_TIMEOUT_S == 10.0
    assert timeouts["https://acme.com/robots.txt"]["read"] == SITE_TIMEOUT_S
    assert timeouts["https://acme.com/careers"]["connect"] == SITE_TIMEOUT_S
    assert timeouts[f"{GREENHOUSE_API}/acme"]["read"] == 20.0


def test_robots_cache_loads_each_origin_once_across_threads(
    router: respx.MockRouter, client: httpx.Client
) -> None:
    calls = {"a.com": 0, "b.com": 0}
    lock = threading.Lock()

    def robots(request: httpx.Request) -> httpx.Response:
        with lock:
            calls[request.url.host] += 1
        time.sleep(0.05)  # a slow robots.txt widens the race window
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    router.get(url__regex=r"^https://[ab]\.com/robots\.txt$").mock(side_effect=robots)
    cache = RobotsCache(client, user_agent=UA)
    start = threading.Barrier(16)
    answers: list[tuple[bool, bool]] = []

    def worker(host: str) -> None:
        start.wait()
        answer = (
            cache.allowed(f"https://{host}/careers"),
            cache.allowed(f"https://{host}/private/x"),
        )
        with lock:
            answers.append(answer)

    threads = [
        threading.Thread(target=worker, args=("a.com" if i % 2 else "b.com",)) for i in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == {"a.com": 1, "b.com": 1}
    assert answers == [(True, False)] * 16
