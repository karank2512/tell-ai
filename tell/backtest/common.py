"""Shared report machinery for ``tell backtest`` (spec section 13 "Reporting rules").

Every study builds one :class:`Report` and hands it to :func:`write_report`, which renders
it with the Jinja templates in ``docs/methods/templates/`` and writes
``<out>/<name>-<YYYY-MM-DD>.md`` plus ``<out>/<name>-latest.md`` (a full copy, so the
latest report is readable on GitHub without following a link). The ``-latest`` copy is
written only when the report is at least as new as every dated ``<name>-*.md`` already in
the directory: regenerating a past date (``--as-of 2026-08-20``) never moves the latest
pointer backwards. It is a convenience copy, not a dated source; quotes cite the dated file.

The dated reports are indexed in ``<out>/README.md`` between :data:`INDEX_START` and
:data:`INDEX_END`. When the directory has no ``README.md`` at all (``weekly.yml`` writes to
``tell-data/site/methods/``, which starts empty), :func:`update_index` creates a minimal one
with the markers; a ``README.md`` without markers is left alone.

Reporting rules enforced here:

* Each report carries the question, the cohort definition, N, the query or script path,
  the numbers, the caveats and the date (the template renders all seven, in that order).
* Every number is a :class:`Metric` with the N behind it. A number with N under
  :data:`MIN_QUOTABLE_N` (30) is rendered as ``value (N=12; too small to quote)``; when a
  headline number, or the report's own N, is that small, the front matter says
  ``quotable: false`` and the launch drafts and resume must not use it.
* A study can add *blockers* (for example "sample too small" below the 100 filings
  13.1 asks for, or "not yet runnable"); any blocker also makes the report
  ``quotable: false``.
* No square brackets ever reach a report: the launch drafts use bracketed placeholders
  for numbers that do not exist yet, so a bracket in a report would look like an
  unfilled number. Rendered text has them replaced by parentheses, and
  :func:`render_report` checks the result.

Dating: ``--as-of DATE`` names the report date. A past date is evaluated at the last
instant of that UTC day (23:59:59.999999); today, or no date, is evaluated at the current
instant (:func:`resolve_now`). The report's date is always the evaluation instant's date.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tell.collectors.base import iso_utc, utcnow

__all__ = [
    "INDEX_END",
    "INDEX_README",
    "INDEX_START",
    "METHODS_DIR",
    "MIN_QUOTABLE_N",
    "PRIVATE_ROOT",
    "REPO_ROOT",
    "TEMPLATES_DIR",
    "TOO_SMALL",
    "Metric",
    "MetricKind",
    "Query",
    "Report",
    "Section",
    "Table",
    "WrittenReport",
    "dated_name",
    "debracket",
    "format_value",
    "latest_name",
    "median",
    "newest_dated",
    "percentile",
    "render_report",
    "resolve_now",
    "share",
    "update_index",
    "write_report",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
"""The checkout (``tell`` runs from its repository; it is never published to PyPI)."""

METHODS_DIR = REPO_ROOT / "docs" / "methods"
"""Default ``--out`` of the public reports."""

TEMPLATES_DIR = METHODS_DIR / "templates"
"""``report.md.j2`` plus one ``<name>.md.j2`` per study extending it."""

PRIVATE_ROOT = REPO_ROOT.parent / "tell-private"
"""``../tell-private`` next to the checkout (PLAN "Local build conventions")."""

MIN_QUOTABLE_N = 30
"""Spec 13 reporting rules: a number with N under 30 is never quoted."""

TOO_SMALL = "too small to quote"

INDEX_START = "<!-- backtest-reports:start -->"
INDEX_END = "<!-- backtest-reports:end -->"
"""Markers around the generated list of dated reports in ``<out>/README.md``."""

INDEX_README = (
    "# Methods reports\n\n"
    "Dated backtest reports written by `tell backtest` (spec section 13). Quote the dated\n"
    "file, with its N and date; the `-latest` copy is a convenience, not a dated source.\n\n"
    f"{INDEX_START}\n{INDEX_END}\n"
)
"""The ``README.md`` :func:`update_index` creates when the output directory has none."""

MetricKind = Literal["share", "days", "hours", "weeks", "count", "number", "text"]

_DATED = re.compile(r"^(?P<name>[a-z0-9-]+?)-(?P<day>\d{4}-\d{2}-\d{2})\.md$")


# --- numbers -----------------------------------------------------------------------------------


_FORMATS: dict[str, str] = {
    "days": "{:.1f} days",
    "hours": "{:.1f} h",
    "weeks": "{:.1f} weeks",
}


def format_value(value: float | str | None, kind: MetricKind) -> str:
    """Human rendering of one number; ``n/a`` for a missing value."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if isinstance(value, str) or kind == "text":
        return str(value)
    if kind == "share":
        return f"{float(value) * 100:.1f}%"
    if kind in _FORMATS:
        return _FORMATS[kind].format(float(value))
    if kind == "count" or isinstance(value, int):
        return f"{round(value):,}"
    return f"{float(value):.2f}"


