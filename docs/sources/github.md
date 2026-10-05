# GitHub org repos and releases (`github`)

Collector: `tell/collectors/github_org.py` (`COLLECTOR`, `list_repos`, `list_releases`, `collect`, `ingest`, `run_fixture`).
Spec: SPEC §3.8 (source, v1.1 for devtools ICPs), §5 (idempotency), §8 (`launch` from a release on a flagship repo).
Documentation: <https://docs.github.com/en/rest/repos/repos#list-organization-repositories>,
<https://docs.github.com/en/rest/releases/releases#list-releases>,
<https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>,
<https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate>.

## Gating

Config-gated per company: only companies whose `companies.github_org` is set are polled. The
value is the org login (`astral-sh`). A `github.com/<org>` URL is accepted too, and the
value is lowercased for request URLs. A value that is not a valid login is logged and
skipped. If two companies claim the same org, the lowest `companies.id` gets the events and
the clash is logged. `company_id` on every event is that company's id.

## Endpoints

| Purpose | Request |
| --- | --- |
| Org repos | `GET https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=100`, following `Link: rel="next"` for at most 10 pages (1,000 most recently pushed repos) |
| Releases | `GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=20` (first page only, newest first) |

Headers on every request: `Accept: application/vnd.github+json`,
`X-GitHub-Api-Version: 2022-11-28`, the project User-Agent
`tell/0.1 (+https://github.com/karankapur/tell)`, and `Authorization: Bearer <GITHUB_TOKEN>`
when `GITHUB_TOKEN` is set. In Actions the workflow's own `GITHUB_TOKEN` is enough because
only public data is read.

Releases are read for at most 5 repos per org: the flagship, then the most-starred non-fork,
non-archived repos. A release on a long-tail repo is not a launch signal, so this keeps the
cost to about 6 requests per org per run.

## Fields used

The response models are the contract. A response that drops a required field fails
validation. The run is then marked `failed`, so its ETags are never reused.

| Model | Required | Optional |
| --- | --- | --- |
| `GithubRepo` (`GithubRepos` is the bare array, `COLLECTOR.response_model`) | `id`, `name`, `full_name`, `html_url`, `fork`, `created_at`, `stargazers_count` | `pushed_at`, `language`, `archived` |
| `GithubRelease` (`GithubReleases`) | `id`, `tag_name`, `html_url`, `draft`, `prerelease`, `created_at`, `published_at` (may be `null`) | `name` |

Unknown keys are ignored and never kept. The collector never reads `owner`, `description`,
release `body` (release notes, which mention contributors by handle), `author`, `assets`
or asset `uploader`. The org login comes from `full_name`.

## Events

| Type | `external_id` | `version` | Natural key | `occurred_at` | `url` | Payload |
| --- | --- | --- | --- | --- | --- | --- |
| `github_repo_created` | `{org}:{repo}` | `created` | `github:{org}:{repo}:created` | repo `created_at` | repo `html_url` | `org`, `name`, `language`, `stars`, `fork`, `is_flagship` |
| `github_release` | `{owner}/{repo}:{release.id}` | `str(release.id)` | `github:{repo}:{release.id}` | `published_at` | release `html_url` | `owner`, `repo`, `tag_name`, `name` (clipped to 200 chars), `prerelease`, `stars`, `is_flagship` |

`event_id = sha256("github|{external_id}|{version}")`. `magnitude` is 1. Drafts and releases
without `published_at` are skipped. A repo-created event is written for every repo,
including forks and archived repos. `fork` is in the payload so Phase 7 can filter on it.

**Flagship repo.** The org's most-starred non-fork repo. Ties are broken by name,
case-insensitively. `payload.is_flagship` is set on both event types. Per SPEC §8, Phase 7
counts only `github_release` events with `is_flagship = true` as `launch`.

## Conditional requests and run notes

