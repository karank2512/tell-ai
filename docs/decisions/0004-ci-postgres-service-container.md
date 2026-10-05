# 0004. CI uses a Postgres 16 service container

- Status: accepted
- Date: 2026-10-05

## Context

The spec ran migrations against a scratch Neon branch in CI, which needs secrets and fails on fork PRs.

## Decision

CI runs a Postgres 16 service container in GitHub Actions and applies `alembic upgrade head` on every run. Neon is production-only.

## Consequences

CI works on fork PRs with no secrets and is deterministic. Local Postgres 14 versus CI Postgres 16 means schema code must avoid version-specific features.
