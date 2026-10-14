# Running tell on GitHub Actions

tell runs entirely on GitHub Actions and GitHub Pages: four ingest runs a day, a Monday
edition, CI on every pull request, and a tag-triggered PyPI release of `tell-mcp`. The code
repo is public, so the minutes are free (SPEC section 14). Nothing here needs a card.

| Workflow | Trigger | What it does |
| --- | --- | --- |
| [`ingest.yml`](../../.github/workflows/ingest.yml) | `17 15 * * *` daily, plus `17 2,8,20 * * *` when `TELL_RUNS_PER_DAY` is `4` (UTC; ADR 0029), and manual | Discovery, four ATS legs, EDGAR + news, then resolve, score, publish to `tell-data`, prune, Slack digest, dead-man |
| [`weekly.yml`](../../.github/workflows/weekly.yml) | `23 6 * * 1` (Mondays 06:23 UTC) and manual | Weekly page, methods reports into `tell-data`, operations line, private Slack summary, schedule keepalive |
| [`ci.yml`](../../.github/workflows/ci.yml) | Pull requests, pushes to `main`, `13 6 * * 1` (Mondays 06:13 UTC), manual | Ruff, mypy, Postgres 16 service + `alembic upgrade head`, pytest with coverage floors, gitleaks; `pip-audit` weekly |
| [`publish-mcp.yml`](../../.github/workflows/publish-mcp.yml) | Push of a tag `tell-mcp-v*` | Builds `packages/tell-mcp` and publishes it to PyPI by trusted publishing |
| [`tell-data-pages.yml`](tell-data-pages.yml) | Copied into `tell-data` at cutover | Deploys `tell-data/site/` to GitHub Pages on every push |
| [`tell-private-intake.yml`](tell-private-intake.yml) | Copied into `tell-private` at cutover; `41 3 * * *` (nightly 03:41 UTC) and manual | `tell intake sync`: the intake form's sheet into watchlists; commits `watchlists/*.yaml` to `tell-private` only |

Schedules avoid `:00` because GitHub delays top-of-the-hour cron runs.

## Secrets and variables (code repo)

Settings -> Secrets and variables -> Actions on `karank2512/tell-ai`. Values never go in
the repo; `.env.example` lists every variable with a comment.

**Secrets**

| Name | Required | Used by | Value |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | ingest, weekly | Neon connection string, `postgresql+psycopg://...?sslmode=require` |
| `SEC_USER_AGENT` | yes | ingest, weekly | `tell/0.1 (<contact address>)`; the SEC requires a real contact |
| `TELL_DATA_PUSH_TOKEN` | yes | ingest, weekly | Fine-grained personal access token: repository access **only** `tell-data`, permission **Contents: Read and write**, nothing else. Set an expiry and a calendar reminder to rotate it |
| `SLACK_WEBHOOK_URL` | yes for the digest | ingest (`tell notify --slack`) | Incoming webhook of the digest channel |
| `SLACK_WEEKLY_WEBHOOK_URL` | optional | weekly | Incoming webhook of the private channel; the step is skipped when unset |

`GITHUB_TOKEN` is provided by Actions; every workflow pins it to `contents: read` and
grants more only where one job needs it (`actions: write` for the weekly keepalive,
`id-token: write` for the PyPI publish job, `pull-requests: read` for gitleaks).

**Variables**

| Name | Required | Value |
| --- | --- | --- |
| `TELL_DATA_REPO` | yes | `karank2512/tell-data` |
| `TELL_BASE_URL` | no | Defaults to `https://karank2512.github.io/tell-data` (tell/config.py) |
| `TELL_FEEDBACK_FORM_URL` | no | Prefilled Google Form link for thumbs and `rate_this_row` |
| `TELL_INTAKE_FORM_URL` | no | "Watch these domains" form linked from the digest |

`TELL_RUN_MANUAL` is neither a secret nor a variable: the workflow derives it from the
event name (see "Re-running one source" below).

