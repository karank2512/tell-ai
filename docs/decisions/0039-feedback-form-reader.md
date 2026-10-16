# 0039. Feedback labels through the Google Form: a nightly reader and `feedback.submission_id`

- Status: proposed
- Date: 2026-10-16

## Context

- **Zero labels.** SPEC section 8's feedback loop (precision by signal type, re-weighting
  at 300 labels per profile) has the table, the report and `POST /v1/feedback`, and no
  labels, because the API is not deployed (ADR 0008 waits for a consumer) and the form
  links on the account page, in the CSV and now on every nudge (ADR 0036) point at a
  Google Form nobody reads back.
- **The nudge is where the click happens.** The 2026-10-15 research made the acted-on rate
  over at least 300 labels the metric that decides what to build next, and put "one-click
  feedback from the nudge" in the week-6 list with "v1 deployed or form" as the dependency.
  The form path needs no deployment and no key.

## Decision

1. **A feedback Google Form with five questions** (`docs/intake/feedback-form.md`): `Signal
   id`, `Domain`, `Verdict` (the six SPEC verdicts as a dropdown), `Watchlist`, `Note`. No
   name, no email, no address collection: a rating is anonymous. `TELL_FEEDBACK_FORM_URL`
   gains a fourth placeholder, `{watchlist}`, which only the nudge fills (the account page
   and the CSV do not know the list).
2. **`tell intake feedback-sync`** reads the sheet published as CSV
   (`TELL_FEEDBACK_CSV_URL`, a `tell-private` secret like the intake sheet's) and writes one
   `feedback` row per response: the signal must exist, the verdict must be one of
   `FEEDBACK_VERDICTS`, a given domain must be the signal's company, and the row lands on
   the named watchlist (which must hold the company) or else on the lowest-id watchlist
   that holds it, the same rule as the API. `created_at` is the sync time.
3. **`feedback.submission_id` (migration 0012)**, SHA-256 of `timestamp | signal_id |
   verdict | watchlist`, unique and nullable: the sheet is re-read nightly and must apply
   nothing twice; API rows keep `NULL`. This is a column SPEC section 3's `feedback` table
   does not have, recorded here as the deviation.
4. **Where it runs.** A step in the `tell-private` intake workflow after the intake sync,
   skipped when the secret is unset, recorded as `runs.source = feedback_sync`, a
   non-pipeline source like `intake` (it never counts toward the ingest success rate).

Interpretations made while implementing (flag if wrong):

- A rating without a watchlist lands on the lowest-id watchlist holding the company, as
  the API does, rather than being rejected: the account page's thumbs row knows no list,
  and a label on the wrong list of the same owner is still a label on the signal.
- A `Domain` that does not match the signal's company rejects the row instead of trusting
  the id: the id is prefilled, the domain is the human-readable check.
- The form's timestamp is not parsed (sheet locale); it only feeds the submission id.

## Consequences

- Every rating link, from the nudge in particular, now ends in a `feedback` row the next
  night, so the precision report and the acted-on rate have data without a deployed API.
- Two operator steps: create the form and set `TELL_FEEDBACK_FORM_URL` on `tell-ai`
  (documented before, now with `{watchlist}`), and set `TELL_FEEDBACK_CSV_URL` on
  `tell-private` plus copy the updated workflow file. Migration 0012 on production first.
- When the v1 API is deployed, the same `feedback` rows come from `POST /v1/feedback`; the
  reader stays for people without a key. A true one-click path (a signed `GET` that records
  the verdict) is the API's, not the form's.
