# 0028. Operations interpretations: ingest.yml shape, poll gap, dead-man vs health, manual marker

- Status: accepted
- Date: 2026-10-06

## Context

Phases 9 to 11 built the run-health and workflow surface (`tell/cli.py`, `tell/ops`,
`.github/workflows/ingest.yml`, `tell/backtest/ops.py`) and made several small calls that
SPEC sections 11 to 13 and PLAN Phase 10 leave open or word differently. CLAUDE.md asks for
every deviation to be recorded here. The Phase 9 and Phase 10-12 verifiers listed them:

1. PLAN Phase 10 says `ingest.yml` follows the SPEC section 11 YAML verbatim with discovery
   "inside the ATS job"; the workflow has a separate budgeted `discover` job and a
   `tell prune --out tell-data` step the SPEC YAML does not show.
2. `tell ingest` skips boards polled in the last **5** hours, not the 6-hour cron interval.
3. `tell deadman` checks a fixed list of scheduled sources and treats a source that never
   ran as silent, while `health.json` shows the same source as `never_run` and not stale
   (ADR 0023).
4. `tell prune` archives events older than 180 days but keeps those a `signals` row
   references.
5. "Weeks unattended" (SPEC 13.4: "days since the last manual intervention ... from the
   `runs` table") needs a marker in `runs`, and the table cannot see a human.

## Decision

1. **Discovery is its own job; prune runs every time.** `discover` runs ahead of the four ATS
   legs with the SPEC budget (`tell discover batch --limit 300 --rps 4`, 30-minute timeout);
   `ats` `needs` it but runs unless the whole workflow was cancelled, so a failed or slow
   discovery pass never blocks polling. Inside the matrix it would have run four times (once
   per vendor) or needed a leader election. `tell prune --out tell-data` runs on every
   `resolve_score_publish` job rather than on a monthly guard: it is idempotent, usually
   finds nothing, and the 180-day archive lands in the same `tell-data` commit as the events
   it removes. Both are described in `docs/deploy/github-actions.md`.
2. **Poll gap is one cron slot less an hour of slack** (`MIN_POLL_GAP = 5 h`). Scheduled runs
   start late by varying amounts; a strict 6-hour gap would skip every other slot for most
   boards. `--force` overrides it. The help text quotes the constant.
3. **The dead-man and the health page disagree on purpose.** `tell deadman` checks exactly
   the sources `ingest.yml` schedules (`SCHEDULED_SOURCES`, enforced by
   `tests/test_workflows.py`) and fails on a scheduled source that never ran, because its
   job is to catch a schedule that stopped feeding a source. The health page lists every
   registered collector, optional ones included (`github` is not scheduled, ADR 0008), and
   shows `never_run` without turning red, because a public page should not be permanently
   red for a collector nobody runs. There is no pause flag in the configuration, so "active
   source" means "scheduled source". A source that *has* run and then falls silent is
   `stale` in both.
4. **Prune keeps evidence.** An event older than 180 days that a `signals` row still cites
   (`signals.evidence_event_id`) is archived to `tell-data/events/` like the rest but not
   deleted, so an account page's receipts never dangle; it is deleted once the signal is.
5. **The manual-intervention marker is written by the run lifecycle.** `start_run` and
   `finish_run` (`tell/collectors/base.py`) write `runs.notes.manual = true` on every run
   recorded while `is_manual_run()` is true: the global CLI flag `tell --manual <command>`
   forces it, otherwise the `TELL_RUN_MANUAL` setting decides. `ingest.yml` exports
   `TELL_RUN_MANUAL=${{ github.event_name == 'workflow_dispatch' }}`, so a hand-dispatched
   run (the SPEC's "re-run one collector after a fix") marks every run it records and a
   scheduled run never does. `tell backtest ops` anchors "weeks unattended" on the latest
   marked run (its `finished_at`), falling back to the first run ever, and prints the
   dead-man proxy of `tell runlog --week` (weeks since the last 24-hour gap) beside it.
   Runs of the nightly intake reader (`runs.source = 'intake'`, which lives in
   `tell-private`) are not pipeline runs and are excluded from success rates and from the
   unattended anchor.

## Consequences

- `ingest.yml` differs from the SPEC YAML by one job and one step; `tests/test_workflows.py`
  pins the shape (discover budget, `ats` needs discover, prune before the commit).
- A board can be polled up to five hours after its previous poll; nothing downstream assumes
  a six-hour spacing.
- `health.json` may say `stale_sources: []` while the dead-man step fails, only for a
  scheduled source that has never run (the first hours after cutover, or a source added to
  the schedule but not yet run). Once every scheduled source has one run the two agree.
- The operator's rule: dispatch `ingest.yml` by hand (Actions -> Run workflow, or
  `gh workflow run ingest.yml -f source=...`) whenever a fix was needed to keep ingest
  green, run local fixes with `tell --manual ...`, and never set `TELL_RUN_MANUAL` on a
  schedule. An unmarked fix makes the unattended number look better than it is; the
  dead-man proxy printed beside it is the cross-check.
