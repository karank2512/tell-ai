# 0032. News: company-feed discovery once per 30 days, its own per-host budget, commits per company

- Status: accepted
- Date: 2026-10-14

## Context

The first production news run polled 53 of 893 eligible companies before its 25-minute
`RUN_BUDGET` ran out and counted 24 companies without a blog feed as errors. Feed
discovery (SPEC 3.7: `/blog`, the homepage, `/feed`, `/rss`, `/blog/rss.xml`,
`/blog/feed`) ran inline, one company at a time, on the same thread as the Google News
queries. A live site without a feed costs six requests at 1 request per second; a dead
host cost six 20-second timeouts, two minutes for one company. A miss that hit a
transport error or a 5xx was counted as an error and not remembered, so the same dead
hosts were probed again on every visit. The whole run also held one database transaction
across all of that network I/O (Neon killed it once, see the 2026-10-13 hotfix).

ADR 0014 already says discovery misses are recorded in `runs.notes["no_feed"]` and
retried after 30 days; only clean misses were, and only when the run reached
`finish_run`.

## Decision

1. **Discovery at most once per company per 30 days.** Every discovery miss is
   remembered in `runs.notes["no_feed"]` (the ADR 0014 mechanism, `DISCOVERY_RETRY`),
   including a site that never answered. The open run row's notes are refreshed every 20
   companies and at the end, so a killed run still remembers. No schema change: a
   `company_feeds` row with `status = 'none'` was considered and rejected, because
   `feed_url` is the table's natural key (`not null unique`) and the static API
   (`tell/publish/json_api.py`) reads "has a `company_feeds` row" as "news coverage".
   A company whose only feeds are `dead` is rediscovered 30 days after the feed died
   (and the row is reactivated if the same URL is found); `active` and `paused` feeds are
   never rediscovered. Found feeds keep the 6-hour conditional polling.
2. **"No feed" is not an error.** A miss is counted in `notes.site.no_feed` (every probe
   answered) or `notes.site.unreachable` (DNS failure, refused or timed-out connection, TLS
   failure, or 429/503 from the site) and never in `runs.errors`. The first unanswered
   probe ends that site's discovery. Real errors stay errors: a Google News failure, a
   stored feed that fails to answer or answers anything but 200/304/404/410, or a
   company's site work raising.
3. **Company sites get their own budget and workers.** Discovery and feed polls run on
   `SITE_WORKERS = 6` threads at `SITE_RPS = 1` request per second per host with a
   `SITE_TIMEOUT_S = 10` second timeout per phase of each hop (redirects are followed hop
   by hop, at most `SITE_MAX_REDIRECTS = 5`, each hop charged to its own host's budget); at
   most 12 companies' site work is outstanding
   before the Google News loop waits. Google News keeps `TELL_RATE_NEWS_RPS` (1 rps), one
   query at a time, and the 429/503 block policy. These are module constants (and
   `collect()` / `ingest()` arguments), not settings: `tell/config.py` is shared.
4. **No transaction across network I/O.** Workers never touch the database; the run's
   thread is the only writer. `ingest()` commits the run row first, then after every
   company (its Google News answer plus whatever site work has finished, each company's
   site writes in a savepoint) and before every wait for a site worker (a full backlog,
   the final drain), keeping the run row's counters current. An unexpected
   exception rolls back only the current company and closes the run `failed` with
   `notes.error`. `ingest(commit=None)` keeps the old flush-only behaviour for callers that
   own the transaction.

## Consequences

Measured with respx on a scaled clock (60% live sites without a feed, 20% dead hosts that
time out, 20% with a feed; 0.3 s per answer), companies per 25-minute run went from 56
(the production run did 53) to the 1,200 `MAX_COMPANIES` cap, with no errors; numbers in
`docs/sources/news.md`. A site that was down during its one discovery attempt is not
retried for 30 days. A run's `runs.notes` JSON is rewritten every 20 companies while it is
open (tens of KB, well inside the Neon budget). `NewsRunResult.notes` gains `site`
(outcome counts) and `site_workers`. `docs/SPEC.md` is unchanged.
