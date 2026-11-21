"""Source exclusion from source to score: news ingest gate, signal build, scoring inputs.

Second local slice (synthetic data only, no network). Covers:

* ``tell ingest --source news`` follows :func:`news_fetch_plan`: with no recorded feed
  permit (the shipped policy) it skips before any database session, collector call or
  HTTP request and exits 0; with a permit it refuses (exit 2), because restricting the
  collector to permitted feeds is collector work this slice does not do.
* :func:`build_signals` derives no signal from an excluded event (Google News, unknown
  provenance, unpermitted feed), and still does from an independent source or a
  permitted company feed.
* Scoring never reads a signal whose source event is excluded, including signals stored
  before this change (legacy rows), so they add nothing to scores or crossings.
* ``slack.new_signals_count`` counts eligible signals only.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell import cli as tell_cli
from tell.collectors import news
from tell.models import EventRow, Score, Signal
from tell.ops import source_eligibility
from tell.ops.source_eligibility import FeedPermit
from tell.publish.slack import new_signals_count
from tell.signals.profiles import load_profiles
from tell.signals.scoring import build_signals, score_companies, score_instant
from tests.ops.helpers import add_company
from tests.publish import scenario as sc

AS_OF = date(2026, 10, 6)
T = score_instant(AS_OF)
OCCURRED = datetime(2026, 10, 5, 12, tzinfo=UTC)
GNEWS_QUERY = "https://news.google.com/rss/search?q=%22Synthetic+Co%22+when%3A7d"
GNEWS_ITEM = "https://news.google.com/rss/articles/CBMiSYNTHETIC?oc=5"
FEED = "https://synthetic.example/blog/atom.xml"
PERMIT = FeedPermit(FEED, "docs/sources/permits/synthetic.md", date(2026, 11, 21))


# --- 1. news ingest gate ------------------------------------------------------------------


class _Tripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def hook(self, label: str) -> Any:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(label)
            msg = f"tripwire: {label} reached while news ingest is gated"
            raise AssertionError(msg)

        return record


@pytest.fixture
def tripwire(monkeypatch: pytest.MonkeyPatch) -> _Tripwire:
    wire = _Tripwire()
    monkeypatch.setattr(tell_cli, "session_scope", wire.hook("tell.cli.session_scope"))
    monkeypatch.setattr(news, "ingest", wire.hook("collectors.news.ingest"))
    monkeypatch.setattr(Engine, "connect", wire.hook("Engine.connect"))
    monkeypatch.setattr(httpx.Client, "send", wire.hook("httpx.Client.send"))
    monkeypatch.setattr(httpx.AsyncClient, "send", wire.hook("httpx.AsyncClient.send"))
    return wire


NEWS_ARGVS = [
    ["ingest", "--source", "news"],
    ["ingest", "--source", "news", "--limit", "5"],
    ["ingest", "--source", "news", "--domain", "synthetic.example"],
    ["ingest", "--source", "news", "--cohort", "formd"],
    ["ingest", "--source", "news", "--when-days", "30"],
]


@pytest.mark.parametrize("argv", NEWS_ARGVS, ids=lambda a: " ".join(a[2:]) or "plain")
def test_news_ingest_is_skipped_without_db_collector_or_http(
    tripwire: _Tripwire, argv: list[str]
) -> None:
    result = CliRunner().invoke(tell_cli.app, argv)
    assert result.exit_code == 0, result.output
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["source"] == "news"
    assert line["status"] == "skipped"
    assert line["reason"] == "google_news_excluded;no_company_feed_permits"
    assert line["policy_version"] == source_eligibility.POLICY_VERSION
    assert tripwire.calls == []


def test_news_ingest_refuses_when_a_permit_exists(
    tripwire: _Tripwire, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A permit would make company feeds fetchable, but the collector still polls Google
    # News; until it is restricted to permitted feeds, ingest refuses instead.
    monkeypatch.setattr(source_eligibility, "PERMITS", (PERMIT,))
    result = CliRunner().invoke(tell_cli.app, ["ingest", "--source", "news"])
    assert result.exit_code == 2, result.output
    assert "permitted feeds" in result.output
    assert tripwire.calls == []


def test_ingest_gate_reads_permits_at_call_time_never_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("TELL_NEWS", "TELL_PERMITS", "TELL_GOOGLE_NEWS"):
        monkeypatch.setenv(name, "on")
    assert source_eligibility.PERMITS == ()
    assert not source_eligibility.news_fetch_plan(source_eligibility.PERMITS).runs


# --- database fixtures --------------------------------------------------------------------


def _event(
    session: Session,
    event_id: str,
    source: str,
    type_: str,
    company_id: int,
    *,
    url: str = "https://example.test/x",
    payload: dict[str, Any] | None = None,
    occurred: datetime = OCCURRED,
) -> EventRow:
    row = EventRow(
        event_id=event_id,
        company_id=company_id,
        source=source,
        type=type_,
        occurred_at=occurred,
        observed_at=occurred + timedelta(hours=1),
        magnitude=Decimal(1),
        url=url,
        payload=payload if payload is not None else {},
        run_id="synthetic",
    )
    session.add(row)
    session.flush()
    return row


def _google(title: str = "Synthetic Co launches a product") -> dict[str, Any]:
    return {
        "title": title,
        "category": "launch",
        "query": GNEWS_QUERY,
        "match": {"rule": "news_query", "guard": "name_in_title"},
    }


def _feed() -> dict[str, Any]:
    return {"title": "Introducing v2", "query": FEED, "match": {"rule": "R1"}}


def _mixed_company(session: Session) -> int:
    """One company with an EDGAR Form D plus Google News, unknown and feed news events."""
    company = add_company(session, "synthetic.example")
    _event(
        session,
        "edgar-d",
        "edgar",
        "form_d_filed",
        company.id,
        url="https://www.sec.gov/Archives/edgar/data/1/",
        payload={"total_offering_amount": 5_000_000},
    )
    _event(
        session,
        "gnews-launch",
        "news",
        "news_launch",
        company.id,
        url=GNEWS_ITEM,
        payload=_google(),
    )
    _event(
        session,
        "gnews-lead",
        "news",
        "news_leadership",
        company.id,
        url=GNEWS_ITEM,
        payload=_google("Synthetic Co hires a CFO"),
    )
    _event(
        session,
        "unknown-launch",
        "news",
        "news_launch",
        company.id,
        url="https://news.example/a",
        payload={"title": "no provenance"},
    )
    _event(
        session,
        "feed-expansion",
        "news",
        "news_expansion",
        company.id,
        url=FEED + "/p",
        payload=_feed(),
    )
    return company.id


def _signal_event_ids(session: Session) -> set[str]:
    return set(session.scalars(select(Signal.event_id)))


# --- 2. signal build ----------------------------------------------------------------------


@pytest.mark.db
def test_build_signals_derives_nothing_from_excluded_events(db_session: Session) -> None:
    _mixed_company(db_session)
    build_signals(db_session, now=T)
    assert _signal_event_ids(db_session) == {"edgar-d"}
    # Idempotent: a rerun still derives nothing from them.
    assert build_signals(db_session, now=T) == 0
    assert _signal_event_ids(db_session) == {"edgar-d"}


@pytest.mark.db
def test_build_signals_follows_a_feed_permit_read_at_call_time(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mixed_company(db_session)
    monkeypatch.setattr(source_eligibility, "PERMITS", (PERMIT,))
    build_signals(db_session, now=T)
    # The permitted feed now derives; Google News and unknown provenance never do.
    assert _signal_event_ids(db_session) == {"edgar-d", "feed-expansion"}


# --- 3. scoring inputs --------------------------------------------------------------------


def _legacy_signal(session: Session, event: EventRow, signal_type: str) -> Signal:
    """A signal stored before this change, derived from an excluded event."""
    signal = Signal(
        company_id=event.company_id,
        event_id=event.event_id,
        signal_type=signal_type,
        role_family=None,
        weight=Decimal(2),
        magnitude=Decimal(1),
        half_life_days=30,
        starts_at=event.occurred_at,
    )
    session.add(signal)
    session.flush()
    return signal


@pytest.mark.db
def test_scoring_ignores_legacy_signals_from_excluded_events(db_session: Session) -> None:
    company_id = _mixed_company(db_session)
    build_signals(db_session, now=T)
    profile = load_profiles()["default"]
    (clean,) = score_companies(db_session, profile, AS_OF, [company_id], write=False)
    for event_id, signal_type in (
        ("gnews-launch", "launch"),
        ("gnews-lead", "leadership"),
        ("unknown-launch", "launch"),
        ("feed-expansion", "expansion"),
    ):
        event = db_session.get(EventRow, event_id)
        assert event is not None
        _legacy_signal(db_session, event, signal_type)
    (scored,) = score_companies(db_session, profile, AS_OF, [company_id])
    assert scored.score == clean.score
    assert scored.score > 0
    cited = {c["signal_id"] for c in scored.contributions}
    edgar_signal = db_session.scalars(select(Signal.id).where(Signal.event_id == "edgar-d")).one()
    assert cited == {edgar_signal}
    stored = db_session.get(Score, (company_id, profile.name, AS_OF))
    assert stored is not None
    assert {c["signal_id"] for c in stored.contributions} == {edgar_signal}


@pytest.mark.db
def test_scenario_news_only_company_is_not_scored_and_never_crosses(
    db_session: Session,
) -> None:
    scenario = sc.build(db_session)
    quill = scenario.companies[sc.QUILLSTONE]
    robotics = scenario.companies[sc.ROBOTICS]
    # With no eligible signal Quillstone is not in the scored set at all (no row); a row,
    # if one were written, could only be a zero with no contributions.
    stored = db_session.get(Score, (quill, "default", sc.AS_OF))
    if stored is not None:
        assert float(stored.score) == 0.0
        assert stored.contributions == []
    profile = sc.profiles()["default"]
    (rescored,) = score_companies(db_session, profile, sc.AS_OF, [quill], write=False)
    assert rescored.score == 0.0
    assert rescored.contributions == []
    crossings = db_session.scalars(
        select(EventRow.company_id).where(EventRow.type == "score_crossed_threshold")
    ).all()
    assert quill not in crossings
    assert robotics in crossings
    news_signals = db_session.scalars(
        select(Signal.id)
        .join(EventRow, EventRow.event_id == Signal.event_id)
        .where(EventRow.source == "news")
    ).all()
    assert news_signals == []


# --- 4. Slack new-signal count ------------------------------------------------------------


@pytest.mark.db
def test_new_signals_count_ignores_legacy_excluded_signals(db_session: Session) -> None:
    company_id = _mixed_company(db_session)
    build_signals(db_session, now=T)
    event = db_session.get(EventRow, "gnews-launch")
    assert event is not None
    _legacy_signal(db_session, event, "launch")
    now = OCCURRED + timedelta(hours=2)  # every event above was observed an hour ago
    assert new_signals_count(db_session, [company_id], now) == 1  # the EDGAR Form D only
