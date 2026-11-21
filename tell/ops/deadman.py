"""The dead-man switch (spec section 12, "Run-health in production"; section 11 ``ingest.yml``).

For each checked source, the latest ``finished_at`` of a ``runs`` row with
``status = 'ok'`` must lie within the last 24 hours. A source that fails the check is
*silent*:

``stale``
    It has run before but its last successful run is older than 24 hours (or it has
    never succeeded).
``never_ran``
    It has no ``runs`` row at all. Unlike the public health page, which shows such a
    source as ``never_run`` without turning red (ADR 0023), the dead-man treats a
    scheduled source that never ran as silent: the step exists to catch a schedule that
    stopped feeding a source.

``skipped_by_policy``
    The source cannot run under source eligibility, decided at call time and never read
    from settings: news, when :func:`~tell.ops.source_eligibility.news_fetch_plan` over
    the current ``PERMITS`` does not run (no company-feed permit; Google News is always
    excluded, and ``tell ingest --source news`` then skips before writing any ``runs``
    row). It is listed in the JSON and the step summary, but it is not silent and never
    fails the check. No run row is invented for it. With a permit, news is checked as
    before.
``policy_breach``
    A policy-skipped source has a ``runs`` row that *started* in the last 24 hours: it ran
    although source eligibility excludes it. That is silent and fails the check, so a
    breach is never hidden as a skip. Older runs (from before the exclusion) are not a
    breach. :func:`policy_statuses` is the one rule the health page and the weekly
    operations line share.

The checked sources default to :data:`SCHEDULED_SOURCES`, the collectors that
``ingest.yml`` runs (the four ATS vendors, EDGAR and news). The optional GitHub
collector is not scheduled, so it is not checked unless named explicitly; there is no
pause flag in the configuration, so "active source" means "scheduled source".

``tell deadman`` exits 1 when any source is silent and appends the report to
``$GITHUB_STEP_SUMMARY`` when that variable is set, which turns the scheduled run red
and triggers GitHub's free failure email.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.models import Run
from tell.ops import source_eligibility

__all__ = [
    "SCHEDULED_SOURCES",
    "SILENT_AFTER",
    "DeadmanReport",
    "SourceCheck",
    "check",
    "policy_skipped",
    "policy_statuses",
    "write_step_summary",
]

SCHEDULED_SOURCES: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "edgar",
    "news",
)
"""The sources ``ingest.yml`` runs on its cron (spec section 11, minus Workday/Oracle)."""

SILENT_AFTER = timedelta(hours=24)
"""A source whose last successful run is older than this is silent."""

CheckStatus = Literal["ok", "stale", "never_ran", "skipped_by_policy", "policy_breach"]
PolicyStatus = Literal["skipped_by_policy", "policy_breach"]

SKIPPED_BY_POLICY: Final = "skipped_by_policy"
POLICY_BREACH: Final = "policy_breach"
_SILENT_STATUSES: frozenset[str] = frozenset({"stale", "never_ran", POLICY_BREACH})


def policy_skipped() -> frozenset[str]:
    """Sources source eligibility stops from running, computed at call time.

    News, unless :func:`~tell.ops.source_eligibility.news_fetch_plan` over the current
    ``PERMITS`` (a code constant, never a setting) would fetch something.
    """
    plan = source_eligibility.news_fetch_plan(source_eligibility.PERMITS)
    return frozenset() if plan.runs else frozenset({"news"})


def policy_statuses(
    session: Session, now: datetime, sources: Sequence[str]
) -> dict[str, PolicyStatus]:
    """``{source: skipped_by_policy | policy_breach}`` for the policy-skipped ``sources``.

    ``policy_breach`` when a ``runs`` row of the source started in ``(now - 24 h, now]``;
    sources that policy does not skip are absent (check them as usual). Reads only.
    """
    skipped = [name for name in dict.fromkeys(sources) if name in policy_skipped()]
    if not skipped:
        return {}
    breached = set(
        session.scalars(
            select(Run.source)
            .where(
                Run.source.in_(skipped),
                Run.started_at > now - SILENT_AFTER,
                Run.started_at <= now,
            )
            .distinct()
        )
    )
    return {name: POLICY_BREACH if name in breached else SKIPPED_BY_POLICY for name in skipped}


@dataclass(frozen=True)
class SourceCheck:
    """The dead-man verdict for one source."""

    source: str
    last_ok_at: str | None
    status: CheckStatus

    @property
    def silent(self) -> bool:
        return self.status in _SILENT_STATUSES


@dataclass(frozen=True)
class DeadmanReport:
    """Every checked source at one instant."""

    checked_at: str
    sources: list[SourceCheck]

    @property
    def silent(self) -> list[str]:
        """Names of the silent sources, in check order."""
        return [s.source for s in self.sources if s.silent]

    @property
    def ok(self) -> bool:
        return not self.silent

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "ok": self.ok,
            "silent": self.silent,
            "sources": [
                {"source": s.source, "last_ok_at": s.last_ok_at, "status": s.status}
                for s in self.sources
            ],
        }

    def markdown(self) -> str:
        """The step-summary block: a verdict line, then one bullet per source."""
        hours = int(SILENT_AFTER.total_seconds() // 3600)
        skipped = [s.source for s in self.sources if s.status == SKIPPED_BY_POLICY]
        checked = len(self.sources) - len(skipped)
        if self.ok and checked == 0:
            head = (
                "**tell dead-man: green.** Nothing to check: every requested source is "
                "skipped by source-eligibility policy."
            )
        elif self.ok:
            head = (
                f"**tell dead-man: green.** All {checked} sources succeeded in the last {hours} h."
            )
        else:
            head = (
                f"**tell dead-man: red.** No successful run in {hours} h: {', '.join(self.silent)}."
            )
        if skipped:
            head += f" Skipped by source-eligibility policy (not checked): {', '.join(skipped)}."
        lines = [f"### dead-man ({self.checked_at})", "", head, ""]
        for s in self.sources:
            when = s.last_ok_at or "never"
            lines.append(f"- `{s.source}`: {s.status} (last ok: {when})")
        return "\n".join(lines) + "\n"


def check(
    session: Session, now: datetime, sources: Sequence[str] = SCHEDULED_SOURCES
) -> DeadmanReport:
    """Check ``sources`` at ``now`` (see the module docstring). Reads only.

    A source in :func:`policy_skipped` (computed now) is reported ``skipped_by_policy``,
    whether it came from the default list or was named explicitly.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = f"now must be timezone-aware, got naive {now!r}"
        raise ValueError(msg)
    names = list(dict.fromkeys(sources))
    if not names:
        msg = "sources must name at least one source"
        raise ValueError(msg)
    last_ok = {
        str(source): finished
        for source, finished in session.execute(
            select(Run.source, func.max(Run.finished_at))
            .where(
                Run.source.in_(names),
                Run.status == "ok",
                Run.finished_at.is_not(None),
                Run.finished_at <= now,
            )
            .group_by(Run.source)
        )
    }
    ever = {
        str(source)
        for source in session.scalars(
            select(Run.source).where(Run.source.in_(names), Run.started_at <= now).distinct()
        )
    }
    by_policy = policy_statuses(session, now, names)
    checks: list[SourceCheck] = []
    for name in names:
        finished: datetime | None = last_ok.get(name)
        status: CheckStatus
        if name in by_policy:
            status = by_policy[name]
        elif name not in ever:
            status = "never_ran"
        elif finished is None or finished < now - SILENT_AFTER:
            status = "stale"
        else:
            status = "ok"
        checks.append(
            SourceCheck(
                source=name,
                last_ok_at=iso_utc(finished) if finished is not None else None,
                status=status,
            )
        )
    return DeadmanReport(checked_at=iso_utc(now), sources=checks)


def write_step_summary(report: DeadmanReport, path: Path) -> None:
    """Append the report's Markdown to a GitHub Actions step summary file."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(report.markdown())