- Each run stores `{"etags": {url: etag}}` in `runs.notes`, covering the first page of each org listing and each releases URL it fully processed.
- It also stores `{"orgs": {org: {"owner", "flagship", "repos": [{"name", "stars"}]}}}`, the per-org summary.
- The next run reads both from the latest github run with `status = 'ok'` and sends `If-None-Match`.
- A `304` is skipped without counting: it is not in `fetched` and not in `skipped`. It is tallied in `notes.not_modified`.
- A `304` on the repo listing reuses the stored summary, so releases are still polled.
- Only page 1 of a listing is conditional. Once it changes, every later page is read in full, and later-page ETags are never stored.
- An ETag is recorded only after its response has been parsed and its events derived. An interrupted listing is never marked as seen.
- ETags and summaries for orgs that are no longer configured are dropped. Those for configured orgs that this run did not reach (for example after a rate-limit stop) are carried forward.

## Rate limit

| Mode | Limit |
| --- | --- |
| With a token | 5,000 requests per hour (the Actions `GITHUB_TOKEN` gets 1,000 per hour per repo) |
| Unauthenticated | 60 requests per hour per IP |
| Conditional `304` | Free: does not count against the limit |

The collector reads `X-RateLimit-Remaining` on every response. When it drops below 50, the
collector finishes the response in hand and then stops the run. The run still finishes
`ok`, with `notes.rate_limited = true`, `rate_limit_remaining`, `rate_limit_reset` (epoch
seconds) and `stopped_at_org`. A `403`/`429` that carries `X-RateLimit-Remaining: 0` or
`Retry-After` (primary or secondary limit) stops the run the same way.

Without a token, the floor of 50 leaves about 10 requests per hour, which is roughly one or
two orgs. Production runs set `GITHUB_TOKEN`.

Requests are serial, per GitHub's best-practice guidance. `tell.http.fetch` retries `5xx`
and `429` with backoff of 1, 2, 4 and 8 s and honours `Retry-After`. A `404` org (renamed or
deleted) counts as an error and is listed in `notes.missing_orgs`, and the run moves on to
the next org.

## Legal note

The collector reads only the documented public GitHub REST API, under the GitHub Terms of
Service (API terms, section H), and stays within the published rate limits. It reads only
org-level public data: public repos' names, stars, language, fork flag, creation date, and
release ids, tags, names and dates. It never reads user profiles, commit authors, release
authors or asset uploaders, and it stores no personal data.

## Fixtures (`tests/fixtures/github/`)

**Provenance.** Live-recorded on 2026-10-05, unauthenticated, one request per endpoint, with
the project User-Agent:
- `GET /orgs/astral-sh/repos?sort=pushed&per_page=100`: 92 repos, no `Link` header.
- `GET /repos/astral-sh/uv/releases?per_page=20`: 20 releases.

**Trimming.** The listing was cut to `uv`, `ruff` and the fork `pubgrub`, and the releases to
the 3 newest. On repos, `owner`, every `*_url` template and the `permissions`/`custom_properties`
objects were removed, and `license` was reduced to `key` and `spdx_id`. On releases,
`author`, `assets` (with their `uploader`), `body`, `reactions` and `upload_url` were
removed. No fixture contains an `@`, a person's login or an email.

| File | Content |
| --- | --- |
| `repos.json` | The trimmed org listing: 3 repos, one of them a fork. The flagship is `uv` (90,411 stars). |
| `normal.json` | Identical to `repos.json`. It is the file the generic contract and idempotency tests replay. |
| `releases.json` | The 3 newest `astral-sh/uv` releases (ids 402618784, 401449793, 399533040). |
| `empty.json` | `[]`: an org with no public repos. |
| `drift.json` | `repos.json` with `created_at` removed. It must fail naming `created_at`. |

`run_fixture(normal.json)` replays the listing. When the listing has a flagship and
`releases.json` sits beside it, it also replays that file as the flagship's releases,
giving 3 `github_repo_created` and 3 `github_release` events (`fetched = 6`). Pagination,
ETag/304 and rate-limit behaviour are covered with `respx` in
`tests/collectors/test_github_org.py`.

## Sample (trimmed)

```json
{"id": 699532645, "name": "uv", "full_name": "astral-sh/uv", "html_url": "https://github.com/astral-sh/uv",
 "fork": false, "created_at": "2023-10-02T20:24:11Z", "stargazers_count": 90411, "language": "Rust", "archived": false}
```
