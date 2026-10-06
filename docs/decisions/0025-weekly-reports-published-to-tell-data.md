# 0025. Weekly methods reports are published to tell-data, never committed to tell-ai

- Status: accepted
- Date: 2026-10-06

## Context

SPEC section 13 has each `tell backtest <name>` write a dated Markdown report under
`docs/methods/`, and section 11 has `weekly.yml` refresh them every Monday and publish the
`methods/` pages to `tell-data`. A scheduled workflow can only land a file in
`tell-ai/docs/methods/` by committing to `tell-ai`, which would make `github-actions` (or a
bot identity) a contributor of the code repo. The owner does not allow that: every commit
on `tell-ai` is a human's.

## Decision

`weekly.yml` runs `tell backtest refresh --out tell-data/site/methods` and commits the
reports to `tell-data` as `tell-bot`, together with the weekly page. It holds a read-only
`GITHUB_TOKEN` for `tell-ai` and pushes nothing there.

- The refreshed reports are published at `tell-data/site/methods/`: rendered on github.com
  and served from GitHub Pages (as plain Markdown, no Jekyll build).
- `tell-ai/docs/methods/` keeps the method write-ups and any report a maintainer
  regenerates by hand and commits under their own name (for example a report quoted by a
  launch asset, so the quote and its source are versioned together).
- Launch assets and the resume quote the dated reports in `tell-data`, which carry the
  generating commit like every other published file.

## Consequences

- The code repo's history stays human-only; no write token for `tell-ai` exists anywhere.
- The latest reports are one hop away from the code (a link from `docs/methods/` and the
  README to `tell-data`), not next to it.
- The Phase 11 `tell backtest refresh` command must accept `--out DIR` (default
  `docs/methods/` for local runs); `tests/test_workflows.py` checks the workflow line against
  the CLI.
