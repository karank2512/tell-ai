from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts import check_coverage


def _report(overall: float, files: dict[str, tuple[int, int]]) -> dict[str, Any]:
    return {
        "totals": {"percent_covered": overall},
        "files": {
            name: {"summary": {"covered_lines": cov, "num_statements": stmts}}
            for name, (cov, stmts) in files.items()
        },
    }


def test_green_when_packages_have_no_code_yet(capsys: pytest.CaptureFixture[str]) -> None:
    report = _report(95.0, {"tell/http.py": (90, 100)})
    assert check_coverage.check(report) == []
    out = capsys.readouterr().out
    assert out.count("skipped (no code yet)") == 3


def test_overall_floor() -> None:
    failures = check_coverage.check(_report(79.9, {}))
    assert failures == ["overall coverage 79.9% < 80%"]


def test_package_floor_applies_once_code_exists() -> None:
    report = _report(
        95.0,
        {
            "tell/collectors/greenhouse.py": (80, 100),
            "tell/collectors/__init__.py": (5, 5),
            "tell/signals/scoring.py": (95, 100),
        },
    )
    failures = check_coverage.check(report)
    assert failures == ["tell/collectors coverage 81.0% < 90%"]


def test_absolute_paths_are_matched() -> None:
    report = _report(95.0, {"/abs/repo/tell/resolve/match.py": (50, 100)})
    assert check_coverage.check(report) == ["tell/resolve coverage 50.0% < 90%"]


def test_main_exit_codes(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_report(90.0, {})))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_report(10.0, {})))
    assert check_coverage.main([str(good)]) == 0
    assert check_coverage.main([str(bad)]) == 1
    assert check_coverage.main([str(tmp_path / "missing.json")]) == 2
