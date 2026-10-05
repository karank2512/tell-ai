# Source: Ashby Job Posting API

Collector: `tell/collectors/ashby.py` (`COLLECTOR.source == "ashby"`, kind `ats`).
Spec: `docs/SPEC.md` section 3.3; idempotency rules in section 5.
Provider docs: developers.ashbyhq.com/docs/public-job-posting-api (public, no key).

## Endpoint

```
GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}?includeCompensation=true
```

One call per board returns every posting on it; there is no pagination and no
detail call. `includeCompensation=true` adds the `compensation` object to each job.
Built by `board_url()` + `fetch_board()` through `tell.http.fetch` with the project
User-Agent (`TELL_HTTP_USER_AGENT`, default `tell/0.1 (+https://github.com/karankapur/tell)`).

## Identifier

`jobBoardName` is the slug in `jobs.ashbyhq.com/{name}` (also seen as
`api.ashbyhq.com/posting-api/job-board/{name}` in embedded career pages); discovery
(spec section 4) stores it as `ats_boards.identifier` with `vendor = 'ashby'`. The
slug is URL-quoted when building the request.

## Fields used

| Ashby field | Where it goes |
| --- | --- |
| `jobs[].id` | `postings.external_id` |
| `title` | `postings.title` (leading/trailing whitespace stripped; real boards carry it) |
| `department` | `postings.department` |
| `location` | `postings.location` (primary location) |
| `workplaceType`, `isRemote` | `postings.remote`: `Remote` -> true, `OnSite`/`Hybrid` -> false; `isRemote` only when `workplaceType` is null (Ashby sets `isRemote: true` on hybrid roles with a remote secondary location) |
| `publishedAt` | `postings.posted_at` and the version marker (rendered with `iso_utc`) |
| `isListed` | `false` -> posting skipped entirely |
| `jobUrl` | `postings.url` and `events.url` |
| `descriptionPlain` | hashed into `postings.content_hash`, then discarded |
| `team`, `employmentType`, `workplaceType`, `isRemote`, `secondaryLocations[].location`, `applyUrl` | event `payload.extra` |
| `compensation` | `payload.extra.compensation`: `compensationTierSummary`, `scrapeableCompensationSalarySummary`, `summaryComponents[]` only (tiers dropped); omitted when empty or when `shouldDisplayCompensationOnJobPostings` is `false` |

Not kept: `descriptionHtml`, `descriptionPlain` text, `address`, compensation tiers.
Strings in `extra` are clipped to 500 characters. No field of this API names a person.

The pydantic contract (`AshbyJobBoard`) requires `apiVersion`, `jobs`, and per job
`id`, `title`, `department`, `team`, `employmentType`, `location`,
`secondaryLocations`, `isRemote`, `workplaceType`, `publishedAt`, `isListed`, `jobUrl`,
`applyUrl`, `descriptionPlain` (the nullable ones may be `null` but must be present).
`compensation` is optional because it only appears with `includeCompensation=true`.

## Rate limit and politeness

- Ashby publishes no rate limit for this API.
- One process-wide `RateBudget` at `TELL_RATE_ATS_RPS` (default 8 req/s) for
  `api.ashbyhq.com`; `TELL_POLL_CONCURRENCY` (8) boards in parallel.
- Poll each board at most every 6 hours (the scheduler reads `ats_boards.last_polled_at`).
- 429 and 5xx: retried by `tell.http.fetch` with 1, 2, 4, 8 s backoff (or `Retry-After`,
  capped at 120 s), then `RetryExhausted` propagates and the run records an error.
- 404: not retried. The first 404 sets `last_polled_at` only; a second consecutive
  404 sets `ats_boards.status = 'dead'`. A successful poll sets `last_verified_at`,
  which resets the streak. A 404 never counts as an observation for `job_closed`:
  the next successful poll restores `last_polled_at` to the last successful poll
  before the state machine runs. Spec section 4 asks discovery to re-verify a board
  after two 404s; that hook belongs to Phase 3.
- Any other 4xx raises `httpx.HTTPStatusError`.

## Legal note