@dataclass(frozen=True)
class Metric:
    """One reported number and the N behind it.

    ``n`` is ``None`` only for numbers that are not estimates from a sample (a plain count
    such as "postings tracked" is its own N). ``headline`` numbers decide the report's
    ``quotable`` flag; breakdown cells (per vendor, per family) are flagged inline only.
    """

    label: str
    value: float | str | None
    n: int | None = None
    kind: MetricKind = "number"
    headline: bool = True

    @property
    def small(self) -> bool:
        """``True`` when the N behind the number is under :data:`MIN_QUOTABLE_N`."""
        return self.n is not None and self.n < MIN_QUOTABLE_N

    @property
    def rendered(self) -> str:
        """``value (N=1,234)`` or ``value (N=12; too small to quote)``."""
        text = format_value(self.value, self.kind)
        if self.n is None:
            return text
        if self.small:
            return f"{text} (N={self.n:,}; {TOO_SMALL})"
        return f"{text} (N={self.n:,})"


def share(part: int, whole: int) -> float | None:
    """``part / whole``; ``None`` when ``whole`` is zero."""
    return part / whole if whole else None


def percentile(values: Iterable[float], q: float) -> float | None:
    """Linear-interpolation percentile (PostgreSQL ``percentile_cont``); ``None`` if empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0.0 <= q <= 1.0:
        msg = f"q must be in [0, 1], got {q}"
        raise ValueError(msg)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def median(values: Iterable[float]) -> float | None:
    """``percentile(values, 0.5)``."""
    return percentile(values, 0.5)


# --- report model ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    """A Markdown table whose cells are text or :class:`Metric` values."""

    headers: Sequence[str]
    rows: Sequence[Sequence[Metric | str]]

    def metrics(self) -> Iterator[Metric]:
        for row in self.rows:
            yield from (cell for cell in row if isinstance(cell, Metric))

    @property
    def markdown(self) -> str:
        def cell(value: Metric | str) -> str:
            text = value.rendered if isinstance(value, Metric) else value
            return text.replace("|", "/").replace("\n", " ")

        lines = [
            "| " + " | ".join(cell(h) for h in self.headers) + " |",
            "| " + " | ".join("---" for _ in self.headers) + " |",
        ]
        lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in self.rows]
        return "\n".join(lines)


@dataclass(frozen=True)
class Section:
    """One block of numbers: prose, labelled metrics, an optional table."""

    title: str
    text: str = ""
    metrics: Sequence[Metric] = ()
    table: Table | None = None


@dataclass(frozen=True)
class Query:
    """A query (or script excerpt) printed verbatim in the report."""

    title: str
    sql: str
    language: str = "sql"


@dataclass
class Report:
    """Everything one dated report says (spec section 13 "Reporting rules")."""

    name: str
    title: str
    question: str
    cohort: str
    n: int
    as_of: date
    generated_at: datetime
    script: str
    sections: list[Section] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    """Lines printed prominently under the cohort (for example "sample too small")."""
    blockers: list[str] = field(default_factory=list)
    """Reasons the report is not quotable beyond its N (each is also a notice)."""
    private: bool = False

    def metrics(self) -> Iterator[Metric]:
        """Every number in the report: section metrics, then table cells."""
        for section in self.sections:
            yield from section.metrics
            if section.table is not None:
                yield from section.table.metrics()

    @property
    def flagged(self) -> int:
        """How many numbers carry the "too small to quote" flag."""
        return sum(1 for m in self.metrics() if m.small)

    @property
    def not_quotable_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.n < MIN_QUOTABLE_N:
            reasons.append(f"N={self.n} is under {MIN_QUOTABLE_N}")
        small = [m.label for s in self.sections for m in s.metrics if m.headline and m.small]
        if len(small) == 1:
            reasons.append(f"1 headline number has N under {MIN_QUOTABLE_N}")
        elif small:
            reasons.append(f"{len(small)} headline numbers have N under {MIN_QUOTABLE_N}")
        reasons.extend(self.blockers)
        return reasons

    @property
    def quotable(self) -> bool:
        """``False`` whenever any reason in :attr:`not_quotable_reasons` holds."""
        return not self.not_quotable_reasons

    @property
    def generated_iso(self) -> str:
        return iso_utc(self.generated_at.replace(microsecond=0))


@dataclass(frozen=True)
class WrittenReport:
    """Where :func:`write_report` put a report."""

    name: str
    path: Path
    latest: Path | None
    n: int
    quotable: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "report": self.name,
            "path": str(self.path),
            "latest": str(self.latest) if self.latest is not None else None,
            "n": self.n,
            "quotable": self.quotable,
        }


# --- dating ----------------------------------------------------------------------------------


def resolve_now(as_of: date | None, *, now: datetime | None = None) -> datetime:
    """The evaluation instant for ``--as-of`` (see the module docstring)."""
    current = (now or utcnow()).astimezone(UTC)
    if as_of is None or as_of >= current.date():
        return current
    return datetime.combine(as_of, time.max, tzinfo=UTC)


def dated_name(name: str, as_of: date) -> str:
    return f"{name}-{as_of.isoformat()}.md"


def latest_name(name: str) -> str:
    return f"{name}-latest.md"


def newest_dated(out_dir: Path, name: str) -> date | None:
    """The date of the newest ``<name>-YYYY-MM-DD.md`` in ``out_dir``; ``None`` if none."""
    days = _dated_reports(out_dir).get(name, [])
    return date.fromisoformat(max(days)) if days else None


# --- rendering ---------------------------------------------------------------------------------


def debracket(text: str) -> str:
    """Replace square brackets with parentheses (see the module docstring)."""
    return text.replace("[", "(").replace("]", ")")


def _environment(templates_dir: Path) -> Environment:
    # Markdown output, not HTML: autoescaping would mangle "<" in SQL and comparisons.
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 - Markdown output, never HTML served to a browser
    )


def render_report(report: Report, *, templates_dir: Path = TEMPLATES_DIR) -> str:
    """The report as Markdown with YAML front matter; never contains a square bracket."""
    env = _environment(templates_dir)
    specific = f"{report.name}.md.j2"
    name = specific if (templates_dir / specific).is_file() else "report.md.j2"
    text = debracket(env.get_template(name).render(r=report))
    text = re.sub(r"\n{3,}", "\n\n", text)
    if "[" in text or "]" in text:  # pragma: no cover - debracket makes this unreachable
        msg = f"report {report.name} still contains a square bracket"
        raise RuntimeError(msg)
    return text


def write_report(
    report: Report,
    out_dir: Path,
    *,
    latest: bool = True,
    templates_dir: Path = TEMPLATES_DIR,
) -> WrittenReport:
    """Write the dated report and, unless a newer dated one exists, the ``-latest`` copy.

    ``latest`` is ``None`` in the result when the copy was not written: either ``latest``
    was ``False`` or a dated ``<name>-YYYY-MM-DD.md`` newer than ``report.as_of`` is
    already in ``out_dir`` (a past date regenerated after a newer report). Public reports
    also refresh the index in ``<out>/README.md`` (created when missing).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    text = render_report(report, templates_dir=templates_dir)
    newest = newest_dated(out_dir, report.name)
    path = out_dir / dated_name(report.name, report.as_of)
    path.write_text(text, encoding="utf-8")
    latest_path: Path | None = None
    if latest and (newest is None or report.as_of >= newest):
        latest_path = out_dir / latest_name(report.name)
        latest_path.write_text(text, encoding="utf-8")
    if not report.private:
        update_index(out_dir)
    return WrittenReport(
        name=report.name, path=path, latest=latest_path, n=report.n, quotable=report.quotable
    )


