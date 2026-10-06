# 0021 — Scoring interpretations beyond the spec's wording

Status: accepted (Phase 7). Spec: `docs/SPEC.md` section 8. Method: `docs/methods/scoring.md`.

## Context

Section 8 gives the signal table, the profile YAML, `contribution()` / `score()`, the daily job
and the six explainability tests, but leaves several operational details to the implementer.
Each one below is a choice that could reasonably have gone another way, so it is recorded here
rather than only in code.

## Decisions

1. **Size band gates actions, not storage.** The raw score is computed and stored for every
   scored company. A company outside the profile's `size_band` never emits
   `score_crossed_threshold` and is left off in-market lists, but its `scores` row is normal.
   A company with no active ATS board (so no open-posting count) counts as in band: the filter
   is about company size, not about what tell can see. Alternative rejected: storing 0 for
   out-of-band companies would break "every stored number is explainable from its parts".

2. **Hysteresis = 7-day cooldown + 0.5 re-arm, measured in scored days.** A crossing fires
   when the most recent stored score before the day was below the threshold and today's is at
   or above it, *and* no crossing for the company and profile has a scored day
   (`payload.as_of`) within the last 7 days of `as_of`, *and* (once one has fired) some stored
   score since that crossing's `as_of` was below `threshold - 0.5`. Both comparisons read the
   stored `as_of` dates, never `occurred_at`: a late backfill run stamps an old day's crossing
   with today's instant, and hysteresis is about the days a company was scored, not about when
   tell happened to run. "Previous score" is the most recent stored day, not strictly
   yesterday, so a skipped run does not fabricate a crossing. A company with no stored history
   counts as previously 0, so its first scored day above the threshold fires: that is the alert
   a watchlist owner wants on day one.

3. **Scoring instant = end of the `as_of` UTC day; crossing events carry the run instant.**
   Every event of the day counts; decay numbers depend only on `(as_of, starts_at)` and reruns
   reproduce them. A `score_crossed_threshold` event, however, is something tell *observed*, so
   its `occurred_at` and `observed_at` are both the run instant (`now`), never the end-of-day
   scoring instant (which would date the event in the future relative to the run that wrote
   it). The scored day lives in `version` (idempotency: a same-day rerun derives the same
   `event_id`) and in `payload.as_of` (what item 2 compares). Earlier Phase 7 builds stamped
   `occurred_at = 23:59:59.999999` of the scored day; that was reversed after review.

4. **`default` scores everyone with an active signal; other profiles score their watchlists.**
   The spec says the daily job recomputes "every company on each watchlist". The public site and
   weekly page need `default` scores for companies on no watchlist, so `default` covers every
   company with at least one active signal.

5. **`news_funding` is not a `funding` signal.** The spec table fires `funding` on
   `form_d_filed` only; the filing is the receipt. Funding headlines stay events (and feed the
   news classifier's review queue) without weight.

6. **First-poll `tech_mentioned_first_time` events yield no signal.** A board's first poll emits
   one baseline event per keyword already on the board (`first_poll: true`, ADR 0019 wording).
   That is inventory, not adoption.

7. **`tech_adoption` keyword lookup goes through the event.** `signals` has no payload column
   (section 5 DDL, unchanged). The keyword a `tech_keywords` list is matched against is read
   from the originating event's payload at scoring time.

8. **A `funding` signal's magnitude follows the current payload amount.** Amendments merge
   into the original event's payload (section 5 rule 4) and never create a second event or
   signal. When a `D/A` changes `totalOfferingAmount`, the next `build_signals` rewrites the
   existing signal's `magnitude` from the payload (`log10(amount)` capped at 8) and nothing
   else: `weight`, `half_life_days`, `starts_at` and the row id stay, so stored contributions
   keep pointing at the same signal. Rationale: the contribution summary is rendered from the
   payload, so the number a reader sees and the amount beside it must come from the same
   source, and `signals` must stay rebuildable from `events` (a rebuild from scratch would
   otherwise disagree with the table it replaced). Alternative rejected: freezing the creation
   magnitude (the first Phase 7 build did this) left "$400,000,000 offering" beside a
   contribution computed from $18M. Only `funding` re-derives; every other type's magnitude is
   fixed at its event's creation.

9. **Idempotency of `signals` is enforced in code.** `(event_id, signal_type, role_family)` is
   unique, but `role_family` is NULL for every type except `hiring_velocity` and PostgreSQL does
   not collide NULLs; `build_signals` reads existing keys before inserting.

10. **Heat ties count half.** Percentile rank = share of other list members strictly below plus
    half of those equal, so ties share a rank and the unique top/bottom are 100/0.

11. **`alert_threshold` lives in the profile YAML** (default 6.0) so a partner profile can set
    its own; `TELL_ALERT_THRESHOLD` in `config.py` remains the documented global default.

## Consequences

No schema change. `tests/test_scoring.py` pins each rule; `docs/methods/scoring.md` explains
them to readers of the published numbers. Changing any of them is a new ADR with pilot feedback
attached (spec section 8).
