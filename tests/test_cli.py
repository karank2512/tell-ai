from __future__ import annotations

import subprocess
import sys

from typer.testing import CliRunner

from tell import __version__
from tell.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"tell {__version__}"


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "version" in result.output


def test_console_script_module_runs() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "tell.cli", "version"], capture_output=True, text=True, check=True
    )
    assert proc.stdout.strip() == f"tell {__version__}"


def test_tell_mcp_stub_runs() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "tell_mcp.server"], capture_output=True, text=True, check=True
    )
    assert proc.stdout.startswith("tell-mcp 0.1.0")
