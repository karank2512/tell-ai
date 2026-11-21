"""Source exclusion wired into the output paths (first local slice; synthetic data only).

Covers: the ``WIRED`` backstop on ``tell publish``/``prune``/``notify`` (zero database and
zero HTTP calls when refused), the JSONL publish/archive filter, the prune delete guard and
its parity with :func:`classify_event`, ``events_held``, naive-datetime refusal, the lineage
stamp on crossings, :func:`classify_stamped_crossing` (same-day rescore stays clean; legacy,
restricted, unknown and non-finite crossings fail closed), the Slack crossing filter and the
HubSpot receipt cutover. No network: every HTTP entry point is patched to record and refuse.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from tell import cli as tell_cli
from tell.crm import hubspot
from tell.models import EventRow, Score, Signal
from tell.ops import prune, source_eligibility
from tell.ops.lineage import deletable_predicate
from tell.ops.source_eligibility import (
    INDEPENDENT_SOURCES,
    POLICY_VERSION,
    STAMP_TOP_N,
    Decision,
    FeedPermit,
    Provenance,
    Reason,
    StoredScore,
    archive_disposition,
    classify_crossing,
    classify_event,
    classify_stamped_crossing,
)
from tell.publish import cli as publish_cli
from tell.publish.data_repo import event_record, events_path, read_event_ids, write_events_jsonl
from tell.publish.slack import crossed_accounts
from tell.signals.profiles import load_profiles
from tell.signals.scoring import score_profile
from tests.ops.helpers import add_company
from tests.publish import scenario as sc

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
DAY = timedelta(days=1)
OLD = NOW - 200 * DAY
GNEWS_QUERY = "https://news.google.com/rss/search?q=%22Synthetic+Co%22+when%3A7d"
GNEWS_ITEM = "https://news.google.com/rss/articles/CBMiSYNTHETIC?oc=5"
FEED = "https://synthetic.example/blog/atom.xml"
CLEAN = Decision(Provenance.INDEPENDENT, Reason.INDEPENDENT_SOURCE)
RESTRICTED = Decision(Provenance.GOOGLE_NEWS, Reason.GOOGLE_NEWS_EXCLUDED)


# --- payload builders --------------------------------------------------------------------


def _google_payload() -> dict[str, Any]:
    return {
        "title": "Synthetic Co raises a round",
        "category": "funding",
        "query": GNEWS_QUERY,
        "match": {"rule": "news_query", "guard": "name_in_title"},
    }


def _feed_payload() -> dict[str, Any]:
    return {"title": "Introducing v2", "query": FEED, "match": {"rule": "R1"}}


def _stamp(
    ids: list[int],
    *,
    score: object = 9.0,
    top: list[dict[str, Any]] | None = None,
    version: object = POLICY_VERSION,
) -> dict[str, Any]:
    """A crossing payload stamped the way ``emit_crossings`` stamps it."""
    contributions = (
        top
        if top is not None
        else [
            {"signal_id": i, "contribution": 1.0, "url": f"https://x.example/{i}"}
            for i in ids[:STAMP_TOP_N]
        ]
    )
    return {
        "profile": "default",
        "as_of": "2026-10-06",
        "score": score,
        "threshold": 6.0,
        "top_contributions": contributions[:2],
        "policy_version": version,
        "signal_ids": sorted(ids),
        "contributions": contributions,
    }


def _legacy(ids: list[int]) -> dict[str, Any]:
    payload = _stamp(ids)
    for key in ("policy_version", "signal_ids", "contributions"):
        del payload[key]
    return payload


# --- pure: classify_stamped_crossing ------------------------------------------------------


def test_stamped_crossing_clean_when_every_input_is_eligible() -> None:
    decision = classify_stamped_crossing(_stamp([1, 2, 3]), {1: CLEAN, 2: CLEAN, 3: CLEAN})
    assert decision == Decision(Provenance.DERIVED, Reason.LINEAGE_CLEAN)
    assert decision.eligible


def test_restricted_contributor_outside_the_top_still_restricts() -> None:
    ids = list(range(1, 9))
    decisions = dict.fromkeys(ids, CLEAN) | {8: RESTRICTED}  # 8 is not in the stamped top 5
    payload = _stamp(ids)
    assert 8 not in [c["signal_id"] for c in payload["contributions"]]
    assert classify_stamped_crossing(payload, decisions).reason is Reason.LINEAGE_RESTRICTED


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_legacy([1, 2]), id="legacy-unstamped"),
        pytest.param(_stamp([1, 2], version=None), id="no-version"),
        pytest.param(_stamp([1, 2], version="source-exclusion/0"), id="old-version"),
        pytest.param(_stamp([1, 2], version=1), id="version-not-a-string"),
        pytest.param(_stamp([1, 2], score=math.nan), id="score-nan"),
        pytest.param(_stamp([1, 2], score=math.inf), id="score-inf"),
        pytest.param(_stamp([1, 2], score=-math.inf), id="score-neg-inf"),
        pytest.param(_stamp([1, 2], score=True), id="score-bool"),
        pytest.param(_stamp([1, 2], score="9.0"), id="score-string"),
        pytest.param(_stamp([1, 2], score=10**400), id="score-huge-int"),
        pytest.param(_stamp([1, 2], score=None), id="score-missing"),
        pytest.param(_stamp([1, 2], top=[]), id="no-contributions"),
        pytest.param(
            _stamp([1, 2], top=[{"signal_id": 3, "contribution": 1.0}]), id="top-not-in-ids"
        ),
        pytest.param(
            _stamp([1, 2], top=[{"signal_id": True, "contribution": 1.0}]), id="top-bool-id"
        ),
        pytest.param(_stamp([1, 2], top=[{"contribution": 1.0}]), id="top-no-id"),
        pytest.param(
            _stamp([1, 2], top=[{"signal_id": 1, "contribution": math.nan}]), id="top-nan"
        ),
        pytest.param(_stamp([1, 2], top=[{"signal_id": 1}]), id="top-no-value"),
        pytest.param({**_stamp([1, 2]), "signal_ids": []}, id="ids-empty"),
        pytest.param({**_stamp([1, 2]), "signal_ids": [1, 1, 2]}, id="ids-duplicate"),
        pytest.param({**_stamp([1, 2]), "signal_ids": [1, True]}, id="ids-bool"),
        pytest.param({**_stamp([1, 2]), "signal_ids": "1,2"}, id="ids-not-a-list"),
        pytest.param(None, id="payload-none"),
        pytest.param([1, 2], id="payload-not-a-mapping"),
    ],
)
def test_malformed_legacy_and_non_finite_crossings_fail_closed(payload: Any) -> None:
    decision = classify_stamped_crossing(payload, {1: CLEAN, 2: CLEAN, 3: CLEAN})
    assert decision.reason is Reason.LINEAGE_INCOMPLETE
    assert not decision.eligible


def test_unknown_signal_id_is_incomplete() -> None:
    assert classify_stamped_crossing(_stamp([1, 2]), {1: CLEAN}).reason is (
        Reason.LINEAGE_INCOMPLETE
    )


def test_legacy_crossings_stay_auditable_with_classify_crossing() -> None:
    # classify_crossing (score-row based) is kept for audit of unstamped crossings.
    legacy = _legacy([1, 2])
    stored = StoredScore(9.0, (1, 2))
    assert classify_crossing(legacy, stored, {1: CLEAN, 2: CLEAN}).eligible
    assert not classify_stamped_crossing(legacy, {1: CLEAN, 2: CLEAN}).eligible


def test_archive_disposition_rejects_naive_datetimes() -> None:
    aware = datetime(2026, 1, 1, tzinfo=UTC)
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        archive_disposition(CLEAN, naive, aware)
    with pytest.raises(ValueError, match="cutoff must be timezone-aware"):
        archive_disposition(CLEAN, aware - DAY, naive)


def test_wired_is_a_false_code_constant_no_setting_can_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert source_eligibility.WIRED is False
    for name in ("WIRED", "TELL_WIRED", "TELL_SOURCE_ELIGIBILITY_WIRED"):
        monkeypatch.setenv(name, "true")
    assert source_eligibility.outputs_blocked()


# --- blocked CLI: zero DB, zero HTTP ------------------------------------------------------


class _Tripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def hook(self, label: str) -> Any:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(label)
            msg = f"tripwire: {label} called by a refused command"
            raise AssertionError(msg)

        return record


@pytest.fixture
def tripwire(monkeypatch: pytest.MonkeyPatch) -> _Tripwire:
    wire = _Tripwire()
    monkeypatch.setattr(tell_cli, "session_scope", wire.hook("tell.cli.session_scope"))
    monkeypatch.setattr(publish_cli, "session_scope", wire.hook("publish.session_scope"))
    monkeypatch.setattr(publish_cli, "get_settings", wire.hook("publish.get_settings"))
    monkeypatch.setattr(
        publish_cli, "load_private_webhooks", wire.hook("publish.load_private_webhooks")
    )
    monkeypatch.setattr(Engine, "connect", wire.hook("Engine.connect"))
    monkeypatch.setattr(httpx.Client, "send", wire.hook("httpx.Client.send"))
    monkeypatch.setattr(httpx.AsyncClient, "send", wire.hook("httpx.AsyncClient.send"))
    monkeypatch.setattr(prune, "prune", wire.hook("ops.prune.prune"))
    return wire


def _blocked_commands(out: Path) -> list[list[str]]:
    o = str(out)
    return [
        ["publish", "--out", o],
        ["publish", "data", "--out", o],
        ["publish", "site", "--out", o],
        ["publish", "csv", "--watchlist", "x", "--out", str(out / "x.csv")],
        ["publish", "weekly", "--out", o],
        ["publish", "health", "--out", o],
        ["publish", "notify", "--slack"],
        ["notify", "--slack"],
        ["notify", "--slack", "--force"],
        ["weekly", "--out", o],
        ["prune", "--out", o],
    ]


def test_output_commands_exit_2_with_zero_db_and_zero_http(
    tripwire: _Tripwire, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "tell-data"
    out.mkdir()
    # Settings cannot unblock it either.
    monkeypatch.setenv("WIRED", "true")
    monkeypatch.setenv("TELL_WIRED", "true")
    runner = CliRunner()
    for argv in _blocked_commands(out):
        result = runner.invoke(tell_cli.app, argv)
        assert result.exit_code == publish_cli.BLOCKED_EXIT_CODE == 2, (argv, result.output)
        assert "source eligibility is not wired" in result.output, argv
        assert result.exception is None or isinstance(result.exception, SystemExit), argv
    assert tripwire.calls == []
    assert list(out.iterdir()) == []


# --- database fixtures --------------------------------------------------------------------


def _row(
    session: Session,
    event_id: str,
    source: str,
    type_: str,
    *,
    url: str = "https://example.test/x",
    payload: dict[str, Any] | None = None,
    observed: datetime = OLD,
) -> EventRow:
    row = EventRow(
        event_id=event_id,
        company_id=None,
        source=source,
        type=type_,
        occurred_at=observed - timedelta(hours=1),
        observed_at=observed,
        magnitude=Decimal(1),
        url=url,
        payload=payload if payload is not None else {},
        run_id="synthetic",
    )
    session.add(row)
    session.flush()
    return row


def _signal(session: Session, event: EventRow, company_id: int) -> Signal:
    signal = Signal(
        company_id=company_id,
        event_id=event.event_id,
        signal_type="funding",
        role_family=None,
        weight=Decimal(3),
        magnitude=Decimal(1),
        half_life_days=45,
        starts_at=event.occurred_at,
    )
    session.add(signal)
    session.flush()
    return signal


EXCLUDED_IDS = {"news-google", "synthetic-src", "cross-restricted", "cross-legacy", "news-feed"}
ELIGIBLE_IDS = {"edgar-a", "gh-b", "velocity-c", "cross-clean"}


def _mixed(session: Session, observed: datetime) -> None:
    """Eligible and excluded rows of one UTC day, crossings with real signal lineage."""
    company = add_company(session, "mixed.example")
    edgar = _row(session, "edgar-a", "edgar", "form_d_filed", observed=observed)
    _row(session, "gh-b", "greenhouse", "job_opened", observed=observed)
    _row(session, "velocity-c", "tell", "hiring_velocity", observed=observed)
    google = _row(
        session,
        "news-google",
        "news",
        "news_funding",
        url=GNEWS_ITEM,
        payload=_google_payload(),
        observed=observed,
    )
    _row(
        session,
        "news-feed",
        "news",
        "news_launch",
        url=FEED + "/p",
        payload=_feed_payload(),
        observed=observed,
    )
    _row(session, "synthetic-src", "synthetic", "news_launch", observed=observed)
    clean_signal = _signal(session, edgar, company.id)
    restricted_signal = _signal(session, google, company.id)
    for event_id, payload in (
        ("cross-clean", _stamp([clean_signal.id])),
        ("cross-restricted", _stamp([clean_signal.id, restricted_signal.id])),
        ("cross-legacy", _legacy([clean_signal.id])),
    ):
        _row(
            session, event_id, "tell", "score_crossed_threshold", payload=payload, observed=observed
        )


def _ids(session: Session) -> set[str]:
    return set(session.scalars(select(EventRow.event_id)))


# --- publish writer: restricted rows absent from outputs ----------------------------------


@pytest.mark.db
def test_restricted_rows_never_reach_the_published_jsonl(
    db_session: Session, tmp_path: Path
) -> None:
    observed = datetime(2026, 10, 6, 3, tzinfo=UTC)
    _mixed(db_session, observed)
    result = write_events_jsonl(db_session, tmp_path, observed.date())
    on_disk = read_event_ids(result.path)
    assert set(on_disk) == ELIGIBLE_IDS
    assert set(result.held) == EXCLUDED_IDS
    assert not set(result.present) & EXCLUDED_IDS
    text = result.path.read_text(encoding="utf-8")
    assert "news.google.com" not in text
    assert "synthetic-src" not in text
    # Held rows are untouched in the database (no raw_ref stamped, nothing deleted).
    for event_id in EXCLUDED_IDS:
        row = db_session.get(EventRow, event_id)
        assert row is not None
        assert row.raw_ref is None
    before = result.path.read_bytes()
    again = write_events_jsonl(db_session, tmp_path, observed.date())
    assert again.written == []
    assert result.path.read_bytes() == before


# --- prune: archive filter, delete guard, events_held -------------------------------------


@pytest.mark.db
def test_already_archived_excluded_rows_are_held_and_files_byte_identical(
    db_session: Session, tmp_path: Path
) -> None:
    _mixed(db_session, OLD)
    # A pre-fix nightly publish already wrote every row, excluded ones included.
    path = events_path(tmp_path, OLD.date())
    path.parent.mkdir(parents=True)
    rows = sorted(db_session.scalars(select(EventRow)), key=lambda r: (r.observed_at, r.event_id))
    lines = [
        json.dumps(
            event_record(r, raw_ref=f"events/x#L{n}"), ensure_ascii=False, separators=(",", ":")
        )
        for n, r in enumerate(rows, start=1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.read_bytes()

    result = prune.prune(db_session, tmp_path, now=NOW)

    assert path.read_bytes() == before  # nothing rewritten, appended or removed
    assert result.events_held == len(EXCLUDED_IDS)
    assert result.events_archived == len(ELIGIBLE_IDS)
    # edgar-a is cited by signals (FK keep); cross-clean is outside the SQL guard (keep).
    assert result.events_deleted == 2
    assert result.events_kept == 2
    remaining = _ids(db_session)
    assert remaining >= EXCLUDED_IDS
    assert remaining & ELIGIBLE_IDS == {"edgar-a", "cross-clean"}
    notes = result.to_dict()
    assert notes["events_held"] == len(EXCLUDED_IDS)
    assert not any(i in json.dumps(notes) for i in EXCLUDED_IDS)  # counts only, no ids


@pytest.mark.db
def test_delete_guard_spares_excluded_rows_whatever_ids_are_passed(db_session: Session) -> None:
    _mixed(db_session, OLD)
    every_id = sorted(_ids(db_session))
    deleted = prune._delete_archived(db_session, every_id, NOW - 180 * DAY)
    remaining = _ids(db_session)
    assert remaining >= EXCLUDED_IDS
    assert deleted == 2
    assert {"gh-b", "velocity-c"}.isdisjoint(remaining)


def _parity_rows(session: Session) -> dict[str, tuple[str, str, str, dict[str, Any]]]:
    specs: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
    for source in sorted(INDEPENDENT_SOURCES):
        specs[f"ind-{source}"] = (source, "anything", "https://x.example", {})
        specs[f"case-{source}"] = (source.upper(), "anything", "https://x.example", {})
    specs |= {
        "tell-velocity": ("tell", "hiring_velocity", "", {}),
        "tell-crossing-stamped": ("tell", "score_crossed_threshold", "", _stamp([1])),
        "tell-crossing-legacy": ("tell", "score_crossed_threshold", "", _legacy([1])),
        "tell-other": ("tell", "something_new", "", {}),
        "news-google": ("news", "news_funding", GNEWS_ITEM, _google_payload()),
        "news-feed": ("news", "news_launch", FEED + "/p", _feed_payload()),
        "news-unknown": ("news", "news_launch", "https://x.example/a", {"title": "t"}),
        "news-conflict": (
            "news",
            "news_launch",
            "https://x.example/a",
            {"query": GNEWS_QUERY, "match": {"rule": "R1"}},
        ),
        "synthetic": ("synthetic", "news_launch", "https://x.example", {}),
        "empty": ("", "x", "", {}),
        "velocity-wrong-source": ("greenhouse-ish", "hiring_velocity", "", {}),
    }
    for event_id, (source, type_, url, payload) in specs.items():
        _row(session, event_id, source, type_, url=url, payload=payload)
    return specs


@pytest.mark.db
def test_delete_guard_matches_classify_event(db_session: Session) -> None:
    specs = _parity_rows(db_session)
    sql = set(db_session.scalars(select(EventRow.event_id).where(deletable_predicate())))
    python = {i for i, (s, t, u, p) in specs.items() if classify_event(s, t, u, p).eligible}
    # Under the shipped policy (no permits) the SQL guard and the classifier agree exactly.
    assert sql == python
    # With a permit the classifier clears the feed row; SQL stays narrower (never wider).
    permit = FeedPermit(FEED, "docs/sources/permits/synthetic.md", date(2026, 11, 20))
    permitted = {
        i
        for i, (s, t, u, p) in specs.items()
        if classify_event(s, t, u, p, permits=(permit,)).eligible
    }
    assert sql <= permitted
    assert permitted - sql == {"news-feed"}


# --- crossings: stamp, same-day rescore, Slack filter -------------------------------------


def _crossing(session: Session, company_id: int) -> EventRow:
    return session.scalars(
        select(EventRow).where(
            EventRow.company_id == company_id, EventRow.type == "score_crossed_threshold"
        )
    ).one()


def _legacy_news_signal(session: Session, company_id: int) -> int:
    """A signal stored before the scoring gate, from one of the company's Google News events.

    Since the gate no news event of the scenario derives a signal, so restricted inputs
    for these tests are built explicitly from the stored Google News rows.
    """
    event = session.scalars(
        select(EventRow)
        .where(EventRow.company_id == company_id, EventRow.source == "news")
        .order_by(EventRow.event_id)
    ).first()
    assert event is not None
    assert "news.google.com" in event.url
    signal = Signal(
        company_id=company_id,
        event_id=event.event_id,
        signal_type="launch",
        role_family=None,
        weight=Decimal(1),
        magnitude=Decimal(1),
        half_life_days=30,
        starts_at=event.occurred_at,
    )
    session.add(signal)
    session.flush()
    return signal.id


@pytest.mark.db
def test_emit_crossings_stamps_policy_full_signal_ids_and_top_n(db_session: Session) -> None:
    scenario = sc.build(db_session)
    # Quillstone (Google News only) is no longer scored, so it never crosses.
    quill = scenario.companies[sc.QUILLSTONE]
    assert (
        db_session.scalars(
            select(EventRow).where(
                EventRow.company_id == quill, EventRow.type == "score_crossed_threshold"
            )
        ).all()
        == []
    )
    for domain in (sc.ROBOTICS, sc.BLUEFIN):
        company_id = scenario.companies[domain]
        payload = _crossing(db_session, company_id).payload
        stored = db_session.get(Score, (company_id, "default", sc.AS_OF))
        assert stored is not None
        assert payload["policy_version"] == POLICY_VERSION
        assert payload["signal_ids"] == sorted(c["signal_id"] for c in stored.contributions)
        assert payload["contributions"] == stored.contributions[:STAMP_TOP_N]
        assert payload["top_contributions"] == stored.contributions[:2]  # unchanged key


@pytest.mark.db
def test_same_day_rescore_keeps_a_stamped_crossing_clean(db_session: Session) -> None:
    scenario = sc.build(db_session)
    robotics = scenario.companies[sc.ROBOTICS]
    crossing = _crossing(db_session, robotics)
    stamped = dict(crossing.payload)
    # A real same-day rerun finds the crossing present and leaves it unchanged.
    profile = load_profiles()["default"]
    score_profile(db_session, profile, sc.AS_OF, now=sc.CROSSED_AT + timedelta(hours=2))
    assert _crossing(db_session, robotics).payload == stamped
    # Now the scores row is upserted with a different value and a restricted input (a
    # legacy signal from a Google News row), as a rescore after new rows could do.
    news_signal = _legacy_news_signal(db_session, scenario.companies[sc.QUILLSTONE])
    stored = db_session.get(Score, (robotics, "default", sc.AS_OF))
    assert stored is not None
    stored.score = Decimal("31.0")
    stored.contributions = [
        *stored.contributions,
        {"signal_id": news_signal, "contribution": 3.3, "url": "https://x.example/n"},
    ]
    db_session.flush()
    accounts = crossed_accounts(db_session, scenario.public, sc.NOW)
    assert [a.domain for a in accounts] == [sc.ROBOTICS]
    assert accounts[0].score == 27.7  # from the stamp, not the rescored row
    assert all(c["signal_id"] != news_signal for c in accounts[0].contributions)


@pytest.mark.db
def test_restricted_and_legacy_crossings_are_absent_from_slack(db_session: Session) -> None:
    scenario = sc.build(db_session)
    # A stamped crossing that cites a legacy Google News signal (written as the pre-gate
    # scorer would have): stamped, but restricted.
    quill_id = scenario.companies[sc.QUILLSTONE]
    restricted = _legacy_news_signal(db_session, quill_id)
    db_session.add(
        EventRow(
            event_id="synthetic-restricted-crossing",
            company_id=quill_id,
            source="tell",
            type="score_crossed_threshold",
            occurred_at=sc.CROSSED_AT,
            observed_at=sc.CROSSED_AT,
            magnitude=Decimal(9),
            url="https://x.example/n",
            payload={**_stamp([restricted]), "as_of": sc.AS_OF.isoformat()},
            run_id="synthetic",
        )
    )
    db_session.flush()
    quill = _crossing(db_session, quill_id)
    assert quill.payload["policy_version"] == POLICY_VERSION  # stamped, but restricted
    public = [a.domain for a in crossed_accounts(db_session, scenario.public, sc.NOW)]
    assert sc.QUILLSTONE not in public
    robotics = _crossing(db_session, scenario.companies[sc.ROBOTICS])
    robotics.payload = _legacy_from(robotics.payload)
    db_session.flush()
    assert crossed_accounts(db_session, scenario.public, sc.NOW) == []


def _legacy_from(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in payload.items()
        if k not in {"policy_version", "signal_ids", "contributions"}
    }


# --- HubSpot receipt cutover --------------------------------------------------------------


@pytest.mark.db
def test_load_receipt_trusts_only_scores_on_or_after_the_cutover(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = add_company(db_session, "receipt.example")
    as_of = date(2026, 10, 6)
    db_session.add(
        Score(
            company_id=company.id,
            icp_profile="default",
            as_of=as_of,
            score=Decimal("7.5"),
            contributions=[{"signal_id": 1, "contribution": 7.5, "url": "https://x.example"}],
        )
    )
    db_session.flush()
    profile = load_profiles()["default"]
    assert source_eligibility.ELIGIBILITY_CUTOVER is None
    with pytest.raises(LookupError, match="cutover"):
        hubspot.load_receipt(db_session, "receipt.example", profile)
    monkeypatch.setattr(source_eligibility, "ELIGIBILITY_CUTOVER", as_of + DAY)
    with pytest.raises(LookupError, match="cutover"):
        hubspot.load_receipt(db_session, "receipt.example", profile)
    monkeypatch.setattr(source_eligibility, "ELIGIBILITY_CUTOVER", as_of)
    assert hubspot.load_receipt(db_session, "receipt.example", profile).as_of == as_of
