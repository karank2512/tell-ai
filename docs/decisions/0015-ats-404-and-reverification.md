# 0015. One 404 / dead-board rule for every ATS collector

- Status: accepted
- Date: 2026-10-05

## Context

Three rules in the spec meet when a job board answers 404:

- SPEC section 3.1, gotchas: a board that 404s is usually a renamed token, so
  discovery should run again before the board is marked dead.
- SPEC section 4 step 5: `ats_boards.last_verified_at` is written by discovery, and a
  board is re-verified every 30 days "and whenever a board returns 404 twice in a row".
- SPEC section 5 rule 3: `job_closed` fires only after two consecutive polls without
  the posting, at least 12 hours apart.

Section 5's `ats_boards` has no column for a 404 streak, so the four ATS collectors
each made up their own bookkeeping, and the four versions disagreed:

- Greenhouse and SmartRecruiters counted a 404 as a poll (advancing `last_polled_at`).
  As a result, `200 (posting present) -> 404 -> 200 (posting absent)` closed the posting
  after one real absence.
- Ashby advanced `last_polled_at` on a 404 and then rewound it to `last_verified_at`
  on the next 200.
- Lever kept the streak in an open `review_queue` item, and it skipped dead boards, so
  a dead board could never come back.
- All four wrote `last_verified_at` on every successful poll. That column belongs to
  discovery's ownership check, and the collector writes broke the 30-day
  re-verification.
- Two 404s six hours apart (two consecutive scheduled runs) were enough to kill a
  board.

## Decision

One rule, implemented once in `tell/collectors/base.py` and called by all four ATS
collectors (Greenhouse, Lever, Ashby, SmartRecruiters):

1. **Collectors never write `ats_boards.last_verified_at`.** Only discovery's ownership
   verification sets it (SPEC section 4 step 5, 30-day re-verification).
2. **A 404 never advances `last_polled_at`.** `last_polled_at` is the time of the last
   poll that answered 200. `sync_board_postings` uses it as "the previous poll", so
   section 5 rule 3 counts only real polls. A 404 between two 200s therefore cannot
   close a posting.
3. **Two new columns hold the streak** (migration `0004_ats_boards_not_found`, model
   `AtsBoard`):

   ```sql
   alter table ats_boards add column not_found_count    integer not null default 0;
   alter table ats_boards add column first_not_found_at timestamptz;
   ```

   - `record_board_not_found(session, board, *, now) -> str` is called on every 404.
     It adds one to `not_found_count`, sets `first_not_found_at` on the first 404 of a
     streak, and returns the board's resulting status. The board becomes
     `status = 'dead'` when `not_found_count >= 2` and
     `now - first_not_found_at >= 12h` (the same 12 hours as section 5 rule 3, so
     consecutive six-hourly runs cannot kill a board on their own). At that point it
     opens one `review_queue` item with `kind = 'board_verify'` and payload
     `{board_id, vendor, identifier, reason: 'not_found_twice', first_not_found_at,
     last_not_found_at}`, unless an open `board_verify` item for the same vendor and
     identifier already exists. That covers items discovery raised as well, so there is
     never more than one open item per board.
   - `record_board_ok(session, board, *, now)` is called on every 200, after
     `sync_board_postings`. It resets `not_found_count` to 0 and `first_not_found_at`
     to `NULL`, sets `last_polled_at = now`, and turns a `dead` board back to `active`.
4. **A dead board is still polled.** Callers may keep polling `dead` boards, and a 200
   brings one back. Lever's `ingest_board` now requests `active` and `dead` boards and
   still skips `paused`, `no_board` and `unsupported`.
5. **Discovery owns what happens after death.** `dead` is the "404 twice in a row"
   marker that `tell.discovery.ats.reverify_due` reads. Discovery decides when to
   re-verify a dead board (once, not on every run); collectors do not call discovery.

Details beyond the orchestrator's brief, recorded here so they are not hidden:

- `record_board_ok` also resolves the open `not_found_twice` item for a board that had
  died. It resolves only items with that reason and never touches items discovery
  raised, because once the board answers again the item is stale.
- A `paused` board keeps its status when it 404s; its count still goes up. `paused`
  means a human owns the board's `board_verify` item, and turning the board `dead`
  would let discovery's re-verification skip that human.
- If a human resolves the item while the board is still dead, the next 404 that meets
  the threshold opens a new item. There is still at most one open item at a time.
- Boards marked `dead` under the old rules start with `not_found_count = 0`. They stay
  `dead` until a 200 revives them or discovery re-verifies them.

## Consequences

- One 404 between two successful polls can no longer produce a `job_closed`.
- A board that 404s on two consecutive six-hourly runs stays `active` until a 404 at
  least 12 hours after the first, which takes one or two more runs.
- `last_verified_at` again means "discovery last confirmed who owns this board", so
  the 30-day re-verification works as SPEC section 4 describes.
- The Ashby rewind, the Lever review-item counter and the Greenhouse/SmartRecruiters
  404-as-a-poll behaviour are gone. A new ATS collector calls the two helpers and
  writes no liveness logic of its own.
- Each collector's tests run the same regression scenarios
  (`tests/collectors/_board_liveness.py`).
- `docs/SPEC.md` is unchanged; this record documents the difference. A future revision
  of section 5 should add the two columns to `ats_boards`.