Unset variables expand to empty strings, and tell's settings ignore empty values, so the
defaults in `tell/config.py` apply. `TELL_REPO_URL` and `TELL_DATA_REPO_URL` are derived
from `github.repository` and `TELL_DATA_REPO`. `TELL_HTTP_USER_AGENT` is
`tell/0.1 (+https://github.com/<this repo>)`.

**Preflight guard.** `ingest.yml` and `weekly.yml` start with a `preflight` job that only
checks which secrets exist (never their values; secrets cannot be read in a job-level
`if:`) and every other job is gated on its outputs. A run whose summary says
`ingest skipped: DATABASE_URL secret not set; see docs/CUTOVER_CHECKLIST.md` (or `weekly
skipped: ...`) did nothing and is green on purpose: the code repo has no `DATABASE_URL`
yet, so there is nothing to poll and no failure email. Once the secrets exist the next
scheduled run goes live by itself, with no edit or re-enable. A `workflow_dispatch` run
behaves the same way. With `DATABASE_URL` set but `TELL_DATA_PUSH_TOKEN` or the
`TELL_DATA_REPO` variable missing, collection, resolve, score and the dead-man still run
and every `tell-data` step (checkout, publish, prune, commit, digest, weekly page) is
skipped with a warning. The weekly `keepalive` job is not gated: it needs no secret and is
what keeps the schedules enabled until cutover.

## ingest.yml, step by step

```
discover (30 min) ──► ats x4 (90 min each, fail-fast: false) ──┐
filings: edgar, then news (45 min) ────────────────────────────┴─► resolve_score_publish (45 min, if: always())
```

1. **discover**: `tell discover batch --limit 250 --rps 4` with
   `TELL_DISCOVERY_MAX_MINUTES=22`, inside the SPEC section 11 budget (at most 300 new
   domains per run at 4 requests per second; boards due for re-verification follow the new
   domains). Six domains are in flight at once (`TELL_DISCOVERY_CONCURRENCY`), all sharing
   the one 4 rps budget. Every domain is committed as it finishes and no new domain starts
   after 22 minutes, so a slow day defers the rest to the next run instead of losing the
   pass (the first production run, 300 serial domains, hit the 30-minute timeout and kept
   nothing). The SPEC places discovery inside the ATS job; it is a job of its own so it
   runs once per workflow rather than once per vendor leg (ADR 0028). At 250 domains a run,
   once a day, the ~4,300 active YC domains take about two and a half weeks; at four runs
   a day, about five days. A failed or timed-out discovery pass never blocks the ATS legs, which `need`
   it but run unless the whole run was cancelled.
2. **ats**: one matrix leg per vendor (`greenhouse`, `lever`, `ashby`, `smartrecruiters`),
   `tell ingest --source <vendor>`, then the run line in the step summary.
3. **filings**: `tell ingest --source edgar` (8-K, S-1 and Form D from the daily index),
   then `tell ingest --source news`; news runs even when EDGAR failed. The job timeout is
   45 minutes (the second production run used 29.9 of the earlier 30); news still stops
   itself after its own 25-minute run budget, so the extra room goes to EDGAR.
4. **resolve_score_publish**: checks out `tell-data` with `TELL_DATA_PUSH_TOKEN`, then
   `tell resolve`, `tell score --all-profiles`, `tell publish --out tell-data`,
   `tell prune --out tell-data`, a commit as
   `tell-bot <tell-bot@users.noreply.github.com>` with message `ingest <UTC timestamp>`
   (or "nothing to commit"), `tell notify --slack`, `tell deadman`.

`tell prune` runs every time rather than on a monthly guard: it is idempotent, finds
nothing to do on most runs, and archives events older than 180 days to
`tell-data/events/` before deleting them, so the archive is committed in the same push
(ADR 0028).
The commit step retries once with `git pull --rebase` if `weekly.yml` pushed in between.

### Run-time estimates (SPEC sections 11 and 14)

