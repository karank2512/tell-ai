# News and RSS (Google News + company blog feeds)

Spec: `docs/SPEC.md` sections 3.7, 8, 11 and 13.1. Collector: `tell/collectors/news.py`;
classifier: `tell/collectors/news_classify.py`. Tests: `tests/collectors/test_news.py`,
`tests/test_news_classify.py`, plus the generic `tests/test_contracts.py` and
`tests/test_idempotency.py`. Schema addition: `company_feeds`
(`docs/decisions/0014-company-feeds-table.md`). Leadership headlines are stored redacted
(`docs/decisions/0016-news-headline-redaction.md`, accepted). Entry point for the CLI:
`news.ingest()` (starts the run, calls `collect()`, finishes the run with the notes the
next run reads).

## Endpoints

| Call | URL | Used by |
| --- | --- | --- |
| Google News search | `GET https://news.google.com/rss/search?q=%22{company name}%22+when%3A7d&hl=en-US&gl=US&ceid=US:en` | `google_news_url`, `_poll_google_news` |
| Blog page | `GET https://{domain}/blog` | `discover_feed` (step 1) |
| Homepage | `GET https://{domain}/` | `discover_feed` (step 2) |
| Advertised feed | every `<link rel="alternate" type="application/rss+xml\|application/atom+xml" href=...>` on those two pages | `discover_feed` |
| Fallbacks | `/feed`, `/rss`, `/blog/rss.xml`, `/blog/feed` on `https://{domain}`, in that order | `discover_feed` (step 3) |
| Stored feed | `company_feeds.feed_url` with `If-None-Match` / `If-Modified-Since` | `_poll_feed` |

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
`ETag` and `Last-Modified`. A clean miss (every probe answered, none was a feed) is
recorded in `runs.notes["no_feed"]` and the domain is not probed again for 30 days; a
miss caused by a transport error or 5xx is counted as an error and retried next run.

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

- 1 request per second per host (`TELL_RATE_NEWS_RPS`, default 1) through
  `tell.http.HostRateBudgets`, so Google News and each company site have their own bucket.
- **No retries anywhere** (`max_attempts=1`). A 429 or 503 from Google News is a block: the
  run stops at once, nothing else is requested, and the run finishes **`failed`** with one
  error and `runs.notes` `"blocked": true`, `blocked_by {status, url, company_id}` and
  `polled_count` (how many companies were polled before the block). Failing the run is what
  lets the SPEC section 11 dead-man (`tell deadman`, no successful run in 24 hours) notice a
  block that persists; a single block is followed by the next scheduled run as usual. The
  companies polled before the block keep their `polled` timestamps, so the next run does not
  query them again. Other Google News failures (other 4xx/5xx, transport errors) count as
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
  `company_feeds.last_polled_at + 6h`. A feed answering 304 costs nothing; 404/410 marks it
  `dead`; 429/5xx sets `last_polled_at` (slow down) and counts an error.
- Per-run budget: at most `MAX_COMPANIES = 1200` companies (1,200 Google News queries at
  1 rps is 20 minutes, inside the 30-minute `filings` job with margin) and
  `RUN_BUDGET = 25 minutes` of wall-clock time, checked before each company. Order:
  watchlist members, then companies active in the last 30 days, then the long-tail bucket;
  least recently polled first within each group, so a capped run resumes where it stopped.
  A run that hits either limit stops cleanly, keeps everything it wrote and finishes `ok`;
  `runs.notes` has `selected` (companies due), `deferred` (due but not reached),
  `budget_exhausted` (the wall clock ran out) and `polled_count`. Both limits are
  `collect()` / `ingest()` arguments (`max_companies`, `budget`), not settings.
- Every request sends `User-Agent: tell/0.1 (+https://github.com/karank2512/tell-ai)`.

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
