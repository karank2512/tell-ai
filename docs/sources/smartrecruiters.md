# SmartRecruiters Posting API

Collector: `tell/collectors/smartrecruiters.py` (`COLLECTOR.source = "smartrecruiters"`, `kind = "ats"`). Spec: `docs/SPEC.md` section 3.4; idempotency rules section 5.

## Endpoints

| Call | URL | When |
| --- | --- | --- |
| List postings | `GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings?limit=100&offset={offset}` | Every poll. Starts at `offset=0` and adds 100 while `offset < totalFound`; stops early on an empty page, and after 200 pages at most |
| Posting detail | `GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings/{id}` | Only for ids not yet in `postings` for that board. A 404 (withdrawn between the two calls) or an exhausted retry skips the description; the posting is still recorded, just without `content_hash` |

Both are public and need no key. Requests go through `tell.http.fetch` with the project User-Agent `tell/0.1 (+https://github.com/karankapur/tell)`.

## Identifier

`ats_boards.identifier` is the SmartRecruiters `companyIdentifier`: the first path segment of `careers.smartrecruiters.com/{company}` or `jobs.smartrecruiters.com/{company}/...` (SPEC section 4), e.g. `BoschGroup`. Every list item also echoes it as `company.identifier`.

## Fields used

| Provider field | Becomes |
| --- | --- |
| `content[].id` | `postings.external_id` |
| `name` | `title` (whitespace-trimmed) |
| `releasedDate` | `posted_at`, and the version marker (`iso_utc` of the parsed value) |
| `location.fullLocation`, else `location.city`, `region`, `country` | `location` |
| `location.remote` | `remote`; when the flag is absent, `true` if "remote" appears in the location text or title, else unknown (`null`) |
| `department.label`, else `function.label` | `department` (many companies send an empty `department` object) |
| `jobAd.sections.jobDescription.text` (detail call) | HTML stripped to plain text, hashed into `content_hash`, then discarded |
| `ref`, `refNumber`, `location.city`, `location.region`, `location.country`, `location.hybrid`, `function.label`, `typeOfEmployment.label`, `experienceLevel.label`, `industry.label` | event payload `extra` |

**Public URL.** The list response has no public posting URL. The collector builds `https://jobs.smartrecruiters.com/{companyIdentifier}/{id}`, which resolves to the slugged page, using the item's `company.identifier` or, when that is missing, the board identifier. It uses a `postingUrl` only if a list item carries one. The detail call's slugged `postingUrl` is deliberately ignored, so a posting's URL does not depend on whether its detail was fetched.

**Required by the contract model** (`SmartRecruitersPostingList`): top-level `offset`, `limit`, `totalFound`, `content`; per item `id`, `name`, `releasedDate`, `location` (with `country`) and `ref`. `department`, `function` and the rest are optional because real boards omit them or send them empty.

