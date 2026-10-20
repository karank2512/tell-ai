# News and RSS (Google News + company blog feeds)

Spec: `docs/SPEC.md` sections 3.7, 8, 11 and 13.1. Collector: `tell/collectors/news.py`;
classifier: `tell/collectors/news_classify.py`. Tests: `tests/collectors/test_news.py`, `tests/collectors/test_news_cohort.py`,
`tests/test_news_classify.py`, plus the generic `tests/test_contracts.py` and
`tests/test_idempotency.py`. Schema addition: `company_feeds`
(`docs/decisions/0014-company-feeds-table.md`); discovery memory, site concurrency and
per-company commits: `docs/decisions/0032-news-feed-discovery-cache.md`. Leadership headlines are stored redacted
(`docs/decisions/0016-news-headline-redaction.md`, accepted). Entry point for the CLI:
`news.ingest()` (starts the run, calls `collect()`, finishes the run with the notes the
next run reads).

## Endpoints

| Call | URL | Used by |
| --- | --- | --- |
| Google News search | `GET https://news.google.com/rss/search?q=%22{company name}%22+when%3A7d&hl=en-US&gl=US&ceid=US:en` (`when%3A120d` for the 13.1 cohort run, below) | `google_news_url`, `_poll_google_news` |
| Blog page | `GET https://{domain}/blog` | `discover_feed` (step 1) |
| Homepage | `GET https://{domain}/` | `discover_feed` (step 2) |
| Advertised feed | every `<link rel="alternate" type="application/rss+xml\|application/atom+xml" href=...>` on those two pages | `discover_feed` |
| Fallbacks | `/feed`, `/rss`, `/blog/rss.xml`, `/blog/feed` on `https://{domain}`, in that order | `discover_feed` (step 3) |
| Stored feed | `company_feeds.feed_url` with `If-None-Match` / `If-Modified-Since` | `_poll_stored_feed` |

**Status: undocumented.** Google publishes no API documentation for the News RSS search
feed. The `q` value is the company name in double quotes plus `when:7d`, encoded with
`quote_plus` (`"` is `%22`, a space is `+`, `:` is `%3A`, `&` in a name is `%26`). Items
carry `title` (headline, then ` - {publisher}`), `link` (a `news.google.com/rss/articles/...`
redirect, never the publisher URL), `pubDate` (RFC 822, GMT), `description` (an HTML
anchor repeating the headline; never read) and `<source url="...">publisher</source>`.

Discovery: the first answer that is a 200 and that `feedparser` reads with at least one
entry wins. Advertised feeds are fetched to confirm them. A feed whose URL, or whose
final URL after redirects, is not on the company's domain (or a subdomain) is ignored,
so every company-feed item is linked by rule R1. The winner is stored in `company_feeds`
with `discovered_from` (the page that advertised it, or the fallback URL itself), its
`ETag` and `Last-Modified`. Discovery runs at most once per company per 30 days
(`DISCOVERY_RETRY`, ADR 0032):

- a miss is never an error. Every probe answering without a feed (404s, HTML, a 5xx on
  one path) is `notes.site.no_feed`; a site that does not answer at all (DNS failure,
  refused or timed-out connection, TLS failure) or answers 429/503 ends its discovery at
  that first request and is `notes.site.unreachable`. Both put the company id in
  `runs.notes["no_feed"]`, which the next 30 days of runs read to skip discovery
  (`notes.site.discovery_not_due`). The run row's notes are refreshed while the run is
  open (every 20 companies and at the end), so a killed run still remembers its misses;
- a company with an `active` or `paused` feed is never rediscovered; one whose only feeds
  are `dead` is rediscovered 30 days after the feed was marked dead, and a rediscovered
  dead feed is set `active` again;
- a feed already stored for another company (two companies on one site) is read for
  this company too, but the company is remembered as a miss, since it owns no feed.

## Fields used

| Feed field (after `feedparser`) | Becomes |
| --- | --- |
| `title` | `payload.title` (whitespace collapsed) for funding, launch and expansion; for leadership the redacted headline (see below). The headline used for the name guard, classification and redaction drops a trailing ` - {source}` |
| `link` | `events.url`, `external_id`, and `payload.key = news:{sha256(url)}` |
| `published_parsed` (else `updated_parsed`, Atom) | `occurred_at` (UTC) |
| `source.title` (Google News `<source>`) | `payload.source`; for a company feed, the feed's own `<title>` (else the domain) |

