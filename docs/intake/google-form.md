# Intake form (Google Form) and the nightly reader

Spec section 15.6; PLAN Phase 12; privacy rule in
[ADR 0027](../decisions/0027-intake-contact-storage.md). The form is how a design partner
(or a job seeker) asks tell to watch a list of companies. It is also the target of the
Slack digest footer line "Reply with a domain to add it" (spec section 9,
`TELL_INTAKE_FORM_URL`): a second submission for the same company and profile adds its
domains to the existing watchlist.

## Form fields

Create the form with these questions, in this order, with these exact titles. The titles
become the response sheet's column headers, which the reader matches (case and
punctuation are ignored; see "Column mapping").

| # | Question title | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| 1 | `Name` | Short answer | Yes | Stored only in `tell-private`. |
| 2 | `Email` | Short answer, validation "Text: email" | Yes | Stored only in `tell-private`; the database gets a random owner handle (`intake-` + 16 hex characters) minted for the watchlist, never anything derived from the address. Do not turn on the form's "Collect email addresses" setting as well (it adds a second `Email Address` column). |
| 3 | `Company` | Short answer | Yes | The company you work for, or, for a job seeker, a name for the list (for example `job search`). Add this as the question's help text: "Job seekers: enter a list name such as 'job search', not your own name." The value names the watchlist in the database (`acme-default`, `job-search-job-seeker`), so it must never be a person's name. |
| 4 | `Role` | Short answer | No | Stored only in `tell-private`. |
| 5 | `Domains (up to 50, one per line)` | Paragraph | Yes | One domain per line; URLs are fine (`https://www.acme.com/careers` becomes `acme.com`). Commas and spaces also separate. More than 50 distinct domains rejects the submission. |
| 6 | `Who do you sell to?` | Dropdown | Yes, unless 8 is ticked | Each option is an ICP profile name, or a label ending in the name in brackets. Today: `Sales teams (default)`. Add an option whenever a profile is added to `tell/profiles/` or `tell-private/profiles/`; an option that names no profile rejects the row. |
| 7 | `Slack webhook URL (optional)` | Short answer, response validation "Regular expression, Matches" `^https://hooks\.slack\.com/.*` | No | A Slack incoming webhook for this watchlist's daily digest. Anything not starting with `https://hooks.slack.com/` rejects the row. Stored only in `tell-private`. |
| 8 | `I'm a job seeker` | Checkboxes, one option `Yes` | No | Ticked selects the `job-seeker` profile whatever question 6 says. |

Form settings: do not collect email addresses automatically, do not allow response
editing (an edited response gets a new timestamp and so counts as a new submission,
which is harmless but noisy), and add this line to the description: "We store your
contact details privately to send you your list; tell's public data never includes
them."

## Publishing the responses as CSV

1. In the form's Responses tab, link a Google Sheet.
2. In the sheet: File, Share, Publish to web; choose the responses tab and
   "Comma-separated values (.csv)"; publish. The URL looks like
   `https://docs.google.com/spreadsheets/d/e/<id>/pub?gid=<gid>&single=true&output=csv`.
3. Treat that URL as a secret: anyone with it can read every response. Store it as the
   GitHub Actions secret `TELL_INTAKE_CSV_URL`; never paste it into an issue, a log or
   a committed file. If it leaks, unpublish and republish (the id changes).

## Column mapping

The first row of the published CSV is the header. The reader maps headers to fields
after lowercasing and turning punctuation into spaces (`I'm a job seeker` -> `i m a job
seeker`):

| Field | Accepted headers | Required column |
| --- | --- | --- |
| `timestamp` | `Timestamp` (added by Google Forms) | Yes |
| `name` | `Name`, `Your name` | No |
| `email` | `Email`, `Email address`, `Your email` | Yes |
| `company` | `Company`, `Your company` | Yes |
| `role` | `Role`, `Your role` | No |
| `domains` | `Domains (up to 50, one per line)`, `Up to 50 domains (one per line)`, `Domains` | Yes |
| `profile` | `Who do you sell to?`, `Who you sell to`, `ICP profile`, `Profile` | Yes |
| `webhook` | `Slack webhook URL (optional)`, `Slack webhook URL`, `Slack webhook` | No |
| `job_seeker` | `I'm a job seeker`, `Job seeker` | No |

