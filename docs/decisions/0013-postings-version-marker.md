# 0013. `postings.version_marker` holds the provider change marker

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 5 is the source of truth for the schema and `tell/models.py` must match
it field for field. Its idempotency rule 2 says a posting's state change creates a
`job_updated` event, never a second `job_opened`, and rule 1 keys events on the
provider's own change marker (`updated_at`, `createdAt`, `publishedAt`). Deciding
whether a marker *changed* requires the marker from the previous poll, and the
section 5 `postings` DDL has no column for it.

The Phase 1 implementation derived the previous marker from `events`: a marker was
"known" when a `job_updated` keyed on it existed, or when the posting's `job_opened`
payload recorded it. That works until the same section's size budget applies: events
older than 180 days are moved to the JSONL archive in `tell-data`. After that prune
every posting still open after 180 days has no event left carrying its marker, and
the first poll afterwards emits one spurious `job_updated` per long-open posting. The
events-based lookup also forced the caller to write each poll's events before the
next poll and treated a marker reverting to an earlier value as "no change".

## Decision

Deviate from the section 5 DDL by one nullable column: `postings.version_marker text`
(migration `0002_postings_version_marker`), the provider change marker last seen for
the posting. `sync_board_postings` sets it on insert and on every poll, and
`job_updated` fires exactly when the incoming marker differs from the stored one, as
decided before the row is refreshed. Nothing in the state machine reads `events` back.
A stored `NULL` (a row written before `0002`) has no previous value: the marker is
recorded and no event is emitted for that poll. A marker that reverts to an earlier
value is a change like any other; if that exact `job_updated` was written before,
`write_events` skips it on conflict. `tests/test_schema.py` lists the column
explicitly so the parity test documents the deviation instead of hiding it.

In the same change event payloads drop `board_id`, the database-internal surrogate
key, which has no meaning in the JSONL archive or after a database rebuild; `vendor`
and `board_identifier` remain and identify the board.

## Consequences

Pruning or rebuilding `events` can never produce a spurious `job_updated`; the
180-day archive policy needs no carve-out for long-open postings. One more `text`
column per posting (about 25 bytes on 300,000 rows) is within the Neon size budget.
`docs/SPEC.md` is unchanged; this record is the documentation of the difference. A
future revision of the spec should add the column to the `postings` DDL.
