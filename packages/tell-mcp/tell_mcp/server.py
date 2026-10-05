"""Entry point for the ``tell-mcp`` console script.

Phase 9 (see ``docs/PLAN.md``) replaces this placeholder with the stdio MCP
server exposing ``watch_accounts``, ``in_market``, ``explain``, ``events`` and
``coverage``.
"""

from __future__ import annotations

from tell_mcp import __version__


def main() -> None:
    """Print a placeholder until the MCP server lands in Phase 9."""
    print(f"tell-mcp {__version__}: MCP server not implemented yet (Phase 9).")


if __name__ == "__main__":
    main()
