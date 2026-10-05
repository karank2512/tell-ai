# 0014. `company_feeds` keeps discovered company RSS/Atom feeds

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 3.7 has the news collector read two kinds of feed: the Google News RSS
search per company, and the company's own blog feed, discovered from the blog page
(`<link rel="alternate" type="application/rss+xml">`, then the fallbacks `/feed`,
`/rss`, `/blog/rss.xml`, `/blog/feed`). The same row sets the rate at 1 request per
second "cached 6 hours".

Discovery costs up to seven requests per company, so it has to be remembered, and
polling a feed politely means conditional requests (`If-None-Match`,
`If-Modified-Since`), which need the `ETag` and `Last-Modified` from the previous
poll. None of the SPEC section 5 tables can hold a feed URL, its validators or the
time it was last polled: `ats_boards` is the closest shape, but its `vendor` /
`identifier` / `confidence` columns are about job boards, and putting feeds there
would leak them into every ATS query and coverage report.

## Decision

Deviate from the section 5 DDL by one table (migration `0003_company_feeds`, model
`tell.models.CompanyFeed`):

```sql
create table company_feeds (
  id              bigserial primary key,
  company_id      bigint not null references companies(id),
  feed_url        text not null unique,
  discovered_from text,                            -- page whose <link rel=alternate> named it, or the fallback URL
  status          text not null default 'active',  -- active|dead|paused
  last_polled_at  timestamptz,                     -- 6-hour cache: not re-polled before this + 6h
  etag            text,                            -- sent back as If-None-Match
  last_modified   text,                            -- sent back as If-Modified-Since
  created_at      timestamptz not null default now()
);
```

`created_at` is `not null` like every other `created_at` in section 5. A feed that
answers 404 or 410 is set to `dead` and never polled again until discovery runs
anew; `paused` is for a human to stop a feed by hand. Discovery misses (no feed on
the domain) are not stored here, because `feed_url` is the natural key; they are
recorded in the news run's `runs.notes` (`no_feed`) and retried after 30 days.

`tests/test_schema.py` lists the table explicitly, so the parity test documents the
deviation instead of hiding it.

## Consequences

Feed discovery runs once per company rather than every run, polls carry
conditional-request headers so unchanged feeds cost a 304, and the 6-hour cache is
enforced from the database rather than from memory. One more small table (one row
per company with a feed, a few hundred bytes each) is well inside the Neon budget.
`docs/SPEC.md` is unchanged; this record is the documentation of the difference. A
future revision of the spec should add the table to section 5.
