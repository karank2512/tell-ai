# 0045. `build_signals` reads only what can still change

- Status: proposed
- Date: 2026-10-23

## Context

Neon's free plan allows 5 GB of public network transfer a month. Every `tell score
--all-profiles` ran `build_signals`, which loaded every linked event of a signal-bearing type
with its whole JSONB payload (about 29.5 MB a run in production, growing 2 to 3 MB a day; Form D
payloads alone were about 21.9 MB), then looked up the existing signal of every one of them.
Almost all of those rows already had their signal, and nothing about them had changed.

An audit of the rest of the daily pipeline (`ingest.yml`) found no other unbounded rescan:

| Step | What it reads every run | Verdict |
| --- | --- | --- |
| EDGAR / ATS / news / GitHub collectors | the provider (unavoidable); `known_accessions` and board reconciliation are keyed or per-board | collection, not reprocessing |
| `tell resolve` | events with `company_id NULL` and no R0-R6 decision (filtered in SQL) | incremental |
| `emit_velocity_events` | `job_opened` of the last 7 days, per-pair series within a bounded lookback | windowed |
| `score_all` / publish scoring | signals within 4 half-lives, joined to their event payloads | windowed (the receipts need the payload) |
| `write_events_jsonl` | events observed on one UTC day | incremental |
| `write_snapshots` | open postings of active boards | bounded by the board set |
| `discover issuers --apply` | full payloads of every unlinked Form D of the last 90 days | bounded by its window, but re-read daily; follow-up candidate |
| `tell prune` | `archive_events` reads every event older than 180 days, full payload | 0 rows today; once events pass 180 days, those kept because a signal references them are re-read every run; follow-up candidate |

Only `build_signals` grows with all of history, so this change is limited to it. The two
follow-ups are noted, not changed.

## Decision

`build_signals` keeps its contract and its result, and reads three narrow things:

1. **New signals: anti-join, plus a filter on events that can derive a signal, plus only the
   keys it needs.** It reads only eligible events (signal-bearing type, linked,
   `occurred_at <= now`) with **no `signals` row under the key they would derive**
   (`NOT EXISTS` on `(event_id, signal_type, role_family)`; `signal_type` follows from the
   event type alone). It also drops events the deriver certainly turns down: an exact SQL
   mirror of the derivers' `is True` flag tests, plus the 8-K Item 5.02 test. Of each row it
   fetches only the payload keys in `scoring.DERIVATION_KEYS`. Both SQL filters can only let
   extra rows through, never drop one that would derive. Python still derives every row and
   re-checks the key.
2. **Amended funding.** For each existing `funding` signal of an eligible `form_d_filed`, it
   reads the signal id, the stored magnitude and the three payload fields the funding
   derivation reads (`is_amendment`, `total_offering_amount`, `totalOfferingAmount`). It
   rewrites any magnitude the payload no longer supports, as before (ADR 0021 item 8).
3. **Existing keys** of the candidate rows only, for the exact Python check.

### Options compared

- **Last-ingest checkpoint (a watermark on `observed_at` or a run id).** Rejected as the
  default. It would need new state (a table or a file, so a migration or CI cache), and it is
  wrong in four cases the pipeline produces every day:
  - An event is often linked to a company by a later `tell resolve`, `discover issuers` or
    `relink-formd` run, after its `observed_at`.
  - A D/A rewrites an old filing's payload in place. `events` has no `updated_at` column, so
    no watermark sees the change.
  - An event whose `occurred_at` was still in the future at the last build becomes eligible
    later.
  - A failed or rolled-back run would advance or lose the checkpoint unless that is handled
    with care.
  Each case needs an overlap window or extra columns. The cost is a deliberate tradeoff (late
  links beyond the window are frozen) or more state to keep correct.
- **Anti-join against `signals` ("already processed?").** Chosen. `signals` already *is* the
  record of what was processed: one row per derived key. The check is stateless, uses the
  existing unique index, heals itself after a failed run, and treats late arrivals, late
  links and newly due events alike: an event is processed on the first run where it is
  eligible and has no signal. It cannot see a payload change on its own, which is why
  amended funding gets its own narrow read (2).
- **Anti-join with full payloads.** Not enough on its own. Events that derive nothing
  (first-poll tech baselines, 8-Ks without Item 5.02, non-flagship releases) never get a
  signal, so they would be re-read every run. Hence the filter on events that can derive and
  the key projection in (1).

### Correctness

- **Late arrivals.** An event written days after its `occurred_at` has no signal, so the
  anti-join returns it on the next build, whatever its dates.
- **Late links, future-dated events, retries.** These work the same way: eligibility is
  checked against current state on every run, and nothing records "done" except the signal
  row itself.
- **Amendments.** Every existing funding signal is re-derived from its event's current
  amount fields on every run, exactly as before. An amendment of any age is picked up, up or
  down, and history is not frozen.
- **Equivalence.** `tests/signals/test_incremental_build.py` keeps the previous full-scan
  algorithm as a reference. On a multi-day scenario it runs both on the same state (the
  reference in a rolled-back savepoint) and requires identical `signals` rows in the same
  insertion order and the same return value. The scenario has a late arrival, a late link,
  a future-dated event, amendments up and down (including the camel-case string amount) and
  edge payloads for every SQL clause. A rebuild from scratch then reproduces the incremental
  table. Removing a key from `DERIVATION_KEYS`, dropping a clause of the filter, or disabling
  the amendment check each makes that test fail.

## Consequences

Measured on a scratch database seeded at production scale (26,551 events, 21.9 MB of Form D
payload text, 19,195 signals, one new day of 113 arrivals), counting the text width of every
row each version's SELECTs return:

| | Rows returned | Bytes returned |
| --- | --- | --- |
| Before (event scan + existing-key lookup) | 45,746 | 35.3 MB |
| After (all three reads) | 16,613 | 0.70 MB |

What remains is about 41 bytes per Form D funding signal, for the amendment check, plus the
day's new events. Growth falls from the size of every new payload to about 41 bytes per new
filing.

A new deriver that reads another payload key must add it to `DERIVATION_KEYS`. A new flag
test in a deriver should be mirrored in `_may_derive` or left out of it, which is always
safe. The equivalence test catches the first mistake and makes the second only a missed
saving.
