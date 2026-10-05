# 0012. tell-mcp is a uv workspace package, not `tell/mcp/server.py`

- Status: accepted
- Date: 2026-10-05

## Context

`docs/PLAN.md` Phase 9 places the MCP server at `tell/mcp/server.py` and packages it
as the `tell-mcp` distribution. ADR 0010 fixes that only `tell-mcp` is published to
PyPI and that users run it with `uvx tell-mcp`. A module inside the `tell` package
cannot be shipped on its own: a wheel built from `tell/mcp/` would either drag the
whole collector stack with it (sqlalchemy, psycopg, alembic, feedparser, lxml,
streamlit, fastapi, ...) or need a second build configuration carving one subpackage
out of the main one, which hatchling does not do cleanly and which every future
dependency change would have to keep in step.

## Decision

`tell-mcp` lives in `packages/tell-mcp/` as a member of the uv workspace declared in
the root `pyproject.toml` (`[tool.uv.workspace] members = ["packages/tell-mcp"]`,
`[tool.uv.sources] tell-mcp = { workspace = true }`). It is its own distribution with
its own `pyproject.toml`, import name `tell_mcp`, and the minimal dependency set the
server needs (`mcp`, `httpx`). The main `tell` package depends on it so one `uv sync`
installs both in development; `tell-mcp` never depends on `tell`. The PLAN Phase 9
path `tell/mcp/server.py` is superseded by `packages/tell-mcp/tell_mcp/server.py`;
the tools, prompt and behaviour described for Phase 9 are unchanged.

## Consequences

`uvx tell-mcp` installs a tiny package: no database driver, no collectors, no UI
frameworks, so it starts quickly in Claude Desktop or Cursor and has a small audit
surface. The server reads the public static JSON via `TELL_BASE_URL` (and later the
FastAPI service via `TELL_API_KEY`), which is the only coupling to the rest of the
project. Coverage for `tell_mcp` is measured and tested separately (`[tool.coverage.run]
omit` excludes the never-created `tell/mcp/*`); ruff and mypy cover
`packages/tell-mcp` through the root configuration. Trusted publishing at cutover
(ADR 0010) points at `packages/tell-mcp`.