def _dated_reports(out_dir: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(out_dir.glob("*.md")):
        match = _DATED.match(path.name)
        if match is not None:
            found.setdefault(match.group("name"), []).append(match.group("day"))
    return found


def update_index(out_dir: Path) -> bool:
    """Rewrite the generated block of ``<out>/README.md``; ``False`` when it has no markers.

    A missing ``README.md`` is created from :data:`INDEX_README` (a title, one line of
    explanation and the markers), so a fresh directory such as ``tell-data/site/methods/``
    gets an index on the first report. An existing ``README.md`` without the markers is
    someone else's file and is left untouched. The block lists, per study, the ``-latest``
    file and every dated report (newest first) as HTML anchors, so the index has no square
    brackets either.
    """
    readme = out_dir / "README.md"
    if not readme.is_file():
        readme.write_text(INDEX_README, encoding="utf-8")
    text = readme.read_text(encoding="utf-8")
    start = text.find(INDEX_START)
    end = text.find(INDEX_END)
    if start < 0 or end < start:
        return False
    lines: list[str] = []
    reports = _dated_reports(out_dir)
    if not reports:
        lines.append("No dated report has been written yet.")
    for name, days in sorted(reports.items()):
        dated = ", ".join(
            f'<a href="{dated_name(name, date.fromisoformat(d))}">{d}</a>'
            for d in sorted(days, reverse=True)
        )
        latest = latest_name(name)
        pointer = f'<a href="{latest}">{latest}</a>' if (out_dir / latest).is_file() else ""
        lines.append(f"- {name}: {pointer} (dated: {dated})")
    block = "\n".join([INDEX_START, *lines, INDEX_END])
    updated = text[:start] + block + text[end + len(INDEX_END) :]
    if updated != text:
        readme.write_text(updated, encoding="utf-8")
    return True
