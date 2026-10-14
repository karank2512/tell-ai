# 0029. Ingest runs once a day by default; four a day when TELL_RUNS_PER_DAY is 4

- Status: accepted
- Date: 2026-10-13

## Context

SPEC section 11 schedules `ingest.yml` four times a day (`17 2,8,14,20 * * *`) on the
premise that the code repository is public, where GitHub Actions minutes are free. Karan
keeps `tell-ai` private while it is still being built. Private repositories on the Free
plan get 2,000 Actions minutes a month, billed per job and rounded up to the minute. A live
run is about eight jobs and an estimated 50 to 60 billed minutes, dominated by the news leg
at 1 request per second, so four runs a day would use roughly 6,000 minutes a month and stop
the schedule (and CI) after about ten days.

## Decision

`ingest.yml` has two cron lines: a daily slot `17 15 * * *` that always runs, and extra
slots `17 2,8,20 * * *` that run only when the repository variable `TELL_RUNS_PER_DAY` is
`4`. The `preflight` job compares `github.event.schedule` (the cron line that fired) with
the extra-slot line and skips the run cleanly otherwise, with a step-summary note. Manual
runs (`workflow_dispatch`) always run.

The daily slot is 15:17 UTC rather than 14:17 so the Slack digest rule (first run at or
after 07:00 `America/Los_Angeles`) still fires every day in winter: 15:17 UTC is 07:17 PST
and 08:17 PDT.

## Consequences

- Private: one live run a day, about 50 to 60 billed minutes, plus three skipped extra
  slots of about 1 minute each and CI on pushes: inside 2,000 minutes a month. Freshness is
  about a day, which still meets the business plan's 24-hour target; the SPEC's 12-hour p95
  needs four runs.
- Public: set `TELL_RUNS_PER_DAY` to `4` (Settings, Secrets and variables, Actions,
  Variables). No file change; the schedule becomes 02:17, 08:17, 15:17 and 20:17 UTC.
- The `discover` job covers 300 new domains per run, so seeding the YC directory takes
  about two weeks at one run a day (a few days at four), unless discovery is run by hand.
- Tests in `tests/test_workflows.py` execute the preflight script for every slot and
  variable combination.
