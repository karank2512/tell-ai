# Method: publishing (tell-data, site, static API, Slack, CSV, weekly, health)

Spec: `docs/SPEC.md` section 9 (outputs), section 12 ("Run-health in production", the
Publish test family), section 11 (the daily `tell-data` commit), section 14 (Pages limits).
Code: `tell/publish/`. Tests: `tests/test_publish.py`, `tests/publish/`. Decisions beyond
the spec's wording: `docs/decisions/0022-public-watchlists.md`,
`docs/decisions/0023-publishing-interpretations.md`.

## 1. Layout of `tell-data`

```
tell-data/
  events/2026/10/06.jsonl                                    # append-only, system of record
  postings/snapshots/2026-10-06/{vendor}-{identifier}.json   # 90-day retention
  site/                                                      # GitHub Pages root
    index.html  health.html  style.css
    companies/{domain}.html
    weekly/{ISO-week}.html
    api/v0/companies/{domain}.json
    api/v0/weekly/latest.json
    api/v0/watchlists/{slug}.json                            # public watchlists only
    api/v0/health.json
```

`tell publish --out tell-data` (no command) runs `data` then `site`; the ingest workflow
then commits and pushes the tree. That daily commit is what keeps the scheduled
workflows alive past GitHub's 60-day inactivity cutoff (spec section 11).

## 2. Events: `events/YYYY/MM/DD.jsonl`

- One line per `events` row **observed** on that UTC day; keys are every column in DDL
  order (`event_id, company_id, source, type, occurred_at, observed_at, magnitude, url,
  payload, raw_ref, run_id`), datetimes ISO 8601 UTC.
- Append-only: a run appends only the `event_id`s the file lacks, never rewrites, reorders
  or removes a line. `data` writes yesterday and today, so the last run of a day and the
  first of the next both land. A truncated last line stops the writer (nothing is
  silently repaired).
- `raw_ref` is the line's own address, `events/2026/10/06.jsonl#L12`; it is also copied
  to `events.raw_ref` when that column was empty.
- The 180-day prune (Phase 9) calls `archive_events(session, out_dir, older_than)`, which
  writes old events to their observed-day files, `fsync`s, and returns the ids now on
  disk; only those rows are deleted.

## 3. Snapshots: `postings/snapshots/`

One file per active, polled board per day: the open postings with `external_id`, `title`,
`role_family`, `location`, `posted_at`, `url`. No descriptions. Day directories older than
90 days are removed by `prune_snapshots` on every `data` run.

## 4. The site and the static API

- **Published companies**: every company with a signal starting in the last 180 days
  (the section 14 size budget). Files of companies that drop out are deleted; weekly pages
  are kept as the archive. At 25,000 account pages the site stays well under 200 MB.
- **Account page**: score (raw sum, one decimal), one bar per contribution with its decayed
  value and its receipt link, the counted postings under a velocity bar, long-open roles in
  grey, the "why not higher" footer (signals that decayed to zero in the last 30 days) and a
  thumbs row per contribution linking to the feedback form prefilled with `signal_id`
  (`TELL_FEEDBACK_FORM_URL`; `{signal_id}`, `{domain}`, `{verdict}` are substituted) or,
  without a form, to a prefilled GitHub issue. Heat appears only for public watchlists that
  include the company.
- **Index**: the 10 largest score increases over 7 days, an excerpt of the weekly lists,
  methods links, the repo and the health page.
- **Numbers add up**: contributions are rounded to tenths by largest remainder so they sum
  to the displayed score exactly.
- **Validation**: every JSON object is validated before it is written, and the whole
  `api/v0` tree again at the end; the first failure stops the run (nothing invalid is
  pushed). Templates render with `StrictUndefined`, so an unresolved variable fails the
  run too.
- **Determinism**: same data, `as_of`, `now` and commit → byte-identical files.

## 5. Weekly page

One edition per ISO week (Monday 00:00 UTC to the next). The site renders the current
edition, the most recent **complete** week, and `api/v0/weekly/latest.json` mirrors it.
Contents: top 25 companies by sales-hiring velocity (sum of `hiring_velocity` magnitudes for
role family `sales` in the week, each with its receipts), top 25 new Form D filings by
offering amount among monitored companies (an active board or a CIK), the "what changed in
the method" line (the newest `CHANGELOG.md` bullet mentioning scoring, velocity or
resolution), coverage numbers (companies monitored, boards polled, filings read) and the
repo link.

