"""Synthetic tests for the unwired source-exclusion candidate (``tell/ops/source_eligibility.py``).

No network, no database, no environment reads. The two fixture tests run the real
``derive_events`` over the existing frozen news fixtures so the provenance rules are
pinned to what the collector writes, not to a hand-written guess.
"""

from __future__ import annotations

import ast
import copy
import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tell.collectors.news import NewsFeed, derive_events
from tell.ops import source_eligibility as se
from tell.ops.source_eligibility import (
    POLICY_VERSION,
    ArchiveDisposition,
    Decision,
    ExclusionAudit,
    FeedPermit,
    NewsCoverage,
    Provenance,
    Reason,
    StoredScore,
    archive_disposition,
    cached_output_trusted,
    classify_crossing,
    classify_event,
    eligible_signal_ids,
    news_coverage,
    news_fetch_plan,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
NOW = datetime(2026, 11, 17, tzinfo=UTC)
GNEWS_QUERY = "https://news.google.com/rss/search?q=%22Quillstone+Analytics%22+when%3A7d"
GNEWS_ITEM = "https://news.google.com/rss/articles/CBMiSYNTHETIC?oc=5"
FEED = "https://quillstone.example/blog/atom.xml"
OTHER_FEED = "https://brightloom.example/feed"
PERMIT = FeedPermit(FEED, "docs/sources/permits/quillstone.md", date(2026, 11, 17))

EXCLUDED = Decision(Provenance.GOOGLE_NEWS, Reason.GOOGLE_NEWS_EXCLUDED)
CLEAN = Decision(Provenance.INDEPENDENT, Reason.INDEPENDENT_SOURCE)


def _google(**over: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "title": "Quillstone Analytics raises $24M Series A",
        "title_redacted": False,
        "source": "Ledger Daily",
        "matched_regex": "funding.raises",
        "category": "funding",
        "query": GNEWS_QUERY,
        "match": {"rule": "news_query", "guard": "name_in_title"},
        "key": "news:abc",
    }
    payload.update(over)
    return GNEWS_ITEM, payload


def _company(feed: str = FEED, **over: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "title": "Introducing Forecast 2.0",
        "title_redacted": False,
        "source": "Quillstone Analytics Blog",
        "matched_regex": "launch.introduces",
        "category": "launch",
        "query": feed,
        "match": {"rule": "R1", "confidence": 0.98},
        "key": "news:def",
    }
    payload.update(over)
    return feed.rsplit("/", 1)[0] + "/post-1", payload


def _news(url: object, payload: Any, **kw: Any) -> Decision:
    return classify_event("news", "news_launch", url, payload, **kw)


# --- 1. provenance, pinned to the real collector output -----------------------------------


def _derived(name: str) -> list[Any]:
    feed = NewsFeed.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))
    events, _ = derive_events(feed, company_id=1, run_id="r", now=NOW)
    assert events, name
    return events


def test_collector_google_news_rows_are_identified_and_excluded() -> None:
    for event in _derived("normal.json"):
        decision = classify_event(event.source, event.type, event.url, event.payload)
        assert decision == Decision(Provenance.GOOGLE_NEWS, Reason.GOOGLE_NEWS_EXCLUDED)
        assert not decision.eligible


def test_collector_company_feed_rows_need_a_feed_specific_permit() -> None:
    events = _derived("normal_company_feed.json")
    for event in events:
        args = (event.source, event.type, event.url, event.payload)
        assert classify_event(*args).reason is Reason.NO_FEED_PERMIT
        assert classify_event(*args, permits=(PERMIT,)).reason is Reason.FEED_PERMITTED
        other = FeedPermit(OTHER_FEED, "docs/sources/permits/brightloom.md", date(2026, 11, 17))
        assert classify_event(*args, permits=(other,)).reason is Reason.NO_FEED_PERMIT


def test_publisher_label_is_never_provenance() -> None:
    url, payload = _google(source="Quillstone Analytics Blog")
    assert _news(url, payload).provenance is Provenance.GOOGLE_NEWS
    url, payload = _company(source="Google News")
    assert _news(url, payload, permits=(PERMIT,)).reason is Reason.FEED_PERMITTED


