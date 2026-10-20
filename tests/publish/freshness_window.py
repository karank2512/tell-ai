"""Loads the hand-authored freshness fixture ``tests/fixtures/freshness/window_2026-10-19.json``.

The fixture is a miniature of the 24 hours behind the first dated operations report
(``tell-data/site/methods/ops-2026-10-19.md``, which printed a freshness p95 of 23298.1 h):
the rows freshness reads, in the shapes the collectors write them. ``runs`` (with
``notes.polled`` naming companies by domain and ``notes.when_days``), ATS boards with their
postings and every poll that observed them (``posting_observations``), company feeds with the
moment tell discovered them, and the events. Each event carries a ``case`` saying what it
stands for; the loader drops it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from tell.collectors.base import FIRST_POLL_KEY
from tell.models import (
    AtsBoard,
    Company,
    CompanyFeed,
    EventRow,
    Posting,
    PostingObservation,
    Run,
)
from tell.publish.health import BACKFILL_KEY, STALE_AFTER

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "freshness" / "window_2026-10-19.json"


@dataclass(frozen=True)
class Window:
    """What :func:`load_window` wrote."""

    now: datetime
    companies: dict[str, int]
    """Domain -> ``companies.id``."""
    cases: dict[str, str]
    """``event_id`` -> the fixture's ``case`` line."""


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_window(session: Session, path: Path = FIXTURE) -> Window:
    """Write the fixture's rows through the ORM and return its evaluation instant."""
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    companies: dict[str, int] = {}
    for domain in doc["companies"]:
        company = Company(domain=domain, name=domain.split(".", maxsplit=1)[0].title())
        session.add(company)
        session.flush()
        companies[domain] = company.id

    for run in doc["runs"]:
        notes = run["notes"]
        if notes is not None:
            notes = {**notes}
            if "polled" in notes:
                notes["polled"] = {
                    str(companies[domain]): run["started_at"] for domain in notes["polled"]
                }
        started = _at(run["started_at"])
        session.add(
            Run(
                run_id=run["run_id"],
                source=run["source"],
                started_at=started,
                finished_at=started,
                status="ok",
                notes=notes,
            )
        )

    for spec in doc["boards"]:
        board = AtsBoard(
            company_id=companies[spec["company"]],
            vendor=spec["vendor"],
            identifier=spec["identifier"],
            confidence=Decimal("0.98"),
            status="active",
        )
        session.add(board)
        session.flush()
        for item in spec["postings"]:
            seen = [_at(at) for at in item["observed"]]
            posting = Posting(
                board_id=board.id,
                external_id=item["external_id"],
                fingerprint=f"fp-{spec['identifier']}-{item['external_id']}",
                title="Account Executive",
                title_norm="account executive",
                role_family="sales",
                posted_at=_at(item["posted_at"]),
                first_seen_at=seen[0],
                last_seen_at=seen[-1],
                url=f"https://jobs.{spec['company']}/{item['external_id']}",
            )
            session.add(posting)
            session.flush()
            session.add_all(
                PostingObservation(posting_id=posting.id, observed_at=at, run_id="fixture")
                for at in seen
            )

    for feed in doc["feeds"]:
        session.add(
            CompanyFeed(
                company_id=companies[feed["company"]],
                feed_url=feed["feed_url"],
                status="active",
                created_at=_at(feed["created_at"]),
            )
        )

    cases: dict[str, str] = {}
    for n, item in enumerate(doc["events"]):
        event_id = hashlib.sha256(f"fixture|{n}|{item['url']}".encode()).hexdigest()
        cases[event_id] = item["case"]
        session.add(
            EventRow(
                event_id=event_id,
                company_id=companies[item["company"]],
                source=item["source"],
                type=item["type"],
                occurred_at=_at(item["occurred_at"]),
                observed_at=_at(item["observed_at"]),
                magnitude=Decimal(1),
                url=item["url"],
                payload=item["payload"],
                run_id=item["run_id"],
            )
        )
    session.flush()
    return Window(now=_at(doc["now"]), companies=companies, cases=cases)


def literal_spec_lags(session: Session, now: datetime) -> list[tuple[str, float]]:
    """``(event_id, observed_at - occurred_at in hours)`` as the report computed it before ADR 0043.

    SPEC section 12 read literally, over the events observed in the 24 hours before ``now``
    (tell's own events left out), with only the two exclusions the code had: first-poll
    baselines and ``payload.backfill``. Sorted by lag.
    """
    first = EventRow.payload[FIRST_POLL_KEY].astext
    backfill = EventRow.payload[BACKFILL_KEY].astext
    lag_h = (
        cast(
            func.greatest(func.extract("epoch", EventRow.observed_at - EventRow.occurred_at), 0),
            Float,
        )
        / 3600.0
    )
    stmt = (
        select(EventRow.event_id, lag_h)
        .where(
            EventRow.observed_at > now - STALE_AFTER,
            EventRow.observed_at <= now,
            EventRow.source != "tell",
            first.is_distinct_from("true"),
            backfill.is_distinct_from("true"),
        )
        .order_by(lag_h, EventRow.event_id)
    )
    return [(str(event_id), float(lag)) for event_id, lag in session.execute(stmt)]
