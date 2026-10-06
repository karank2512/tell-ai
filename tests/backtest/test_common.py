"""Report model, the N < 30 rule, rendering without brackets, dating and the index."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tell.backtest.common import (
    INDEX_END,
    INDEX_START,
    TEMPLATES_DIR,
    Metric,
    Query,
    Report,
    Section,
    Table,
    format_value,
    median,
    newest_dated,
    percentile,
    render_report,
    resolve_now,
    share,
    update_index,
    write_report,
)
from tests.backtest.helpers import NOW


def _report(n: int = 40, **kwargs: object) -> Report:
    report = Report(
        name="noise",
        title="A test report",
        question="Is this a question?",
        cohort="Every row [synthetic].",
        n=n,
        as_of=NOW.date(),
        generated_at=NOW,
        script="tell/backtest/noise.py",
        sections=[
            Section(
                title="Numbers",
                text="Some text.",
                metrics=[Metric("Share", 0.25, n, "share")],
                table=Table(["key", "value"], [["a|b", Metric("cell", 3.0, 5, "days", False)]]),
            )
        ],
        caveats=["A caveat with [brackets]."],
        queries=[Query("Query", "select 1;")],
    )
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        (None, "share", "n/a"),
        (float("nan"), "days", "n/a"),
        ("text", "number", "text"),
        (0.4567, "share", "45.7%"),
        (3.04, "days", "3.0 days"),
        (14.1417, "hours", "14.1 h"),
        (2.07, "weeks", "2.1 weeks"),
        (1234.4, "count", "1,234"),
        (12345, "number", "12,345"),
        (0.97333, "number", "0.97"),
    ],
)
def test_format_value(value: float | str | None, kind: str, expected: str) -> None:
    assert format_value(value, kind) == expected  # type: ignore[arg-type]  # kind is a Literal


def test_metric_rendering_flags_small_n() -> None:
    assert Metric("x", 0.5, 29, "share").rendered == "50.0% (N=29; too small to quote)"
    assert Metric("x", 0.5, 30, "share").rendered == "50.0% (N=30)"
    assert Metric("x", 1500, None, "count").rendered == "1,500"
    assert Metric("x", None, 0, "days").rendered == "n/a (N=0; too small to quote)"
    assert Metric("x", 0.5, 29).small
    assert not Metric("x", 0.5).small


def test_percentile_matches_percentile_cont() -> None:
    assert percentile([], 0.5) is None
    assert median([-7, 0, 10]) == 0.0
    assert percentile([-7, 0, 10], 0.75) == 5.0
    assert median([1, 2, 3, 4]) == 2.5
    assert percentile([5], 0.9) == 5.0
    with pytest.raises(ValueError, match="q must be"):
        percentile([1], 1.5)
    assert share(1, 4) == 0.25
    assert share(1, 0) is None


def test_resolve_now() -> None:
    assert resolve_now(None, now=NOW) == NOW
    assert resolve_now(NOW.date(), now=NOW) == NOW
    assert resolve_now(date(2026, 9, 30), now=NOW) == datetime(
        2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC
    )


def test_quotable_rules() -> None:
    assert _report(40).quotable
    small = _report(12)
    assert not small.quotable
    assert small.not_quotable_reasons[0] == "N=12 is under 30"
    headline = _report(40)
    headline.sections = [Section("s", metrics=[Metric("m", 0.1, 3, "share")])]
    assert not headline.quotable
    assert "1 headline number has N under 30" in headline.not_quotable_reasons
    breakdown = _report(40)
    breakdown.sections = [Section("s", metrics=[Metric("m", 0.1, 3, "share", headline=False)])]
    assert breakdown.quotable
    assert breakdown.flagged == 1
    blocked = _report(40, blockers=["not yet runnable"])
    assert not blocked.quotable


def test_render_has_every_part_and_no_brackets() -> None:
    text = render_report(_report(12))
    assert text.startswith("---\nreport: noise\n")
    assert "quotable: false" in text
    assert "flagged_numbers: 2" in text
    for heading in (
        "## Question",
        "## Cohort",
        "## Numbers",
        "## Query or script",
        "## Caveats",
        "## Date",
    ):
        assert heading in text
    assert "Cohort N: 12." in text
    assert "25.0% (N=12; too small to quote)" in text
    assert "3.0 days (N=5; too small to quote)" in text
    assert "| a/b |" in text
    assert "select 1;" in text
    assert "(brackets)" in text
    assert "(synthetic)" in text
    assert "[" not in text
    assert "]" not in text
    assert "**Not quotable:** N=12 is under 30" in text


def test_render_falls_back_to_base_template(tmp_path: Path) -> None:
    (tmp_path / "report.md.j2").write_text(
        (TEMPLATES_DIR / "report.md.j2").read_text(encoding="utf-8"), encoding="utf-8"
    )
    text = render_report(_report(40, name="other"), templates_dir=tmp_path)
    assert "report: other" in text
    assert "quotable: true" in text


def test_write_report_dated_latest_and_index(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"# Methods\n\n{INDEX_START}\nold\n{INDEX_END}\ntail\n", encoding="utf-8")
    written = write_report(_report(40), tmp_path)
    assert written.path == tmp_path / "noise-2026-10-06.md"
    assert written.latest == tmp_path / "noise-latest.md"
    assert written.path.read_text(encoding="utf-8") == written.latest.read_text(encoding="utf-8")
    assert written.to_dict()["quotable"] is True
    older = _report(40, as_of=date(2026, 9, 29))
    write_report(older, tmp_path, latest=False)
    index = readme.read_text(encoding="utf-8")
    assert '- noise: <a href="noise-latest.md">noise-latest.md</a>' in index
    assert (
        '<a href="noise-2026-10-06.md">2026-10-06</a>, <a href="noise-2026-09-29.md">2026-09-29</a>'
        in index
    )
    assert "old" not in index
    assert index.endswith("tail\n")
    assert "[" not in index


def test_past_as_of_never_moves_the_latest_pointer_back(tmp_path: Path) -> None:
    newest = write_report(_report(40), tmp_path)  # 2026-10-06
    assert newest.latest is not None
    older = write_report(_report(40, as_of=date(2026, 8, 20)), tmp_path)
    assert older.path == tmp_path / "noise-2026-08-20.md"
    assert older.latest is None
    assert older.to_dict()["latest"] is None
    latest = (tmp_path / "noise-latest.md").read_text(encoding="utf-8")
    assert "as_of: 2026-10-06" in latest
    assert "as_of: 2026-08-20" not in latest
    # The same date again, or a newer one, refreshes the copy.
    assert write_report(_report(40), tmp_path).latest is not None
    newer = write_report(_report(40, as_of=date(2026, 10, 13)), tmp_path)
    assert newer.latest is not None
    assert "as_of: 2026-10-13" in (tmp_path / "noise-latest.md").read_text(encoding="utf-8")
    assert newest_dated(tmp_path, "noise") == date(2026, 10, 13)
    assert newest_dated(tmp_path, "ops") is None


def test_update_index_creates_a_readme_when_the_directory_has_none(tmp_path: Path) -> None:
    """``weekly.yml`` writes to ``tell-data/site/methods``, which starts without a README."""
    out = tmp_path / "methods"
    written = write_report(_report(40), out)
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Methods reports\n")
    assert "not a dated source" in readme
    assert f'<a href="{written.path.name}">2026-10-06</a>' in readme
    assert '<a href="noise-latest.md">noise-latest.md</a>' in readme
    assert "[" not in readme


def test_update_index_without_markers(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("no markers\n", encoding="utf-8")
    assert update_index(tmp_path) is False
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "no markers\n"
    (tmp_path / "README.md").write_text(f"{INDEX_START}\n{INDEX_END}\n", encoding="utf-8")
    assert update_index(tmp_path) is True
    assert "No dated report has been written yet." in (tmp_path / "README.md").read_text(
        encoding="utf-8"
    )


def test_private_report_skips_index(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"{INDEX_START}\n{INDEX_END}\n", encoding="utf-8")
    write_report(_report(40, private=True), tmp_path, latest=False)
    assert readme.read_text(encoding="utf-8") == f"{INDEX_START}\n{INDEX_END}\n"
