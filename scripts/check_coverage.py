"""Enforce the coverage floors from docs/SPEC.md section 12.

Reads ``coverage.json`` (``pytest --cov-report=json``) and exits non-zero when the
overall line coverage is below 80 or any of ``tell/collectors``, ``tell/resolve``,
``tell/signals`` is below 90. A package floor is skipped while that package has no
measured statements yet, so the gate works from Phase 0 onwards.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OVERALL_FLOOR = 80.0
PACKAGE_FLOORS: dict[str, float] = {
    "tell/collectors": 90.0,
    "tell/resolve": 90.0,
    "tell/signals": 90.0,
}


def _normalize(path: str) -> str:
    return path.replace("\\", "/")


def package_coverage(files: dict[str, Any], package: str) -> tuple[int, int]:
    """Return ``(covered_lines, num_statements)`` summed over files under ``package``."""
    prefix = package.rstrip("/") + "/"
    covered = statements = 0
    for name, info in files.items():
        norm = _normalize(name)
        if norm.startswith(prefix) or f"/{prefix}" in norm:
            summary = info["summary"]
            covered += int(summary["covered_lines"])
            statements += int(summary["num_statements"])
    return covered, statements


def check(report: dict[str, Any]) -> list[str]:
    """Return a list of human-readable failures (empty means green)."""
    failures: list[str] = []
    overall = float(report["totals"]["percent_covered"])
    status = "ok" if overall >= OVERALL_FLOOR else "FAIL"
    print(f"overall: {overall:.1f}% (floor {OVERALL_FLOOR:.0f}) {status}")
    if overall < OVERALL_FLOOR:
        failures.append(f"overall coverage {overall:.1f}% < {OVERALL_FLOOR:.0f}%")

    files = report.get("files", {})
    for package, floor in PACKAGE_FLOORS.items():
        covered, statements = package_coverage(files, package)
        if statements == 0:
            print(f"{package}: skipped (no code yet)")
            continue
        pct = 100.0 * covered / statements
        status = "ok" if pct >= floor else "FAIL"
        print(f"{package}: {pct:.1f}% (floor {floor:.0f}) {status}")
        if pct < floor:
            failures.append(f"{package} coverage {pct:.1f}% < {floor:.0f}%")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", default="coverage.json", type=Path)
    args = parser.parse_args(argv)
    if not args.report.exists():
        print(f"{args.report}: not found; run `make test` first", file=sys.stderr)
        return 2
    with args.report.open(encoding="utf-8") as fh:
        report = json.load(fh)
    failures = check(report)
    for failure in failures:
        print(f"coverage gate: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