| Leg | Estimate |
| --- | --- |
| Each ATS vendor | 6,000 boards at 8 concurrent, ~250 ms each: under 4 minutes; 25,000 boards stays well under the 90-minute timeout |
| EDGAR | One daily index file plus one XML per new filing: a minute or two, under 500 requests a day |
| EDGAR + news (measured) | The second production run (2026-10-13) used 29.9 of the then 30-minute timeout; now 45 minutes |
| News | The slow one at 1 request per second: companies with activity in the last 30 days plus every watchlist member, the long tail rotated over a week; stops itself after its 25-minute run budget |
| Discovery (measured) | 80 real YC domains, 4 rps: serial 6.06 s a domain (5.8 s on the 2026-10-13 snapshot runs), six workers 4.47 s a domain before the deeper scan (ADR 0030) and **4.74 s a domain** with it (379.4 s for 80, re-measured 2026-10-14). 250 domains is about 20 minutes; the 22-minute budget defers anything past it; job timeout 30 minutes |
| Resolve, score, publish, prune | A few minutes, dominated by writing account pages |
| Total | An estimated 50 to 60 billed minutes per live run (each job is rounded up to the minute); one run a day fits the 2,000 free minutes of a private repository, four a day need the repository to be public (ADR 0029); Neon about 25 minutes of 0.25 CU per run, ~60 CU-hours a month of the 100 free |

If an ATS leg approaches 90 minutes, split its boards across more matrix legs (SPEC
section 14) rather than raising the timeout.

### Failure policy

- A failed matrix leg never stops the other vendors (`fail-fast: false`).
- `resolve_score_publish` runs `if: always()`, so one broken vendor never blocks the
  publish or the digest; it publishes whatever the healthy sources produced.
- The **dead-man** step (`tell deadman`) is last. It exits 1 when any scheduled source
  (the four ATS vendors, `edgar`, `news`) has had no successful run in 24 hours, or has
  never run at all, writes the report to the step summary, and turns the run red. The
  health page shows a never-run source as `never_run` without turning red (ADR 0023), so
  the two disagree only until every scheduled source has run once (ADR 0028). GitHub emails the repo owner about a
  failed scheduled run, which is the alert: no pager, no cost. One bad run does not page;
  24 hours of silence does.
- The health page (`health.html`, `api/v0/health.json`) is rebuilt by every publish with
  `last_ok_at`, `success_rate_7d`, `fetched_24h`, `inserted_24h` and `freshness_p95_h` per
  source, and the digest opens with a red line when a source is stale.
- `concurrency: ingest` with `cancel-in-progress: false` serializes runs, so two polls never
  race on the same board; a run that starts while the previous one is still going waits.

### Re-running one source

After a fix, run one collector without waiting for the next slot: Actions -> ingest ->
Run workflow, and set **source** to one of `greenhouse`, `lever`, `ashby`,
`smartrecruiters`, `edgar`, `news` or `discover`. Or from a terminal:

```bash
gh workflow run ingest.yml -f source=lever
```

The other legs are skipped (the three other ATS legs finish as no-ops), and resolve,
score, publish and the dead-man still run so the fix shows up on the site. Reruns are safe:
every collector is idempotent (SPEC section 5), and boards polled in the last five hours
are skipped. Leave **source** empty to run everything.

A manual run counts as an operator intervention. `ingest.yml` exports
`TELL_RUN_MANUAL=true` on `workflow_dispatch` (and `false` on the schedule), and the run
lifecycle in `tell/collectors/base.py` then writes `runs.notes.manual = true` on every run
the workflow records (discover, each ATS leg, edgar, news, resolve, score, publish, prune).
`tell backtest ops` and `tell runlog --week` measure "weeks unattended" (SPEC 13.4) from
the latest such row; without one they count from the first run ever (ADR 0028). The same
marker is written locally by the global flag `tell --manual <command>` (for example
`uv run tell --manual ingest --source lever --force` after a hotfix), which wins over the
variable for that invocation.

