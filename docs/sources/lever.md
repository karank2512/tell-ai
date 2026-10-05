# Lever Postings API (`lever`)

Collector: `tell/collectors/lever.py` (`COLLECTOR`, `fetch_board`, `parse`, `ingest_board`, `run_fixture`).
Spec: SPEC §3.2 (source), §5 (idempotency). Documentation: <https://github.com/lever/postings-api>.

## Endpoint

| Tenant | Request |
| --- | --- |
| Global | `GET https://api.lever.co/v0/postings/{site}?mode=json` |
| EU | `GET https://api.eu.lever.co/v0/postings/{site}?mode=json` |

No key or authentication. The response is a bare JSON array of every published posting on
the board (`[]` for a board with no openings). The collector requests the whole array in one
call; it does not use the `skip`/`limit` paging parameters.

## Identifier

`ats_boards.identifier` is the site slug from `jobs.lever.co/{site}`, or `eu:{site}` for a
board found at `jobs.eu.lever.co/{site}` (which is served by the EU API host). Discovery
(SPEC §4) writes it. `parse_identifier` accepts only `[A-Za-z0-9][A-Za-z0-9._-]*` after the
optional `eu:` prefix, so a malformed identifier can never change the request path.

## Fields used

The response model (`LeverPostings`, a root model over `list[LeverPosting]`) is the contract.
These fields are required, and a response that drops one fails validation:
`id`, `text`, `categories`, `createdAt`, `hostedUrl`, `descriptionPlain`, `workplaceType`.
Inside `categories`, every key is optional because boards leave out the ones they don't use
(for example, Spotify leaves out `commitment` on some postings and Palantir has no
`department`). `salaryRange` is optional.

| Lever field | Goes to |
| --- | --- |
| `id` | `postings.external_id` |
| `text` | `title` (stripped) |
| `categories.department` (falls back to `categories.team`) | `department` |
| `categories.location` | `location` |
| `workplaceType` (`remote` → true; `onsite`, `on-site` or `hybrid` → false; otherwise true if any location text contains "remote", else null) | `remote` |
| `createdAt` (epoch ms → UTC datetime) | `posted_at` |
| `createdAt` as a string | version marker (`postings.version_marker`, `job_updated` version) |
| `hostedUrl` | `url` |
| `descriptionPlain` | hashed into `content_hash`, then discarded (never stored) |
| `categories.team`, `categories.department`, `categories.commitment`, `categories.allLocations`, `workplaceType`, `country`, `salaryRange{currency,interval,min,max}` | event payload `extra` (strings clipped to 200 chars) |

`extra.commitment_tags` lists `"intern"` and/or `"contract"` when `categories.commitment`
contains those words (for example "Internship" or "Full Time Contractor"). Phase 6 uses it
to tag `is_intern`. The raw `commitment` is always kept next to it. Nothing personal is read:
Lever postings carry no hiring-manager or recruiter fields in this API, and description text,
`lists`, `additional` and `opening` are never copied.

## Rate limit and politeness

Lever publishes no rate limit.
- Poll each board at most every 6 hours, 8 boards in parallel (`TELL_POLL_CONCURRENCY`).
- `TELL_RATE_ATS_RPS` (default 8) sets the requests per second per host. The two Lever hosts get separate token buckets.
- 429 and 5xx responses are retried by `tell.http.fetch` with backoff of 1, 2, 4 and 8 s. `Retry-After` is honoured.
- Every request sends the project User-Agent `tell/0.1 (+https://github.com/karankapur/tell)`.

## Board liveness (404)

- **First 404:** the board stays `active`. The collector opens a `review_queue` item with `kind='board_verify'` and payload `{vendor, board_identifier, reason: "http_404", consecutive_404s: 1, first_404_at, last_404_at}`. Discovery can then re-verify the site, since a renamed site is the usual cause (SPEC §4 step 5).
- **Second consecutive 404:** `ats_boards.status = 'dead'` and the item's streak goes to 2. The item stays open for discovery.
- **Successful poll in between:** the item is resolved and the streak starts over.
- **Boards that are not `active`:** never requested.