Contract model: `NewsFeed {origin, feed_url, company {name, domain}, items[]}` with
`NewsItem {title, link, pub_date, source}`; `source` is required but may be `null`. The
drift fixture removes `pub_date`. A Google News response that fails the contract stops
the run with `status = 'failed'` and `notes.drift`. Company blog feeds are not a provider
contract: entries without a title, link or date are dropped and counted as skipped.

## Events

| Item | Value |
| --- | --- |
| `source` | `news` |
| `type` | `news_funding`, `news_leadership`, `news_launch`, `news_expansion` (SPEC section 13.1 counts `news_funding`) |
| `external_id` / `version` | the item URL / `"1"`, so `event_id = sha256("news\|<url>\|1")` |
| `occurred_at` / `observed_at` | `pubDate` / the run's `now` |
| `magnitude` | 1 |
| `company_id` | the company queried (Google News) or owning the feed |
| `payload` | exactly `title`, `title_redacted`, `source`, `matched_regex`, `category`, `query` (the request URL), `match`, `key` |
| `payload.title_redacted` | `true` for `news_leadership` (the title is redacted), `false` otherwise (the title is the feed's, unchanged) |
| `payload.match` | Google News: `{"rule": "news_query", "guard": "name_in_title"}`; own-domain feed: `{"rule": "R1", "confidence": 0.98}` |

**Freshness (ADR 0043).** `occurred_at` is the item's own `pubDate`, so it is the age of the
item, not tell's lag, whenever tell had not looked before: the feed's whole archive is read
when discovery finds a feed (posts years old), and a company's first Google News query
returns up to 7 days of items. Freshness leaves out company-feed items dated before
`company_feeds.created_at` and Google News items with no earlier query for the company
(`runs.notes.polled`), and measures a later Google News item from its `pubDate` or tell's
previous query for the company, whichever is later.

**Stored:** title (redacted for leadership), URL, pubDate, publisher name, the matching
rule. **Never stored:** the raw leadership headline, the `description`, article text,
author names, images, or anything fetched from the publisher's site (the publisher URL is
never followed).

## Leadership redaction (ADR 0016)

SPEC section 3.7 says to store the headline; the PII rule says no person's name in any
payload. Until Karan confirms, leadership headlines are stored through
`news_classify.redact_leadership`, a deterministic heuristic:

1. After `appoints`, `names`, `hires`, `promotes`, `taps`, `welcomes` (and `succeeds`,
   `succeeding`, `replaces`, `replacing`), the run of 1-4 capitalised tokens is the person,
   if it ends at a safe boundary: the end, ` as `, ` to `, ` its `, ` the ` (and similar
   words: `new`, `for`, `from`, `at`, ...), a role word (`Chief`, `CEO`, ...), `-`, `|` or
   a colon. Role and boundary words never join the run; initials (`Q.`, `Dr.`) do.
2. `X joins Acme as CFO`: the run of 1-4 capitalised tokens right before ` joins`.
3. A second name after `and` / `&`, or after a comma or semicolon once a verb was seen.
4. Each span becomes `[person]`; the rest of the headline is kept.
5. No span, or an undelimited run (a comma apposition, too many capitalised tokens):
   only the role phrase from the regex groups is kept (verb, `as`/`to`, qualifiers,
   role), else `<verb> [person]`.

| Headline (synthetic) | `payload.title` |
| --- | --- |
| Acme appoints {First Last} as chief revenue officer | Acme appoints [person] as chief revenue officer |
| {First Last} joins Acme as CFO | [person] joins Acme as CFO |
| Acme appoints {First Last} as CFO, {Other Name} as COO | Acme appoints [person] as CFO, [person] as COO |
| Acme appoints {First Last}, former rival executive, as CFO | appoints as CFO |
| Quillstone Analytics appoints new chief revenue officer | appoints new chief revenue officer |

The URL stays the receipt for the full headline. Placeholder names appear only in test
code strings, never in fixtures.

Name guard (Google News only): the company name is NFKC-normalized, casefolded, its
punctuation turned into spaces, and a trailing legal suffix (`inc`, `llc`, `ltd`,
`corp`, `co`, ...) dropped; it must appear as whole words in the equally normalized
headline. "Quillstone Ridge outfitter opens new office" is skipped for "Quillstone
Analytics" even though it would classify. Items failing the guard and items that do not
classify are counted in `skipped` and never stored. The same URL seen for two companies
is stored once (first writer wins, the second is skipped on conflict).

## Classification (`news_classify.py`)

Title regexes, case-insensitive, whitespace collapsed; first match wins, in this order:

