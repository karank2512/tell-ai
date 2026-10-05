# 0006. Gold set location and public sample

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 6 keeps the 300-row gold set in the private repo, but fork CI must still exercise entity resolution.

## Decision

The full gold set lives in `tell-private`. A scrubbed 50-row sample ships at `tests/fixtures/gold_sample.csv` so fork CI always runs the resolution tests. The full 300-row gate runs on `main` after cutover using a read-only deploy key.

## Consequences

Forks get meaningful resolution tests without private data. The precision and recall gate on the full set only runs after cutover.
