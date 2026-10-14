# Cutover runbook (PLAN Phase 13)

The single authoritative sequence for taking tell from a code-complete checkout to an
unattended production run. Karan executes it, step by step, in this order; each step says
what to click or type and how to know it worked. Nothing here is needed before the build
is code-complete and `make lint` / `make test` are green. Links point at the detailed
pages; this file is the order and the checklist.

Conventions: `tell-ai` is the code repo (`karank2512/tell-ai`), `tell-data` the published
data and site (`karank2512/tell-data`), `tell-private` the private repo. Local checkouts
sit side by side under `/Users/karankapur/Desktop/Tell/` (`tell`, `tell-data`,
`tell-private`). Every `uv run` from the `tell` checkout needs
`export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell` first (ADR 0011; `make` sets it).
Never paste a secret into a terminal history you keep, an issue, a commit or a log.

## 0. Decisions to confirm before anything runs

These are business rules the agents could not take (SPEC section 17 style). Confirm or
reverse each one, then edit the ADR's status line; a reversal is a code change the
agents make before step 7.

- [x] **ADR 0015, one 404 / dead-board rule** (confirmed by Karan 2026-10-06) ([docs/decisions/0015](decisions/0015-ats-404-and-reverification.md)):
  a 404 never advances `last_polled_at`; a board is `dead` after two 404s at least 12 hours
  apart; discovery re-verifies dead boards once per 30 days; a 200 revives. To confirm: no
  change. To reverse: say which of the four rules changes; it is one function in
  `tell/collectors/base.py` (`record_board_not_found` / `record_board_ok`).
- [x] **ADR 0016, leadership headline redaction** (confirmed by Karan 2026-10-06; [docs/decisions/0016](decisions/0016-news-headline-redaction.md),
  status `accepted`): `news_leadership` events store a redacted headline (`[person]`) and
  the URL stays the receipt. To confirm: set the status to `accepted`. Alternatives the ADR
  lists: store no title for leadership events, or drop the PII rule for headlines (the
  agents advise against the second).
- [ ] **Gold set** (`tell-private/gold/README.md` in the private repo,
  [docs/methods/gold-set.md](methods/gold-set.md)): 300 rows, of which 259 have
  `verification_source = unverified` (a best-effort legal form built from the name and
  country), 36 `edgar_fts`, 4 `yc`, 1 `discovery`. Spot-check per the README: every row with
  a `cik` (36), the 32 rows noted `same-name form d issuer(s) elsewhere, not linked`, the 7
  near-duplicate pairs and the 15 renamed companies. Edit cells in place, set
  `verification_source` to what you checked, keep the file sorted by `domain`. Until this is
  done the methods page quotes the numbers with the draft caveat; after it, refresh
  `tests/fixtures/gold_sample.csv` only if a sampled row changed (the sample is the same
  rows).
- [ ] **Intake slug collisions** (ADR 0027): two unrelated submitters who type the same
  company name share one watchlist, and the later submitter's Slack webhook replaces the
  earlier one's ("last non-empty wins", `tell/intake/reader.py`). Once per-watchlist
  digests are wired (they are not yet; every digest goes to `SLACK_WEBHOOK_URL`), that
  would send the first customer's account list to the second's Slack. Decide: keep the
  first webhook unless the new submitter's email is already among the record's contacts,
  or reject / queue a slug collision from a new email. Record the choice in ADR 0027.
- [ ] **SEC contact address** (SPEC section 17 default: personal email now, a role
  address when a domain exists). It goes into `SEC_USER_AGENT` in step 5.

## 1. Repositories

Install and sign in: `brew install gh && gh auth login` (GitHub.com, HTTPS, browser).

### 1a. `tell-ai` (exists)

- [ ] Public, default branch `main`, the full checkout pushed:
  `cd /Users/karankapur/Desktop/Tell/tell && git status && git push origin main`.
- [ ] Settings -> Actions -> General: "Allow all actions", workflow permissions
  "Read repository contents" (every workflow sets `permissions: contents: read` itself).
- [ ] Settings -> Branches: protect `main` (require the `ci` checks to pass). Optional but
  it is what makes "CI green on every PR" true.

### 1b. `tell-data` (exists, private, skeleton pushed)

