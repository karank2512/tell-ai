# 0003. Drop the Workday and Oracle collectors

- Status: accepted
- Date: 2026-10-05

## Context

Workday and Oracle career endpoints are undocumented; the spec had them off by default behind a flag.

## Decision

Do not build Workday or Oracle collectors. Discovery still detects Workday, Oracle and Workable patterns and records them in `ats_boards` with `status='unsupported'`. Nothing polls them. `TELL_ENABLE_UNDOCUMENTED_ATS` is not created.

## Consequences

No legal or maintenance exposure from undocumented endpoints. Coverage reporting stays honest because unsupported boards are visible as "we cannot see this one". The `ats_boards.status` enum gains `unsupported`.
