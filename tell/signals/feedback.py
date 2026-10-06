"""Feedback loop v1 (spec section 8): verdict labels and the precision-by-signal report.

``feedback.verdict`` maps to a label per signal: ``useful``, ``worked``, ``booked`` and
``won`` are positives; ``not_useful`` and ``already_knew`` are negatives. The weekly
report groups a profile's labels by ``signal_type`` and ``role_family`` and prints
precision per group. Weights stay hand-tuned until a profile has at least
:data:`MIN_LABELS_FOR_REWEIGHTING` labels; the report says so while that holds.
Nothing here changes a weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tell.models import FEEDBACK_VERDICTS, Feedback, Signal, Watchlist

__all__ = [
    "MIN_LABELS_FOR_REWEIGHTING",
    "NEGATIVE",
    "POSITIVE",
    "VERDICT_LABELS",
    "PrecisionReport",
    "PrecisionRow",
    "format_markdown",
    "label_for",
    "precision_report",
]

POSITIVE: Final = "positive"
NEGATIVE: Final = "negative"

VERDICT_LABELS: Final[dict[str, str]] = {
    "useful": POSITIVE,
    "worked": POSITIVE,
    "booked": POSITIVE,
    "won": POSITIVE,
    "not_useful": NEGATIVE,
    "already_knew": NEGATIVE,
}
"""Every ``feedback.verdict`` value and its label."""

if set(VERDICT_LABELS) != set(FEEDBACK_VERDICTS):  # pragma: no cover - module invariant
    msg = "VERDICT_LABELS must cover exactly the feedback verdicts of tell.models"
    raise RuntimeError(msg)

MIN_LABELS_FOR_REWEIGHTING = 300
"""Below this many labels per profile the spec keeps weights hand-tuned."""

HAND_TUNED_NOTE = (
    "weights stay hand-tuned: {labels} labels for profile {profile!r}, "
    f"fewer than the {MIN_LABELS_FOR_REWEIGHTING} the spec requires before a logistic "
    "re-weighting may be proposed"
)


def label_for(verdict: str) -> str:
    """``positive`` or ``negative``; raises ``KeyError`` for an unknown verdict."""
    return VERDICT_LABELS[verdict]


@dataclass(frozen=True)
class PrecisionRow:
    """Labels of one ``(signal_type, role_family)`` under one profile."""

    signal_type: str
    role_family: str | None
    positives: int
    negatives: int

    @property
    def labels(self) -> int:
        return self.positives + self.negatives

    @property
    def precision(self) -> float | None:
        """``positives / labels``; ``None`` when there are no labels."""
        return self.positives / self.labels if self.labels else None


@dataclass(frozen=True)
class PrecisionReport:
    """The weekly report for one profile."""

    profile: str
    rows: list[PrecisionRow]
    note: str | None
    """Set while the profile has fewer than :data:`MIN_LABELS_FOR_REWEIGHTING` labels."""

    @property
    def labels(self) -> int:
        return sum(r.labels for r in self.rows)

    @property
    def positives(self) -> int:
        return sum(r.positives for r in self.rows)

    @property
    def precision(self) -> float | None:
        return self.positives / self.labels if self.labels else None


def precision_report(session: Session, profile: str) -> PrecisionReport:
    """Precision by ``signal_type`` and ``role_family`` for one profile's feedback.

    Feedback belongs to a profile through its watchlist (``watchlists.icp_profile``).
    Rows are ordered by signal type then family; verdicts outside
    :data:`VERDICT_LABELS` are ignored.
    """
    stmt = (
        select(Signal.signal_type, Signal.role_family, Feedback.verdict, func.count())
        .select_from(Feedback)
        .join(Signal, Signal.id == Feedback.signal_id)
        .join(Watchlist, Watchlist.id == Feedback.watchlist_id)
        .where(Watchlist.icp_profile == profile, Feedback.verdict.in_(list(VERDICT_LABELS)))
        .group_by(Signal.signal_type, Signal.role_family, Feedback.verdict)
    )
    counts: dict[tuple[str, str | None], dict[str, int]] = {}
    for signal_type, role_family, verdict, count in session.execute(stmt):
        bucket = counts.setdefault((str(signal_type), role_family), {POSITIVE: 0, NEGATIVE: 0})
        bucket[label_for(str(verdict))] += int(count)
    rows = [
        PrecisionRow(key[0], key[1], bucket[POSITIVE], bucket[NEGATIVE])
        for key, bucket in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
    ]
    total = sum(r.labels for r in rows)
    note = None
    if total < MIN_LABELS_FOR_REWEIGHTING:
        note = HAND_TUNED_NOTE.format(labels=total, profile=profile)
    return PrecisionReport(profile=profile, rows=rows, note=note)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def format_markdown(report: PrecisionReport) -> str:
    """The report as a Markdown table with a totals line and the hand-tuned note."""
    lines = [
        f"## Precision by signal type: profile `{report.profile}`",
        "",
        "| signal_type | role_family | positives | negatives | precision |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {r.signal_type} | {r.role_family or '-'} | {r.positives} | {r.negatives} "
        f"| {_pct(r.precision)} |"
        for r in report.rows
    )
    if not report.rows:
        lines.append("| - | - | 0 | 0 | n/a |")
    lines.append(
        f"| **all** | | {report.positives} | {report.labels - report.positives} "
        f"| {_pct(report.precision)} |"
    )
    lines.append("")
    lines.append(f"Labels: {report.labels}.")
    if report.note:
        lines.append(f"Note: {report.note}.")
    return "\n".join(lines) + "\n"
