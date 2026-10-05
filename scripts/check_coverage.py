"""Enforce the coverage floors from docs/SPEC.md section 12.

Reads a ``coverage.json`` report (``pytest --cov-report=json``) and exits non-zero
when the overall line coverage is below the overall floor or any listed package is
below its floor. The floors live in ``pyproject.toml``::

    [tool.tell.coverage]
    overall = 80
    packages = { "tell/collectors" = 90, "tell/resolve" = 90, "tell/signals" = 90 }

When the table is missing the defaults above apply. A package floor is skipped while
that package has no measured statements yet, so the gate works from Phase 0 onwards.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"

OVERALL_FLOOR = 80.0
PACKAGE_FLOORS: dict[str, float] = {
    "tell/collectors": 90.0,
    "tell/resolve": 90.0,
    "tell/signals": 90.0,
}


@dataclass(frozen=True)
class Floors:
    """The coverage floors: overall percentage and one percentage per package prefix."""

    overall: float = OVERALL_FLOOR
    packages: dict[str, float] = field(default_factory=lambda: dict(PACKAGE_FLOORS))


def load_floors(pyproject: Path = DEFAULT_PYPROJECT) -> Floors:
    """Read ``[tool.tell.coverage]`` from ``pyproject``; defaults when the table is absent.

    ``overall`` and every ``packages`` value must be numbers; anything else is a
    configuration error and raises ``ValueError`` so a typo cannot silently lower a
    floor.
    """
    if not pyproject.is_file():
        return Floors()
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    table = data.get("tool", {}).get("tell", {}).get("coverage")
    if table is None:
        return Floors()
    overall = _number(table.get("overall", OVERALL_FLOOR), "overall")
    raw_packages = table.get("packages", PACKAGE_FLOORS)
    if not isinstance(raw_packages, dict):
        msg = "[tool.tell.coverage].packages must be a table of package -> floor"
        raise ValueError(msg)
    packages = {
        _normalize(str(name)).rstrip("/"): _number(value, f"packages.{name}")
        for name, value in raw_packages.items()
    }
    return Floors(overall=overall, packages=packages)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"[tool.tell.coverage].{name} must be a number, got {value!r}"
        raise ValueError(msg)
    return float(value)


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


def check(report: dict[str, Any], floors: Floors | None = None) -> list[str]:
    """Return a list of human-readable failures (empty means green)."""
    floors = floors if floors is not None else Floors()
    failures: list[str] = []
    overall = float(report["totals"]["percent_covered"])
    status = "ok" if overall >= floors.overall else "FAIL"
    print(f"overall: {overall:.1f}% (floor {floors.overall:.0f}) {status}")
    if overall < floors.overall:
        failures.append(f"overall coverage {overall:.1f}% < {floors.overall:.0f}%")

    files = report.get("files", {})
    for package, floor in floors.packages.items():
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
    parser.add_argument(
        "--pyproject",
        default=DEFAULT_PYPROJECT,
        type=Path,
        help="pyproject.toml holding [tool.tell.coverage] (default: the repo's)",
    )
    args = parser.parse_args(argv)
    if not args.report.exists():
        print(f"{args.report}: not found; run `make test` first", file=sys.stderr)
        return 2
    try:
        floors = load_floors(args.pyproject)
    except ValueError as exc:
        print(f"coverage gate: {exc}", file=sys.stderr)
        return 2
    with args.report.open(encoding="utf-8") as fh:
        report = json.load(fh)
    failures = check(report, floors)
    for failure in failures:
        print(f"coverage gate: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
