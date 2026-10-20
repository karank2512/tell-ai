# 0043. Freshness p95 is tell's lag: first looks, first-seen-only events and backfills are left out

- Status: proposed
- Date: 2026-10-20

## Context

- **The first dated operations report printed a number that cannot be right.**
  `tell-data/site/methods/ops-2026-10-19.md` gave "Freshness p95, hours (SPEC section 12
  definition): 23298.1 h (N=455)", about 970 days, for a pipeline that polls every few
  hours. Re-running the report's query over the public events JSONL in tell-data
  (`events/2026/10/18.jsonl` and `19.jsonl`, window ending 2026-10-19T07:02:24Z) gives exactly
  23298.1 h over 455 events: 371 Google News items (p95 152.1 h), 18 board events (all under
  12 h but one) and 66 company-blog items whose `occurred_at` is the post's publication date,
  2015 to 2026 (median 12,880 h). All 66 arrived on the first poll on record for their feed
  (no earlier event from the same feed URL since collection began on 2026-10-14): discovery
  stores the whole feed and every post that classifies becomes an event. 66 of 455 is 14.5%,
  so the p95 lands among them.
- **SPEC section 12 is a formula, not a definition of what it measures.** It says
  `freshness_p95_h` is the "95th percentile of `observed_at - occurred_at` over the last 24 h
  of events" and 13.4 quotes it. Read literally it is the age of what tell reads for the first
  time. The SPEC's own target, "four runs a day keeps p95 freshness under 12 hours for ATS and
  filings" (section 11), only makes sense as tell's lag: an EDGAR filing dated D (stored at D
  00:00 UTC) is not in the daily index before about D+1 02:02 UTC, so its literal value is
  never under 26 h. ADR 0023 already left board first polls out ("they measure a job's age,
  not tell's lag") and the Form D backfill left out `payload.backfill`; the same problem had
  three more doors: company-feed archives, Google News items on a company's first query or
  from the widened 13.1 cohort window, and postings dated long before the board listed them
  (Greenhouse's `first_published` on a re-listed job: one in the window, 1,116 h).
- `docs/SPEC.md` is read-only (CLAUDE.md), so the reading is recorded here.

## Decision

1. **Freshness is tell's lag**: `observed_at` minus the earliest moment tell could have seen
   the event. That moment is the source's own timestamp (`occurred_at`; for EDGAR, filing
   date + 26 h, the index time SPEC 13.1 uses for `alert_at`), moved forward to tell's
   previous look at the same place when that look listed the whole place and did not include
   the event: the board's previous poll (`posting_observations`) for ATS events, and the
   company's previous Google News query (`runs.notes.polled`) for Google News items.
2. **Left out, and counted beside N** wherever the number is printed:
   - *first look*: tell was not watching yet, so the age is not tell's lag. A board's first
     poll (`payload.first_poll`) or any ATS event without an earlier poll of its board; a
     Google News item without an earlier query for its company; a company-feed item dated
     before tell discovered the feed (`company_feeds.created_at`); an EDGAR filing, or any
     other source's event, visible before tell's first run of that source.
   - *first-seen-only*: no source timestamp (`occurred_at = observed_at`: `job_updated`,
     `job_closed`, postings without a `posted_at`); the lag is zero by construction.
   - *backfill*: `payload.backfill` (quarterly Form D data sets) and Google News items of
     news runs whose `runs.notes.when_days` is above 7 (the 13.1 cohort fill).
3. **One implementation** (`tell.publish.health.freshness`) for `health.json`, the Slack
   context line, `tell runlog --week` and `tell backtest ops`. The ops report labels the
   number "lag from the earliest moment tell could have seen the event to the moment it was
   recorded; first looks, first-seen-only events and backfills excluded", prints its N, the
   three exclusion counts and each source's own p95. N under 30 stays not quotable.

## Consequences

- On the 2026-10-19 window the literal formula's 23,298 h becomes a number bounded by poll
  gaps; the hand-authored miniature in `tests/fixtures/freshness/` goes from 72,585 h to
  103.0 h. Reports written before this ADR must not be quoted for freshness.
- News dominates the pooled p95 because Google News reaches the long tail once a week; the
  per-source table shows boards and EDGAR separately.
- Company feeds keep no poll history, so a feed item dated after discovery counts from its
  own date and can only overstate the lag. The GitHub collector (not scheduled, ADR 0008)
  is covered by the first-run rule only: an org added later brings its old repositories in
  as counted events.
- `health.json` keeps its schema; the per-event audit is `freshness_events()`.