`ats_boards` has no column for a 404 streak, and `base.py` has no helper for one. The open
review item is where the streak is kept. It is private to this module.

`last_polled_at` is changed only by a successful poll (in `base.sync_board_postings`), so a
transient 404 never counts as a poll in which postings were missing. `last_verified_at` is
set on every successful poll.

## Legal note

From SPEC §3.2: "Lever's docs say published postings are publicly viewable and may be scraped
by third parties; its site terms forbid scraping the app, which this does not touch".

## Idempotency

- **Key:** `lever:{site}:{id}:{createdAt}`. The version is `str(createdAt)`. Event `external_id` is `lever:{ats_boards.identifier}:{id}`, built by `base`, so EU boards read `lever:eu:{site}:{id}`.
- **`job_opened`:** emitted once per posting, keyed on the first `posted_at` seen.
- **`job_updated`:** emitted when `createdAt` differs from the stored `postings.version_marker`.
- **`job_closed`:** emitted after two consecutive polls without the posting, at least 12 h apart (base state machine).

## Event types emitted

`job_opened`, `job_updated`, `job_closed` (`events.source = 'lever'`).

## Fixtures (`tests/fixtures/lever/`)

| File | Provenance |
| --- | --- |
| `normal.json` | Live-recorded on 2026-10-05: one request to `https://api.lever.co/v0/postings/spotify?mode=json` (82 postings). Trimmed by hand to 4 postings: remote/Permanent, onsite/"Full Time Contractor", remote/Internship, and hybrid with no `commitment`. Every text field was cut to its first sentence and `lists` to one item. Checked for names, emails and phone numbers; none found. |
| `empty_board.json` | `[]`, the documented response for a board with no published postings. |
| `drift.json` | `normal.json` with `createdAt` removed from the first posting; it must fail naming `createdAt`. |
| `updated.json` | `normal.json` with the fourth posting's `createdAt` moved forward by 24 h (`1789545941933` → `1789632341933`). |

Neither recorded board (Spotify; Palantir was also checked) publishes `salaryRange`.
`tests/collectors/test_lever.py` therefore adds a `salaryRange` object in the documented shape
(`currency`, `interval`, `min`, `max`) to a copy of a fixture posting. The EU host is tested
with respx against the same fixture body.

## Sample response (trimmed, one element)

```json
[
  {
    "additionalPlain": "Spotify is an equal opportunity employer.",
    "additional": "<div>Spotify is an equal opportunity employer.</div>",
    "categories": {
      "commitment": "Internship",
      "department": "Engineering",
      "location": "New York, NY",
      "team": "Personalization",
      "allLocations": ["New York, NY", "London", "Stockholm"]
    },
    "createdAt": 1790268243188,
    "descriptionPlain": "PLEASE READ BEFORE APPLYING\nThis posting is intended specifically for candidates who are participating in RecSys and is not a general application for open roles at Spotify.",
    "description": "<div>PLEASE READ BEFORE APPLYING ...</div>",
    "id": "e7f6e680-bf86-4da6-8711-afe44b84fa41",
    "lists": [],
    "text": "RecSys 2026 — Intern",
    "country": "US",
    "workplaceType": "remote",
    "opening": "",
    "openingPlain": "",
    "descriptionBody": "<div>PLEASE READ BEFORE APPLYING ...</div>",
    "descriptionBodyPlain": "PLEASE READ BEFORE APPLYING ...",
    "hostedUrl": "https://jobs.lever.co/spotify/e7f6e680-bf86-4da6-8711-afe44b84fa41",
    "applyUrl": "https://jobs.lever.co/spotify/e7f6e680-bf86-4da6-8711-afe44b84fa41/apply"
  }
]
```
