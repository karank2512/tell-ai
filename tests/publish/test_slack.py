"""Slack digest: Block Kit shape, timing across the four daily runs, sequential sends."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, start_run
from tell.models import Run, Watchlist
from tell.publish.slack import (
    DIGEST_SOURCE,
    MAX_ACCOUNTS,
    SEND_INTERVAL_S,
    CrossedAccount,
    build_digest_blocks,
    crossed_accounts,
    new_signals_count,
    send_digests,
    should_send_now,
)
from tests.publish import scenario as sc

WEBHOOK = "https://hooks.slack.invalid/services/T000/B000/XXXX"
TZ = "America/Los_Angeles"
BASE = "https://pages.example/tell-data"


def _account(n: int, score: float) -> CrossedAccount:
    return CrossedAccount(
        name=f"Company {n}",
        domain=f"c{n}.example",
        score=score,
        contributions=[
            {
                "signal_type": "funding",
                "contribution": score - 1,
                "summary": "Form D: $18,000,000 offering",
                "url": f"https://www.sec.gov/{n}/",
            },
            {
                "signal_type": "hiring_velocity",
                "contribution": 1.0,
                "summary": "4 new sales roles in 7 days vs 0.5 baseline",
                "url": f"https://boards.example/{n}",
            },
            {
                "signal_type": "launch",
                "contribution": 0.5,
                "summary": "A <launch> & more | here",
                "url": f"https://news.example/{n}",
            },
        ],
    )


# --- blocks --------------------------------------------------------------------------------------


def test_digest_blocks_shape() -> None:
    crossed = [_account(n, 6.0 + n) for n in range(12)]
    blocks = build_digest_blocks(
        "Pilot list",
        date(2026, 10, 6),
        crossed,
        17,
        8.25,
        BASE,
        [],
        intake_url="https://forms.example/intake",
    )
    assert blocks[0] == {
        "type": "header",
        "text": {"type": "plain_text", "text": "tell · Pilot list · 2026-10-06", "emoji": False},
    }
    assert blocks[1]["type"] == "context"
    assert blocks[1]["elements"][0]["text"] == (
        "17 new signals · 12 accounts crossed the threshold · freshness p95 8.25h"
    )
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == MAX_ACCOUNTS
    first = sections[0]["text"]["text"].splitlines()
    assert first[0] == "*Company 11* (c11.example) · score 17.0"
    assert first[1] == "• <https://www.sec.gov/11/|Form D: $18,000,000 offering>"
    assert first[3] == "• <https://news.example/11|A &lt;launch&gt; &amp; more / here>"
    assert first[-1] == (
        "Why now: Form D: $18,000,000 offering and 4 new sales roles in 7 days vs 0.5 baseline."
    )
    scores = [float(s["text"]["text"].split("score ")[1].split("\n")[0]) for s in sections]
    assert scores == sorted(scores, reverse=True)
    footer = blocks[-1]
    assert footer["type"] == "context"
    links = footer["elements"][0]["text"]
    assert f"<{BASE}/companies/c11.example.html|c11.example>" in links
    assert "c1.example" not in links  # only the ten shown accounts
    assert f"<{BASE}/weekly/2026-W40.html|Weekly page>" in links
    assert footer["elements"][1]["text"] == (
        "Reply with a domain to add it: <https://forms.example/intake|watchlist intake form>"
    )
    json.dumps(blocks)  # serialisable as-is


def test_digest_blocks_stale_line_first_and_empty_day() -> None:
    blocks = build_digest_blocks(
        "Pilot", date(2026, 10, 6), [], 0, None, BASE + "/", ["news", "edgar"]
    )
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"].startswith(":red_circle: *Stale sources:* edgar, news")
    assert blocks[1]["type"] == "header"
    assert blocks[2]["elements"][0]["text"].endswith("freshness p95 n/a")
    assert not [b for b in blocks[3:] if b["type"] == "section"]
    footer = blocks[-1]["elements"]
    assert footer[0]["text"] == f"<{BASE}/weekly/2026-W40.html|Weekly page>"
    assert "issues/new?template=watch-domains.yml" in footer[1]["text"]


def test_long_contribution_lists_stay_under_the_section_limit() -> None:
    many = [
        {"signal_type": "launch", "summary": "x" * 200, "url": f"https://news.example/{i}"}
        for i in range(40)
    ]
    account = CrossedAccount(name="Big", domain="big.example", score=9.0, contributions=many)
    blocks = build_digest_blocks("L", date(2026, 10, 6), [account], 1, 1.0, BASE, [])
    text = next(b for b in blocks if b["type"] == "section")["text"]["text"]
    assert len(text) <= 3000
    assert "• …" in text
    assert text.endswith(".")
    lone = CrossedAccount(name="Q", domain="q.example", score=6.0, contributions=[])
    blocks = build_digest_blocks("L", date(2026, 10, 6), [lone], 1, 1.0, BASE, [])
    assert "Why now: the score crossed the alert threshold." in json.dumps(blocks)


# --- timing ----------------------------------------------------------------------------------------


def _record_digest(session: Session, at: datetime, status: Literal["ok", "failed"] = "ok") -> None:
    run_id = start_run(session, DIGEST_SOURCE, now=at)
    finish_run(session, run_id, status=status, fetched=1, inserted=1, skipped=0, now=at)


@pytest.mark.db
def test_should_send_now_across_the_four_daily_runs(db_session: Session) -> None:
    # Cron 17 2,8,14,20 UTC; Los Angeles is UTC-7 in October: 19:17, 01:17, 07:17, 13:17.
    day = [datetime(2026, 10, 6, h, 17, tzinfo=UTC) for h in (2, 8, 14, 20)]
    assert [should_send_now(db_session, t, TZ, 7) for t in day] == [True, False, True, True]
    # 02:17 UTC is 19:17 on the 5th locally: that day's digest went out at its 07:17 run.
    _record_digest(db_session, datetime(2026, 10, 5, 14, 17, tzinfo=UTC))
    assert should_send_now(db_session, day[0], TZ, 7) is False
    assert should_send_now(db_session, day[1], TZ, 7) is False  # 01:17 local, too early
    assert should_send_now(db_session, day[2], TZ, 7) is True  # first run at/after 07:00
    _record_digest(db_session, day[2])
    assert should_send_now(db_session, day[3], TZ, 7) is False  # already sent today
    next_day = datetime(2026, 10, 7, 14, 17, tzinfo=UTC)
    assert should_send_now(db_session, next_day, TZ, 7) is True
    # A failed send does not count: the next run retries.
    _record_digest(db_session, next_day, status="failed")
    assert should_send_now(db_session, next_day + timedelta(hours=6), TZ, 7) is True
    with pytest.raises(ValueError, match="timezone-aware"):
        should_send_now(db_session, datetime(2026, 10, 6, 14), TZ, 7)


# --- sending ---------------------------------------------------------------------------------------


@pytest.fixture
def frozen() -> Iterator[None]:
    with freeze_time(sc.NOW):
        yield


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_crossed_accounts_and_new_signals_from_the_scenario(db_session: Session) -> None:
    scenario = sc.build(db_session)
    accounts = crossed_accounts(db_session, scenario.public, sc.NOW)
    assert [a.domain for a in accounts] == [sc.ROBOTICS, sc.QUILLSTONE]
    robotics = accounts[0]
    assert robotics.name == "Example Robotics"
    assert robotics.score == 27.7
    assert robotics.contributions[0]["signal_type"] == "funding"
    assert all(c["url"] for c in accounts[1].contributions)
    assert len(accounts[1].contributions) == 5  # every contribution, from the stored score
    private = crossed_accounts(db_session, scenario.private, sc.NOW)
    assert [a.domain for a in private] == [sc.ROBOTICS, sc.BLUEFIN]
    assert crossed_accounts(db_session, scenario.public, sc.NOW + timedelta(days=2)) == []
    members = [scenario.companies[d] for d in (sc.ROBOTICS, sc.QUILLSTONE, sc.GREENHOUSE)]
    assert new_signals_count(db_session, members, sc.NOW) == 5  # the news observed 10-05
    assert new_signals_count(db_session, [], sc.NOW) == 0
    empty = Watchlist(owner="team-z", name="Empty")
    db_session.add(empty)
    db_session.flush()
    assert crossed_accounts(db_session, empty, sc.NOW) == []


def _digest_runs(session: Session) -> list[Run]:
    return list(session.scalars(select(Run).where(Run.source == DIGEST_SOURCE)))


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_send_digests_one_message_per_watchlist_with_a_pause(db_session: Session) -> None:
    sc.build(db_session)
    sleeps: list[float] = []
    now = datetime(2026, 10, 6, 14, 17, tzinfo=UTC)  # 07:17 in Los Angeles
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(WEBHOOK).respond(200, text="ok")
        outcome = send_digests(
            db_session, now=now, webhook_url=WEBHOOK, sleep=sleeps.append, tz=TZ, hour=7
        )
    assert outcome.sent == 2
    assert outcome.errors == 0
    assert sleeps == [SEND_INTERVAL_S]
    bodies: list[dict[str, Any]] = [json.loads(call.request.content) for call in route.calls]
    # edgar and greenhouse have not succeeded in 24 hours: the red line comes first.
    assert all(b["blocks"][0]["text"]["text"].startswith(":red_circle:") for b in bodies)
    assert [b["blocks"][1]["text"]["text"] for b in bodies] == [
        "tell · Pilot list · 2026-10-06",
        "tell · Private list · 2026-10-06",
    ]
    assert bodies[0]["blocks"][2]["elements"][0]["text"].startswith(
        "5 new signals · 2 accounts crossed the threshold · freshness p95 "
    )
    assert bodies[0]["text"] == "tell · Pilot list · 2026-10-06: 2 accounts crossed"
    (run,) = _digest_runs(db_session)
    assert (run.status, run.fetched, run.inserted, run.errors) == ("ok", 2, 2, 0)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(WEBHOOK).respond(200)
        later = send_digests(
            db_session,
            now=now + timedelta(hours=6),
            webhook_url=WEBHOOK,
            sleep=sleeps.append,
            tz=TZ,
            hour=7,
        )
        assert later.skipped == "not the digest run"
        assert route.call_count == 0
        forced = send_digests(
            db_session,
            now=now + timedelta(hours=6),
            webhook_url=WEBHOOK,
            sleep=sleeps.append,
            force=True,
            tz=TZ,
            hour=7,
        )
        assert forced.sent == 2
    assert send_digests(db_session, now=now, webhook_url=None).skipped == "no webhook"


@pytest.mark.db
@pytest.mark.usefixtures("frozen")
def test_send_digests_records_a_failed_run_when_every_send_fails(db_session: Session) -> None:
    db_session.add(Watchlist(owner="team-x", name="Only list"))
    db_session.flush()
    now = datetime(2026, 10, 6, 14, 17, tzinfo=UTC)
    with respx.mock as mock:
        mock.post(WEBHOOK).respond(404, text="no_service")
        outcome = send_digests(
            db_session, now=now, webhook_url=WEBHOOK, sleep=lambda _: None, tz=TZ, hour=7
        )
    assert (outcome.sent, outcome.errors) == (0, 1)
    (run,) = _digest_runs(db_session)
    assert run.status == "failed"
    assert should_send_now(db_session, now + timedelta(hours=6), TZ, 7) is True
    with respx.mock as mock:
        mock.post(WEBHOOK).mock(side_effect=httpx.ConnectError("down"))
        outcome = send_digests(
            db_session,
            now=now + timedelta(hours=6),
            webhook_url=WEBHOOK,
            sleep=lambda _: None,
            tz=TZ,
            hour=7,
        )
    assert (outcome.sent, outcome.errors) == (0, 1)
