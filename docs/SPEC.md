# tell — Build spec for the agent team

Oct 5, 2026 · @Karan

This tab is the implementation-grade companion to the plan: every endpoint, table, rule, workflow and number an AI coding agent needs to build the $0 version of tell without guessing, and the checklist Karan uses to review its work.

## 1. How the agent team works from this spec

Agents build, Karan reviews: every change ships as a pull request against `main` with a filled checklist, and nothing merges without the human review in section 16 for its milestone.

**Repos**

| Repo | Visibility | Holds |
| --- | --- | --- |
| `tell` | Public | Code, workflows, docs, fixtures, the spec copied in as `docs/SPEC.md` |
| `tell-data` | Public | Daily normalized events as JSONL plus the generated static site (GitHub Pages); nothing personal, nothing raw beyond postings metadata and filing metadata |
| `tell-private` | Private | Watchlists from design partners, Slack webhook config, the gold set; the only repo with customer input |

**Rules for every PR**

1. One ticket per PR; the ticket names the acceptance test it closes (section 12).
2. `ruff`, `mypy --strict` on `tell/`, and `pytest` green in CI; no `# type: ignore` without a comment.
3. No network calls in tests: adapters are tested against recorded fixtures under `tests/fixtures/<source>/`.
4. Any new source ships with: fixture, schema-contract test, idempotency test, a `docs/sources/<source>.md` page with endpoint, rate limit, legal note and sample response.
5. Any change to scoring ships with the explainability test passing and a one-line entry in `CHANGELOG.md` saying what moved and why.
6. Secrets only via GitHub Actions secrets; `.env.example` lists every variable with a comment; the agent never commits a value.
7. No new paid dependency, no new hosted service, no credit card anywhere; a PR that needs one stops and asks.

**Definition of done for a milestone**

- The milestone's acceptance tests pass in CI on `main`.
- The nightly workflow has run unattended for the number of days the milestone states.
- The run-health page shows every source green or explicitly paused.
- Karan's review checklist (section 16) is signed off in the PR description.

**Agent conventions (`CLAUDE.md` at the repo root)**

- Read `docs/SPEC.md` and the relevant `docs/sources/*.md` before touching a source.
- Prefer boring code: standard library, `httpx`, `pydantic`, `sqlalchemy`, `alembic`, `rapidfuzz`, `feedparser`, `lxml`; no new framework without a note in `docs/decisions/`.
- When a provider's response differs from the fixture, record a new fixture, update the contract test, and say so in the PR; never patch around it silently.
- Log every run as one JSON line per source: `run_id`, `source`, `started_at`, `finished_at`, `fetched`, `inserted`, `skipped`, `errors`.
- Ask before any decision in section 17; never guess a business rule.

## 2. Repo layout, tooling, configuration

One Python package, one database, one static site: the `tell` repo is a monolith with a `tell/` package, workflows under `.github/`, and everything the agents need to know under `docs/`.

```
tell/
  pyproject.toml            # uv-managed; python = ">=3.12"
  CLAUDE.md                 # agent conventions (section 1)
  .env.example
  .github/workflows/
    ci.yml                  # lint, type-check, tests on every PR
    ingest.yml              # cron: collectors, resolution, scoring, publish
    weekly.yml              # cron: weekly page and backtest refresh
  alembic/                  # migrations; one file per PR that touches schema
  tell/
    config.py               # pydantic-settings; every env var typed
    db.py                   # engine, session, SKIP LOCKED helpers
    models.py               # SQLAlchemy models = the DDL in section 5
    collectors/
      base.py               # Collector protocol, Event dataclass, idempotency key
      greenhouse.py  lever.py  ashby.py  workday.py  oracle.py  smartrecruiters.py
      edgar.py              # daily index + Form D XML
      news.py               # Google News RSS + company feeds
      github_org.py         # v1.1
    discovery/ats.py        # domain -> board discovery (section 4)
    resolve/                # entity resolution (section 6)
      normalize.py  match.py  review.py
    signals/
      taxonomy.py           # role families (section 7)
      velocity.py           # fingerprints, reposts, baselines
      scoring.py            # weights, decay, explain (section 8)
    publish/
      site.py               # static JSON + HTML for tell-data (section 9)
      slack.py              # Block Kit digest
      csv_export.py         # the "50 domains" deliverable
    api/app.py              # optional FastAPI (section 10)
    mcp/server.py           # tell-mcp, stdio (section 10)
    cli.py                  # `tell ingest --source greenhouse`, `tell score`, `tell publish`
  tests/
    fixtures/<source>/      # recorded responses, one per fixture case
    test_contracts.py  test_idempotency.py  test_scoring.py  test_resolve.py
  docs/
    SPEC.md  sources/  decisions/  methods/
```

**Tooling**

| Tool | Why |
| --- | --- |
| `uv` | Fast installs, lockfile, `uvx tell-mcp` for the MCP server |
| `ruff` (lint + format), `mypy --strict` | Agents produce more code than a human reviews; the linters are the first reviewer |
| `pytest`, `respx` (httpx mocking), `freezegun` | Recorded-fixture tests with frozen clocks so decay math is deterministic |
| `httpx` with a shared client: 20s timeout, `Retry-After` honoured, exponential backoff 1s, 2s, 4s, 8s, max 4 tries on 429 and 5xx | One place for politeness |
| `pydantic` v2 models for every provider response | The contract test is the model: a missing field fails CI |
| `rapidfuzz`, `tldextract`, `feedparser`, `lxml` | Matching, domain parsing, RSS, Form D XML |
| `alembic` | Every schema change is a migration; CI runs `alembic upgrade head` against a scratch Neon branch |
| `pre-commit` | ruff, mypy, a secret scanner (`gitleaks`) |

**Configuration (`.env.example`, all read by `tell/config.py`)**

| Variable | Example | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://...neon.tech/tell?sslmode=require` | Neon pooled connection string |
| `SEC_USER_AGENT` | `tell/0.1 (karan@<domain>)` | Required by the SEC; a real contact address |
| `TELL_HTTP_USER_AGENT` | `tell/0.1 (+https://github.com/<org>/tell)` | Sent to every other source |
| `SLACK_WEBHOOK_URL` |  | Per workspace; stored only as an Actions secret |
| `GITHUB_TOKEN` | provided by Actions | For the GitHub collector and for pushing to `tell-data` |
| `TELL_DATA_REPO` | `<org>/tell-data` | Where JSONL and the site are pushed |
| `TELL_POLL_CONCURRENCY` | `8` | Parallel board fetches |
| `TELL_RATE_EDGAR_RPS` | `5` | Half the SEC ceiling |
| `TELL_RATE_NEWS_RPS` | `1` | Google News is undocumented; stay slow |

**Secrets policy.** Only `DATABASE_URL`, `SLACK_WEBHOOK_URL` and a `TELL_DATA_PUSH_TOKEN` (fine-grained PAT, contents write, one repo) exist as secrets. Design-partner watchlists live in `tell-private`, read at run time by a job with a read-only deploy key.

## 3. Data sources, one by one

Every v0 source is a documented public endpoint or a public-domain government file; the two undocumented ATS vendors (Workday, Oracle) ship behind a feature flag with stricter politeness and are the first to be paused if anything looks off.

**3.1 Greenhouse Job Board API** (documented at developers.greenhouse.io/job-board.html; GET endpoints need no key)

| Item | Detail |
| --- | --- |
| List jobs | `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true` |
| Board token | The slug in `boards.greenhouse.io/{token}` or `job-boards.greenhouse.io/{token}` |
| Fields used | `id`, `title`, `location.name`, `absolute_url`, `first_published` (ISO 8601), `updated_at`, `departments[].name`, `offices[].name`, `content` (HTML, entity-encoded; decode once), `metadata[]` (often a "Workplace Type") |
| Gotchas | `departments` is only present with `content=true`; `content` is double-encoded HTML; a board that 404s is usually a renamed token, so re-run discovery before marking dead |
| Rate limit | None published; poll each board at most every 6 hours, 8 boards in parallel, back off on 429 |
| Legal note | Greenhouse states the data is public and read access needs no authentication; the only automated-access clause found binds job-seeker accounts, not the feed |
| Idempotency key | `greenhouse:{board_token}:{job.id}:{updated_at}` |
| Fixture | `tests/fixtures/greenhouse/stripe.json` (recorded once, trimmed to 5 jobs) |

**3.2 Lever Postings API** (documented at github.com/lever/postings-api)

