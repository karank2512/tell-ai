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
    assert out.count("skipped (no code yet)") == len(check_coverage.PACKAGE_FLOORS)


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


# --- floors from pyproject.toml -----------------------------------------------------------


def test_repo_pyproject_declares_the_hard_constraint_floors() -> None:
    floors = check_coverage.load_floors()
    assert floors.overall == 80.0
    assert floors.packages == {
        "tell/collectors": 90.0,
        "tell/discovery": 90.0,  # ADR 0035 acceptance criteria
        "tell/resolve": 90.0,
        "tell/signals": 90.0,
    }
    assert floors == check_coverage.Floors()  # the defaults mirror pyproject.toml


def test_floors_default_when_table_or_file_is_missing(tmp_path: Path) -> None:
    assert check_coverage.load_floors(tmp_path / "nope.toml") == check_coverage.Floors()
    bare = tmp_path / "pyproject.toml"
    bare.write_text('[project]\nname = "x"\n')
    assert check_coverage.load_floors(bare) == check_coverage.Floors()


def test_floors_are_read_from_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.tell.coverage]\noverall = 70\npackages = { 'tell/http.py' = 95.5, 'tell/x/' = 50 }\n"
    )
    floors = check_coverage.load_floors(pyproject)
    assert floors == check_coverage.Floors(
        overall=70.0, packages={"tell/http.py": 95.5, "tell/x": 50.0}
    )
    # The configured floors drive check(): overall 75 passes 70, tell/x 40% fails 50.
    report = _report(75.0, {"tell/x/a.py": (40, 100)})
    assert check_coverage.check(report, floors) == ["tell/x coverage 40.0% < 50%"]


def test_malformed_floors_are_rejected(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.tell.coverage]\noverall = "eighty"\n')
    with pytest.raises(ValueError, match="overall must be a number"):
        check_coverage.load_floors(pyproject)
    pyproject.write_text("[tool.tell.coverage]\npackages = [1, 2]\n")
    with pytest.raises(ValueError, match="packages must be a table"):
        check_coverage.load_floors(pyproject)
    pyproject.write_text("[tool.tell.coverage]\npackages = { a = true }\n")
    with pytest.raises(ValueError, match=r"packages\.a must be a number"):
        check_coverage.load_floors(pyproject)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(99.0, {})))
    assert check_coverage.main([str(report), "--pyproject", str(pyproject)]) == 2


def test_main_honours_pyproject_option(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.tell.coverage]\noverall = 99\n")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(90.0, {})))
    assert check_coverage.main([str(report)]) == 0  # repo floors: 80
    assert check_coverage.main([str(report), "--pyproject", str(pyproject)]) == 1
