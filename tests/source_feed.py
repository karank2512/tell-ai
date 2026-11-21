"""A test-only permitted company feed, for synthetic news events in mechanics tests.

Source eligibility excludes news rows whose provenance is Google News or unknown. Tests
that exercise scoring, receipts or the API with synthetic ``news_*`` events tag them as
coming from :data:`TEST_FEED` (``payload.match.rule = "R1"``, ``payload.query`` = the feed)
and ask for the ``permitted_test_feed`` fixture, which records :data:`TEST_PERMIT` in
``source_eligibility.PERMITS`` for that test only. Production ``PERMITS`` stays empty.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from tell.ops.source_eligibility import FeedPermit

TEST_FEED = "https://news.example.test/feed.xml"
TEST_PERMIT = FeedPermit(TEST_FEED, "docs/sources/permits/test-feed.md", date(2026, 11, 21))


def feed_provenance(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """``payload`` plus the company-feed provenance markers of :data:`TEST_FEED`."""
    return {"match": {"rule": "R1"}, "query": TEST_FEED, **(payload or {})}
