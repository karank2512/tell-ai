# Feedback form (Google Form) and the nightly reader

ADR 0039; SPEC section 8 (the feedback loop) and section 9 (the thumbs row). Until the v1
API is deployed, a rating is a click on a prefilled Google Form link: from a Slack nudge
(`useful` / `not useful`), from the account page's thumbs row, or from the CSV's
`rate_this_row`. `tell intake feedback-sync` reads the form's response sheet nightly and
writes one `feedback` row per response, which is what the precision report
(`tell/signals/feedback.py`) and, at 300 labels per profile, the re-weighting need.

## Form fields

Create the form with these questions, in this order, with these exact titles. The titles
become the response sheet's column headers, which the reader matches (case and
punctuation ignored).

| # | Question title | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| 1 | `Signal id` | Short answer, response validation Number, Greater than 0 | Yes | Prefilled by every link; the stored `signals.id`. A row whose id is not a stored signal is rejected. |
| 2 | `Domain` | Short answer | No | Prefilled. When present it must be the signal's company, so a pasted id cannot land on the wrong account. |
| 3 | `Verdict` | Dropdown with exactly these options: `useful`, `not_useful`, `already_knew`, `worked`, `booked`, `won` | Yes | Prefilled by the link (`useful` or `not_useful`); the person can change it to `already_knew`, or come back later and pick `worked` / `booked` / `won`. |
| 4 | `Watchlist` | Short answer | No | Prefilled by nudge links with the watchlist slug; the account page and the CSV leave it empty. The reader then uses the lowest-id watchlist holding the company, the same rule as `POST /v1/feedback`. |
| 5 | `Note` | Paragraph | No | Free text, cut at 1,000 characters. Ask people not to write names or emails here; the note is stored in the database. |

Form settings: **do not collect email addresses** (the form must stay anonymous; an
`Email Address` column, if present, is ignored but should not exist), do not limit to one
response, do not allow response editing. Description: "One click per signal. tell stores
the verdict and your note against the signal; nothing identifies you."

## The prefilled link

1. In the form editor: the three-dot menu, **Get pre-filled link**.
2. Fill the questions with these literal values: Signal id `1`, Domain `DOMAIN`, Verdict
   `useful`, Watchlist `WATCHLIST`. Leave Note empty. Click **Get link**, copy it.
3. In the copied URL replace the values with placeholders: the `entry.<n>=1` that holds
   the signal id becomes `entry.<n>={signal_id}`, `DOMAIN` becomes `{domain}`, `useful`
   becomes `{verdict}` and `WATCHLIST` becomes `{watchlist}`. The result looks like
   `https://docs.google.com/forms/d/e/<id>/viewform?usp=pp_url&entry.111={signal_id}&entry.222={domain}&entry.333={verdict}&entry.444={watchlist}`.
4. Store it as the Actions variable `TELL_FEEDBACK_FORM_URL` on `tell-ai` (the ingest
   job's `notify` step and the publish steps read it). It is public: anyone may rate a
   signal.

`tell.publish.json_api.feedback_link` substitutes the placeholders URL-encoded; a form URL
without placeholders gets them appended as query parameters, which Google ignores, so
set the placeholders.

## Publishing the responses as CSV

As for the intake form (`docs/intake/google-form.md`): link a response sheet, publish that
tab as CSV, and store the link as the `tell-private` Actions secret
`TELL_FEEDBACK_CSV_URL`. Anyone with the link can read every response, so it is a secret
even though the responses hold no names. If it leaks, unpublish and republish.

## Column mapping

| Field | Accepted headers | Required column |
| --- | --- | --- |
| `timestamp` | `Timestamp` (added by Google Forms) | Yes |
| `signal_id` | `Signal id`, `Signal`, `signal_id` | Yes |
| `verdict` | `Verdict`, `Rating`, `Was this useful` | Yes |
| `domain` | `Domain`, `Company domain` | No |
| `watchlist` | `Watchlist`, `Watchlist slug`, `List` | No |
| `note` | `Note`, `Notes`, `Comment`, `Comments` | No |

A missing required column fails the whole sync (exit 1, `runs.status='failed'`).

## What the reader does with a row

Rejected rows are listed as `{"row": N, "reason": "..."}` with nothing else (N is the
sheet row, the header being row 1): `missing or invalid signal id`, `unknown verdict`,
`unknown signal`, `domain does not match the signal`, `unknown watchlist`, `watchlist
does not hold the signal's company`, `no watchlist holds the signal's company`.

A valid row is a response whose id is `sha256(timestamp | signal_id | verdict |
watchlist)`, stored in `feedback.submission_id`; a response already stored is `skipped`.
The row lands on the named watchlist, else the lowest-id watchlist holding the signal's
company. `created_at` is the sync time, not the form's timestamp (the sheet's timestamp
is in the sheet's locale and tell does not parse it).

## Running it

```sh
tell intake feedback-sync --csv-url "$TELL_FEEDBACK_CSV_URL"
tell intake feedback-sync --csv-file responses.csv
```

One JSON line: `run_id`, `source` (`feedback_sync`), `rows`, `applied`, `skipped`,
`errors`, `rejected_rows`. Then `tell signals feedback-report --profile default` (the
existing precision report) reads the labels.

## The nightly job

The step is part of `docs/deploy/tell-private-intake.yml` (the workflow copied into
`tell-private`), after the intake sync and before the commit, and runs only when the
`TELL_FEEDBACK_CSV_URL` secret is set. Cutover: add the secret in `tell-private`, copy the
updated workflow file over `.github/workflows/intake.yml`.