def test_off_domain_company_feed_rule_is_company_feed_not_google() -> None:
    url, payload = _company(match={"rule": "company_feed"})
    assert _news(url, payload) == Decision(Provenance.COMPANY_FEED, Reason.NO_FEED_PERMIT)


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        # Google rule and query, but a publisher URL: not claimed as Google, still excluded.
        ("https://ledgerdaily.example/a", _google()[1]),
        # Company-feed rule, Google item URL.
        (GNEWS_ITEM, _company()[1]),
        # Company-feed rule, Google query.
        (_company()[0], _company(query=GNEWS_QUERY)[1]),
        # Google rule on a company feed's query and URL.
        _company(match={"rule": "news_query"}),
    ],
)
def test_conflicting_markers_fail_closed(url: str, payload: dict[str, Any]) -> None:
    assert _news(url, payload, permits=(PERMIT,)) == Decision(
        Provenance.UNKNOWN, Reason.PROVENANCE_CONFLICT
    )


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        (GNEWS_ITEM, {k: v for k, v in _google()[1].items() if k != "match"}),
        (GNEWS_ITEM, {k: v for k, v in _google()[1].items() if k != "query"}),
        ("", _google()[1]),
        (None, _google()[1]),
        ("not a url", _google()[1]),
        ("http://[::1", _google()[1]),
        (GNEWS_ITEM, _google(match="news_query")[1]),
        (GNEWS_ITEM, _google(match={"rule": 7})[1]),
        (GNEWS_ITEM, _google(query=None)[1]),
        (_company()[0], _company(match={"rule": "R9"})[1]),
        (GNEWS_ITEM, None),
        (GNEWS_ITEM, ["not", "a", "mapping"]),
        (GNEWS_ITEM, {}),
    ],
)
def test_missing_or_unrecognized_provenance_fails_closed(url: object, payload: Any) -> None:
    decision = _news(url, payload, permits=(PERMIT,))
    assert decision == Decision(Provenance.UNKNOWN, Reason.PROVENANCE_UNKNOWN)
    assert not decision.eligible


@pytest.mark.parametrize("source", sorted(se.INDEPENDENT_SOURCES))
def test_independent_sources_stay_eligible(source: str) -> None:
    decision = classify_event(source, "anything", "https://x.example", {"title": "news"})
    assert decision == CLEAN
    assert decision.eligible


def test_tell_events_split_by_type() -> None:
    assert classify_event("tell", "hiring_velocity", "", {}) == CLEAN
    crossing = classify_event("tell", "score_crossed_threshold", "", {})
    assert crossing.reason is Reason.LINEAGE_REQUIRED
    assert not crossing.eligible
    assert classify_event("tell", "something_new", "", {}).reason is Reason.UNRECOGNIZED_SOURCE


@pytest.mark.parametrize("source", ["gdelt", "edgar_backfill", "", "NEWS", "Google"])
def test_unrecognized_sources_fail_closed(source: str) -> None:
    decision = classify_event(source, "news_funding", GNEWS_ITEM, _google()[1])
    assert decision.reason is Reason.UNRECOGNIZED_SOURCE
    assert not decision.eligible


# --- 2. permission is code, not configuration -------------------------------------------


def test_default_fetch_plan_fetches_nothing() -> None:
    plan = news_fetch_plan()
    assert (plan.google_news, plan.company_feed_urls, plan.runs) == (False, frozenset(), False)
    assert se.PERMITS == ()


def test_permits_never_enable_google_news() -> None:
    plan = news_fetch_plan((PERMIT,))
    assert plan.google_news is False
    assert plan.company_feed_urls == frozenset({FEED})
    assert plan.runs


def test_environment_cannot_enable_news(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TELL_GOOGLE_NEWS_ENABLED",
        "TELL_NEWS_ENABLED",
        "TELL_NEWS_PERMITS",
        "TELL_SOURCE_POLICY",
    ):
        monkeypatch.setenv(name, "1")
    # The module reads no environment at import or call time (see the AST test below),
    # so these settings can only matter if someone adds such a read; then this fails.
    assert se.PERMITS == ()
    assert news_fetch_plan().runs is False
    url, payload = _google()
    assert not classify_event("news", "news_funding", url, payload).eligible