## 6. Run-health

Per source (registered collectors, then the `resolve`, `score`, `publish` jobs):
`last_ok_at`, `success_rate_7d` (ok / finished runs started in 7 days), `fetched_24h`,
`inserted_24h`, `freshness_p95_h` (95th percentile of `observed_at - occurred_at` over the
source's events observed in the last 24 hours, first-poll baselines excluded), `stale` (has
run, but no success in 24 hours) and `status` (`ok` / `stale` / `never_run`). Any stale
source turns the health page's banner red and adds a red line at the top of the Slack
digest.

## 7. Slack digest timing

`tell publish notify --slack` runs after every ingest. It sends only on the **first run at
or after `TELL_SLACK_DIGEST_HOUR` (default 7) local time (`TELL_SLACK_TZ`, default
`America/Los_Angeles`) on a local day with no digest sent yet**; each send is a `runs` row
with source `slack_digest`, and a run whose every message failed is recorded `failed` so
the next run retries. With the `17 2,8,14,20` UTC cron that is the 07:17 run in Pacific
summer time and the 12:17 run in winter. One message per watchlist, sent sequentially
with a 1.1 s pause (incoming webhooks allow about one per second). `--force` sends now.

Crossed accounts are `score_crossed_threshold` events observed in the last 24 hours for the
watchlist's profile and companies, at most 10 per message, highest score first, each with
one bullet per contribution (receipt linked) and a "Why now" sentence from the top two.

**Routing (ADR 0036).** A watchlist whose `tell-private/watchlists/<slug>.yaml` record has a
`slack_webhook_url` gets its digest on that webhook (`--private-dir`, default
`../tell-private`; the workflow passes the read-only checkout); every other watchlist goes
to `SLACK_WEBHOOK_URL`; a watchlist with neither is skipped and counted
(`runs.skipped`, `without_webhook` in the JSON line).

### 7a. Per-event nudges

`tell notify --slack` also runs the nudges (`tell/publish/nudge.py`) on **every** call,
before the digest. For each watchlist with a private webhook, every
`score_crossed_threshold` of the last 24 hours that has no `nudges` row with status `ok` for
that watchlist becomes one message to that webhook, paced 1.1 s apart:

- `*{name}* ({domain}) · score {score} · crossed {threshold}`, then one receipt bullet per
  contribution (same 2900-character budget as the digest), `Why now:` from the top two, and
  the enrichment gate's verdict (`tell/signals/gate.py`, ADR 0038): `Worth enriching: yes ·
  {family a} + {family b}`, `skip · one signal family`, `skip · a credit went to this
  company in the last 90 days`, `hold · board unreadable (dead:lever:acme)` or `hold · this
  month's credit budget is spent`. The verdict is decided once per (crossing, watchlist),
  written to `enrichment_verdicts` (migration 0011) before the POST, and read back on a
  resend; the watchlist's budget is `credit_budget_monthly` in its `tell-private` record,
  default 100 a month;
- a context line with the account page and, when `TELL_FEEDBACK_FORM_URL` is set,
  prefilled `useful` / `not useful` links for the top contribution's signal.

The `nudges` table (migration 0010) holds one row per (event, watchlist): `ok` once a 2xx
came back, `failed` with the HTTP status (or `NULL` on a transport error) otherwise; a
failed row is updated and retried on the next run while the event is inside the window, and
after 24 hours a crossing is never nudged. A run with at least one candidate is recorded as
`runs.source = slack_nudge` (`fetched` candidates, `inserted` sent, `errors`, and the
verdict counts in `notes.verdicts`), not a dead-man source. The global webhook never
receives nudges.

## 8. CSV with receipts

`tell publish csv --watchlist NAME --out FILE` writes exactly the spec's columns:
`domain, name, score, heat, top_signal_1, top_signal_1_date, top_signal_1_url,
top_signal_2, top_signal_2_date, top_signal_2_url, new_roles_7d_sales, new_roles_7d_eng,
new_roles_7d_other, funding_last_180d_usd, funding_url, open_postings, long_open_roles,
ats, coverage_note, account_page, rate_this_row`. `heat` is the percentile within the
exported list; `coverage_note` is `greenhouse:acme`, `no_board`, `unsupported:workday` or
`dead:lever:acme`, so gaps are explicit; `rate_this_row` is the prefilled feedback link
when `TELL_FEEDBACK_FORM_URL` is set, else empty.
