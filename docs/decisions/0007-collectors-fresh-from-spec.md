# 0007. Collectors are built fresh from the spec

- Status: accepted
- Date: 2026-10-05

## Context

An earlier scanner exists, but its structure was not designed around the one-shape event model and idempotency rules.

## Decision

Build every collector fresh from SPEC section 3 with no port of the scanner code.

## Consequences

Consistent event model, fixtures and contract tests from the start, at the cost of re-implementing logic that already exists elsewhere.
