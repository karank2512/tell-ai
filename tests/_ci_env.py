"""Make typer/rich help output identical everywhere (imported first by ``tests/conftest.py``).

On GitHub Actions typer forces a colour terminal (``GITHUB_ACTIONS`` is set) and rich
wraps help text at 80 columns, so ``CliRunner`` output carries ANSI codes and line
breaks that plain-text assertions do not expect. Tests must see the same output locally
and in CI, so the environment hints are removed before typer reads them, and the
module-level constants are patched too in case ``typer.rich_utils`` was imported earlier.
"""

from __future__ import annotations

import os

for _name in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_name, None)
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ["TERMINAL_WIDTH"] = "200"
os.environ["COLUMNS"] = "200"

try:
    from typer import rich_utils as _rich_utils
except ImportError:  # pragma: no cover - typer always ships rich support here
    _rich_utils = None

if _rich_utils is not None:
    _rich_utils.FORCE_TERMINAL = None
    _rich_utils.MAX_WIDTH = 200