The local checkout `/Users/karankapur/Desktop/Tell/tell-data` tracks
`https://github.com/karank2512/tell-data.git`: two commits, the skeleton (`b7ca1c5`) and
`77e4d73`, which added `.github/workflows/pages.yml` and the README's Pages paragraph; both
are pushed (`git -C ../tell-data status` is clean and up to date with `origin/main`).

- [ ] Make it **public** (free Pages needs a public repo): Settings -> General -> Danger
  Zone -> Change repository visibility -> Public. The skeleton holds no data yet, so there
  is nothing to review first; later commits are made by `tell publish` and hold no personal
  data by construction (the PII guard runs in CI).
- [ ] Pages: steps in section 2 below, after the workflow file is in place.

### 1c. `tell-private` (local repo exists; GitHub repo to confirm)

The local checkout `/Users/karankapur/Desktop/Tell/tell-private` is already a git
repository: branch `main`, one commit `e41660a` ("tell-private: gold set (300 companies),
layout, business plan"), `origin` set to `https://github.com/karank2512/tell-private.git`,
working tree clean. It tracks `README.md`, `.gitignore`, `docs/BUSINESS_PLAN.md`, `gold/`
(`companies.csv`, `README.md`) and, each held by a `.gitkeep`, the empty directories the
code fills later: `profiles/` (ICP overrides by name), `watchlists/` (`tell intake sync`
writes `<slug>.yaml`), `reports/` (`tell backtest dogfood`) and `cache/`
(`tell backtest velocity-lead --refresh` creates `cache/wayback/` under it on first use, so
no subfolder needs tracking). Do **not** run `git init` or `gh repo create --source` here
again: the first would re-initialise the repo and the second fails because the remote
`origin` already exists.

What the checkout cannot tell you is whether the GitHub repository exists and holds the
commit (the remote-tracking ref `origin/main` is present locally, which suggests a push
happened; confirm it):

```sh
cd /Users/karankapur/Desktop/Tell/tell-private
git status -sb                                    # "## main...origin/main", nothing listed
gh repo view karank2512/tell-private --json visibility,defaultBranchRef
```

- [ ] If `gh repo view` fails with "Could not resolve to a Repository": create it empty,
  then push the existing commit over the remote that is already configured:
  `gh repo create karank2512/tell-private --private` (no `--source`, no `--remote`), then
  `git -C /Users/karankapur/Desktop/Tell/tell-private push -u origin main`.
- [ ] If it exists: `git -C /Users/karankapur/Desktop/Tell/tell-private push -u origin main`
  (prints "Everything up-to-date" when `e41660a` is already there).
- [ ] `gh repo view karank2512/tell-private --json visibility` says `PRIVATE`.
- [ ] Nothing in `tell-private` is ever referenced from a public page; `tell-ai` CI does
  not check it out (see the deploy-key note in section 4).

## 2. GitHub Pages on `tell-data`

Pages cannot serve the `site/` folder directly, so it is deployed by a workflow
([docs/deploy/github-actions.md, "GitHub Pages on tell-data"](deploy/github-actions.md#github-pages-on-tell-data)).
The workflow is already in `tell-data` as `.github/workflows/pages.yml` (commit `77e4d73`,
pushed). It is the template [docs/deploy/tell-data-pages.yml](deploy/tell-data-pages.yml)
plus `with: enablement: true` on `actions/configure-pages`, which lets the first run turn
Pages on by itself; the template in this repo stays the source copy, so after any future
edit here run `diff docs/deploy/tell-data-pages.yml ../tell-data/.github/workflows/pages.yml`
and carry the change over.

- [ ] `tell-data` -> Settings -> Pages -> Build and deployment -> Source: **GitHub
  Actions** (not "Deploy from a branch"). Do this by hand even though `enablement: true`
  would do it, so the first run cannot fail on a permissions edge case.
- [ ] Actions -> `pages` -> Run workflow once by hand (no `site/**` file has been pushed
  yet, so nothing triggered it). The run's `deploy` job prints the URL
  `https://karank2512.github.io/tell-data/`. It shows the skeleton's placeholder until the
  first `ingest.yml` run publishes.

## 3. Database: Neon free project, schema, seed, discovery

- [ ] Create a Neon account (no card) and a project: name `tell`, Postgres 16 or newer,
  the region nearest the GitHub runners (US East). Keep the defaults (database `neondb`,
  role `neondb_owner`).
- [ ] Copy the connection string from the project's **Connect** panel (pooled or direct,
  either works) and rewrite it for SQLAlchemy + psycopg 3:
  `postgresql+psycopg://<role>:<password>@<host>/neondb?sslmode=require` (replace the
  leading `postgresql://` with `postgresql+psycopg://`; a `channel_binding=require`
  parameter may stay). This is `DATABASE_URL`.
- [ ] Schema, from the `tell` checkout, with the production URL in the environment of this
  shell only:

  ```sh
  cd /Users/karankapur/Desktop/Tell/tell
  export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell
  export DATABASE_URL='postgresql+psycopg://...'   # the Neon string; do not put it in .env
  uv run alembic upgrade head
  uv run alembic current        # prints 0007 (head)
  ```

- [ ] Seed the YC directory (ADR 0009; one request to the public mirror):
  `uv run tell seed-yc --fetch`. The JSON line reports the companies inserted (about
  4,000 domains).
- [ ] Supervised discovery, 300 domains per run at 4 requests per second
  (`uv run tell discover batch --limit 300 --rps 4`, 10 to 15 minutes each). Run one now
  and read its line (boards found, `no_board`, `unsupported`); then either repeat it a few
  times a day over the first week, or let `ingest.yml` do it: its `discover` job runs the
  same command on every scheduled run (once a day while `tell-ai` is private, four times a
  day once `TELL_RUNS_PER_DAY` is `4`; ADR 0029), so the directory is covered within about
  two weeks (a few days at four runs) without any hand runs. Discovery writes only `companies` and
  `ats_boards`; nothing is polled yet.
- [ ] Neon dashboard: compute hours stay far under the 100 CU-hours a month (SPEC
  section 14 estimates about 60); the project scales to zero between runs.

## 4. Every Actions secret and variable

Where each one goes and where its value comes from. Set them with the UI (repo ->
Settings -> Secrets and variables -> Actions -> New repository secret / variable) or with
`gh secret set NAME --repo karank2512/tell-ai` (prompts for the value) and
`gh variable set NAME --body VALUE --repo karank2512/tell-ai`. Reference:
[docs/deploy/github-actions.md, "Secrets and variables"](deploy/github-actions.md#secrets-and-variables-code-repo).

### 4a. `tell-ai` secrets

| Secret | Used by | Where it comes from |
| --- | --- | --- |
| `DATABASE_URL` | `ingest.yml`, `weekly.yml` | Step 3 (the `postgresql+psycopg://...?sslmode=require` string) |
| `SEC_USER_AGENT` | `ingest.yml`, `weekly.yml` | Step 5: `tell/0.1 (<real contact email>)` |
| `TELL_DATA_PUSH_TOKEN` | `ingest.yml`, `weekly.yml` (checkout and push of `tell-data`) | Fine-grained personal access token: github.com -> Settings -> Developer settings -> Personal access tokens -> Fine-grained -> Generate. Resource owner `karank2512`, repository access **Only select repositories: `tell-data`**, Repository permissions **Contents: Read and write** (Metadata read-only is added automatically), nothing else. Set an expiry (one year) and a calendar reminder to rotate it. A PAT push triggers the Pages workflow; a `GITHUB_TOKEN` push would not |
| `SLACK_WEBHOOK_URL` | `ingest.yml` (`tell notify --slack`) | Step 6, the digest channel's incoming webhook |
| `SLACK_WEEKLY_WEBHOOK_URL` | `weekly.yml` (optional; the step is skipped when unset) | Step 6, the private channel's incoming webhook |

`GITHUB_TOKEN` is provided by Actions; nothing to set.

Until `DATABASE_URL` is set here, every scheduled and manual `ingest.yml` and `weekly.yml`
run ends green after its `preflight` job, with the summary line `ingest skipped:
DATABASE_URL secret not set; see docs/CUTOVER_CHECKLIST.md` (or `weekly skipped: ...`)
and every other job skipped (the weekly keepalive still runs). That is expected, not a
failure. Nothing needs re-enabling afterwards: the first run after the secrets exist goes
live on its own. If `DATABASE_URL` is set but `TELL_DATA_PUSH_TOKEN` or `TELL_DATA_REPO` is
not, the runs collect and score but skip every `tell-data` step with a warning, so set all
three together ([preflight guard](deploy/github-actions.md#secrets-and-variables-code-repo)).

### 4b. `tell-ai` variables

| Variable | Required | Value |
| --- | --- | --- |
| `TELL_DATA_REPO` | yes | `karank2512/tell-data` |
| `TELL_BASE_URL` | no | leave unset: defaults to `https://karank2512.github.io/tell-data` |
| `TELL_RUNS_PER_DAY` | no | leave unset while `tell-ai` is private (one run a day fits the free minutes); set to `4` once the repository is public (ADR 0029) |
| `TELL_FEEDBACK_FORM_URL` | no | Prefilled Google Form link for thumbs and `rate_this_row` (`{signal_id}`, `{domain}`, `{verdict}` placeholders); without it, feedback links open a prefilled GitHub issue |
| `TELL_INTAKE_FORM_URL` | no | The "Watch these domains" Google Form link ([docs/intake/google-form.md](intake/google-form.md)); without it, the digest footer links the repo's issue form |

### 4c. `tell-ai` environment

- [ ] Settings -> Environments -> New environment `pypi`. Optional: add yourself as a
  required reviewer and restrict deployment branches/tags to `tell-mcp-v*`. The
  `publish-mcp.yml` publish job runs in it (step 7 of this list, PyPI).

### 4d. `tell-private` secrets (for the nightly intake job, section 6b)

| Secret | Value |
| --- | --- |
| `DATABASE_URL` | the same Neon string |
| `TELL_INTAKE_CSV_URL` | the published-as-CSV link of the intake form's response sheet ([docs/intake/google-form.md, "Publishing the responses as CSV"](intake/google-form.md#publishing-the-responses-as-csv)); a secret, anyone with it can read every response |

### 4e. `tell-data`

Nothing: `pages.yml` uses the job's own `GITHUB_TOKEN` (`pages: write`, `id-token: write`).

### 4f. The `tell-private` deploy key (not wired yet)

PLAN Phase 13 step 3 mentions a read-only deploy key for `tell-private` so that CI on `main`
can run the resolution test against the full 300-row gold set (`TELL_GOLD_SET`,
`tests/test_resolve.py`). **No workflow references such a key today**; CI runs the public
50-row sample, and the full set is run locally
(`TELL_GOLD_SET=../tell-private/gold/companies.csv make test`). Create the key only when
`ci.yml` gains a guarded `actions/checkout` of `tell-private` (`ssh-key: ${{ secrets.TELL_PRIVATE_DEPLOY_KEY }}`,
skipped on fork pull requests, which have no secrets): `ssh-keygen -t ed25519 -f tell-private-ci -N ""`,
public half under `tell-private` -> Settings -> Deploy keys (read-only), private half as the
`tell-ai` secret `TELL_PRIVATE_DEPLOY_KEY`. Until then: nothing to do.

### 4g. Local `.env`

Nothing production goes into the checkout's `.env`; the dev database stays
`postgresql+psycopg://karankapur@localhost:5432/tell_dev`. Every variable above is listed
with a comment in [`.env.example`](../.env.example).

## 5. SEC contact and the EDGAR fixtures

- [ ] Set `SEC_USER_AGENT` to `tell/0.1 (<contact email>)` with the address decided in
  section 0 (the SEC requires a real contact; the default in `tell/config.py` is a
  placeholder that EDGAR refuses). Store it as the `tell-ai` secret (section 4a) and export
  it in the shell for the next step.
- [ ] Re-record the hand-crafted EDGAR fixtures. The fixtures under `tests/fixtures/edgar/`
  were written by hand because the live request without a contact address was refused
  ([docs/sources/edgar.md, "Fixtures"](sources/edgar.md#fixtures-testsfixturesedgar)). There is
  **no automated recorder** (`make refresh-fixtures` prints a pointer to this step), so the
  procedure is manual and supervised, at or below 5 requests a second:

  1. `export SEC_USER_AGENT='tell/0.1 (<contact email>)'`.
  2. Fetch yesterday's daily index with that User-Agent:
     `curl -sS -A "$SEC_USER_AGENT" https://www.sec.gov/Archives/edgar/daily-index/form.$(date -u -v-1d +%Y%m%d).idx -o /tmp/form.idx`
     (`-v-1d` is BSD `date`); a 403 means the User-Agent is still wrong.
  3. Pick from it two Form D filings (one with `Indefinite` amounts if possible), one D/A,
     one 8-K and one S-1 and fetch each `primary_doc.xml` / index page from the accession
     folder, one request a second.
  4. Trim each file to the shape the current fixture has (same elements, under 50 KB) and
     **scrub**: remove the Form D `relatedPersonsList` block entirely (names of officers and
     directors), every address line, phone and email. The PII guard
     (`tests/test_pii_guard.py`) and the collectors' key filter will refuse anything missed.
  5. Replace `form.YYYYMMDD.idx`, `formd_1.xml`, `formd_2.xml`, `formd_amendment.xml`,
     regenerate `drift.xml` (the `formd_1` without `totalOfferingAmount`) and the JSON
     renderings (`normal.json`, `amendment.json`, `empty.json`, `drift.json`) the way
     `docs/sources/edgar.md` describes; update `submissions_*.json` only if the shape moved.
  6. `git diff --stat tests/fixtures/edgar` and read every hunk; then
     `make test` (contract, idempotency and PII tests) must stay green. A field that moved
     is a contract change: update the pydantic model and say so in the PR, never patch
     around it.
- [ ] The other sources (Greenhouse, Lever, Ashby, SmartRecruiters, Google News, GitHub, YC)
  were recorded live and need nothing.

## 6. Slack

### 6a. Incoming webhooks

- [ ] In the Slack workspace: api.slack.com/apps -> Create New App -> From scratch, name
  `tell`, pick the workspace. Features -> Incoming Webhooks -> On -> Add New Webhook to
  Workspace -> pick the **digest channel** -> copy the `https://hooks.slack.com/services/...`
  URL -> `tell-ai` secret `SLACK_WEBHOOK_URL`.
- [ ] Repeat "Add New Webhook to Workspace" for the **private weekly channel** ->
  `SLACK_WEEKLY_WEBHOOK_URL` (optional).
- [ ] Digest timing: the first `ingest.yml` run at or after 07:00 `America/Los_Angeles`
  sends it (`TELL_SLACK_DIGEST_HOUR` = 7, `TELL_SLACK_TZ`). The daily slot is 15:17 UTC
  (07:17 PST, 08:17 PDT; ADR 0029), so the digest goes out on that run all year. Nothing to set for the default. A different hour or
  zone is a code change, not a repository variable: no workflow maps `vars.TELL_SLACK_*`
  into the job environment, so add `TELL_SLACK_DIGEST_HOUR: "<hour>"` (and, if needed,
  `TELL_SLACK_TZ`) to the top-level `env:` block of `.github/workflows/ingest.yml` in a PR.
- [ ] Test the webhook without waiting:
  `SLACK_WEBHOOK_URL='https://hooks.slack.com/...' uv run tell notify --slack --force`
  from the checkout against the production `DATABASE_URL` after the first ingest (step 7),
  or `curl -X POST -H 'Content-type: application/json' --data '{"text":"tell webhook test"}' "$SLACK_WEBHOOK_URL"`.

### 6b. Intake form and the nightly job in `tell-private` (when the first design partner asks)

- [ ] Build the Google Form per [docs/intake/google-form.md](intake/google-form.md), publish
  its response sheet as CSV, store the link as the `tell-private` secret `TELL_INTAKE_CSV_URL`
  (section 4d), set `TELL_INTAKE_FORM_URL` on `tell-ai` (section 4b).
- [ ] `cp /Users/karankapur/Desktop/Tell/tell/docs/deploy/tell-private-intake.yml /Users/karankapur/Desktop/Tell/tell-private/.github/workflows/intake.yml`,
  commit, push. It runs nightly at 03:41 UTC and on Run workflow; it needs `tell-ai` public
  (section 1a) to check the code out.

## 7. PyPI: `tell-mcp` by trusted publishing

Reference: [docs/deploy/github-actions.md, "Publishing tell-mcp to PyPI"](deploy/github-actions.md#publishing-tell-mcp-to-pypi); ADR 0010.

- [ ] Create a PyPI account with two-factor authentication. Check that the name `tell-mcp`
  is free (`https://pypi.org/project/tell-mcp/` is a 404).
- [ ] PyPI -> Your account -> Publishing -> **Add a new pending publisher**: PyPI project
  name `tell-mcp`, owner `karank2512`, repository `tell-ai`, workflow name `publish-mcp.yml`,
  environment name `pypi`. (The `pypi` environment on `tell-ai` is section 4c.)
- [ ] `packages/tell-mcp/pyproject.toml` says `version = "0.1.0"`; the workflow refuses a
  tag that does not match it. Tag and push from `main`:

  ```sh
  cd /Users/karankapur/Desktop/Tell/tell
  git tag tell-mcp-v0.1.0 && git push origin tell-mcp-v0.1.0
  ```

- [ ] Actions -> `publish-mcp`: `build` green, `publish` (environment `pypi`) green; the
  first successful run turns the pending publisher into the project's trusted publisher.
  `https://pypi.org/project/tell-mcp/0.1.0/` exists.
- [ ] `uvx tell-mcp --help` works on this machine; the README quickstart's Claude Desktop
  snippet now resolves.

## 8. First supervised `ingest.yml` runs and the hand-checks

Run one source at a time from the Actions page (Actions -> ingest -> Run workflow ->
**source**) or:

```sh
gh workflow run ingest.yml -R karank2512/tell-ai -f source=discover
gh workflow run ingest.yml -R karank2512/tell-ai -f source=greenhouse   # then lever, ashby, smartrecruiters
gh workflow run ingest.yml -R karank2512/tell-ai -f source=edgar
gh workflow run ingest.yml -R karank2512/tell-ai -f source=news
gh run watch -R karank2512/tell-ai
```

Each dispatched run is marked `runs.notes.manual = true` (`TELL_RUN_MANUAL` is `true` on
`workflow_dispatch`), which is correct for this supervised phase: "weeks unattended" starts
counting from the last of them. After every per-source run the `resolve_score_publish` job
runs too, pushes `tell-data`, and the Pages workflow deploys it.

- [ ] Each run: the step summary shows the run line (`status: ok`), `tell-data` has a new
  commit by `tell-bot`, `https://karank2512.github.io/tell-data/health.html` lists the source
  green. The dead-man step is **expected to fail** until every scheduled source has one
  successful run (a never-run scheduled source is silent; ADR 0028); after the sixth source
  it must be green.
- [ ] Then one full run with **source** empty: `gh workflow run ingest.yml -R karank2512/tell-ai`.
  Every job green, dead-man green, the site and `api/v0/health.json` updated, no Slack
  digest unless the run fell at or after the digest hour.
- [ ] Leave the schedule alone from here. Fix anything needed and re-run only that source
  (that is what the `manual` marker is for).

### SPEC section 16 hand-checks: M1 (collectors and graph)

- [ ] `tell ingest --source greenhouse --board <token>` twice for three boards you know
  (against production `DATABASE_URL`, with `--manual --force` on the second); the second
  run reports `inserted: 0`.
- [ ] Open 10 random `postings` rows and their URLs; title, location and posted date match
  the live page.
- [ ] Pick 10 companies from your own target list; `tell discover domain --domain <d>`
  finds the board for at least 7, and the misses are logged with the pages checked.
- [ ] `tests/fixtures/*/drift.json` exists for every vendor and CI shows the contract test
  failing on it (run `uv run pytest tests/test_contracts.py -q` and read the drift cases).
- [ ] No column, fixture or log line contains a person's name or email
  (`uv run pytest tests/test_pii_guard.py -q`; read one Actions log end to end).

### SPEC section 16 hand-checks: M2 (filings, news, scoring, digest)

- [ ] Take yesterday's Form D daily index; for a sample of 10 filings by monitored
  companies, count by hand which the linker linked (`events.company_id` set,
  `payload.match` rule); at least 9 agree and the tenth is in `review_queue`, not silently
  wrong.
- [ ] Open 10 `form_d_filed` events; each `url` opens the filing folder on sec.gov and the
  amount in `payload` matches `primary_doc.xml`.
- [ ] For 5 companies, add the contributions on the account page with a calculator; they
  equal the score.
- [ ] Receive the Slack digest two mornings in a row without touching anything (this is the
  first soak gate, section 9).
- [ ] Your own `karan-targets` watchlist surfaces at least one posting you had not seen.
  No CLI creates a watchlist (they come from the intake form or the undeployed v1 API), so
  create it once with SQL against the production database (`psql "$DATABASE_URL"` after
  turning the `postgresql+psycopg://` prefix back into `postgresql://`), owner being any
  handle, profile `job-seeker` (SPEC 13.5), private:

  ```sql
  insert into watchlists (owner, name, icp_profile, slug, is_public)
  values ('karan', 'karan-targets', 'job-seeker', 'karan-targets', false);
  insert into watchlist_items (watchlist_id, company_id)
  select w.id, c.id from watchlists w, companies c
  where w.name = 'karan-targets' and c.domain in ('example.com', 'example.org');  -- your targets
  ```

  Then `uv run tell publish csv --watchlist karan-targets --out /tmp/karan-targets.csv`
  is the list with receipts, and `tell backtest dogfood` reads the same watchlist.
- [ ] Report 13.1 exists with N, the query and caveats: `tell-data/site/methods/speed-vs-press-<date>.md`
  after the first Monday run, or now with `uv run tell backtest speed-vs-press --out /tmp/methods`.

## 9. The soak: what "unattended" means afterwards

After section 8 nothing is touched unless a run turns red. GitHub's failure email on a
scheduled run is the only alert. The M2 to M4 sign-offs are calendar-bound; tick each when
its condition has held, and quote numbers only from dated reports in
`tell-data/site/methods/`.

- [ ] **M2**: the digest arrived two consecutive mornings with no manual run in between;
  report 13.1 published with N >= 100 or an explicit "sample too small".
- [ ] **M3** (platform): `api/v0/companies/<domain>.json` opens in a browser, every
  contribution has a URL, the JSON validates in CI; `tell-mcp` in Claude Desktop (section 7)
  answers "which of these ten domains are in market and why?" with citing URLs; a Clay table
  of 20 domains with the HTTP column fills with score and top signal
  ([docs/api/README.md](api/README.md)); the dead-man turns a run red when a source is
  silent (there is no pause flag; the equivalent check is `uv run tell deadman --source github`
  exiting 1 for the never-scheduled collector, and ADR 0028 explains the rule); five design
  partners have watchlists in `tell-private` and have received a CSV.
- [ ] **M4** (launch): reports 13.1, 13.3, 13.4 published and dated, the README quotes them
  with those dates; the README quickstart works on a clean machine in under 5 minutes
  (`git clone`, `uv sync`, `make migrate`, `discover domain`, `ingest`); the weekly page
  renders for the current week with real, correctly linked top-25 companies; every number
  in the LinkedIn post and Show HN draft traces to a report line
  (`docs/launch/numbers.yaml`, checked by `tests/backtest/test_launch_numbers.py`); legal
  notes per source are in `docs/sources/` and linked from the README.
- [ ] **Weeks unattended** is quoted from `tell backtest ops` (the Monday report) and counts
  from the last run marked manual. During the soak: dispatch `ingest.yml` by hand only when
  a fix was needed to keep ingest green; run terminal fixes with `tell --manual ...`; never
  set `TELL_RUN_MANUAL` on a schedule ([docs/methods/README.md](methods/README.md#manual-interventions-and-weeks-unattended),
  ADR 0028).
- [ ] Standing care: rotate `TELL_DATA_PUSH_TOKEN` before it expires; watch the Neon
  compute-hours graph once a month; if GitHub ever emails a "scheduled workflow will be
  disabled" warning, `gh workflow enable ingest.yml -R karank2512/tell-ai` (the weekly
  `keepalive` job should make this unnecessary, ADR 0026).

## Known follow-ups, not blocking cutover

- Per-watchlist Slack routing (`tell.intake.load_private_watchlists` -> each record's
  webhook) is documented but not wired in `tell/publish/slack.py`; design partners who give
  a webhook receive nothing there until it is. Decide the slug-collision rule (section 0)
  first.
- The v1 FastAPI service is coded and not deployed ([docs/deploy/render.md](deploy/render.md));
  deploy it only when a consumer needs query parameters the static JSON cannot answer
  (ADR 0008).
- `tell backtest velocity-lead --refresh` (Wayback reconstruction, 13.2) is run locally by a
  maintainer and its cache committed to `tell-private/cache/wayback`; the weekly job reads
  the cache only.
