# 0009. Seed domains come from the YC directory only

- Status: accepted
- Date: 2026-10-05

## Context

A broad seed list is needed to exercise discovery and resolution before any partner watchlist exists.

## Decision

Seed `companies` from the public YC company directory only (about 5k domains). Karan's target list arrives later through the watchlist intake.

## Consequences

No private or purchased lists in the public repo. Seeding is documented in `docs/sources/yc-directory.md`. Coverage skews to YC companies until watchlists arrive.