| Item | Detail |
| --- | --- |
| List postings | `GET https://api.lever.co/v0/postings/{site}?mode=json` (EU tenants: `https://api.eu.lever.co/v0/postings/{site}?mode=json`) |
| Site | The slug in `jobs.lever.co/{site}` |
| Fields used | `id`, `text` (title), `categories.team`, `categories.department`, `categories.location`, `categories.commitment`, `workplaceType`, `createdAt` (epoch ms), `hostedUrl`, `descriptionPlain`, `salaryRange` when present |
| Legal note | Lever's docs say published postings are publicly viewable and may be scraped by third parties; its site terms forbid scraping the app, which this does not touch |
| Idempotency key | `lever:{site}:{id}:{createdAt}` |

**3.3 Ashby Job Posting API** (documented at developers.ashbyhq.com/docs/public-job-posting-api)

| Item | Detail |
| --- | --- |
| List postings | `GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}?includeCompensation=true` |
| Name | The slug in `jobs.ashbyhq.com/{name}` |
| Fields used | `jobs[].id`, `title`, `department`, `team`, `employmentType`, `location`, `secondaryLocations[]`, `isRemote`, `workplaceType`, `publishedAt`, `isListed`, `jobUrl`, `applyUrl`, `descriptionPlain`, `compensation` |
| Idempotency key | `ashby:{name}:{id}:{publishedAt}` |

**3.4 SmartRecruiters Posting API** (documented public API; add in week 3)

| Item | Detail |
| --- | --- |
| List postings | `GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings?limit=100&offset=0`, paginate on `totalFound` |
| Fields used | `content[].id`, `name`, `releasedDate`, `location.city`, `location.country`, `department.label`, `function.label`, `ref` (detail URL) |
| Note | Posting detail (`/postings/{id}`) is a second call; fetch only for new ids |

**3.5 Workday and Oracle career sites** (undocumented; flag `TELL_ENABLE_UNDOCUMENTED_ATS`, default off in CI, on in production only after Karan signs off)

| Item | Detail |
| --- | --- |
| Workday | `POST https://{tenant}.{wd1..wd5}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with JSON `{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}`; response `jobPostings[]` with `title`, `externalPath`, `locationsText`, `postedOn` ("Posted 3 Days Ago" style, parse relative), `bulletFields[]` (req id) |
| Oracle Cloud HCM | `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=CX_1,limit=100,offset=0`; `items[0].requisitionList[]` with `Id`, `Title`, `PostedDate`, `PrimaryLocation` |
| Politeness | Check `robots.txt` on the host before the first call and daily after; 1 request per second per host; stop on any 403 and open a review item |
| Legal note | These are the career site's own XHR endpoints, not a published API; the scanner already uses them, but the company version treats them as the first thing to drop if a vendor objects |
| Idempotency key | `workday:{tenant}:{req_id}` and `oracle:{host}:{Id}` plus the posted date |

**3.6 SEC EDGAR** (public domain; fair-access rules at sec.gov/os/accessing-edgar-data: max 10 requests per second, declare a `User-Agent` with contact info; the collector runs at 5 per second)

| Item | Detail |
| --- | --- |
| New filings each day | `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx`, filter form types `D`, `D/A`, `8-K`, `S-1` |
| Per-company history | `https://data.sec.gov/submissions/CIK{10 digits, zero-padded}.json` |
| Form D structured fields | Filing folder `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/primary_doc.xml`: `primaryIssuer` (`entityName`, `cik`, `issuerAddress` city, state, zip, `jurisdictionOfInc`, `yearOfInc`, `entityType`), `offeringData.typeOfFiling.newOrAmendment.isAmendment`, `offeringData.typeOfFiling.dateOfFirstSale`, `industryGroup.industryGroupType`, `offeringSalesAmounts.totalOfferingAmount`, `totalAmountSold`, `totalRemaining`, `investors.totalNumberAlreadyInvested`, `federalExemptionsExclusions` |
| Do not store | `relatedPersonsList` (officer and director names and addresses): personal data, not needed for the signal |
| Backfill for the backtest | Quarterly Form D data sets (TSV in ZIP) at sec.gov/dera/data/form-d, posted through 2026 Q1 as of October 2026; the current quarter comes from the daily index |
| 8-K items to flag | Item 5.02 (officer or director change), 1.01 (material agreement), 2.01 (acquisition completed); public companies only, so low volume for tell's targets |
| CIK to ticker | `https://www.sec.gov/files/company_tickers_exchange.json` |
| Full-text search (ad hoc) | `https://efts.sec.gov/LATEST/search-index?q="{name}"&forms=D` for verification, not for polling |
| Idempotency key | `edgar:{accession_number}`; amendments (`D/A`) attach to the original by `fileNumber` and do not create a second funding event |

**3.7 News and RSS**

