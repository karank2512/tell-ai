# 0005. Local-first dev database

- Status: accepted
- Date: 2026-10-05

## Context

Cloud accounts (Neon, Slack, PyPI, GitHub repos) are not needed to build and test the code.

## Decision

Develop and test against a local Postgres (Docker compose Postgres 16, or an existing local server such as Homebrew Postgres). One guided cutover milestone creates the cloud resources.

## Consequences

Fast, free, offline-friendly development. Production-only concerns (Neon limits, secrets) are verified at cutover. Tests create and drop their own temporary database per session.