| Category | Rule name | Pattern |
| --- | --- | --- |
| leadership | `leadership.appoints` | `\bappoints\b` |
| leadership | `leadership.names_as` | `\bnames\b.*\bas (?:(?:its\|their\|the\|a\|new) )*(?:chief\|vp\|vice president\|head of\|president)\b` |
| leadership | `leadership.joins_as` | `\bjoins\b.*\bas\b` |
| leadership | `leadership.hires_as` | `\bhires\b.*\bas\b` |
| leadership | `leadership.promotes_to` | `\bpromotes\b.*\bto\b` |
| funding | `funding.raises` | `\braises\b` |
| funding | `funding.series` | `\bseries [a-e]\b` |
| funding | `funding.seed` | `\bseed (?:round\|funding)\b` |
| funding | `funding.funding_round` | `\bfunding round\b` |
| funding | `funding.closes_round` | `\bcloses\b.*\bround\b` |
| funding | `funding.secures_dollars` | `\bsecures \$` |
| funding | `funding.secures_funding` | `\bsecures (?:[\w-]+ ){0,2}funding\b` |
| funding | `funding.gets_in_funding` | `\bgets \$\d+(?:[.,]\d+)?\s?(?:k\|m\|mn\|million\|b\|bn\|billion)? in (?:[\w-]+ )?funding\b` |
| funding | `funding.dollar_amount` | `\$\d+(?:\.\d+)?\s?(?:m\|million\|b\|billion) (?:round\|funding\|investment)\b` |
| funding | `funding.announces` | `\bannounces\b.*\b(?:funding\|round\|raise)\b` |
| funding | `funding.funding` | `\bfunding\b` (the SPEC's bare keyword; last, so the specific rules name the match) |
| launch | `launch.launches` | `\blaunches\b` |
| launch | `launch.introduces` | `\bintroduc(?:es\|ing)\b` |
| launch | `launch.unveils` | `\bunveils\b` |
| launch | `launch.debuts` | `\bdebuts\b` |
| launch | `launch.releases` | `\breleases\b` |
| launch | `launch.rolls_out` | `\brolls out\b` |
| expansion | `expansion.opens_office` | `\bopens\b.*\boffice\b` |
| expansion | `expansion.expands_to` | `\bexpands (?:to\|into)\b` |
| expansion | `expansion.new_office` | `\bnew (?:office\|headquarters)\b` |

Two small widenings of the orchestrator's rule list, both for real headline shapes:
`names ... as its new chief ...` (articles allowed between `as` and the title) and
`introducing` (the usual blog-post headline). Known false positives kept on purpose
(the rule list is a business rule; tightening it is a decision, not a fix): `raises`
also matches "raises prices" / "raises concerns", `releases` matches "releases
earnings"; the bare `funding` keyword also matches "cuts funding for ...", and
`announces ... round` matches "announces new round of layoffs". `tests/test_news_classify.py`
holds 62 labelled headlines (no person names), all of which must pass, plus 26 redaction
cases with placeholder names.

## Rate limit and politeness

- Two separate budgets. **Google News**: `TELL_RATE_NEWS_RPS` (default 1 request per
  second), one company at a time on the run's writer thread. **Company sites** (discovery
  and feed polls): `SITE_RPS = 1` request per second *per host* (`tell.http.HostRateBudgets`),
  `SITE_TIMEOUT_S = 10` seconds per phase (connect, read, ...) of each request, on
  `SITE_WORKERS = 6` worker threads, so a dead host or a slow blog no longer holds up Google News or the other sites. At most
  `SITE_WORKERS * SITE_BACKLOG` (12) companies' site work is outstanding; beyond that the
  Google News loop waits for one to finish. Workers never touch the database. Redirects
  are followed hop by hop (`SITE_MAX_REDIRECTS = 5`), each hop charged to its own host's
  1 rps budget, so a redirect to another host is never a free request there.
- Short transactions: `ingest()` commits the run row at once, then after every company
  (its Google News answer and any finished site work, each company's site writes in a
  savepoint), and before every wait for a site worker (a full backlog, the final drain),
  so no transaction is held across network I/O. The open run row's counters
  follow every commit and its notes (`polled`, `no_feed`, `site`) every 20 companies, so a
  killed run keeps both its events and its rotation and discovery memory. An unexpected
  exception closes the run `failed` with `notes.error`, keeping what was committed.
