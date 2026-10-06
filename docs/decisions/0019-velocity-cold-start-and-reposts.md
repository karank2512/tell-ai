# 0019. Hiring velocity: cold start, vanished-listing reposts, long episodes

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 7 defines `new7`, `base` (floor 0.5), the firing rules and repost detection,
but says nothing about three situations the Phase 6 verification reproduced:

1. **Cold start.** On a board's first poll only the postings open right now emit
   `job_opened`, each dated by `posted_at` (or by the poll when there is none).
   Requisitions that had already closed are invisible, so `base` is built only from
   survivors and comes out too low, and `new7` looks like a spike: one first poll with
   five sales postings and no `posted_at` gave `new7 = 5`, `fires = True`. At launch every
   seed board is polled for the first time at once, so the first runs could fire in bulk.
2. **Reworded twin replacing an old listing in one poll.** Posting A (opened more than 30
   days ago) disappears in the same poll in which a reworded B with the same fingerprint
   appears. A is not closed yet (closing needs two absences 12 hours apart), so repost
   rule 1 (`closed_at` within 30 days) cannot apply, rule 2 fails on `content_hash`, and
   B counts as `job_opened`. Literally compliant, but a common repost pattern.
3. **Long episodes.** "One event per episode" walked back at most 90 days to find the
   episode's first day; a small company opening two sales roles every week (the
   small-company rule has no ratio condition) fired continuously and got a second event
   on day 98.

## Decision

- **First-poll events are flagged and never counted.** `sync_board_postings` sets
  `payload["first_poll"] = true` on every `job_opened` (and `tech_mentioned_first_time`)
  emitted by a board's first poll (`board.last_polled_at` was `NULL`). Velocity leaves
  those events out of `new7`, and therefore out of every `base` window, but keeps them in
  the same-requisition chain so a same-fingerprint posting that follows one of them within
  30 days is still one requisition. The first signal a new board can produce is the
  second poll's postings against the 0.5 floor. The events stay in the table as receipts.
  This is the orchestrator's option (g); the alternative (suppress firing for 35 days per
  board) was rejected because it also hides genuine spikes in a board's first month.
- **Repost rule 3, `vanished_within_30d`.** A new `external_id` whose fingerprint matches
  an open posting that is absent from this poll and was last seen within 30 days is a
  repost of it (most recently seen wins), tried after rules 1 and 2. It is rule 1 caught
  before the close is written: the vanished posting will be closed at the next qualifying
  poll with a `closed_at` inside the 30-day window, after which rule 1 would say the same
  thing. If the old posting reappears, the two sit side by side and velocity counts the
  fingerprint once anyway.
- **Capped walk-back marks the whole history.** When the episode walk-back reaches the
  90-day cap without a 7-day false run, the episode may be older than it can see, and any
  earlier `hiring_velocity` event for the (company, family) marks it: no second event for
  a pair that keeps firing.

Also folded into this record, from the same verification: `job_closed` copies `is_intern`
from the posting's own `job_opened` / `job_reposted` payload (found by its deterministic
`event_id`) instead of deciding from the title alone, so the flags agree; tech-keyword
events of a board that was not yet resolved to a company still count as "seen" once it is
linked, so resolving a board never re-emits its keywords.

## Consequences

- No schema change. `first_poll` is a payload key; `repost_reason` gains a third value.
- `docs/methods/hiring-velocity.md` sections 3, 5 and 6 describe the behaviour; the repost
  share table gets a `vanished_within_30d` row for the soak numbers. The repost share and
  firing rate after the soak are measured excluding first-poll events.
- Launch behaviour: the first poll of the seed boards produces no `hiring_velocity`
  events; signals start with the second poll. Baselines are understated for the first 35
  days (only what tell has seen), which the floor and the ratio rule already tolerate.
