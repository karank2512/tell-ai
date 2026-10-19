"""MCP Registry metadata for tell-mcp (docs/mcp.md "Listing in the MCP Registry").

``packages/tell-mcp/server.json`` is what ``mcp-publisher`` sends to the official registry when
a ``tell-mcp-v*`` tag publishes the package (``.github/workflows/publish-mcp.yml``). A mistake
in it only shows up as a rejected listing after a release, and a published version cannot be
edited, so these tests keep the file in step with ``pyproject.toml``, the code and the package
README. They read local files only; no test touches the network.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tell_mcp import __version__
from tell_mcp.client import DEFAULT_BASE_URL

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packages" / "tell-mcp"
SERVER_JSON = PACKAGE_DIR / "server.json"
SERVER_NAME = "io.github.karank2512/tell-mcp"
CURRENT_SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
"""The server.json schema the registry itself treats as current (it rejects an older one)."""

_NAME_CHAR = re.compile(r"[A-Za-z0-9._/-]")


def _pyproject() -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads(
        (PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    )
    return data


def _server() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    return data


def _package() -> dict[str, Any]:
    [package] = _server()["packages"]
    assert isinstance(package, dict)
    return package


def _has_mcp_name(content: str, name: str) -> bool:
    """The registry's PyPI ownership check: the literal ``mcp-name: <name>`` (one space) in the
    package description, followed by the end of the text, a character that cannot continue a
    server name, or an HTML comment close, so a longer name sharing the prefix does not count."""
    token = f"mcp-name: {name}"
    start = 0
    while (index := content.find(token, start)) >= 0:
        rest = content[index + len(token) :]
        if not rest or not _NAME_CHAR.match(rest[0]) or rest.startswith(("-->", "--!>")):
            return True
        start = index + 1
    return False


def test_server_json_versions_match_pyproject() -> None:
    """The registry checks the PyPI release named here, and a published version is immutable,
    so the server version, the package version, pyproject and ``__version__`` move together."""
    version = _pyproject()["project"]["version"]
    assert _server()["version"] == version
    assert _package()["version"] == version
    assert __version__ == version


def test_server_json_points_at_the_pypi_package() -> None:
    project = _pyproject()["project"]
    package = _package()
    assert package["registryType"] == "pypi"
    assert package["registryBaseUrl"] == "https://pypi.org"
    assert package["identifier"] == project["name"]
    assert package["transport"] == {"type": "stdio"}
    assert package["runtimeHint"] == "uvx"  # `uvx tell-mcp` is how the docs start it
    repository = _server()["repository"]
    assert repository["source"] == "github"
    assert repository["url"] == project["urls"]["Repository"]
    assert (REPO_ROOT / repository["subfolder"] / "pyproject.toml").is_file()


def test_server_json_follows_the_registry_rules() -> None:
    """Spot checks of what the registry enforces (the schema is not vendored; the file was
    validated against the published schema when it was written)."""
    server = _server()
    assert server["$schema"] == CURRENT_SCHEMA
    assert server["name"] == SERVER_NAME
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", server["name"])
    assert 1 <= len(server["description"]) <= 100
    assert 1 <= len(server["title"]) <= 100
    # GitHub OIDC from karank2512/tell-ai may only publish under io.github.karank2512/.
    assert server["name"].startswith("io.github.karank2512/")


def test_server_json_documents_the_environment() -> None:
    env = {var["name"]: var for var in _package()["environmentVariables"]}
    assert env["TELL_BASE_URL"]["default"] == DEFAULT_BASE_URL
    assert env["TELL_API_KEY"]["isSecret"] is True
    assert not any(var.get("isRequired") for var in env.values())  # runs with no key, no account
    docs = (REPO_ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    for name in env:
        assert f"`{name}`" in docs, f"{name} is not documented in docs/mcp.md"


def test_readme_carries_the_registry_ownership_line() -> None:
    """PyPI serves the README as the package description; the registry reads it from there."""
    readme = _pyproject()["project"]["readme"]
    assert isinstance(readme, str)  # a file name, not an inline table
    text = (PACKAGE_DIR / readme).read_text(encoding="utf-8")
    assert _has_mcp_name(text, _server()["name"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<!-- mcp-name: io.github.karank2512/tell-mcp -->", True),
        ("<!-- mcp-name: io.github.karank2512/tell-mcp-->", True),
        ("mcp-name: io.github.karank2512/tell-mcp\n", True),
        ("mcp-name: io.github.karank2512/tell-mcp", True),
        ("mcp-name: io.github.karank2512/tell-mcp-pro", False),  # longer name, same prefix
        ("mcp-name: io.github.karank2512/tell-mcpX", False),
        ("mcp-name:io.github.karank2512/tell-mcp", False),  # the registry wants one space
        ("io.github.karank2512/tell-mcp", False),
    ],
)
def test_ownership_check_follows_the_registry_rule(text: str, expected: bool) -> None:
    assert _has_mcp_name(text, SERVER_NAME) is expected


def test_docs_name_the_registry_entry() -> None:
    docs = (REPO_ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    assert "## Listing in the MCP Registry" in docs
    assert SERVER_NAME in docs