A missing required column fails the whole sync (exit 1, `runs.status='failed'`): the form
was edited and the reader must follow. A missing optional column reads as empty.

## What the reader does with a row

Validation (a failing row is listed as `{"row": N, "reason": "..."}` and nothing of it
is stored; `N` is the sheet row, the header being row 1):

- `missing email` / `invalid email` / `missing company`;
- `no valid domains` / `too many domains (N > 50)` (domains are registrable domains via
  `tldextract`, deduplicated; lines that are not domains are dropped and counted in
  `rejected_domains`);
- `unknown profile` (checked against `tell.signals.profiles.load_profiles()`, shipped
  plus `tell-private/profiles/`);
- `invalid slack webhook url`;
- `slug taken`: a watchlist with the slug `slug(company)-slug(profile)` already exists and
  intake did not make it (it is public, or its owner is not an `intake-` handle, or the
  private record carries a different handle). Intake never adds domains to a hand-made or
  published list (ADR 0022); a person renames one of the two.

A valid row is a submission whose id is `sha256(timestamp | email | company)`. For each
submission not yet applied:

1. the watchlist `slug(company)-slug(profile)` is created (owner = a fresh random handle,
   or the handle in the private record when the database lost the list; `icp_profile` =
   the profile, `is_public = false`) or, when intake made it, reused;
2. each domain without a `companies` row gets one (`name` = the domain label), so the
   next `tell discover batch` looks for its job board;
3. missing `watchlist_items` are added (never removed);
4. after the commit, `tell-private/watchlists/<slug>.yaml` is written with the contacts,
   the webhook, the domains and the applied submission ids.

Re-running the sync over the same sheet applies nothing and writes nothing.

## Running it

```sh
tell intake sync --csv-url "$TELL_INTAKE_CSV_URL" --private-dir ../tell-private
tell intake sync --csv-file responses.csv            # a downloaded copy; same result
```

`--csv-url` defaults to `TELL_INTAKE_CSV_URL`; `--private-dir` defaults to
`../tell-private` relative to the working directory (the private checkout next to the code
checkout) and must not be inside the `tell` checkout. The command prints one JSON line of
counts (`rows`, `applied`, `skipped`, `errors`, `watchlists_created`, `companies_created`,
`items_added`, `rejected_domains`, `rejected_rows`, `private_files_written`) and nothing
personal. A private watchlist file that does not parse or validate exits 1 with only the
error type (`invalid private watchlist file (ValueError)`): the message could quote the
file and the file name is a customer's slug. A `--csv-file` that is not UTF-8 exits 1 with
`intake CSV is not UTF-8` (Google's CSV export is UTF-8; re-download rather than re-save
from a spreadsheet application).

## The nightly job

[`docs/deploy/tell-private-intake.yml`](../deploy/tell-private-intake.yml) is the
workflow. It runs in `tell-private`, the only repo with customer input, so its logs and
its commits are private; nothing about it touches `tell-ai` or `tell-data`. Cutover step:

1. Copy the file to `tell-private/.github/workflows/intake.yml`.
2. In `tell-private`: Settings -> Secrets and variables -> Actions, add `DATABASE_URL`
   (the same Neon string the code repo uses) and `TELL_INTAKE_CSV_URL` (the published-sheet
   link from above).
3. The job runs nightly at 03:41 UTC (and on **Run workflow**): it checks out
   `tell-private` and the code, runs `tell intake sync --private-dir ../tell-private`, and
   commits the changed `watchlists/*.yaml` back as `tell-bot`. Exit 1 (sheet unreadable,
   columns drifted, a bad private file) fails the run, and GitHub's failure email is the
   alert.

`tell-ai` is public after cutover, so the code checkout needs no token; until then the
job cannot run, which is fine because there is no sheet to read yet either.

## Slack digest hook

`tell.intake.load_private_watchlists(private_dir)` returns `{slug: PrivateWatchlist}`.
To send a watchlist's digest to its own webhook, the digest job looks up
`records.get(watchlist.slug)` and posts to `record.slack_webhook_url` when it is set,
falling back to `SLACK_WEBHOOK_URL` otherwise. `tell/publish/slack.py` does not do this
yet; wiring it is a separate change.