The rule: re-run from the Actions page or `gh workflow run` whenever a fix was needed to
keep ingest green, use `tell --manual` for a fix applied from a terminal, and never export
`TELL_RUN_MANUAL` in a scheduled job. An unmarked fix makes the unattended number look
better than it is, and a marked scheduled run resets it for nothing; the dead-man proxy
(weeks since the last 24-hour gap) is printed beside the headline number as the
cross-check.

## weekly.yml

Mondays at 06:23 UTC, before the 08:17 ingest run:

1. `tell weekly --week current --out tell-data`: the edition for the last complete ISO
   week (`site/weekly/<YYYY-Www>.html`, `site/api/v0/weekly/latest.json`).
2. `tell backtest refresh --out tell-data/site/methods`: the dated methods reports. This
   reads the Wayback cache only and never fetches: the runner has no `tell-private`
   checkout (the cache lives in `tell-private/cache/wayback`), so the 13.2 Wayback
   reconstruction reports every board as "not fetched yet" until a maintainer runs
   `tell backtest velocity-lead --refresh` locally and commits the cache to `tell-private`.
3. `tell runlog --week`: the operations line, into the step summary.
4. Commit and push `tell-data` as `tell-bot` (`weekly <UTC timestamp>`).
5. If `SLACK_WEEKLY_WEBHOOK_URL` is set, a plain-text summary (top hiring, top Form D,
   coverage, the operations line, the page link) to the private channel, for turning into
   the LinkedIn post.
6. **keepalive**: re-enables `ingest.yml`, `weekly.yml` and `ci.yml` through the API (see
   below).

**The weekly workflow never commits to this repo.** The owner does not want
`github-actions` (or any bot) to appear as a contributor of `tell-ai`, so refreshed reports
go to `tell-data/site/methods/`: browsable as rendered Markdown on github.com and served
from Pages. `docs/methods/` in this repo holds the method write-ups and whatever reports a
maintainer regenerates by hand. See [ADR 0025](../decisions/0025-weekly-reports-published-to-tell-data.md).

The published copy is indexed: on the first report `tell backtest refresh` creates
`tell-data/site/methods/README.md` with the generated list of dated reports (newest first)
and the `-latest` pointers, and rewrites that list every Monday. The dated file
`<name>-YYYY-MM-DD.md` is the citable source; `<name>-latest.md` is a convenience copy that
is only written when the report is at least as new as every dated one already there, so a
regenerated past date never moves it backwards.

Use **Run workflow** with **week** = `YYYY-Www` to rebuild a past edition.

## The 60-day rule

GitHub disables a public repository's scheduled workflows after 60 days without activity
in **that** repository. The ingest commits land in `tell-data`, so they keep `tell-data`
active but do nothing for the schedules in `tell-ai`, contrary to what SPEC section 11
assumes. Two things keep the schedules alive:

- **keepalive** (`weekly.yml`): every Monday it calls
  `PUT /repos/{repo}/actions/workflows/{file}/enable` for the three scheduled workflows,
  using the job's own `GITHUB_TOKEN` with `actions: write`. Re-enabling resets the
  inactivity timer without a commit (ADR 0026).
- Any push to `tell-ai` by a maintainer also counts as activity.

If a schedule is ever disabled anyway (GitHub emails a warning a week before), re-enable
it under Actions -> the workflow -> "Enable workflow", or with
`gh workflow enable ingest.yml`. The daily `tell-data` commit still matters: it is the
published record and it is what triggers the Pages deploy.

## GitHub Pages on tell-data

Pages can serve a branch only from its root or from `/docs`; `/site` is not an option, and
tell's generator writes the site to `tell-data/site/` next to `events/` and `postings/`,
which must not be public web pages. So Pages deploys from a workflow:

1. Copy [`docs/deploy/tell-data-pages.yml`](tell-data-pages.yml) to
   `tell-data/.github/workflows/pages.yml` and push it.
