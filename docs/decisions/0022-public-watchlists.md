# 0022. `watchlists.slug` and `watchlists.is_public` mark the lists that are published

- Status: accepted
- Date: 2026-10-06

## Context

SPEC section 9 publishes `api/v0/watchlists/{slug}.json` "only for watchlists marked
public", and section 10 lists `GET .../api/v0/watchlists/{slug}.json` as "Scored list for a
public watchlist". The section 5 `watchlists` table has `id`, `owner`, `name` and
`icp_profile` only: nothing marks a list public and nothing gives it a URL-safe name.
`name` is free text and `owner` is an email or handle that must never be published.

## Decision

Deviate from the section 5 DDL by two columns (migration `0006_watchlists_public`,
model `tell.models.Watchlist`):

```sql
alter table watchlists add column slug text unique;                       -- nullable
alter table watchlists add column is_public boolean not null default false;
```

- `is_public` defaults to `false`, so every existing and every new list is private until
  someone flips it on purpose.
- `slug` is nullable (a private list needs none) and unique (two lists cannot claim one
  URL). The published file is `api/v0/watchlists/{slug}.json`; the JSON schema accepts
  `^[a-z0-9][a-z0-9-]*$`.
- `tell.publish.json_api.build_watchlist_json` refuses a list that is not public or has
  no slug, and never writes `owner`. The site generator publishes exactly the lists with
  `is_public` and a slug, and deletes `watchlists/*.json` files of lists that stopped
  being public.

## Consequences

- `tests/test_schema.py` lists both columns (types, nullability, the `false` server
  default, the `watchlists_slug_key` unique constraint) and `HEAD_REVISION` moves to
  `0006`.
- Design-partner watchlists in `tell-private` stay private by default; making one public
  is an explicit, reviewable change.
