# Greenhouse Job Board API

Spec: `docs/SPEC.md` section 3.1. Collector: `tell/collectors/greenhouse.py`. Tests:
`tests/collectors/test_greenhouse.py` plus the generic `tests/test_contracts.py` and
`tests/test_idempotency.py`. Vendor docs: developers.greenhouse.io/job-board.html.

## Endpoints

| Call | URL | Used by |
| --- | --- | --- |
| List jobs | `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true` | `fetch_board`, `ingest_board` |
| Board meta | `GET https://boards-api.greenhouse.io/v1/boards/{board_token}` (returns `name`, `content`) | `fetch_board_meta` (discovery ownership check, SPEC section 4 step 4) |

Both are public GET endpoints with no key. `content=true` is required: without it the
response has no `departments`, `offices` or `content`, and the contract fails.

## Identifier

The board token is the slug in `boards.greenhouse.io/{token}` or
`job-boards.greenhouse.io/{token}` (also seen as `boards-api.greenhouse.io/v1/boards/{token}`
and `grnh.se/{token}` on careers pages). Discovery (SPEC section 4) finds it; it is stored
as `ats_boards.identifier` with `vendor = 'greenhouse'`. The token is path-quoted when the
URL is built.

## Fields used

| Provider field | Becomes |
| --- | --- |
| `id` | `postings.external_id` (as a string) |
| `title` | `title` (whitespace collapsed) |
| `location.name` | `location` (whitespace collapsed; blank or `null` becomes `NULL`) |
| `absolute_url` | `url` |
| `first_published` | `posted_at` (ISO 8601 with offset; may be `null`) |
| `updated_at` | version marker: `ObservedPosting.version = iso_utc(updated_at)` |
| `departments[].name` | `department` = the first name; all names in `payload.extra.departments` |
| `offices[].name` | `payload.extra.offices` |
| `content` | entity-encoded HTML: `html.unescape` once, tags stripped (script/style skipped), entities decoded, whitespace collapsed, hashed into `content_hash`, then discarded |
| `metadata[]` | only the entry named `Workplace Type` (case-insensitive) is read, into `payload.extra.workplace_type`; no other custom field is kept, because boards put arbitrary values there (some name the recruiting team) |

`remote`: Workplace Type `Remote` is `true`; `Hybrid` / `Onsite` / `On-site` / `In office`
are `false`; with no usable Workplace Type, a location containing the word "remote"
(`US-Remote`, `Remote in the US`) is `true`; otherwise `NULL` (unknown).

Every field above is required by the pydantic contract (`GreenhouseJobsResponse`);
`first_published`, `metadata` and `location.name` must be present but may be `null`
(Stripe sends `metadata: null` on every job). Unknown fields are allowed and ignored. The
drift case removes `updated_at`.

## Gotchas (observed 2026-10-14)

One poll of every active board in the 2026-10-13 snapshot (36 boards, 1,918 jobs):

| Field | Null / missing / wrong type | Handling |
| --- | --- | --- |
| `location.name` | `null` on 1 job (1 board, `podium81`); the job still lists an office | `location` stored as `NULL`, `remote` unknown unless Workplace Type says otherwise. Before this fix the strict `str` failed the whole board (1 of 36 boards never polled) |
| `metadata` | `null` on 1,061 jobs (26 boards) | already allowed |

Every other field the contract reads was present and typed as declared on every job.

One bad job never fails a board. `fetch_board` validates the envelope (`jobs`,
`meta.total`) strictly, then each job on its own (`validate_board`): a job that breaks
`GreenhouseJob` is skipped, logged at WARNING with the board token, the job id and the
field path (e.g. `location.name (string_type)`), and counted in the board's
`BoardRunResult.errors`; the rest of the board is ingested. Only when every job of a
non-empty board fails does the board fail with the strict board-level error, so a
provider-wide rename stays loud and never closes every posting. `tell ingest` adds the skipped jobs to `runs.errors` and names the board in
`notes.invalid_jobs` (`{board: count}`). A skipped job is still listed, so its id is
passed to `sync_board_postings` and its posting is never closed while it fails (its
`last_seen_at` is refreshed; a skipped record without a usable id suppresses every
close on that board for that poll).
`parse()` on raw JSON and the generic contract test still use the strict model.

## Rate limit and politeness

No published limit. tell polls each board at most every 6 hours, 8 boards in parallel
(`TELL_POLL_CONCURRENCY`), with one token bucket for the host at `TELL_RATE_ATS_RPS`
(default 8 requests per second, `shared_budget()`). 429 and 5xx are retried by
`tell.http.fetch` on the 1, 2, 4, 8 s schedule, honouring `Retry-After`. Every request
sends `User-Agent: tell/0.1 (+https://github.com/karank2512/tell-ai)`.

