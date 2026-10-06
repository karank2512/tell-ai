"""``docs/launch/numbers.yaml`` names report lines that exist (SPEC 16 M4).

Every launch-draft placeholder maps to a ``field`` that must be the exact label of a
:class:`tell.backtest.common.Metric` in the named report, and to a dated-report path in
``tell-data/site/methods/`` (ADR 0025) that never matches the ``-latest`` copy.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from tell.backtest import noise, ops, speed_vs_press, velocity_lead
from tell.backtest.common import Report, dated_name, latest_name
from tell.backtest.wayback import WaybackClient
from tests.backtest.helpers import DAY, NOW, run

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_DIR = REPO_ROOT / "docs" / "launch"
NUMBERS = LAUNCH_DIR / "numbers.yaml"
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+\.[a-z_.]+)\s*\}\}")
"""A dotted name; the prose `{{ placeholder }}` in the status lines is not one."""


def _numbers() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(NUMBERS.read_text(encoding="utf-8"))
    return data


def _reports(session: Session, tmp_path: Path) -> dict[str, Report]:
    run(session, "greenhouse", NOW - 70 * DAY)  # 70 days of collection: prospective runnable
    return {
        "speed_vs_press": speed_vs_press.build_report(session, NOW),
        "velocity_lead": velocity_lead.build_report(session, NOW, wayback=WaybackClient(tmp_path)),
        "noise": noise.build_report(session, NOW),
        "ops": ops.build_report(session, NOW, gold_doc=tmp_path / "absent.md"),
    }


def test_every_field_is_a_metric_label_of_its_report(db_session: Session, tmp_path: Path) -> None:
    reports = _reports(db_session, tmp_path)
    specs = _numbers()["reports"]
    assert set(specs) == set(reports)
    for study, spec in specs.items():
        report = reports[study]
        labels = {m.label for m in report.metrics()}
        pattern = str(spec["report"])
        assert pattern.startswith("tell-data/site/methods/"), study
        assert fnmatch(f"tell-data/site/methods/{dated_name(report.name, NOW.date())}", pattern)
        assert not fnmatch(f"tell-data/site/methods/{latest_name(report.name)}", pattern), study
        fields = {k: v for k, v in spec.items() if k != "report"}
        assert fields, study
        for key, entry in fields.items():
            assert entry["field"] in labels, f"{study}.{key}: {entry['field']!r} is not a label"
            assert entry["status"] == "pending soak", f"{study}.{key}"
            for draft in entry["used_in"]:
                text = (LAUNCH_DIR / draft).read_text(encoding="utf-8")
                assert f"reports.{study}.{key}" in text, f"{study}.{key} not used in {draft}"


def test_every_placeholder_in_the_drafts_is_mapped() -> None:
    data = _numbers()
    known = {
        f"reports.{study}.{key}"
        for study, spec in data["reports"].items()
        for key in spec
        if key != "report"
    } | set(data["inputs"])
    for draft in sorted(LAUNCH_DIR.glob("*.md")):
        text = draft.read_text(encoding="utf-8")
        for name in PLACEHOLDER.findall(text):
            assert name in known, f"{draft.name}: {{{{ {name} }}}} is not in numbers.yaml"
        if draft.name != "README.md":  # the how-to names the bracket rule; the drafts obey it
            assert "[N]" not in text, draft.name
            assert "[X]" not in text, draft.name


def test_linkedin_bullets_match_the_report_windows() -> None:
    """The quoted claim must say what its source line measures."""
    text = (LAUNCH_DIR / "linkedin-post.md").read_text(encoding="utf-8")
    assert f"in the {velocity_lead.PRE_WEEKS} weeks before" in text
    assert "60 days before" not in text