2. In `tell-data`: Settings -> Pages -> Build and deployment -> Source: **GitHub Actions**.
3. Every push to `main` that touches `site/**` uploads `site/` with
   `actions/upload-pages-artifact` and publishes it with `actions/deploy-pages`. The ingest
   and weekly pushes use the `TELL_DATA_PUSH_TOKEN` PAT, and PAT pushes do trigger
   workflows (pushes made with `GITHUB_TOKEN` would not).

The site is then at `https://karank2512.github.io/tell-data/` (`TELL_BASE_URL`). No Jekyll
build runs, so Markdown reports under `site/methods/` are served as plain text; their
rendered view is the github.com file view. Four ingest pushes a day and one weekly push
stay far below the Pages limit of 10 builds an hour, and the site budget (under 200 MB at
25,000 account pages, pruned past 180 days of silence) is far below the 1 GB soft limit.

## The intake job on tell-private

The nightly intake reader (SPEC 15.6) runs from
[`docs/deploy/tell-private-intake.yml`](tell-private-intake.yml), copied to
`tell-private/.github/workflows/intake.yml`. It lives there, not here, because its input is
customer contact details: the logs and the commits of a private repo are private. It
needs two secrets **on `tell-private`**: `DATABASE_URL` (the same string as above) and
`TELL_INTAKE_CSV_URL` (the published-as-CSV link of the form's response sheet, a secret
because anyone with it can read every response). It checks out `tell-private` and the
code (`karank2512/tell-ai`, public after cutover), runs
`uv run tell intake sync --private-dir ../tell-private` and commits `watchlists/*.yaml`
back to `tell-private` as `tell-bot` with `contents: write` on that repo only. It writes
nothing to `tell-ai` or `tell-data`. Steps and the form itself:
[docs/intake/google-form.md](../intake/google-form.md).

## Publishing tell-mcp to PyPI

`publish-mcp.yml` builds `packages/tell-mcp` with `uv build --package tell-mcp` and
publishes with `pypa/gh-action-pypi-publish` through **trusted publishing**: PyPI accepts a
short-lived OIDC token from this exact workflow, so no PyPI token is stored anywhere. Only
the `publish` job gets `id-token: write`, and it runs in the `pypi` environment.

One-time setup (PLAN Phase 13 step 5):

1. Create a PyPI account with two-factor authentication.
2. PyPI -> Your account -> Publishing -> **Add a new pending publisher** (the project does
   not exist yet): PyPI project name `tell-mcp`, owner `karank2512`, repository `tell-ai`,
   workflow name `publish-mcp.yml`, environment name `pypi`.
3. In `tell-ai`: Settings -> Environments -> New environment `pypi`. Optionally add
   yourself as a required reviewer so each release waits for one click, and restrict
   deployment to tags matching `tell-mcp-v*`.

Releasing:

```bash
# bump version in packages/tell-mcp/pyproject.toml, merge, then:
git tag tell-mcp-v0.1.0
git push origin tell-mcp-v0.1.0
```

The build job fails when the tag does not match the package version. The first successful
run turns the pending publisher into the project's trusted publisher.

## CI and security checks (SPEC section 12)

- `test`: `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy --strict tell`,
  `alembic upgrade head` against a Postgres 16 service container (ADR 0004; works on fork
  PRs without secrets), `pytest -q --cov=tell --cov=tell_mcp --cov-fail-under=80`, then
  `scripts/check_coverage.py` for the 90% floors on `collectors/`, `resolve/`, `signals/`.
  `tests/test_workflows.py` parses every workflow here and checks every `uv run tell ...`
  line against the CLI, so a renamed command or flag fails CI, not a 02:17 cron.
- `secret-scan`: gitleaks over the full history on every trigger.
- `audit`: `pip-audit` on the Monday 06:13 UTC schedule (and manual runs).
- The PII guard (no `email`, `phone`, `first_name`, `last_name` column; no `@` in a fixture
  outside a URL) runs inside the test suite.
