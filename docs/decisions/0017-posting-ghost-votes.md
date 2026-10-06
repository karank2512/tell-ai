# 0017. `posting_ghost_votes` stores job-seeker ghost-job votes

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 7 (stale requisitions): "Job-seeker thumbs ('this looks like a ghost job')
feed a `ghost_votes` count that will inform the threshold later; it does not change
weights until there are 500 votes." PLAN Phase 6 repeats it: "`ghost_votes` counter
stored, inert until 500 votes."

None of the section 5 tables can hold the votes. A plain `postings.ghost_votes integer`
column would be a counter with no history: it could not be audited, de-duplicated
later or re-derived, and every vote would be a read-modify-write on the hottest table
in the schema.

## Decision

Deviate from the section 5 DDL by one table (migration `0005_posting_ghost_votes`, model
`tell.models.PostingGhostVote`):

```sql
create table posting_ghost_votes (
  id          bigserial primary key,
  posting_id  bigint not null references postings(id),
  voted_at    timestamptz not null,
  source      text not null            -- the surface the vote came through: 'site', 'api'
);
create index on posting_ghost_votes (posting_id);
```

- One row per vote. Votes are anonymous: no voter identity, handle, IP address or
  cookie is stored (spec section 1, no PII), so a vote cannot be traced to a person.
  The trade-off is that duplicate votes cannot be detected yet; when the threshold
  work starts, any de-duplication scheme gets its own decision record.
- `tell.signals.velocity` exposes `record_ghost_vote(session, posting_id, *, now,
  source)`, `ghost_votes(session, posting_id)` (count for one posting),
  `ghost_votes_total(session)` and `ghost_votes_active(session)` (total >= 500,
  `GHOST_VOTES_INERT_UNTIL`).
- The count is **inert**. No weight, magnitude or score reads it. Even when
  `ghost_votes_active` turns true, nothing changes automatically: the rule that would
  use the votes is a later decision with the measured vote data attached.
- Nothing collects votes yet. The intake surface (site button, API endpoint) arrives
  with the outputs and API phases.

## Consequences

- `tests/test_schema.py` lists the table with its columns, primary key, foreign key and
  index, and `HEAD_REVISION` moves to `0005`.
- `postings` keeps exactly the spec's columns (plus `version_marker`, ADR 0013).
- Pruning: votes reference `postings`, which are never deleted, so no cascade rule is
  needed. If postings are ever pruned, votes go first.