SPEC section 3.3 gives no legal note for Ashby (sections 3.1 and 3.2 do for Greenhouse
and Lever). What is known: this is Ashby's documented public Job Posting API, it needs
no authentication, and it serves the same postings the employer publishes on
`jobs.ashbyhq.com`. The collector reads only that API, never the Ashby app.
A reviewed legal note is an open item for Karan.

## Idempotency key

`ashby:{name}:{id}:{publishedAt}`. In code: `events.external_id =
"ashby:{identifier}:{id}"`, `version = iso_utc(publishedAt)`, `event_id =
sha256("ashby|ashby:{identifier}:{id}|{version}")`. `postings.version_marker` holds
the last `publishedAt` seen (ADR 0013).

## Event types emitted

All through `base.sync_board_postings`:

- `job_opened`: once per posting, keyed on the first `publishedAt` seen.
- `job_updated`: when `publishedAt` differs from `postings.version_marker` (Ashby
  re-publishing a posting); never a second `job_opened`.
- `job_closed`: posting absent from two consecutive successful polls at least 12 h apart.

## Fixtures (`tests/fixtures/ashby/`)

| File | Content | Provenance |
| --- | --- | --- |
| `normal.json` | 4 jobs | Live-recorded 2026-10-05 from the public `ramp` board (one request, project User-Agent), trimmed from 158 jobs to 4 (Hybrid with 3 secondary locations and compensation; Remote; OnSite with empty compensation; OnSite Temporary with compensation). `descriptionPlain`/`descriptionHtml` cut to the first "About the Role" paragraph and checked for names; everything else verbatim. |
| `empty_board.json` | `{"apiVersion": "1", "jobs": []}` | Hand-made from the recorded envelope. |
| `drift.json` | `normal` without `jobs[1].publishedAt` | Derived; contract test expects `publishedAt` in the error. |
| `updated.json` | `normal` with `jobs[2].publishedAt` bumped to `2026-09-30T09:12:44.118+00:00` | Derived; yields exactly one `job_updated`. |

Observed quirks kept in the fixture: a title with a leading space, `isRemote: true`
on Hybrid roles, and `summaryComponents` entries that omit `currencyCode` (parsed as null).

No real board in the recording had `isListed: false`; the filter is tested by
flipping the flag in memory in `tests/collectors/test_ashby.py`.

## Sample response (trimmed)

```json
{
  "apiVersion": "1",
  "jobs": [
    {
      "id": "4c24a55a-ea3b-4cea-a5b5-7862938616bf",
      "title": "Procurement Solutions Architect",
      "department": "Sales",
      "team": "Solutions Consulting",
      "employmentType": "FullTime",
      "location": "Remote (US)",
      "shouldDisplayCompensationOnJobPostings": true,
      "secondaryLocations": [
        {"location": "San Francisco, CA", "address": {"postalAddress": {"addressCountry": "USA", "addressLocality": "San Fransisco"}}},
        {"location": "New York, NY (HQ)", "address": {"postalAddress": {"addressRegion": "NY", "addressCountry": "USA", "addressLocality": "New York City"}}}
      ],
      "publishedAt": "2026-09-21T20:33:59.665+00:00",
      "isListed": true,
      "isRemote": true,
      "workplaceType": "Remote",
      "address": {"postalAddress": {"addressCountry": "United States"}},
      "jobUrl": "https://jobs.ashbyhq.com/ramp/4c24a55a-ea3b-4cea-a5b5-7862938616bf",
      "applyUrl": "https://jobs.ashbyhq.com/ramp/4c24a55a-ea3b-4cea-a5b5-7862938616bf/application",
      "descriptionHtml": "<h1><strong>About the Role</strong></h1><p>...</p>",
      "descriptionPlain": "ABOUT THE ROLE\n\n...",
      "compensation": {
        "compensationTierSummary": "$182,260 – $278,300 • Offers Equity • Multiple Ranges",
        "scrapeableCompensationSalarySummary": "$182,260 - $278,300",
        "compensationTiers": ["..."],
        "summaryComponents": [
          {"compensationType": "EquityCashValue", "minValue": null, "maxValue": null, "interval": "1 YEAR"},
          {"minValue": 182260, "maxValue": 278300, "currencyCode": "USD", "interval": "1 YEAR", "compensationType": "Salary"}
        ]
      }
    }
  ]
}
```