| Item | Detail |
| --- | --- |
| Google News RSS | `https://news.google.com/rss/search?q=%22{company name}%22+when%3A7d&hl=en-US&gl=US&ceid=US:en`; items carry `title`, `link` (a news.google.com redirect), `pubDate`, `source` |
| Company feeds | Discover from the company's blog page: `<link rel="alternate" type="application/rss+xml">`, then fall back to `/feed`, `/rss`, `/blog/rss.xml`, `/blog/feed` |
| Classification | Title regexes first (`raises`, `series [a-e]`, `funding`, `launches`, `announces`, `appoints`, \`names .\* as (chief |
| Store | Title, URL, `pubDate`, source name, matched regex; never full article text |
| Rate | 1 request per second, cached 6 hours; the feed is undocumented, so treat a block as a signal to slow down, not retry |
| Idempotency key | `news:{sha256(url)}` |

**3.8 GitHub (v1.1, devtools ICPs)**

| Item | Detail |
| --- | --- |
| Org repos | `GET https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=100` |
| Releases | `GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=20` |
| Rate limit | 5,000 requests per hour with a token; 60 unauthenticated; conditional requests with `If-None-Match` returning 304 do not count |
| Idempotency key | `github:{repo}:{release.id}` and `github:{org}:{repo}:created` |

**3.9 Government contracts (v2)**: USAspending API `POST https://api.usaspending.gov/api/v2/search/spending_by_award/` filtered by recipient name; SAM.gov entity API requires a free key. Out of scope until a public-sector seller asks.

**Fixture discipline.** Each source directory under `tests/fixtures/` holds at least three recorded cases: `normal`, `empty_board`, and one `drift` case that deliberately omits a field so the contract test proves it fails loudly. Fixtures are trimmed by hand to under 50 KB and scrubbed of anything personal.

## 4. ATS discovery: domain to job board

The watchlist is a list of domains, and nothing in the ATS APIs maps a domain to a board token, so discovery is its own job: given `acme.com`, find the board or record that none was found.

**Procedure (`tell discover --domain acme.com`)**

1. Fetch `https://acme.com/careers`, `/jobs`, `/join`, `/company/careers`, and the homepage, in that order, stopping at the first 200; follow at most 2 redirects; honour `robots.txt`.
2. Scan the HTML (and any `<iframe src>`, `<script src>`, `<a href>`) for the known patterns:
   - `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}`, `boards-api.greenhouse.io/v1/boards/{token}`, `grnh.se/{token}`
   - `jobs.lever.co/{site}`, `jobs.eu.lever.co/{site}`
   - `jobs.ashbyhq.com/{name}`, `api.ashbyhq.com/posting-api/job-board/{name}`
   - `{tenant}.wd{1-5}.myworkdayjobs.com/{site}`
   - `careers.smartrecruiters.com/{company}`, `apply.workable.com/{sub}`, `{host}/hcmUI/CandidateExperience` (Oracle)
3. If nothing is found, try the slug guesses derived from the domain label (`acme`, `acmeinc`, `acme-inc`) against each public API with a HEAD or a small GET; a 200 with at least one job counts; record the guess as `confidence = 0.6` and the page link as `0.95`.
4. Verify the hit belongs to the domain: the board's company name (Greenhouse `/boards/{token}` returns `name`; Ashby and Lever expose it in the page title) must fuzzy-match the domain label or the company name on the watchlist at ratio 85 or better; otherwise queue for review.
5. Write a row to `ats_boards` (section 5) with `vendor`, `identifier`, `discovered_from` (URL), `confidence`, `last_verified_at`; re-verify every 30 days and whenever a board returns 404 twice in a row.

**Outputs and limits.** Expect 60 to 75 percent of venture-backed US software companies to resolve to one of the three documented vendors; log the rest as `no_board` with the pages checked so the number is reportable ("coverage"). One discovery pass over 5,000 domains is about 15,000 requests; run it at 4 per second from Actions and spread it over the first week.

**Seed lists for the first 5,000 domains.** Every YC company (the public company directory lists domain and batch), every company on Karan's own target list, and the design partners' CSVs; no purchased lists.

## 5. Database schema and idempotency rules

Eleven tables, every write an upsert on a natural key, so a rerun of any job changes nothing; the DDL below is the source of truth and `tell/models.py` must match it field for field.

```sql
-- companies: one row per resolved company, keyed by domain
create table companies (
  id            bigserial primary key,
  domain        text not null unique,          -- registrable domain, lowercase, no www
  name          text not null,                 -- display name
  legal_name    text,                          -- from Form D entityName when matched
  cik           text unique,                   -- 10-digit, zero-padded
  hq_city       text, hq_state text, hq_country text,
  github_org    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table company_aliases (
  company_id    bigint not null references companies(id),
  alias         text not null,                 -- normalized form (section 6)
  source        text not null,                 -- 'ats_board','edgar','news','manual'
  primary key (company_id, alias)
);

create table ats_boards (
  id               bigserial primary key,
  company_id       bigint references companies(id),
  vendor           text not null,              -- greenhouse|lever|ashby|smartrecruiters|workday|oracle
  identifier       text not null,              -- token / site / name / tenant+site / host
  discovered_from  text,
  confidence       numeric(3,2) not null,
  status           text not null default 'active', -- active|no_board|dead|paused
  last_verified_at timestamptz,
  last_polled_at   timestamptz,
  unique (vendor, identifier)
);

-- postings: current state of every posting ever seen
create table postings (
  id               bigserial primary key,
  board_id         bigint not null references ats_boards(id),
  external_id      text not null,              -- vendor's job id
  fingerprint      text not null,              -- section 7
  title            text not null,
  title_norm       text not null,
  role_family      text not null,              -- section 7 taxonomy
  department       text, location text, remote boolean,
  posted_at        timestamptz,                -- first_published / createdAt / publishedAt
  first_seen_at    timestamptz not null,
  last_seen_at     timestamptz not null,
  closed_at        timestamptz,                -- set when absent from two consecutive polls
  repost_of        bigint references postings(id),
  url              text not null,
  content_hash     text,                       -- sha256 of descriptionPlain, for tech keywords
  unique (board_id, external_id)
);
create index on postings (board_id, fingerprint);
create index on postings (role_family, posted_at);

-- posting_observations: one row per poll per posting; the audit trail for velocity
create table posting_observations (
  posting_id   bigint not null references postings(id),
  observed_at  timestamptz not null,
  run_id       text not null,
  primary key (posting_id, observed_at)
);

-- events: the one shape every source becomes (plan tab, section 8)
create table events (
  event_id      text primary key,              -- sha256(source|external_id|version)
  company_id    bigint references companies(id),
  source        text not null,
  type          text not null,                 -- job_opened, job_closed, form_d_filed, ...
  occurred_at   timestamptz not null,
  observed_at   timestamptz not null,
  magnitude     numeric not null default 1,
  url           text not null,
  payload       jsonb not null,                -- source fields kept, no PII
  raw_ref       text,                          -- tell-data path of the JSONL line
  run_id        text not null
);
create index on events (company_id, occurred_at desc);
create index on events (type, occurred_at desc);

create table signals (
  id            bigserial primary key,
  company_id    bigint not null references companies(id),
  event_id      text not null references events(event_id),
  signal_type   text not null,                 -- hiring_velocity, funding, leadership, launch, tech_adoption
  role_family   text,
  weight        numeric not null,              -- w_k at creation, per ICP profile 'default'
  magnitude     numeric not null,
  half_life_days integer not null,
  starts_at     timestamptz not null,
  unique (event_id, signal_type, role_family)
);

create table scores (
  company_id    bigint not null references companies(id),
  icp_profile   text not null,
  as_of         date not null,
  score         numeric not null,
  contributions jsonb not null,                -- [{signal_id, contribution, url, occurred_at}]
  primary key (company_id, icp_profile, as_of)
);

create table watchlists (
  id        bigserial primary key,
  owner     text not null,                     -- email or handle, stored in tell-private only
  name      text not null,
  icp_profile text not null default 'default'
);
create table watchlist_items (
  watchlist_id bigint not null references watchlists(id),
  company_id   bigint not null references companies(id),
  primary key (watchlist_id, company_id)
);

create table feedback (
  id           bigserial primary key,
  watchlist_id bigint not null references watchlists(id),
  signal_id    bigint not null references signals(id),
  verdict      text not null,                  -- useful|not_useful|already_knew|worked|booked|won
  note         text,
  created_at   timestamptz not null default now()
);

create table runs (
  run_id       text primary key,
  source       text not null,
  started_at   timestamptz not null,
  finished_at  timestamptz,
  fetched      integer default 0, inserted integer default 0, skipped integer default 0,
  errors       integer default 0,
  status       text not null,                  -- running|ok|failed
  notes        jsonb
);

create table review_queue (
  id          bigserial primary key,
  kind        text not null,                   -- entity_match|board_verify|news_classify
  payload     jsonb not null,
  status      text not null default 'open',    -- open|resolved|rejected
  created_at  timestamptz not null default now(),
  resolved_at timestamptz
);
```

**Idempotency rules the tests enforce**

1. `events.event_id` is `sha256(f"{source}|{external_id}|{version}")` where `version` is the provider's own change marker (`updated_at`, `createdAt`, accession number); re-fetching an unchanged posting produces the same key and `ON CONFLICT DO NOTHING`.
2. A posting's state changes (title edit, location change) create a new `events` row of type `job_updated`, never a second `job_opened`; `job_opened` is keyed on the first `posted_at` seen.
3. `job_closed` fires only after two consecutive polls without the posting, at least 12 hours apart, to survive a provider's transient empty response.
4. Form D amendments (`D/A`) update the original `form_d_filed` event's payload (amounts) and never create a new funding signal.
5. The idempotency test runs every collector twice against the same fixture and asserts `count(*)` on `events`, `postings`, `signals` is unchanged after the second run.

**Size budget on Neon's 1 GB.** A posting row is about 600 bytes, an event about 900 bytes with payload. 25,000 companies at an average of 12 open postings is 300,000 postings (about 180 MB) and roughly 5,000 events a day (about 4.5 MB a day, 1.6 GB a year), so prune `posting_observations` to 90 days and move events older than 180 days to the JSONL archive in `tell-data`, which is their system of record anyway.

## 6. Entity resolution

The domain is the identity; every other source has to earn its link to a domain through rules that are deterministic, logged, and testable against a hand-labelled gold set.

**Normalization (`resolve/normalize.py`)**, applied to every name before comparison, in this order:

1. Unicode NFKC, lowercase, strip accents.
2. Remove legal suffixes at the end: `inc`, `inc.`, `incorporated`, `llc`, `l.l.c.`, `ltd`, `limited`, `corp`, `corporation`, `co`, `company`, `plc`, `gmbh`, `s.a.`, `pbc`, `holdings`, `labs` only when preceded by another word.
3. Remove punctuation except internal hyphens; collapse whitespace; drop a leading `the`.
4. Keep a second form with digits spelled (`3` to `three`) and common tokens expanded (`ai` kept, `tech`/`technologies` unified to `technologies`).
5. Domain label: `tldextract(domain).domain`, with the same punctuation rules.

**Match rules (`resolve/match.py`)**, evaluated top to bottom; the first rule that fires sets `confidence`:

| Rule | Condition | Confidence | Outcome |
| --- | --- | --- | --- |
| R1 domain | Source provides a URL on the watched domain (ATS board found via discovery on that domain; company RSS on the domain) | 0.98 | auto-link |
| R2 exact alias | Normalized name equals an existing `company_aliases.alias` | 0.95 | auto-link |
| R3 name plus geography | `rapidfuzz.fuzz.token_set_ratio(norm_name, alias) >= 92` and the Form D issuer state equals `hq_state` (or city matches when state is missing) | 0.90 | auto-link, log |
| R4 name only, unique | ratio >= 95 and exactly one candidate company in the whole table | 0.85 | auto-link, log |
| R5 ambiguous | ratio >= 85 with two or more candidates, or 85 to 91 with one | 0.60 | review queue |
| R6 none | otherwise | 0 | unlinked event (kept, `company_id` null) |

Every decision writes `{rule, ratio, candidates[:5]}` into the event payload so a wrong link can be audited. Form D also gives `issuerPreviousNameList`; each previous name becomes an alias when a filing is linked, which is how renamed companies keep their history.

**Review queue.** `review_queue.kind = 'entity_match'` rows are resolved by Karan in a small Streamlit page (or a CSV round-trip) with three buttons: link, create new company, reject. A resolved item writes an alias so the same pair never comes back. Target: under 20 open items a week by week 4; if higher, tighten R5's lower bound.

**Gold set and the metric.** `tell-private/gold/companies.csv` holds 300 companies with domain, legal name, CIK (when any), and board identifier, labelled by hand from YC's directory and Karan's target list. `tests/test_resolve.py` runs the matcher over the gold set's names and reports precision (correct links over asserted links) and recall (links found over links that exist). Release gate: precision at least 0.97, recall at least 0.85; the numbers go into the README verbatim.

**What is deliberately not done.** No LinkedIn lookups, no paid enrichment, no LLM in the auto-link path. An LLM may propose answers for the review queue in v1.1, with the human still clicking the button.

## 7. Role-family taxonomy and hiring velocity

Hiring velocity is the headline signal and the easiest one to get wrong, so it is defined as net-new requisition fingerprints per role family over a 7-day window, against the company's own 28-day baseline, after reposts and stale requisitions are removed.

**Role families (`signals/taxonomy.py`)**: title is lower-cased and matched against ordered keyword lists; the first family that matches wins; `other` if none.

| Family | Title contains (any) | Department hint |
| --- | --- | --- |
| `sales` | account executive, ae, sdr, bdr, sales development, business development rep, account manager, enterprise sales, sales engineer, solutions engineer, revenue | sales |
| `gtm_ops` | revops, revenue operations, sales operations, gtm engineer, growth engineer, marketing operations, demand gen, lifecycle | marketing, operations |
| `marketing` | marketing, content, brand, product marketing, seo, events | marketing |
| `customer` | customer success, csm, support, implementation, onboarding, solutions architect, forward deployed | customer, support |
| `engineering` | software engineer, backend, frontend, full stack, platform, infrastructure, devops, sre, security engineer, mobile | engineering |
| `ml_ai` | machine learning, ml engineer, ai engineer, research scientist, applied scientist, data scientist, nlp, llm | research, data |
| `data` | data engineer, analytics engineer, data analyst, bi | data |
| `product_design` | product manager, product designer, ux, design | product |
| `finance_ops` | finance, accounting, controller, people, recruiter, talent, hr, legal, operations manager, chief of staff | g&a |
| `exec` | chief, vp, vice president, head of, director (only when none of the above matched first) |  |

Rules: `sales engineer` and `solutions engineer` go to `sales` on purpose (they are bought-for roles); `intern` and `contract` postings are tagged `is_intern` and excluded from velocity; titles in languages other than English fall to `other` until a translation table exists.

**Requisition fingerprint.** `sha1(title_norm | department_norm | location_norm | remote_flag)` where `title_norm` strips seniority tokens (`senior`, `sr`, `staff`, `principal`, `lead`, `ii`, `iii`, roman numerals) and `location_norm` keeps city and country only. Two postings with the same fingerprint on one board within 30 days are one requisition.

**Repost detection.** When a fingerprint that was `closed_at` within the last 30 days reappears, the new posting gets `repost_of` set and emits `job_reposted`, not `job_opened`; reposts do not count toward velocity. A posting whose `external_id` changes but whose fingerprint and `content_hash` are identical within 7 days is also a repost.

**Stale requisitions.** A posting open more than 90 days gets weight 0.25 in any magnitude it feeds and is listed in the account page under "long-open roles"; over 180 days it is excluded. Job-seeker thumbs ("this looks like a ghost job") feed a `ghost_votes` count that will inform the threshold later; it does not change weights until there are 500 votes.

**Velocity.** For company c, family f, day t:

- `new7(c,f,t)` = count of `job_opened` events with `role_family = f` in `(t-7d, t]`, excluding reposts and interns.
- `base(c,f,t)` = mean of `new7` over the four non-overlapping 7-day windows in `(t-35d, t-7d]`; floor at 0.5 so a company with no history does not divide by zero.
- `ratio = new7 / base`; a signal fires when `new7 >= 3` and `ratio >= 1.5`, or `new7 >= 2` for `sales`, `gtm_ops` and `customer` at companies with fewer than 50 open postings.
- `magnitude = min(new7, 10)`; the cap stops a 500-posting board from drowning everything.
- The signal's `url` is the board URL; the account page lists each counted posting with its own URL, so the receipt is the list.

**Tech keywords (`tech_adoption`, light version).** A keyword table (`kubernetes`, `snowflake`, `salesforce`, `hubspot`, `clay`, `dbt`, `rust`, `pytorch`, `vllm`, `temporal`, and so on, maintained in `docs/methods/tech-keywords.md`) is matched against `descriptionPlain`; a keyword that appears in a company's postings for the first time emits `tech_mentioned_first_time` with the posting as the receipt. Useful for devtools and data ICPs; weight 0 in the default profile.

**Methods note.** `docs/methods/hiring-velocity.md` records the definitions above plus measured numbers: share of postings flagged as reposts, median posting lifetime per family, share of boards with any posting over 180 days. These numbers are the launch content.

## 8. Signals, weights, decay, scoring

A score is a sum of decayed contributions, one per signal, computed from `signals` rows at scoring time; nothing is ever stored that cannot be rebuilt from events, and the test suite proves the contributions add up to the score to the cent.

**Signal types and defaults (`default` ICP profile)**

| `signal_type` | Fires on | Magnitude | Weight `w` | Half-life (days) | Receipt |
| --- | --- | --- | --- | --- | --- |
| `hiring_velocity` | Velocity rule in section 7, per role family | `min(new7, 10)` | 1.0 for the profile's target families, 0.3 for others | 30 | Board URL plus the counted postings |
| `funding` | `form_d_filed` (new, not amendment) | `log10(totalOfferingAmount)` capped at 8, or 6 if amount undisclosed | 4.0 | 90 | Filing index URL |
| `leadership` | News regex `appoints`, `names .* as`, `joins as`; 8-K Item 5.02 | 1 | 2.0 | 60 | Article or filing URL |
| `launch` | News regex `launches`, `introduces`, `unveils`; GitHub release on a flagship repo | 1 | 1.5 | 45 | Article or release URL |
| `expansion` | News regex `opens .* office`, `expands to` | 1 | 1.0 | 60 | Article URL |
| `tech_adoption` | `tech_mentioned_first_time` | 1 | 0 (profile-specific) | 90 | Posting URL |

Half-lives and weights are the July blueprint's defaults and are tuning starts, changed only through `docs/decisions/` with pilot feedback attached.

**ICP profiles (`profiles/*.yaml`)**, one per design partner, checked into `tell-private`:

```yaml
name: sells-to-sales-teams
target_families: [sales, gtm_ops, customer]   # weight 1.0 on hiring_velocity
other_families_weight: 0.3
weights: { funding: 4.0, leadership: 2.0, launch: 1.5, expansion: 1.0, tech_adoption: 0 }
half_life_days: { hiring_velocity: 30, funding: 90, leadership: 60, launch: 45, expansion: 60, tech_adoption: 90 }
tech_keywords: []                               # devtools profiles list the keywords that count
size_band: { min_open_postings: 3, max_open_postings: 400 }
```

**Scoring (`signals/scoring.py`)**

```python
def contribution(sig, profile, t):
    w = profile.weight_for(sig)  # family-aware for hiring_velocity
    hl = profile.half_life_days[sig.signal_type]
    age_days = (t - sig.starts_at).total_seconds() / 86400
    if age_days < 0 or age_days > 4 * hl:  # beyond four half-lives: zero, and dropped from the list
        return 0.0
    return w * float(sig.magnitude) * 0.5 ** (age_days / hl)


def score(company_id, profile, t):
    parts = [(s, contribution(s, profile, t)) for s in active_signals(company_id, t)]
    parts = [(s, c) for s, c in parts if c > 0]
    return sum(c for _, c in parts), parts  # stored together in scores.contributions
```

Display uses the raw sum rounded to one decimal; a 0 to 100 "heat" is computed per watchlist as a percentile rank of raw scores and shown beside it, so the number a rep sees is comparable within their list while the raw sum stays explainable.

**Daily job.** `tell score --profile <name> --as-of <date>` recomputes every company on each watchlist for that profile, writes `scores`, and emits `score_crossed_threshold` when a company's raw score crosses the profile's alert threshold (default 6.0, roughly one funding event or two fresh velocity signals) upward within the last day; the Slack digest and webhooks key off that event.

**Explainability tests (`tests/test_scoring.py`)**

1. Sum test: for 200 random companies and 5 random dates, `abs(score - sum(contributions)) < 1e-9`.
2. Monotone decay: a contribution at `t+1d` is less than or equal to the contribution at `t`.
3. Half-life: a contribution at `t + hl` equals half the contribution at `t`, within 1e-9.
4. No orphan contributions: every contribution references a `signals` row that references an `events` row with a non-empty `url`.
5. Profile isolation: changing weights in profile A leaves profile B's scores unchanged.
6. Threshold hysteresis: a score oscillating around 6.0 by less than 0.5 does not emit more than one `score_crossed_threshold` in 7 days.

**Feedback loop (v1).** `feedback.verdict` values map to a label per signal: `useful` and `worked|booked|won` are positives, `not_useful` and `already_knew` are negatives. A weekly report prints precision by `signal_type` and `role_family` per profile; weights are adjusted by hand from that report until there are at least 300 labels per profile, after which a logistic re-weighting (one coefficient per signal type, fitted on labels) can be proposed as a PR with the before and after precision in the description.

## 9. Outputs: static site, JSON, Slack, CSV, weekly page

Everything a user sees is generated by the nightly job and pushed to `tell-data`, served by GitHub Pages; there is no server to keep awake, and every page links to the commit that produced it.

**`tell-data` layout**

```
tell-data/
  events/2026/10/06.jsonl        # one event per line, the day's ingest, append-only
  postings/snapshots/2026-10-06/{vendor}-{identifier}.json   # trimmed current board state, 90-day retention
  site/                          # GitHub Pages root (branch: gh-pages or /site on main)
    index.html                   # this week's top movers, methods links, repo link
    weekly/2026-W41.html         # the shareable weekly page
    companies/{domain}.html      # account page: score, contributions, receipts, long-open roles
    api/v0/companies/{domain}.json
    api/v0/weekly/latest.json
    api/v0/watchlists/{slug}.json  # only for watchlists marked public
    health.html  api/v0/health.json
```

**Per-domain JSON (`api/v0/companies/{domain}.json`)**, the contract every other surface reads:

```json
{
  "domain": "acme.com", "name": "Acme", "as_of": "2026-10-06",
  "profile": "default", "score": 9.4, "heat": 87,
  "contributions": [
    {"type": "funding", "contribution": 5.9, "occurred_at": "2026-09-29",
     "summary": "Form D: $18,000,000 offering, $18,000,000 sold",
     "url": "https://www.sec.gov/Archives/edgar/data/.../"},
    {"type": "hiring_velocity", "role_family": "sales", "contribution": 3.5,
     "occurred_at": "2026-10-03", "summary": "4 new sales roles in 7 days vs 0.5 baseline",
     "url": "https://boards.greenhouse.io/acme",
     "postings": [{"title": "Account Executive, Mid-Market", "posted_at": "2026-10-01", "url": "..."}]}
  ],
  "open_postings": 23, "long_open_roles": 2,
  "sources": {"ats": "greenhouse:acme", "edgar_cik": "0001234567", "news": true},
  "generated_by": "https://github.com/<org>/tell-data/commit/<sha>"
}
```

Rules: amounts as integers in USD, dates as ISO 8601, every contribution has a `url`, `heat` is the watchlist percentile when requested through a watchlist and omitted otherwise. The schema lives in `docs/api/company.schema.json` and the site generator validates every file against it before pushing.

**Account page (`companies/{domain}.html`).** Static HTML from a Jinja template: score and heat, a bar per contribution with its decayed value, the receipt link beside each, the counted postings expanded under a velocity bar, long-open roles in grey, a "why not higher" footer listing signals that decayed to zero in the last 30 days, and a thumbs row that posts to the feedback form (a Google Form prefilled with `signal_id`) until the API exists.

**Slack digest (`publish/slack.py`)**, 08:00 in the workspace's time zone, Block Kit, one message per watchlist:

- Header: `tell · {watchlist} · {date}`; context line: `{n} new signals · {m} accounts crossed the threshold · freshness p95 {h}h`.
- One section per account that crossed the threshold, at most 10, highest first: `*{name}* ({domain}) · score {score}` then one bullet per contribution with its receipt as a link, then `Why now:` one sentence built from the top two contributions.
- Footer: links to the account pages and the weekly page; a "reply with a domain to add it" line (watched by a tiny Actions job reading a Google Form, not Slack events, to stay free and simple).
- Rate: Slack incoming webhooks allow about one message per second; send sequentially with a 1.1 s sleep.

**CSV with receipts (`publish/csv_export.py`)**, the "send me 50 domains" deliverable, one row per domain:

| Column | Content |
| --- | --- |
| `domain`, `name` | Resolved company |
| `score`, `heat` | As above |
| `top_signal_1`, `top_signal_1_date`, `top_signal_1_url` | Highest contribution |
| `top_signal_2`, `top_signal_2_date`, `top_signal_2_url` | Second |
| `new_roles_7d_sales`, `new_roles_7d_eng`, `new_roles_7d_other` | Velocity inputs |
| `funding_last_180d_usd`, `funding_url` | From Form D |
| `open_postings`, `long_open_roles` | Context |
| `ats`, `coverage_note` | `greenhouse:acme` or `no_board` so gaps are explicit |
| `account_page` | Link |
| `rate_this_row` | Prefilled feedback form link |

**Weekly page (`weekly/2026-W41.html`)**, the content asset: top 25 companies by sales-hiring velocity across the whole monitored set (not watchlists), top 25 new Form D filings by amount among monitored companies, a "what changed in the method" line, coverage numbers (companies monitored, boards polled, filings read), and the repo link. Generated Monday 06:00 UTC; the LinkedIn post is written by hand from it.

**Public health page (`health.html`)**: per source, last successful run, rows fetched and inserted, p95 freshness, 7-day success rate, and a red banner if any source has not succeeded in 24 hours. It is a sales asset, so it is linked from the README.

## 10. API and MCP server

The v0 API is the static JSON on GitHub Pages; the MCP server is a local package that reads it; a real FastAPI service is added only when a Clay user needs query parameters the static files cannot answer, and it runs on Render's free tier.

**Static API (v0, week 5)**

| Call | Returns |
| --- | --- |
| `GET https://<org>.github.io/tell-data/api/v0/companies/{domain}.json` | The per-domain object in section 9 |
| `GET .../api/v0/weekly/latest.json` | Weekly top lists |
| `GET .../api/v0/watchlists/{slug}.json` | Scored list for a public watchlist |
| `GET .../api/v0/health.json` | Run-health |

Clay: an HTTP API enrichment column with URL `https://<org>.github.io/tell-data/api/v0/companies/{{domain}}.json`, no auth, then columns mapped from `score`, `contributions[0].summary`, `contributions[0].url`. GitHub Pages sends CORS-friendly static responses and caches for 10 minutes, which is fine for nightly data.

**MCP server (`tell-mcp`, published to PyPI; `uvx tell-mcp`)**, stdio transport, built with the official Python MCP SDK; configured in Claude Desktop or Cursor with `TELL_BASE_URL` (defaults to the public Pages URL) and an optional `TELL_API_KEY` for the FastAPI service later.

| Tool | Input schema | Output |
| --- | --- | --- |
| `watch_accounts` | `{"domains": [string], "watchlist": string}` | Writes a request to the intake form endpoint (v0: returns the form link prefilled); v1: creates the watchlist via API |
| `in_market` | `{"watchlist": string, "since_days": int (default 7), "min_score": number (default 6.0), "families": [string]}` | `[{domain, name, score, heat, top_contributions[:3]}]` sorted by score |
| `explain` | `{"domain": string}` | The full contributions list with receipts and the "why not higher" decayed signals |
| `events` | `{"domain": string, "since": date, "types": [string]}` | Raw events with URLs, newest first, max 200 |
| `coverage` | `{"domains": [string]}` | Which domains have a board, a CIK, a feed; the honest "we cannot see this one" answer |

Tool descriptions tell the agent to cite the `url` of any contribution it mentions and to prefer `explain` before writing outreach copy. A prompt template `why_now_email` is shipped as an MCP prompt that takes a domain and returns the two-sentence "why now" with its receipts.

**FastAPI service (v1, when needed), on Render free**

- `GET /v1/companies/{domain}`, `GET /v1/in-market?watchlist=&since=&min_score=&family=`, `GET /v1/companies/{domain}/events?since=`, `POST /v1/watchlists`, `POST /v1/feedback`.
- Auth: `Authorization: Bearer <key>`; keys are `tell_` plus 32 random bytes base64url, stored as SHA-256 in a `api_keys` table with `owner`, `created_at`, `last_used_at`, `daily_quota`.
- Metering: one row per request in `api_usage (key_id, day, calls)` upserted; quota check before the handler.
- Cold starts: Render spins the service down after 15 minutes idle and takes up to a minute to wake, so the docs tell Clay users to set a 60 s timeout and retry once; a cron ping from Actions every 10 minutes during business hours keeps it warm within the 750 free hours (10 h × 22 days = 220 h).
- Everything the service answers is also in the static JSON, so an outage degrades to stale, not to down.

## 11. GitHub Actions workflows

Three workflows: `ci.yml` on every PR, `ingest.yml` four times a day, `weekly.yml` on Monday mornings; all on a public repo, so minutes are free, and the daily commit to `tell-data` is what keeps the schedules alive past GitHub's 60-day inactivity cutoff.

**Facts that shape the schedules** (GitHub's usage docs): Actions is free on standard runners for public repositories; a job may run up to 6 hours; a public repo's scheduled workflows are disabled automatically after 60 days without repository activity; cron runs can be delayed at the top of the hour, so the schedules avoid `:00`.

```yaml
# .github/workflows/ingest.yml
name: ingest
on:
  schedule:
    - cron: "17 2,8,14,20 * * *"     # four runs a day, off the hour
  workflow_dispatch:
    inputs:
      source: { description: "single source to run", required: false }
concurrency: { group: ingest, cancel-in-progress: false }
permissions: { contents: read }
env:
  DATABASE_URL: ${{ secrets.DATABASE_URL }}
  SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}
  TELL_HTTP_USER_AGENT: "tell/0.1 (+https://github.com/${{ github.repository }})"
jobs:
  ats:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    strategy:
      fail-fast: false
      matrix: { vendor: [greenhouse, lever, ashby, smartrecruiters] }
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - run: uv run tell ingest --source ${{ matrix.vendor }}
      - run: uv run tell runlog --source ${{ matrix.vendor }} >> $GITHUB_STEP_SUMMARY
  filings:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - run: uv run tell ingest --source edgar
      - run: uv run tell ingest --source news
  resolve_score_publish:
    needs: [ats, filings]
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with: { repository: ${{ vars.TELL_DATA_REPO }}, path: tell-data, token: ${{ secrets.TELL_DATA_PUSH_TOKEN }} }
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - run: uv run tell resolve
      - run: uv run tell score --all-profiles
      - run: uv run tell publish --out tell-data
      - run: |
          cd tell-data && git config user.name tell-bot && git config user.email tell-bot@users.noreply.github.com
          git add -A && git commit -m "ingest $(date -u +%FT%TZ)" || echo "nothing to commit"
          git push
      - run: uv run tell notify --slack      # digest only on the 08:00-local run, decided inside the command
      - run: uv run tell deadman            # fails the job if any source has no success in 24h
```

**`ci.yml`**: on `pull_request` and `push` to `main`; steps `uv sync`, `ruff check`, `ruff format --check`, `mypy --strict tell`, `pytest -q --cov=tell --cov-fail-under=80`, then `alembic upgrade head` against a throwaway Neon branch created with the Neon GitHub Action and deleted at the end (branches are free within the 10-per-project limit).

**`weekly.yml`**: `cron: "23 6 * * 1"`; runs `tell weekly --week current`, `tell backtest --refresh`, publishes `weekly/` and `methods/` pages to `tell-data`, and posts the weekly summary to the private Slack channel for Karan to turn into the LinkedIn post.

**Operational notes**

- `concurrency: ingest` serializes runs so two polls never race on the same board; the Postgres `SKIP LOCKED` helper in `db.py` protects the few places two jobs do touch the same rows.
- Discovery of new domains runs inside `ats` with a budget: at most 300 new domains per run, 4 requests per second.
- Run time estimate: 6,000 boards at 8 concurrent, about 250 ms each, is under 4 minutes per vendor; EDGAR's daily index is one file plus one XML per new filing; news is the slow one at 1 request per second, so it polls only companies with activity in the last 30 days plus every watchlist member, and rotates the rest over a week.
- Four runs a day keeps p95 freshness under 12 hours for ATS and filings; the plan tab's target is 24.
- `workflow_dispatch` with a `source` input is how an agent reruns one collector after a fix; the idempotency rules make reruns safe.
- Failure policy: a failed matrix leg does not stop the others (`fail-fast: false`); `resolve_score_publish` runs `if: always()` so one broken vendor never blocks the digest; the dead-man step turns the whole run red when a source has been silent for 24 hours, which also emails Karan through GitHub's default failure notification, at no cost.

## 12. Testing, CI, run-health

Five test families gate every merge, and the same checks run in production as the dead-man switch; a collector that cannot pass them does not ship.

**Test families and their acceptance criteria**

| Family | File | What it proves | Gate |
| --- | --- | --- | --- |
| Contract | `test_contracts.py` | Each provider fixture parses into its pydantic model; the `drift` fixture raises `ValidationError` naming the missing field | 100% of sources |
| Idempotency | `test_idempotency.py` | Running each collector twice on the same fixture leaves `events`, `postings`, `signals` counts unchanged; a changed `updated_at` yields exactly one `job_updated` | Zero new rows on rerun |
| Resolution | `test_resolve.py` | Gold-set precision and recall; every rule in section 6 has a positive and a negative case | precision >= 0.97, recall >= 0.85 |
| Velocity | `test_velocity.py` | Repost within 30 days is not a new role; stale over 90 days weighs 0.25; baseline floor 0.5; family assignment for 60 labelled titles | 60 of 60 titles correct |
| Scoring | `test_scoring.py` | The six explainability tests in section 8 | All pass |
| Publish | `test_publish.py` | Every generated JSON validates against `company.schema.json`; every contribution has a URL; HTML has no unresolved template variables | All pass |

**Fixtures and clocks.** `respx` mocks `httpx`; `freezegun` pins `now()` so decay numbers are reproducible; fixtures are committed and never fetched in CI. A `make refresh-fixtures` target re-records them with real calls and shows the diff for review.

**Coverage.** `--cov-fail-under=80` overall; `collectors/`, `resolve/`, `signals/` at 90 or better, enforced per-package in `pyproject.toml`.

**Run-health in production**

- Every collector writes a `runs` row at start and end; the `deadman` command reads them: for each active source, if `max(finished_at where status='ok') < now() - 24h`, exit 1 with the source names in the step summary.
- `health.json` is rebuilt every run with, per source: `last_ok_at`, `success_rate_7d`, `fetched_24h`, `inserted_24h`, `freshness_p95_h` (95th percentile of `observed_at - occurred_at` over the last 24 h of events).
- Alerts: the failed run itself (GitHub emails the repo owner on scheduled-workflow failure) and a red line at the top of the Slack digest when any source is stale; no paid pager.
- Weekly, `tell runlog --week` prints the operations line for the resume: weeks unattended, success rate, p95 freshness, companies and postings tracked.

**Security checks in CI.** `gitleaks` on every PR, `pip-audit` weekly, a test that asserts no column named `email`, `phone`, `first_name` or `last_name` exists in any model (PII guard), and a test that fails if any fixture contains an `@` sign outside a URL.

## 13. Backtest and metric procedures

Four studies, each a `tell backtest <name>` command that writes a dated Markdown report under `docs/methods/` with its numbers, its query, and its caveats; the resume and the launch post quote those reports and nothing else.

**13.1 Speed vs press (retrospective, runnable in week 3)**

Question: how long after a Form D filing does the first news mention appear, and how fast would tell have alerted?

1. Cohort: every Form D (`isAmendment = false`) in the last 90 days by an issuer linked to a monitored company (rule R1 to R4), `N >= 100` or the report says the sample is too small.
2. For each, query Google News RSS for the company name with `when:120d` and keep items whose title matches the funding regex; `first_press_at` = earliest `pubDate` within `[filing_date - 14d, filing_date + 60d]` (the pre-window catches announcements that precede the filing).
3. `alert_at` = the time the filing appeared in the daily index plus the ingest cadence (6 h), computed, not assumed.
4. Report: share with any press within 60 days; median and p75 of `first_press_at - filing_date`; share where the filing came first; median `alert_at - filing_date`.
5. Caveat line: Google News coverage is incomplete for small companies; the "no press within 60 days" share is reported, not hidden.

```sql
with d as (
  select e.company_id, e.occurred_at::date as filed_on
  from events e where e.type = 'form_d_filed' and (e.payload->>'is_amendment')::bool = false
    and e.occurred_at >= now() - interval '90 days' and e.company_id is not null),
p as (
  select n.company_id, min(n.occurred_at::date) as first_press_on
  from events n join d using (company_id)
  where n.type = 'news_funding' and n.occurred_at::date between d.filed_on - 14 and d.filed_on + 60
  group by n.company_id)
select count(*) as n, count(p.company_id) as with_press,
       percentile_cont(0.5) within group (order by first_press_on - filed_on) as median_lag_days,
       sum(case when first_press_on > filed_on then 1 else 0 end) as filing_first
from d left join p using (company_id);
```

**13.2 Hiring velocity as a leading indicator (two versions)**

*Prospective (clean, runnable from week 10 onward).* From the day collection starts, every company has a full posting history. For each new Form D after day 60 of collection, compute `new7` per week for the 8 weeks before filing and a baseline from weeks 9 to 12; a "spike" is any pre-filing week with `new7 >= 3` and `ratio >= 1.5`. Report the share of filings preceded by a spike, the median lead (days from first spike week to filing), and the same statistic for a control group: companies with no filing in the window, matched to the filers by open-posting count decile. The difference between the two shares is the number to quote; if it is small, say so.

*Retrospective (rougher, runnable in week 4, labelled as such).* Posting histories do not exist before collection, so reconstruct them two ways and report both: (a) from current postings' `posted_at` fields (survivorship bias: closed roles are invisible, so this undercounts spikes), and (b) from Wayback Machine snapshots of the board page via the CDX API (`https://web.archive.org/cdx/search/cdx?url=boards.greenhouse.io/{token}&output=json&from=2026&filter=statuscode:200`, then `https://web.archive.org/web/{timestamp}id_/{url}`), which exist for a minority of boards. Form D filings for the window come from the quarterly Form D data sets (2025 Q4 to 2026 Q2) plus the daily index. The report states the coverage of each reconstruction and quotes the prospective study as the real result once it exists.

**13.3 Noise handling**

- Share of postings that are reposts: `count(repost_of is not null) / count(*)` over postings first seen in the last 90 days, by vendor and by family.
- Median posting lifetime per family from `closed_at - posted_at`.
- Share of boards with at least one posting open more than 180 days.
- Rated usefulness on filtered vs unfiltered: for the first three design partners, send two CSVs on the same day, one with reposts and stale roles included and one without, each row rated; report the two precision figures. Quote only when both lists have at least 50 rated rows.

**13.4 Operations and resolution**

- Weeks unattended: days since the last manual intervention on `main` that was needed to keep ingest green, from the `runs` table.
- Success rate: `ok / (ok + failed)` over the period, by source and overall.
- Freshness p95 as defined in section 12.
- Companies monitored: `count(*) from companies where exists an active board or a CIK`; postings tracked: `count(*) from postings where closed_at is null`.
- Match precision and recall from the gold set run in CI; also the live review-queue rate (items per 1,000 events).

**13.5 Dogfood (Karan's own search)**

A watchlist `karan-targets` with his target companies and an ICP profile `job-seeker` whose target families are the roles he applies to. Record, per surfaced posting, `surfaced_at - posted_at` (hours), whether he applied, and any interview that followed. Report median hours to surface, applications generated, interviews; these are the only metrics in this spec that mix personal outcomes with system output, and they stay in `tell-private`.

**Reporting rules.** Each report has: question, cohort definition, N, the query or script path, the numbers, the caveats, the date. A number with N under 30 is reported with the N beside it and never used in a resume bullet. Every bullet in the plan tab's section 14 maps to exactly one report.

## 14. Free-tier limits, verified, and what to do at each ceiling

The $0 stack fits inside published free tiers with room to spare, with one correction to the plan tab: Hugging Face Spaces no longer offers free Docker compute, so the optional API goes to Render's free tier instead.

| Service | Free allowance (checked Oct 4 to 5, 2026) | tell's expected use | At the ceiling |
| --- | --- | --- | --- |
| GitHub Actions, public repo | Standard runners free; 6 h per job; 20 concurrent jobs on Free; scheduled workflows disabled after 60 days without repo activity ([GitHub docs](https://docs.github.com/en/actions/reference/usage-limits-billing-and-administration)) | About 40 minutes a day across four runs | Daily `tell-data` commit keeps schedules alive; split the ATS matrix further if a leg nears 90 minutes |
| GitHub Pages | Static hosting for public repos; soft limits of about 1 GB site size and 100 GB bandwidth a month, 10 builds an hour (GitHub's published guidance, re-check before launch) | Site under 200 MB at 25,000 account pages; 4 builds a day | Prune account pages for companies with no signal in 180 days; move snapshots out of the site tree |
| Neon Free | 100 projects; 10 branches and 100 CU-hours per project per month; 1 GB Postgres storage per project, 20 GB total; 5 GB network transfer per project per month; scale to zero after 5 minutes; 6 h restore history; no card ([Neon FAQ](https://neon.com/faqs/free-plan-limits-and-quotas)) | 4 runs × about 25 minutes of 0.25 CU = about 2 CU-hours a day, 60 a month; storage 300 to 500 MB with pruning | Archive events older than 180 days to JSONL (already the plan); a second free project for the backtest warehouse if CU-hours run short |
| Render Free web service (optional API) | Spins down after 15 minutes idle, about a minute to wake; 750 instance hours per workspace per month; 5 GB bandwidth; may be suspended for unusually high outbound traffic; free Postgres expires after 30 days so never use it ([Render docs](https://render.com/docs/free)) | A warm-up ping during business hours uses about 220 hours a month | Serve from static JSON (always available); no polling from Render, ever |
| Hugging Face Spaces | Static Spaces free; Docker and Gradio Spaces now require a paid plan ([HF docs](https://huggingface.co/docs/hub/en/spaces-overview)) | Not used | Removed from the plan |
| Streamlit Community Cloud | Free for public GitHub repos; apps sleep when idle | The review-queue page only, behind a password, if a CSV round-trip proves too slow | Keep it off the critical path |
| Vercel Hobby | Free, non-commercial use only | Not needed while the site is static on Pages | Revisit only for a logged-in feed |
| SEC EDGAR | No key; 10 requests per second; declared `User-Agent` with contact ([SEC](https://www.sec.gov/os/accessing-edgar-data)) | Under 500 requests a day | n/a |
| Greenhouse, Lever, Ashby, SmartRecruiters | Public, no key, no published limits | 6,000 to 25,000 requests a day total at 8 concurrent | Back off on 429; halve concurrency for a vendor that blocks |
| Google News RSS | Undocumented | Under 3,000 requests a day at 1 per second, cached 6 h | Rotate the long tail weekly; stop on repeated 429 or 503 |
| GitHub API | 5,000 requests per hour with a token; 304 responses free | v1.1 only | Conditional requests |
| Slack incoming webhooks | Free workspace; about 1 message per second per webhook | A handful of messages a day | Sleep 1.1 s between sends |
| Google Forms and Sheets | Free | Intake and feedback until the API exists | n/a |
| PyPI | Free | `tell-mcp` package | n/a |
| Wayback Machine CDX | Free, be polite (1 request per second) | Backtest 13.2 only | Cache results in `tell-private` |

**What costs money and is deliberately not used:** any LLM API in the ingest path, Fly.io (card required), Cloudflare R2 (billing enablement required), a domain name, Stripe, a paid Slack plan, any data vendor. The first dollar, when Karan chooses to spend one, buys a domain and a Render Starter instance (about $7 a month) for an always-on API.

## 15. Launch assets

Every asset below leads with something real the reader gets (a scored list, a number, a repo) and ends with one plain ask; brackets are numbers that come from the section 13 reports and never ship as brackets.

**15.1 The "50 domains" message (LinkedIn DM or email, cold or warm; under 80 words)**

> Hi \[first name\], I'm Karan, a CS grad out of UW–Madison. I built tell: it watches public job boards and SEC filings and turns them into dated, source-linked buying signals.
>
> I'd like to run it on your accounts. Send me 50 domains and I'll send back a scored CSV by Friday, every row linked to the posting or filing behind it. No login, nothing to install.
>
> If it's useful, tell me what's wrong with it. If it isn't, that's useful too.

Warm variant: replace the first line with one sentence about the one thing that person told Karan in their own earlier conversation (never a detail from someone else's), then the same ask. Follow-up after 5 days, one line: "Still happy to run this on 50 domains if you send them over." No third message.

**15.2 The delivery message (with the CSV)**

> Here's the CSV for your 50. Top of the list: \[company\], \[one-sentence why-now with the link\]. Two things I'd ask: thumb each row (the last column is a 10-second form), and tell me which accounts you already knew about. Receipts are in the URL columns; the method is written up here: \[methods link\].

**15.3 LinkedIn launch post (week 8; about 180 words)**

> I built a tool to watch companies' job boards for my job search. Around week two I realized I had accidentally built sales software.
>
> A company that opens four sales-engineer roles in a week is about to buy sales-engineering tools. The posting is public, timestamped and free. So is the Form D they filed last Tuesday.
>
> tell reads those public sources and turns them into buying signals with receipts. Every score is a list of dated events, each linked to the filing or posting behind it. No LinkedIn scraping, no personal data.
>
> What it caught in its first \[N\] weeks:
>
> - &#91;N\] funding rounds a median \[X\] days before the first press mention
> - &#91;X\]% of \[N\] funding events had a hiring spike in the 60 days before
> - &#91;N\] companies watched, \[N\] postings tracked, p95 freshness \[H\] hours, $0 a month to run
>
> It's open source: \[repo link\]. If you're in RevOps or sales and want to try it on your accounts, send me 50 domains and I'll send back a scored CSV.

One image: the account page for a real company with the receipts visible. No carousel.

**15.4 Show HN**

Title: `Show HN: Tell – explainable buying signals from job boards and SEC filings`

> I built a scanner to watch ATS boards (Greenhouse, Lever, Ashby, Workday) during my job search, then noticed hiring velocity is a buying signal: a company opening six SDR roles is about to buy sales tools. Tell is that scanner pointed at sales teams.
>
> It polls public job-board JSON, SEC EDGAR Form D filings and news feeds, resolves everything to a company domain, and computes an additive score with per-signal exponential decay, so every score decomposes into named, dated, source-linked events. The parts that took real work: entity resolution across sources (careers-page name vs EDGAR filer name), telling reposts and ghost jobs from real hiring, and keeping ingestion idempotent so reruns add zero rows.
>
> It runs on GitHub Actions cron, Neon's free Postgres and GitHub Pages: $0 a month. Numbers so far: \[from the reports\]. What it deliberately does not do: scrape LinkedIn or store anything about people.
>
> Repo: \[link\]. Methods write-up with the measured repost rates: \[link\]. I'd like feedback on the scoring model and on public sources I'm missing.

Post between 8 and 10 am Eastern on a Tuesday or Wednesday; answer every comment for the first six hours; never argue about the ATS vendors' terms, link the legal notes.

**15.5 README outline (`tell/README.md`)**

1. One line and a 30-second GIF of an account page with receipts.
2. The origin story in four sentences.
3. What it does, what it does not do (no scraping, no PII, company-level only).
4. Quickstart: `uvx tell-mcp` with Claude Desktop or Cursor in three lines; `git clone`, `uv sync`, `tell ingest --source greenhouse --board stripe` for the curious.
5. Architecture (Mermaid diagram mirroring the plan tab's picture).
6. Data sources with the legal note per source and a link to `docs/sources/`.
7. Methods and numbers: links to the section 13 reports, with the headline numbers inline and dated.
8. Run-health: link to `health.html` and the Actions badge.
9. Writing a collector: the `Collector` protocol, fixture rules, the PR checklist.
10. Limitations: coverage bias toward companies on major ATSs, Google News gaps, undocumented vendors behind a flag.
11. Roadmap, license (MIT), contact.

**15.6 Intake form (Google Form)** fields: name, email, company, role, up to 50 domains (one per line), who you sell to (dropdown matching the ICP profiles), Slack webhook URL (optional), "I'm a job seeker" checkbox. The nightly job reads the published-as-CSV sheet and creates watchlists in `tell-private`.

## 16. Human review checklist per milestone

Karan reviews outcomes, not diffs: each milestone has a short list of things to try by hand, and a PR is merged only when every box is ticked in its description.

**M1 Collectors and graph (end of week 2)**

- [ ] Run `tell ingest --source greenhouse --board <three boards you know>` twice; the second run reports `inserted: 0`.
- [ ] Open 10 random `postings` rows and their URLs; title, location and posted date match the live page.
- [ ] Pick 10 companies from your own target list; `tell discover` finds the board for at least 7, and the misses are logged with the pages checked.
- [ ] `tests/fixtures/*/drift.json` exists for every vendor and CI shows the contract test failing on it.
- [ ] No column, fixture or log line contains a person's name or email.

**M2 Filings, news, scoring, digest (end of week 4)**

- [ ] Take yesterday's Form D daily index; count the filings linked to monitored companies by hand for a sample of 10; the linker agrees on at least 9 and the tenth is in the review queue, not silently wrong.
- [ ] Open 10 funding events; each `url` opens the filing folder on sec.gov and the amount in `payload` matches `primary_doc.xml`.
- [ ] For 5 companies, add the contributions on the account page with a calculator; they equal the score.
- [ ] Receive the Slack digest two mornings in a row without touching anything.
- [ ] Your own `karan-targets` watchlist surfaces at least one posting you had not seen.
- [ ] Report 13.1 exists with N, the query, and caveats.

**M3 Platform (end of week 6)**

- [ ] Fetch `api/v0/companies/<domain>.json` in a browser; every contribution has a URL; the JSON validates in CI.
- [ ] Add `tell-mcp` to Claude Desktop with the three-line config and ask: "which of these ten domains are in market and why?"; the answer cites URLs.
- [ ] Build a Clay table with 20 domains and the HTTP column; 20 rows fill with score and top signal.
- [ ] `health.html` shows every source green; pause one vendor in config and confirm the dead-man step turns the next run red within a day.
- [ ] Five design partners have watchlists in `tell-private` and have received a CSV.

**M4 Launch (end of week 8)**

- [ ] Reports 13.1, 13.3, 13.4 are published under `docs/methods/` with dates; the README quotes them with those dates.
- [ ] README quickstart works on a clean machine in under 5 minutes.
- [ ] The weekly page renders for the current week and the top-25 companies are real and correctly linked.
- [ ] Every number in the LinkedIn post and Show HN draft traces to a report line.
- [ ] Legal notes per source are in `docs/sources/` and linked from the README.

**M5 Conversions and application pack (end of week 12)**

- [ ] Feedback table has at least 100 rated signals from at least 5 raters before any precision number is quoted.
- [ ] Report 13.2 (prospective) runs on at least 60 days of collection, with the control group.
- [ ] Twelve weekly scorecards exist; the application's progress answer is assembled only from them.
- [ ] Every resume bullet in the plan tab's section 14 has its brackets replaced from a named report, or the bullet is dropped.

**Standing checks on every PR**: CI green; no new paid service; `CHANGELOG.md` line present; the agent's PR description names the acceptance test it closes and lists anything it was unsure about.

## 17. Decisions Karan has to make before the agents start

| Decision | Options | Default if undecided by Oct 6 |
| --- | --- | --- |
| YC vehicle | tell, or Foreman | tell (plan tab, section 4) |
| GitHub org and repo names | personal account vs a new org; `tell` vs `tell-signals` if the name is taken | New org named after the product; check PyPI for `tell-mcp` availability first |
| Undocumented vendors (Workday, Oracle) | Off until launch; on from day one; on only for watchlist members | Off until launch; on for watchlist members only after launch |
| Public watchlists | Only Karan's; design partners opt in | Only Karan's; partners private by default |
| Contact address in the SEC `User-Agent` | Personal email; a role address on a domain he controls | Personal email now; swap when a domain exists |
| Review-queue tool | CSV round-trip; a Streamlit page | CSV round-trip for the first 4 weeks |
| Job-seeker mode in public | Separate public page; a profile flag only | Profile flag only until the sales story has landed |
| License | MIT; Apache-2.0 | MIT |
| Alert threshold and half-lives | Section 8 defaults; his own numbers | Section 8 defaults, revisited after 100 ratings |
| Who gets the first 50-domain offers | The 20 names he picks from his conversations | He picks; the agent never contacts anyone |

## Sources for this tab

- [Accessing EDGAR Data](https://www.sec.gov/os/accessing-edgar-data), SEC: 10 requests per second, User-Agent requirement
- [Form D Data Sets](https://www.sec.gov/dera/data/form-d), SEC: quarterly structured Form D files
- [Usage limits, billing, and administration](https://docs.github.com/en/actions/reference/usage-limits-billing-and-administration), GitHub Actions: public-repo pricing, 60-day schedule rule
- [What are the limits and quotas for Neon's Free plan?](https://neon.com/faqs/free-plan-limits-and-quotas), Neon
- [Deploy for Free](https://render.com/docs/free), Render: spin-down and 750 hours
- [Spaces Overview](https://huggingface.co/docs/hub/en/spaces-overview), Hugging Face: Docker Spaces need a paid plan
- Greenhouse Job Board API (developers.greenhouse.io/job-board.html), Lever Postings API (github.com/lever/postings-api), Ashby Job Posting API (developers.ashbyhq.com): endpoints and fields confirmed through the search results and third-party write-ups listed in the plan tab; the agent must open each vendor page and record the sample response as the fixture before relying on a field
