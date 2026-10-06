# 0027. Intake contact details live only in `tell-private`; the database keeps a random handle

- Status: accepted
- Date: 2026-10-06

## Context

SPEC section 15.6 defines the intake Google Form (name, email, company, role, up to 50
domains, "who you sell to", optional Slack webhook URL, "I'm a job seeker") and says the
nightly job "reads the published-as-CSV sheet and creates watchlists in `tell-private`".
Section 1 says `tell-private` is "the only repo with customer input" and holds the Slack
webhook config; section 5 gives `watchlists.owner` as free text ("an email or handle"),
and the project rule is that no column, log line or payload holds a person's name, email
or phone. Watchlists themselves must live in the database, because scoring, heat and the
Slack digest read `watchlists` / `watchlist_items`.

## Decision

The intake reader (`tell/intake/reader.py`, `tell intake sync`) splits every submission:

- **Database (company-level only).** `watchlists.owner` is a random per-watchlist handle:
  `intake-` followed by 16 hex characters from `secrets.token_hex`, minted once when the
  watchlist is created, written to the private record and copied back from that record
  (or from the existing database row) on every later sync. It is never derived from the
  email or any other field, so nothing in the database points at a person, two submitters
  can never share a handle, and the prefix lets the reader tell an intake list from a
  hand-made one with the same slug. `watchlists.name` and
  `watchlists.slug` are both `slug(company) + "-" + slug(profile)` (for example
  `acme-default`); `is_public` stays `false`, so ADR 0022 never publishes an intake list
  without a deliberate change. `watchlist_items` get the submitted domains; a domain
  with no `companies` row gets one with `name` = its domain label, so
  `tell discover batch` picks it up. Items are only ever added, never removed.
- **`tell-private/watchlists/<slug>.yaml` (private repo only).** The company as typed,
  the contacts (name, email, role, form timestamp), the Slack webhook URL (last non-empty
  one wins), the union of submitted domains, the profile, the job-seeker flag, the owner
  handle and the list of applied submission ids. Nothing else stores the raw email, the
  name, the role or the webhook. `tell.intake.load_private_watchlists(private_dir)` is
  the read side; the Slack digest is expected to use it to post a watchlist's digest to
  its own webhook (`tell/publish/slack.py` is not changed by this ADR).
- **Logs and runs.** The CLI's JSON line, the `runs` row (`source='intake'`, not a health
  source) and its `notes` carry counts plus rejected row numbers and reasons, never a
  field value, a slug or the sheet URL, because Actions logs of the public repo are public.
- **Idempotency.** A submission's id is `sha256(timestamp | lowercased email | company)`,
  stored only in the private file. A submission already recorded there whose watchlist
  exists in the database is skipped. The private files are written after the database
  commit; if that write fails, the next sync re-applies the submission (no duplicate rows,
  since watchlists are found by slug and items by primary key) and writes the file then.
- **Guard.** The reader refuses a private directory inside the public `tell` checkout.
- **Slug taken.** Intake only ever adds to a watchlist it made. When the slug
  `slug(company)-slug(profile)` already belongs to a watchlist that is public, whose
  `owner` is not an `intake-` handle (hand-made), or whose private record carries a
  different handle (files and database disagree), the row is rejected as `slug taken` and
  nothing is written; a person renames one of the two. A watchlist with an `intake-` owner
  and no private record (the write after the commit was lost) is reused and its record is
  rewritten.

Interpretations made while implementing (flag if wrong):

- The "I'm a job seeker" checkbox selects the `job-seeker` profile whatever the dropdown
  says. The dropdown value must be a profile name, or a label ending in the name in
  brackets (`Sales teams (default)`); unknown profiles reject the row.
- More than 50 distinct valid domains rejects the whole row (no silent truncation); lines
  that are not domains are dropped and counted.
- A webhook that does not start with `https://hooks.slack.com/` rejects the row.
- One watchlist per company and profile: two people from the same company with the same
  profile share a list; the first submitter's handle stays the owner.

## Consequences

- No schema change: `owner`, `name`, `slug` and `is_public` already exist (0001, 0006).
- The handle carries no information: it cannot be guessed from an address or shared by
  two submitters, and the only join back to a person is the private record in
  `tell-private`. Losing that record loses the contact, not the watchlist; the next sync
  recreates the record from the sheet and keeps the handle already on the row. A hash of
  the email was considered and rejected because a common local part (`sales`) is
  guessable by hashing candidates and the v1 API grants private-watchlist access by owner
  equality.
- The published-as-CSV sheet holds the raw contacts and is readable by anyone with the
  link; the URL is a secret (`TELL_INTAKE_CSV_URL`, a GitHub Actions secret), never
  logged, and the docs recommend the job run where logs are private.
- A company name typed into the form becomes part of a database slug; it is a company,
  not a person, and the list stays private unless someone sets `is_public`. The form
  therefore tells job seekers to enter a list name (`job search`) in the `Company`
  question, never their own name (`docs/intake/google-form.md`).