- **No retries anywhere** (`max_attempts=1`). A 429 or 503 from Google News is a block: the
  run stops at once, nothing else is requested, and the run finishes **`failed`** with one
  error and `runs.notes` `"blocked": true`, `blocked_by {status, url, company_id}` and
  `polled_count` (how many companies were polled before the block). Failing the run is what
  lets the SPEC section 11 dead-man (`tell deadman`, no successful run in 24 hours) notice a
  block that persists; a single block is followed by the next scheduled run as usual. The
  companies polled before the block keep their `polled` timestamps, so the next run does not
  query them again. `tell ingest --source news` nevertheless **exits 0** when the block is
  the run's only failure (`news.is_block_only_failure`: status `failed`, `notes.blocked`
  true, no `notes.drift`, no `notes.error`), so one 429/503 no longer turns the scheduled
  ingest job red; the run row stays `failed`, and `tell deadman` still alarms when news has
  no `ok` run in 24 hours. A contract failure (`drift`) or a crash (`error`) still exits 1. Other Google News failures (other 4xx/5xx, transport errors) count as
  an error for that company, which is not marked polled, and the run continues.
- Rotation (PLAN Phase 4, SPEC section 11), read from `runs.notes["polled"]`
  (`{company_id: timestamp}`) of the news runs started in the last 7 days, whatever their
  status:
  - watchlist members and companies with any event whose `occurred_at` is in the last 30
    days are polled **at most once per 24 hours** (nightly), not every run;
  - the long tail only on its weekday, `companies.id % 7 == now.weekday()` (UTC), and at
    most once per 7 days, so it is covered once a week;
  - both intervals take off one hour of slack (`SCHEDULE_SLACK`) so cron start jitter does
    not push a company a whole 6-hour run later each day;
  - explicit `company_ids` (a manual rerun) replace the rotation and only skip companies
    polled less than 6 hours ago.
- 6-hour feed cache: a company feed is not re-polled before
  `company_feeds.last_polled_at + 6h` (`notes.site.feeds_cached`). A feed answering 304
  costs nothing (`notes.site.not_modified`); 404/410 marks it `dead` (`notes.site.dead`);
  any other non-200 answer sets `last_polled_at` (slow down) and counts an error; no
  answer at all counts an error and leaves `last_polled_at` alone. Feed-poll failures are
  real errors: the feed was there before.
- Per-run budget: at most `MAX_GOOGLE_REQUESTS = 600` Google News requests (one per company
  polled, no retries), `MAX_COMPANIES = 1200` companies and `RUN_BUDGET = 25 minutes` of
  wall-clock time, all checked before each company. The request cap keeps the four scheduled
  runs a day at or under 2,400 Google News requests, inside the SPEC section 14 guidance of
  under 3,000 a day (600 requests at 1 rps is 10 minutes, well inside the 45-minute
  `filings` job, which runs EDGAR first). Order:
  watchlist members, then companies active in the last 30 days, then the long-tail bucket;
  least recently polled first within each group, so a capped run resumes where it stopped.
  A run that hits either limit stops cleanly, keeps everything it wrote and finishes `ok`;
  `runs.notes` has `selected` (companies due), `deferred` (due but not reached),
  `google_requests` (sent this run), `request_cap_reached`, `budget_exhausted` (the wall
  clock ran out) and `polled_count`. The limits are `collect()` / `ingest()` arguments
  (`max_requests`, `max_companies`, `budget`), not settings.
- Every request sends `User-Agent: tell/0.1 (+https://github.com/karank2512/tell-ai)`.

### Throughput (2026-10-14, ADR 0032)

The first production run polled 53 of 893 eligible companies in its 25 minutes. A
simulation with `respx` on a scaled clock (1 simulated second = 20 ms; every answer takes
0.3 s; per 10 companies 6 live sites without a feed, 2 dead hosts that hold each
connection for the client's timeout, 2 with a feed advertised on `/blog`; 2,000 companies
offered, no `MAX_COMPANIES` cap):

| Run | Companies in 25 min | `runs.errors` | Site outcomes |
| --- | ---: | ---: | --- |
| before (65c9edc: inline discovery, 20 s timeout, every probe tried) | 56 | 11 | 35 misses in `no_feed`; dead hosts counted as errors |
| after, first pass (discovery due everywhere) | 1,435 | 0 | 286 found, 863 no feed, 286 unreachable |
| after, next day (misses remembered) | 1,462 | 0 | 286 feeds polled, 1,149 discoveries not due |

After the change Google News at 1 request per second is the bound, so a scheduled run
reaches its request cap (`MAX_GOOGLE_REQUESTS = 600`, added 2026-10-14; before it, the
`MAX_COMPANIES = 1200` cap) well inside `RUN_BUDGET`.

## Cohort runs (SPEC 13.1 speed vs press)

The 13.1 report reads `news_funding` events dated from 14 days before to 60 days after each
Form D in its cohort. Scheduled runs only ask Google News for `when:7d`, so press about a
backfilled filing (`tell backfill formd`, `docs/sources/edgar.md`) is never collected by them.
The cohort run fills that gap:

