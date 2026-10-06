# 0023. Publishing interpretations: rounding, heat on public pages, weekly edition, run-health

- Status: accepted
- Date: 2026-10-06

## Context

SPEC section 9 (outputs), section 12 (run-health) and section 14 (Pages limits) leave a
few details open that Phase 8 had to settle in code. None changes a weight, a half-life or
a signal; each is a presentation or measurement rule, recorded here so it can be revisited.

## Decision

1. **Rounding that adds up.** `score` is the raw sum rounded half-up to one decimal.
   Contributions are rounded to one decimal by largest-remainder apportionment, so the
   displayed contributions add up to the displayed score exactly (section 16 M2: "sum by
   calculator"). A tiny contribution may display as `0.0`; it is still listed with its
   receipt.
2. **`why_not_higher` in the JSON.** The per-domain object carries the section 9 keys plus
   `why_not_higher` (`type`, `role_family?`, `decayed_at`, `url`, `summary`), the account
   page's footer in machine form, documented in `docs/api/company.schema.json`.
3. **`generated_by` is the code commit.** `{TELL_REPO_URL}/commit/{sha}` with the sha from
   `GITHUB_SHA` or `git rev-parse HEAD`, else `local`. The section 9 example shows a
   `tell-data` commit, which does not exist yet when the files are generated.
4. **Heat on public pages.** Heat is a percentile within one watchlist (section 8), so the
   public account page shows it only for public watchlists that include the company
   ("heat 87 in pilot"), and the per-domain JSON carries it only when served through
   `api/v0/watchlists/{slug}.json`.
5. **Scores are computed, not read.** Published scores are computed from `signals` at
   publish time (`write=False`); they equal the stored `scores` rows for the same day.
   The Slack digest reads the stored row of the crossing's scored day.
6. **Weekly edition.** One edition covers one ISO week (Monday 00:00 UTC to the next).
   `--week current` resolves to the most recent *complete* week, because the weekly job
   runs Monday 06:23 UTC. "Sales-hiring velocity" sums `hiring_velocity` magnitudes of role
   family `sales` only (not `gtm_ops` / `customer`). "What changed in the method" is the
   first `CHANGELOG.md` bullet mentioning scoring, velocity or resolution.
7. **Run-health.** `freshness_p95_h` leaves out first-poll baselines (`payload.first_poll`,
   postings already open when a board was first polled): they measure a job's age, not
   tell's lag. A source with no `runs` row at all is `never_run`, not stale, so an
   optional collector the schedule does not run (GitHub) cannot paint the public page red;
   the dead-man step is what fails a run for a silent scheduled source. Health sources
   are the registered collectors plus the internal jobs `resolve`, `score`, `publish`.
8. **Digest hour.** `TELL_SLACK_DIGEST_HOUR` defaults to 7: the first ingest run at or
   after 07:00 local time on a local day with no digest sent yet sends it. With the
   `17 2,8,14,20` cron and Pacific time that is the 07:17 run in summer and the 12:17 run in
   winter; the spec's "08:00" has no ingest run near it.
9. **Size budget.** Account pages and JSON exist for companies with a signal starting in
   the last 180 days; files for companies that drop out are deleted at the next render.
   Weekly pages are never deleted.

## Consequences

- Readers can check every number on a page with a calculator.
- The weekly page is stable for a week; `api/v0/weekly/latest.json` changes on Mondays.
- Health is honest about sources that never ran without flagging them as failures.
