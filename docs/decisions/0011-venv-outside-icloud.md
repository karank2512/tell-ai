# 0011. Keep the uv environment outside iCloud-synced folders

- Status: accepted
- Date: 2026-10-05

## Context

The local checkout lives at `~/Desktop/Tell/tell`. With iCloud Drive's Desktop &
Documents sync on, macOS sets the `UF_HIDDEN` flag on every file under a
dot-directory such as `.venv`. CPython 3.12 skips hidden `.pth` files, so the
editable installs of `tell` and `tell_mcp` vanish from `sys.path`: `make test`
fails on `tests/test_cli.py::test_tell_mcp_stub_runs` (`ModuleNotFoundError:
tell_mcp`) even though every Phase 1 test passes. Two independent verifiers
reproduced it; clearing the flag on one `.pth` file makes the import work again,
but anything `uv sync` writes is re-flagged.

## Decision

The `Makefile` detects a checkout under `~/Desktop`, `~/Documents` or
`~/Library/Mobile Documents` on macOS and, unless `UV_PROJECT_ENVIRONMENT` is
already set, exports `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell` for every target.
`make env` prints the path in use. `pyproject.toml`, `uv.lock` and CI are
unchanged; contributors elsewhere keep uv's default `.venv`.

## Consequences

`make lint`, `make test` and `make migrate` are green from the synced checkout
with no per-shell setup. Direct `uv run ...` invocations outside `make` still use
`.venv` unless the shell exports the same variable (documented in
`CONTRIBUTING.md`). The stale `.venv` inside the checkout can be deleted at any
time. Moving the checkout off iCloud remains an option that makes the override
inert.
