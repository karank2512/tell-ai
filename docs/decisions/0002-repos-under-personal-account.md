# 0002. Repos live under the personal GitHub account

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 17 suggests a new org named after the product. Creating an org adds overhead before launch.

## Decision

Create `tell-ai` (the code repo, https://github.com/karank2512/tell-ai), `tell-data` (https://github.com/karank2512/tell-data) and `tell-private` under Karan's personal GitHub account (karank2512). The code repo is named `tell-ai`; the Python package and CLI keep the name `tell`. The Pages URL and `TELL_DATA_REPO` are environment-parametrized.

## Consequences

Nothing is hardcoded to an account, so moving to an org later is a configuration change plus a repo transfer.