def test_module_is_pure_standard_library() -> None:
    tree = ast.parse(Path(se.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {
        "__future__",
        "json",
        "math",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "typing",
        "urllib",
    }
    assert imported <= allowed, imported - allowed
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not ({"os", "environ", "getenv", "open"} & (names | attrs))


@pytest.mark.parametrize(
    ("feed_url", "evidence"),
    [
        ("https://news.google.com/rss/search?q=x", "docs/x.md"),
        ("https://www.google.com/feed", "docs/x.md"),
        ("http://quillstone.example/feed", "docs/x.md"),
        ("https://user:pw@quillstone.example/feed", "docs/x.md"),
        ("https:///feed", "docs/x.md"),
        (FEED, "https://quillstone.example/terms"),
        (FEED, "notes/x.md"),
        (FEED, "docs/../secrets.md"),
        (FEED, ""),
    ],
)
def test_bad_permits_are_refused(feed_url: str, evidence: str) -> None:
    with pytest.raises(ValueError, match=r"must|cannot"):
        FeedPermit(feed_url, evidence, date(2026, 11, 17))


# --- 3. descendants and lineage ------------------------------------------------------------


def _crossing(score: object = 9.0, top: object = None) -> dict[str, Any]:
    return {
        "profile": "default",
        "as_of": "2026-11-10",
        "score": score,
        "threshold": 8.0,
        "top_contributions": [{"signal_id": 1, "url": "https://x"}] if top is None else top,
    }


def test_clean_crossing_is_eligible() -> None:
    decision = classify_crossing(_crossing(), StoredScore(9.0, (1, 2)), {1: CLEAN, 2: CLEAN})
    assert decision == Decision(Provenance.DERIVED, Reason.LINEAGE_CLEAN)
    assert decision.eligible


def test_any_restricted_contributor_restricts_the_crossing() -> None:
    # Signal 2 is a small Google News contribution, not in the top two.
    decision = classify_crossing(
        _crossing(top=[{"signal_id": 1}]),
        StoredScore(9.0, (1, 3, 2)),
        {1: CLEAN, 2: EXCLUDED, 3: CLEAN},
    )
    assert decision.reason is Reason.LINEAGE_RESTRICTED
    assert not decision.eligible


@pytest.mark.parametrize(
    ("payload", "stored", "decisions"),
    [
        (_crossing(), None, {1: CLEAN}),  # no stored score row
        (_crossing(), StoredScore(9.0, ()), {}),  # empty contributions
        (_crossing(score=9.0), StoredScore(8.7, (1,)), {1: CLEAN}),  # row rescored since
        (_crossing(score="9.0"), StoredScore(9.0, (1,)), {1: CLEAN}),
        (_crossing(score=True), StoredScore(1.0, (1,)), {1: CLEAN}),
        (_crossing(top=[]), StoredScore(9.0, (1,)), {1: CLEAN}),
        (_crossing(top="x"), StoredScore(9.0, (1,)), {1: CLEAN}),
        (_crossing(top=[{"signal_id": 5}]), StoredScore(9.0, (1,)), {1: CLEAN, 5: CLEAN}),
        (_crossing(top=[{"signal_id": True}]), StoredScore(9.0, (1,)), {1: CLEAN}),
        (_crossing(top=["1"]), StoredScore(9.0, (1,)), {1: CLEAN}),
        (_crossing(), StoredScore(9.0, (1, 2)), {1: CLEAN}),  # signal 2 unresolved
    ],
)
def test_incomplete_lineage_fails_closed(
    payload: dict[str, Any], stored: StoredScore | None, decisions: dict[int, Decision]
) -> None:
    decision = classify_crossing(payload, stored, decisions)
    assert decision.reason is Reason.LINEAGE_INCOMPLETE
    assert not decision.eligible


NONFINITE = [math.nan, math.inf, -math.inf]
"""NaN compares false with everything and ``inf - inf`` is NaN: both once cleared a crossing."""


def _lineage(
    crossed: object, stored: Any, decisions: dict[int, Decision] | None = None
) -> Reason:
    decisions = {1: CLEAN, 2: CLEAN} if decisions is None else decisions
    payload = _crossing(score=crossed, top=[{"signal_id": 1}])
    # ``stored`` is Any on purpose: non-float stored scores are among the inputs under test.
    return classify_crossing(payload, StoredScore(stored, (1, 2)), decisions).reason


@pytest.mark.parametrize("value", NONFINITE, ids=["nan", "+inf", "-inf"])
@pytest.mark.parametrize("position", ["crossed", "stored"])
def test_nonfinite_score_in_either_position_fails_closed(position: str, value: float) -> None:
    crossed, stored = (value, 9.0) if position == "crossed" else (9.0, value)
    assert _lineage(crossed, stored) is Reason.LINEAGE_INCOMPLETE
    # Unknown, not restricted: a non-finite score means the stored row cannot be trusted.
    assert _lineage(crossed, stored, {1: CLEAN, 2: EXCLUDED}) is Reason.LINEAGE_INCOMPLETE


@pytest.mark.parametrize("stored", NONFINITE, ids=["nan", "+inf", "-inf"])
@pytest.mark.parametrize("crossed", NONFINITE, ids=["nan", "+inf", "-inf"])
def test_nonfinite_scores_in_both_positions_fail_closed(crossed: float, stored: float) -> None:
    assert _lineage(crossed, stored) is Reason.LINEAGE_INCOMPLETE


@pytest.mark.parametrize(
    ("crossed", "stored"),
    [
        (10**400, 9.0),  # too large for a float: OverflowError before the fix
        (9.0, 10**400),
        (9.0, Decimal("9.0")),  # TypeError before the fix; callers must pass a float
        (9.0, Decimal("NaN")),
        (9.0, "9.0"),
        (9.0, None),
        (9.0, True),
    ],
)
def test_unusable_stored_or_crossed_score_fails_closed(crossed: object, stored: object) -> None:
    assert _lineage(crossed, stored) is Reason.LINEAGE_INCOMPLETE


@pytest.mark.parametrize(
    ("crossed", "stored", "decisions", "expected"),
    [
        (9.0, 9.0, None, Reason.LINEAGE_CLEAN),
        (9, 9.0, None, Reason.LINEAGE_CLEAN),
        (9.0 + 1e-10, 9.0, None, Reason.LINEAGE_CLEAN),  # within tolerance
        (0.0, -0.0, None, Reason.LINEAGE_CLEAN),
        (9.0, 9.0, {1: CLEAN, 2: EXCLUDED}, Reason.LINEAGE_RESTRICTED),
        (9.0, 9.0, {1: CLEAN}, Reason.LINEAGE_INCOMPLETE),  # signal 2 unresolved
        (9.0, 8.7, None, Reason.LINEAGE_INCOMPLETE),
        (9.0 + 1e-8, 9.0, None, Reason.LINEAGE_INCOMPLETE),  # outside tolerance
        (1e308, -1e308, None, Reason.LINEAGE_INCOMPLETE),  # finite, difference overflows
    ],
)
def test_finite_score_controls_unchanged(
    crossed: object, stored: float, decisions: dict[int, Decision] | None, expected: Reason
) -> None:
    assert _lineage(crossed, stored, decisions) is expected


def test_mixed_history_keeps_independent_signals_only() -> None:
    rows = {
        1: classify_event("edgar", "form_d_filed", "https://www.sec.gov/x", {}),
        2: classify_event("greenhouse", "job_opened", "https://boards.example/1", {}),
        3: classify_event("tell", "hiring_velocity", "", {}),
        4: _news(*_google()),
        5: _news(*_company()),
        6: _news(GNEWS_ITEM, {}),
    }
    assert eligible_signal_ids(rows) == frozenset({1, 2, 3})
    assert eligible_signal_ids({**rows, 5: _news(*_company(), permits=(PERMIT,))}) == {1, 2, 3, 5}


def test_cached_outputs_without_this_policy_version_are_untrusted() -> None:
    assert cached_output_trusted(POLICY_VERSION)
    assert not cached_output_trusted(None)
    assert not cached_output_trusted("source-exclusion/0")


# --- 4. retention is separate from exclusion ----------------------------------------------


CUTOFF = NOW - timedelta(days=180)


@pytest.mark.parametrize("age", [timedelta(0), timedelta(days=181), timedelta(days=3650)])
@pytest.mark.parametrize(
    "decision",
    [
        EXCLUDED,
        Decision(Provenance.COMPANY_FEED, Reason.NO_FEED_PERMIT),
        Decision(Provenance.UNKNOWN, Reason.PROVENANCE_UNKNOWN),
        Decision(Provenance.UNKNOWN, Reason.PROVENANCE_CONFLICT),
        Decision(Provenance.DERIVED, Reason.LINEAGE_REQUIRED),
        Decision(Provenance.DERIVED, Reason.LINEAGE_INCOMPLETE),
        Decision(Provenance.DERIVED, Reason.LINEAGE_RESTRICTED),
    ],
)
def test_excluded_rows_are_held_at_any_age(decision: Decision, age: timedelta) -> None:
    assert archive_disposition(decision, NOW - age, CUTOFF) is ArchiveDisposition.HOLD


def test_eligible_rows_keep_todays_prune_behavior() -> None:
    old, new = NOW - timedelta(days=181), NOW - timedelta(days=1)
    assert archive_disposition(CLEAN, old, CUTOFF) is ArchiveDisposition.ARCHIVE_THEN_PRUNE
    assert archive_disposition(CLEAN, new, CUTOFF) is ArchiveDisposition.LIVE


def test_no_reason_is_both_excluded_and_archivable() -> None:
    for reason in Reason:
        decision = Decision(Provenance.UNKNOWN, reason)
        disposition = archive_disposition(decision, NOW - timedelta(days=4000), CUTOFF)
        assert (disposition is ArchiveDisposition.HOLD) == (not decision.eligible)


# --- 5. reprocessing semantics ----------------------------------------------------------


def test_classification_is_deterministic_and_never_mutates_input() -> None:
    for url, payload in (_google(), _company(), (GNEWS_ITEM, {"match": {}})):
        before = copy.deepcopy(payload)
        first = _news(url, payload)
        assert _news(url, payload) == first
        assert payload == before


def test_exclusion_is_reversible_without_touching_the_row() -> None:
    url, payload = _company()
    snapshot = json.dumps(payload, sort_keys=True)
    assert not _news(url, payload).eligible
    assert _news(url, payload, permits=(PERMIT,)).eligible
    assert not _news(url, payload, permits=()).eligible  # permit withdrawn
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_one_feed_permit_changes_nothing_else() -> None:
    other_url, other = _company(OTHER_FEED)
    assert not _news(other_url, other, permits=(PERMIT,)).eligible
    assert not _news(*_google(), permits=(PERMIT,)).eligible
    assert not _news(GNEWS_ITEM, _google(query=FEED)[1], permits=(PERMIT,)).eligible


def test_every_decision_carries_the_policy_version() -> None:
    decisions = [_news(*_google()), _news(*_company()), classify_event("edgar", "x", "", {})]
    assert {d.policy_version for d in decisions} == {POLICY_VERSION}


# --- 6. coverage, audit and log hygiene -----------------------------------------------------


def test_coverage_is_unavailable_not_zero() -> None:
    assert news_coverage() == (
        NewsCoverage.UNAVAILABLE,
        "google_news_excluded;no_company_feed_permits",
    )
    assert news_coverage((PERMIT,))[0] is NewsCoverage.PARTIAL
    empty = ExclusionAudit().to_log_dict()
    assert empty["evaluated"] == 0
    assert empty["news_coverage"] == "unavailable"


SECRET_DB = "postgresql+psycopg://" + "synthetic-user:synthetic-pw@db.example/tell"
SECRET_HOOK = "https://hooks.slack.example/services/" + "T000/B000/synthetic"
TITLE = "Quillstone Analytics raises $24M Series A"


def test_log_line_holds_counts_only() -> None:
    url, google = _google(source=SECRET_HOOK, extra=SECRET_DB)
    company_url, company = _company(title=TITLE + " " + SECRET_DB)
    audit = ExclusionAudit()
    audit.extend(
        [
            ("e-google", _news(url, google)),
            ("e-company", _news(company_url, company)),
            ("e-unknown", _news(SECRET_HOOK, {"query": SECRET_DB})),
            ("e-edgar", classify_event("edgar", "form_d_filed", SECRET_HOOK, {"x": SECRET_DB})),
        ]
    )
    line = audit.to_log_line()
    for leaked in (
        SECRET_DB,
        SECRET_HOOK,
        TITLE,
        GNEWS_ITEM,
        GNEWS_QUERY,
        FEED,
        "e-google",
        "://",
        "@",
    ):
        assert leaked not in line
    record = json.loads(line)
    assert set(record) == {
        "policy_version",
        "evaluated",
        "eligible",
        "excluded",
        "by_reason",
        "by_provenance",
        "news_coverage",
        "news_coverage_reason",
        "permits",
    }
    assert (record["evaluated"], record["eligible"], record["excluded"]) == (4, 1, 3)
    assert record["by_reason"] == {
        "google_news_excluded": 1,
        "independent_source": 1,
        "no_feed_permit": 1,
        "provenance_unknown": 1,
    }
    assert audit.excluded_ids() == ("e-google", "e-company", "e-unknown")


def test_decisions_and_permits_carry_no_row_content() -> None:
    url, payload = _google(title=TITLE, source=SECRET_HOOK)
    rendered = repr(_news(url, payload))
    for leaked in (TITLE, SECRET_HOOK, "google.com"):
        assert leaked not in rendered
