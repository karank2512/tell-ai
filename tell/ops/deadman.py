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
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.collectors.base import iso_utc
from tell.models import Run

__all__ = [
    "SCHEDULED_SOURCES",
    "SILENT_AFTER",
    "DeadmanReport",
    "SourceCheck",
    "check",
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

CheckStatus = Literal["ok", "stale", "never_ran"]


@dataclass(frozen=True)
class SourceCheck:
    """The dead-man verdict for one source."""

    source: str
    last_ok_at: str | None
    status: CheckStatus

    @property
    def silent(self) -> bool:
        return self.status != "ok"


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
        if self.ok:
            head = (
                f"**tell dead-man: green.** All {len(self.sources)} sources "
                f"succeeded in the last {hours} h."
            )
        else:
            head = (
                f"**tell dead-man: red.** No successful run in {hours} h: {', '.join(self.silent)}."
            )
        lines = [f"### dead-man ({self.checked_at})", "", head, ""]
        for s in self.sources:
            when = s.last_ok_at or "never"
            lines.append(f"- `{s.source}`: {s.status} (last ok: {when})")
        return "\n".join(lines) + "\n"


def check(
    session: Session, now: datetime, sources: Sequence[str] = SCHEDULED_SOURCES
) -> DeadmanReport:
    """Check ``sources`` at ``now`` (see the module docstring). Reads only."""
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
    checks: list[SourceCheck] = []
    for name in names:
        finished: datetime | None = last_ok.get(name)
        status: CheckStatus
        if name not in ever:
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