**Never kept:** `creator` (a recruiter's name; the models drop unknown nested keys and the fixtures have it removed), street `address`, `postalCode`, coordinates, and any description text.

## Rate limit and politeness

SmartRecruiters publishes no rate limit for the public Posting API. The collector:

- polls each board at most every 6 hours, with 8 boards in parallel (`TELL_POLL_CONCURRENCY`);
- shares one token bucket for `api.smartrecruiters.com` at `TELL_RATE_ATS_RPS` (default 8 per second) across list and detail calls;
- backs off on 429 and 5xx (1, 2, 4, 8 s, honouring `Retry-After`) and never retries other 4xx;
- makes the second (detail) call only for postings it has not seen before, so a steady-state poll costs `ceil(totalFound / 100)` requests.

**404 handling.** A 404 on the list call writes nothing and records the poll in `last_polled_at`. The board goes to `status = 'dead'` on the second consecutive 404. "Consecutive" means the previous poll also ended in a 404: `last_verified_at` is null or older than `last_polled_at`. A successful poll sets `last_verified_at`, and it moves a `dead` board back to `active`. Transport errors and exhausted 5xx retries do not change either column. Because a 404 counts as a poll, a posting missing from the next successful poll at least 12 hours later is closed. This is the same "two consecutive polls without it" rule that SPEC section 5 applies to empty responses.

## Legal note

SPEC section 3.4 has no legal-note row for SmartRecruiters. The applicable SPEC text is the section 3 preamble: "Every v0 source is a documented public endpoint". The Posting API is SmartRecruiters' documented public, unauthenticated API for publishing job ads to career sites. tell reads only published postings and never touches candidate or customer-account endpoints. (Open question for the orchestrator: the SPEC row is missing, so this needs a proper legal-note line.)

## Idempotency key

`smartrecruiters:{companyIdentifier}:{id}:{releasedDate}`. In `tell` terms the event's `external_id` is `smartrecruiters:{identifier}:{id}` and `version` is `releasedDate` rendered as ISO 8601 UTC (for example `2026-10-05T15:27:17.013000+00:00`). `job_opened` is keyed on the first `posted_at` seen.

## Events emitted

All events come from `base.sync_board_postings`:

- `job_opened`: once per new posting;
- `job_updated`: when `releasedDate` differs from the `postings.version_marker` stored at the previous poll, as when a posting is re-released;
- `job_closed`: when a posting is absent from two consecutive polls at least 12 hours apart.

## Fixtures (`tests/fixtures/smartrecruiters/`)

| File | Content | Provenance |
| --- | --- | --- |
| `normal.json` | List page, 3 postings, `totalFound: 3` | Live-recorded 2026-10-05 from `BoschGroup` (one request, `limit=10`), trimmed to 3 items with `totalFound` rewritten to 3 |
| `updated.json` | `normal` with posting `744000153523769`'s `releasedDate` bumped to `2026-10-06T09:12:44.501Z` | Derived from `normal.json` |
| `drift.json` | `normal` with `releasedDate` removed from the first item | Derived from `normal.json` (`drift_missing_field = "releasedDate"`) |
| `empty_board.json` | `{"offset":0,"limit":100,"totalFound":0,"content":[]}` | Live-recorded 2026-10-05 from company `smartrecruiters` |
| `paged_1.json`, `paged_2.json` | Pages at offset 0 and 100 with `totalFound: 150`, trimmed to 3 and 2 items | Items live-recorded from `BoschGroup` (same request), `offset`/`limit`/`totalFound` rewritten to describe a 150-posting board |
| `detail.json` | Detail for posting `744000153523769` | Live-recorded 2026-10-05 (one request). Description sections cut to a short excerpt; `creator`, `referralUrl`, `videos`, street address and coordinates removed |

All fixtures went through the same trimming: `creator` removed, `location` reduced to `city`/`region`/`country`/`remote`/`hybrid`/`fullLocation`, and `customField` cut to two entries. No file contains a person's name, an email address or a phone number.

## Sample response (trimmed list page)

```json
{
  "offset": 0,
  "limit": 100,
  "totalFound": 3,
  "content": [
    {
      "id": "744000153546489",
      "name": "Digital Marketing Coordinator",
      "refNumber": "REF286341H",
      "company": { "identifier": "BoschGroup", "name": "Bosch Group" },
      "releasedDate": "2026-10-05T15:27:17.013Z",
      "location": {
        "city": "Lincolnshire",
        "region": "IL",
        "country": "us",
        "remote": false,
        "hybrid": true,
        "fullLocation": "Lincolnshire, IL, United States"
      },
      "industry": { "id": "mechanical_engineering", "label": "Mechanical Or Industrial Engineering" },
      "department": {},
      "function": { "id": "manufacturing", "label": "Manufacturing" },
      "typeOfEmployment": { "id": "permanent", "label": "Full-time" },
      "experienceLevel": { "id": "associate", "label": "Associate" },
      "visibility": "PUBLIC",
      "ref": "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings/744000153546489",
      "language": { "code": "en", "label": "English", "labelNative": "English (US)" }
    }
  ]
}
```
