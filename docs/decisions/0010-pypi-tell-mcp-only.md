# 0010. Publish only tell-mcp to PyPI

- Status: accepted
- Date: 2026-10-05

## Context

The name `tell` is taken on PyPI; `tell-mcp` was verified available.

## Decision

Publish only the `tell-mcp` distribution to PyPI. The main package keeps the import name `tell` but is never published.

## Consequences

Users run `uvx tell-mcp`. The main package is installed from a git clone. PyPI trusted publishing for `tell-mcp` is set up at cutover.
