# 0008. Optional code is included but not deployed

- Status: accepted
- Date: 2026-10-05

## Context

The spec marks the FastAPI service, GitHub org collector and Streamlit review page as optional.

## Decision

Write and test all optional code. Deploy nothing beyond GitHub Pages and GitHub Actions. The CSV review round-trip remains the default workflow.

## Consequences

More code to maintain and keep covered, but no hosting cost and no new hosted service. Deployment waits until a consumer needs it.
