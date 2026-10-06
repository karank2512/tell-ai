# 0026. Scheduled workflows are kept alive by re-enabling them through the API

- Status: accepted
- Date: 2026-10-06

## Context

GitHub disables a public repository's scheduled workflows after 60 days without activity
in that repository. SPEC sections 11 and 14 rely on the daily `tell-data` commit to keep
`ingest.yml` and `weekly.yml` alive, but those commits land in `tell-data`; the schedules
live in `tell-ai`, which may go quiet for weeks during the unattended soak (the "weeks
unattended" number is the point of it). A keepalive commit to `tell-ai` would make a bot a
contributor of the code repo, which ADR 0025 rules out.

## Decision

`weekly.yml` has a `keepalive` job that calls
`PUT /repos/{owner}/{repo}/actions/workflows/{file}/enable` for `ingest.yml`,
`weekly.yml` and `ci.yml` every Monday with the job's `GITHUB_TOKEN`, granted
`actions: write` for that job only. Re-enabling an enabled workflow is a no-op that resets
the inactivity timer, without a commit.

## Consequences

- The schedules survive any length of unattended operation with no commit to `tell-ai`.
- The daily `tell-data` commit is still the published record and the trigger of the Pages
  deploy; it is no longer what keeps the schedules alive.
- If GitHub ever stops counting the enable call as activity, its warning email arrives a
  week before the cutoff; re-enable by hand (`gh workflow enable ingest.yml`) and revisit.