A 404 is never retried. Greenhouse answers an unknown token with
`404 {"status":404,"error":"Job not found"}`. A board that 404s is usually a renamed
token. `ingest_board` hands every 404 to `base.record_board_not_found`: `not_found_count` goes up by one and `first_not_found_at` is set on the first 404 of a streak. The board becomes `status = 'dead'` once the count is at least 2 and the current 404 is at least 12 hours after the first one; at that moment one `review_queue` item (`kind='board_verify'`, `reason: 'not_found_twice'`) is opened unless an open `board_verify` item for the board already exists. A 404 never advances `last_polled_at`, so it never counts as a poll without a posting for `job_closed` (SPEC section 5 rule 3). A 200 calls `base.record_board_ok`, which resets the count, sets `last_polled_at`, turns a `dead` board back to `active` and resolves that item. The collector never writes `last_verified_at`; that column is discovery's ownership verification (SPEC section 4 step 5). The rule is the same for all four ATS collectors: see [ADR 0015](../decisions/0015-ats-404-and-reverification.md).

## Legal note

From SPEC section 3.1: "Greenhouse states the data is public and read access needs no
authentication; the only automated-access clause found binds job-seeker accounts, not
the feed".

## Idempotency key

`greenhouse:{board_token}:{job.id}:{updated_at}`. `base.sync_board_postings` builds the
posting key `greenhouse:{board_token}:{job.id}` and uses `updated_at` (UTC ISO 8601) as
the version marker stored in `postings.version_marker` (ADR 0013).

## Events emitted

`job_opened` (once per posting, keyed on the first `first_published` seen), `job_updated`
(when `updated_at` differs from the stored marker), `job_closed` (absent from two
consecutive polls at least 12 hours apart). All from the shared state machine in
`tell/collectors/base.py`. Payloads carry `vendor`, `board_identifier`, the posting fields
above and `extra = {departments, offices, workplace_type}`; never the description.

## Fixtures (`tests/fixtures/greenhouse/`)

All live-recorded on 2026-10-05 with the project User-Agent, one request per endpoint,
then trimmed by hand. `content` was replaced by one or two short neutral sentences in the
same entity-encoded shape (one Stripe job keeps a double-encoded `&amp;amp;` to exercise
the decode); `offices[].child_ids` trimmed to three ids. No person names, emails or phone
numbers. The SPEC names the recording `stripe.json`; it is `normal.json` here because the
collector contract in `tell/collectors/base.py` globs `normal*.json`.

| File | Provenance |
| --- | --- |
| `normal.json` | Board `stripe`, `/jobs?content=true`, 5 of 716 jobs (two with "remote" in the location, one with a trailing space in the location) |
| `normal_airbnb.json` | Board `airbnb`, `/jobs?content=true`, 3 of 149 jobs, one each with Workplace Type Hybrid / Remote / Onsite |
| `empty_board.json` | The documented shape of a board with no jobs (`{"jobs": [], "meta": {"total": 0}}`), hand-written |
| `drift.json` | `normal.json` with `updated_at` removed from every job |
| `updated.json` | `normal.json` with job 8197886's `updated_at` bumped to `2026-10-02T11:05:12-04:00` |
| `board_meta.json` | Board `stripe`, `GET /v1/boards/stripe`, verbatim |
| `not_found.json` | Body of the 404 for a token that does not exist, verbatim |
| `normal_nulls.json` | Board `podium81`, `/jobs?content=true`, recorded 2026-10-14, 3 of 58 jobs: `location.name: null` (8063639), `Remote, US` (8243091) and `Lehi, Utah` (5670478); `content` cut to the first intro paragraph, `meta.total` rewritten to 3 |

Other boards probed while looking for a Workplace Type example (`gitlab`, `figma`,
`cloudflare`, `dropbox`, list without content) carry no such entry; nothing from them is
stored.

## Sample response (trimmed)

```json
{
  "jobs": [
    {
      "id": 8214444,
      "title": "Business Systems Engineer, Tech Foundations",
      "location": { "name": "San Francisco, CA" },
      "absolute_url": "https://careers.airbnb.com/positions/8214444?gh_jid=8214444",
      "first_published": "2026-09-18T16:54:20-04:00",
      "updated_at": "2026-09-29T20:00:54-04:00",
      "departments": [
        { "id": 252, "name": "Information Technology", "child_ids": [], "parent_id": 73703 }
      ],
      "offices": [
        { "id": 63868, "name": "United States", "location": null, "child_ids": [246412], "parent_id": 17515 }
      ],
      "metadata": [
        { "id": 9245691, "name": "Is this job part of ACC?", "value": false, "value_type": "yes_no" },
        { "id": 10216612, "name": "Workplace Type", "value": "Remote", "value_type": "single_select" }
      ],
      "content": "&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;Airbnb was born in 2007 ...",
      "internal_job_id": 3554999,
      "requisition_id": "ONE",
      "company_name": "Airbnb",
      "language": "en"
    }
  ],
  "meta": { "total": 1 }
}
```
