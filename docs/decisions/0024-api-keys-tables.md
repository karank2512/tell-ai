# 0024. `api_keys` and `api_usage` back the FastAPI v1 service

- Status: accepted
- Date: 2026-10-06

## Context

SPEC section 10 (FastAPI service, v1) authenticates `Authorization: Bearer <key>` with
keys that are `tell_` plus 32 random bytes base64url, "stored as SHA-256 in a `api_keys`
table with `owner`, `created_at`, `last_used_at`, `daily_quota`", and meters "one row per
request in `api_usage (key_id, day, calls)` upserted; quota check before the handler".
Section 5 (the DDL every model must match) lists neither table, and section 10 leaves
some behaviour open: how a key is shown to an operator, how one is withdrawn, what a
refused call counts, and how the v1 routes read their parameters.

## Decision

Two tables (migration `0007_api_keys`, models `tell.models.ApiKey` / `ApiUsage`):

```sql
create table api_keys (
  id           bigserial primary key,
  key_hash     text not null unique,          -- sha256(full key), lowercase hex
  key_prefix   text not null,                 -- first 12 characters, for display
  owner        text not null,                 -- a handle, never an email
  created_at   timestamptz not null default now(),
  last_used_at timestamptz,
  daily_quota  integer not null default 1000,
  revoked_at   timestamptz
);
create table api_usage (
  key_id  bigint not null references api_keys(id),
  day     date not null,                      -- UTC calendar day
  calls   integer not null default 0,
  primary key (key_id, day)
);
```

Additions beyond section 10's column list: `key_prefix` (an operator must tell keys apart
without the secret; 12 characters = `tell_` + 7 random characters, not enough to guess
the key) and `revoked_at` (a leaked key is withdrawn without deleting its usage
history).

Behaviour (`tell/api/auth.py`, `tell/api/app.py`, `tell/api/cli.py`):

1. **Keys.** `tell api create-key --owner HANDLE [--daily-quota N]` prints the raw key
   once; only the hash and prefix are stored. `owner` must match
   `[a-z0-9][a-z0-9._-]{0,63}` (lowercased), so an email address is refused (no PII).
   `tell api revoke-key <id|prefix>` sets `revoked_at`; a second revoke keeps the first
   timestamp. A missing, malformed, unknown or revoked key all answer the same 401.
2. **Metering.** One `INSERT ... ON CONFLICT DO UPDATE SET calls = calls + 1 WHERE calls <
   daily_quota RETURNING calls` per request, committed before the handler runs: no row
   back means the quota is spent and the answer is 429 with `Retry-After` = seconds to
   the next 00:00 UTC. Refused calls are not counted; a call whose handler then fails
   (404, 422) is. A quota of 0 refuses everything. `last_used_at` is written at most once
   a minute per key. `/healthz` is neither authenticated nor metered.
3. **Owner and watchlists.** `POST /v1/watchlists` creates a private list (`is_public`
   false, no slug) whose `watchlists.owner` is the key's owner handle. `/v1/in-market`
   accepts a watchlist id or slug that the owner owns or that is public; any other list
   is a 404, the same as a missing one, so private lists are not enumerable.
4. **In-market.** Scores are computed fresh at request time (`write=False`; the service
   never writes `scores`). A company is listed when it is in the profile's size band, its
   raw score is at least `min_score` (default: the profile's `alert_threshold`, 6.0), and
   at least one contribution occurred on or after `since` (default: 7 days before today,
   matching the MCP `in_market` default) with a `role_family` in `family` when given
   (repeatable or comma-separated; unknown families are 422). Heat is over the whole
   watchlist. Each row carries the top three contributions in the static object's shape.
5. **Feedback.** `POST /v1/feedback` takes an optional `watchlist_id`; it must be one of
   the owner's lists and hold the signal's company (else 404 / 422). Without it, the
   owner's lowest-id list holding the company is used; none is 422.
6. **Companies and events.** `/v1/companies/{domain}` returns
   `tell.publish.json_api.build_company_json` for today under `?profile=` (default
   `default`), without `heat`, exactly the static `api/v0` object. Domains are looked up
   lowercase without scheme, path or `www.`, then by registrable domain.
   `/v1/companies/{domain}/events` is newest first by `occurred_at`, `?since=` a date
   (inclusive, UTC), `?types=` repeatable or comma-separated, `?limit=` at most 200.
7. **Errors and CORS.** Every error is an RFC 9457 `application/problem+json` body.
   No CORS middleware is installed (CORS off by default).

## Consequences

- `tests/test_schema.py` lists both tables (columns, keys, the `key_hash` unique
  constraint, the `api_usage.key_id` foreign key, nullability, defaults, types) and
  `HEAD_REVISION` moves to `0007`.
- The raw key exists only in the operator's terminal and the consumer's secret store;
  a lost key is revoked and replaced, never recovered.
- `api_usage` grows by one row per key per day; at a handful of keys it needs no prune.
- The service is coded and tested only; `Dockerfile` and `docs/deploy/render.md` cover a
  later deployment on Render's free tier (ADR 0008).