```sh
uv run tell ingest --source news --cohort formd                 # when:120d, filings of the last 90 days
uv run tell ingest --source news --cohort formd --when-days 120 --cohort-days 90 --limit 1200
```

- `--when-days N` (1-365; `google_news_url(name, N)`) sets the `when:{N}d` window; the
  default is 7, or 120 with `--cohort`. `--cohort formd` polls `formd_cohort()`: companies
  linked (`company_id` set, so run `tell resolve` after a backfill) to a `form_d_filed`
  event filed in the last `--cohort-days` days (default 90), instead of the rotation.
- Every other rule is the scheduled one: name guard, classification, leadership redaction,
  the payload keys (`query` is the `when:120d` request URL), 1 rps, the block policy, the
  600-request, 1,200-company and 25-minute budgets (rerun to continue a capped run), the company-feed
  cache. Idempotent: one event per article URL, so a rerun inserts nothing new.
- `runs.notes` adds `when_days` to every news run, and `cohort`, `cohort_size`,
  `cohort_days` to cohort runs. The 6-hour "recently polled" skip only counts runs whose
  `when_days` is at least the one asked for (a run without the field counts as 7), so a
  scheduled 7-day poll an hour earlier never hides a company from the 120-day cohort run,
  while a cohort poll does satisfy a later narrower one. Cohort polls also count for the
  rotation (`notes.polled`).
- Events from a 120-day query are observed today but can be four months old: freshness
  counts them as backfills, never as tell's lag (`runs.notes.when_days` above 7, ADR 0043).
- Report dated in the past (`tell backtest speed-vs-press --as-of D`, e.g. when the newest
  posted Form D data set ends before the last 90 days): use `--cohort-days` = days from D-90
  to today, and `--when-days` = days from D-104 to today (capped at 365), so both the cohort
  and the press window `[filing - 14d, filing + 60d]` are covered.

## Legal note

The Google News feed is undocumented and its channel `<copyright>` says the feed is made
available for rendering results in a personal feed reader for personal, non-commercial use.
tell stores only the headline, the Google redirect URL, the date and the publisher name, and
never article text, but the terms question is open: **Karan decides** whether the Google
News half ships enabled (SPEC section 17 style decision). Company blog feeds are published
by the companies for syndication; only headlines, URLs and dates are kept.

## Fixtures (`tests/fixtures/news/`)

| File | What |
| --- | --- |
| `normal.xml` | Google News RSS for the fictional "Quillstone Analytics" (`quillstone.example`): 8 items, 6 classifiable across all four categories, 1 unclassifiable, 1 that fails the name guard |
| `empty.xml` | the same channel with zero items |
| `company_feed.xml` | the company's Atom blog feed: 5 entries (launch, expansion, funding, one unclassifiable, one without a date) |
| `blog.html` | a blog page advertising the Atom feed with `<link rel="alternate" type="application/atom+xml" href="/blog/atom.xml">` |
| `normal.json`, `empty.json` | the JSON rendering of `normal.xml` / `empty.xml` as `NewsFeed` (what the generic tests read) |
| `normal_company_feed.json` | the JSON rendering of the complete entries of `company_feed.xml` |
| `drift.json` | `normal.json` with `pub_date` removed from every item |

Provenance: **hand-crafted**, shaped on one live Google News response recorded on
2026-10-05 (`q="Ramp" when:7d`, one request with the project User-Agent). The live channel
and item structure (generator, image, `guid isPermaLink="false"`, escaped-HTML
`description`, `<source url>`) is kept; every headline, publisher, URL and id is invented,
no person is named, and the channel's `<webMaster>` element (it holds an address) and the
long copyright text are dropped. The company feed and blog page are hand-written to the
Atom 1.0 and HTML link-discovery conventions. The JSON renderings are generated from the
XML by `parse_feed_items` and a test asserts they still match.

## Sample response (trimmed)

```xml
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
  <generator>NFE/5.0</generator>
  <title>""Quillstone Analytics" when:7d" - Google News</title>
  <item>
    <title>Quillstone Analytics raises $24M Series A to expand forecasting platform - Ledger Daily</title>
    <link>https://news.google.com/rss/articles/CBMi...?oc=5</link>
    <guid isPermaLink="false">CBMi...</guid>
    <pubDate>Thu, 01 Oct 2026 13:05:00 GMT</pubDate>
    <description>&lt;a href="..."&gt;...&lt;/a&gt;</description>
    <source url="https://ledgerdaily.example">Ledger Daily</source>
  </item>
</channel></rss>
```
