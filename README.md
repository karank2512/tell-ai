# tell

[![ci](https://github.com/karank2512/tell-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/karank2512/tell-ai/actions/workflows/ci.yml)
[![license: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Explainable buying signals from public job boards, SEC filings and news: every score is a list of dated events, each linked to the posting or filing behind it.**

<!-- 30-second GIF: an account page with its receipts visible. Not recorded yet; see docs/launch/README.md ("Recording the README GIF"). Add the image here when it exists; do not commit a placeholder image. -->

> The 30-second demo GIF (an account page with receipts) is still to be recorded. Recording steps are in [docs/launch/README.md](docs/launch/README.md).

## Why it exists

I built a tool to watch companies' job boards for my own job search. Around week two I noticed that hiring velocity is a buying signal: a company that opens several sales-engineer roles in a week is about to buy sales-engineering tools. The postings are public, timestamped and free, and so are the SEC Form D filings a company files when it raises money. tell reads those public sources, resolves them to a company domain, and turns them into scores that decompose into named, dated events with receipts.

## What it does, and what it does not

It does:

- Poll public job-board JSON (Greenhouse, Lever, Ashby, SmartRecruiters), SEC EDGAR filings (Form D, 8-K, S-1) and news feeds, once per source at polite rates.
- Resolve every event to a company domain (rules R1-R6, with a review queue for the unsure cases) and classify postings into ten role families.
- Compute hiring velocity (new postings in 7 days against a four-week baseline, with repost and stale-posting handling) and an additive score with per-signal exponential decay.
- Publish static pages, JSON, a Slack digest and a CSV in which every row links to its receipt.

It does not:

- Scrape LinkedIn or any logged-in site.
- Store anything about people: no names, emails or phone numbers in any column, fixture, log line or payload. Form D related-person lists are never read into storage.
- Score individuals. Everything is company-level.
- Store job descriptions or article text. A description is hashed and scanned for technology keywords, then discarded; news keeps only the headline, URL, date and publisher.

Headline-redaction policy: a leadership-change headline usually names a person, so tell never stores it as published. The stored headline has the person's name redacted (`payload.title_redacted = true`) and the article URL stays as the receipt for the full headline. Funding, launch and expansion headlines are stored unchanged. This is provisional pending maintainer confirmation; see [ADR 0016](docs/decisions/0016-news-headline-redaction.md).

## Quickstart

### In Claude Desktop or Cursor

`tell-mcp` is a small stdio MCP server that reads tell's public static JSON. It needs no key and no account. Add this to `claude_desktop_config.json` (or Cursor's `~/.cursor/mcp.json`):

```json
{ "mcpServers": { "tell": { "command": "uvx", "args": ["tell-mcp"],
  "env": { "TELL_BASE_URL": "https://karank2512.github.io/tell-data" } } } }
```

Restart the app and ask: "Which of these ten domains are in market and why?" The answer should cite a receipt URL for every signal. Tools: `watch_accounts`, `in_market`, `explain`, `events`, `coverage`, plus the `why_now_email` prompt. Details: [docs/mcp.md](docs/mcp.md). `uvx tell-mcp` works once the first release is on PyPI (a cutover step); until then run it from a checkout with `uv run tell-mcp`.

### From source

```sh
git clone https://github.com/karank2512/tell-ai && cd tell-ai && uv sync
make migrate                                   # creates the schema in your local Postgres
uv run tell discover domain --domain stripe.com   # finds and records the job board for a domain
uv run tell ingest --source greenhouse --domain stripe.com
```

`ingest` polls a board that discovery has already recorded, so run `discover` for the company first; `--domain` picks the board by the company's domain, `--board <token>` by the board token discovery printed. It prints one JSON run line, and running it twice in a row reports `inserted: 0`. (Prefix `uv run` if the environment is not activated.)

Local Postgres: tell needs a PostgreSQL server and reads it from `DATABASE_URL` (point it at your own server and user, for example `postgresql+psycopg://$USER@localhost:5432/tell_dev`; see [CONTRIBUTING.md](CONTRIBUTING.md#database-url)). Without a local server, `make dev` starts Postgres 16 from `docker-compose.yml` on port 5433, then set `DATABASE_URL=postgresql+psycopg://tell:tell@localhost:5433/tell_dev`. Other commands: `tell discover batch`, `tell resolve`, `tell score --all-profiles`, `tell publish --out DIR`, `tell notify`, `tell weekly`, `tell runlog`, `tell deadman`, `tell prune`, `tell backtest`, `tell intake sync`, `tell api`; run `tell --help`.

## Architecture

```mermaid
flowchart TD
  subgraph sources [Public sources]
    GH[Greenhouse API]
    LV[Lever API]
    AS[Ashby API]
    SR[SmartRecruiters API]
    ED[SEC EDGAR]
    NW[Google News RSS + company feeds]
    GOC[GitHub org API v1.1]
  end
  subgraph ingest [tell package - GitHub Actions cron]
    COL[collectors/* - httpx, fixtures, idempotency keys]
    DISC[discovery/ats.py - domain to board]
    RES[resolve/* - normalize, R1-R6 match rules, review queue]
    SIG[signals/* - taxonomy, fingerprints, velocity, scoring+decay]
  end
  PG[(Postgres - local dev / Neon prod)]
  subgraph outputs [publish/* - nightly]
    DATA[tell-data repo - JSONL events + snapshots]
    SITE[GitHub Pages site - account pages, weekly, health, api/v0 JSON]
    SLK[Slack Block Kit digest]
    CSV[CSV with receipts]
  end
  subgraph consumers [Consumers]
    MCP[tell-mcp - uvx, 5 tools]
    CLAY[Clay HTTP column]
    REP[Humans - pages, digest]
  end
  API[api/app.py FastAPI - coded + tested, not deployed]
  sources --> COL --> PG
  DISC --> PG
  RES --> PG
  PG --> SIG --> PG
  PG --> outputs
  SITE --> MCP
  SITE --> CLAY
  SITE --> REP
  SLK --> REP
  PG -.-> API
```

Data repo: [karank2512/tell-data](https://github.com/karank2512/tell-data) (append-only `events/YYYY/MM/DD.jsonl` is the system of record). The static API is documented in [docs/api/README.md](docs/api/README.md).

## Data sources

Every source is a documented public endpoint or a public-domain government file. Full pages, with endpoint, rate limit, legal note and sample response, are in [docs/sources/](docs/sources/README.md).

| Source | Page | Legal note, in one line |
| --- | --- | --- |
| Greenhouse Job Board API | [greenhouse.md](docs/sources/greenhouse.md) | Greenhouse states the data is public and read access needs no authentication; its only automated-access clause binds job-seeker accounts, not the feed. |
| Lever Postings API | [lever.md](docs/sources/lever.md) | Lever's docs say published postings are publicly viewable and may be scraped by third parties; its site terms forbid scraping the app, which tell does not touch. |
| Ashby Job Posting API | [ashby.md](docs/sources/ashby.md) | Ashby's documented public posting API, no authentication; a reviewed legal note is still an open item. |
| SmartRecruiters Posting API | [smartrecruiters.md](docs/sources/smartrecruiters.md) | SmartRecruiters' documented public API for published job ads; candidate and customer endpoints are never touched; a reviewed legal note is still an open item. |
| SEC EDGAR (Form D, 8-K, S-1) | [edgar.md](docs/sources/edgar.md) | US government work, public domain; SEC fair-access policy followed (5 requests per second against a 10 limit, declared User-Agent). |
| Google News RSS and company feeds | [news.md](docs/sources/news.md) | The Google feed is undocumented and its terms say personal, non-commercial use; only headline, URL, date and publisher are kept, and whether this half ships enabled is an open maintainer decision. Company blog feeds are published for syndication. |
| GitHub org repos and releases | [github.md](docs/sources/github.md) | Documented public REST API within published limits; org-level public data only, never user profiles or authors. Coded and tested, config-gated per company. |
| YC public directory (seed list only) | [yc-directory.md](docs/sources/yc-directory.md) | Four public company facts per row (name, website, batch, location), no personal data. |

Workday, Oracle and Workable boards are detected by discovery and recorded as `unsupported` so coverage reporting stays honest ("we cannot see this one"). They are never polled. See [ADR 0003](docs/decisions/0003-drop-workday-oracle-collectors.md).

## Methods and numbers

Method pages (what is measured and how):

- [Entity resolution](docs/methods/entity-resolution.md) and the [gold set](docs/methods/gold-set.md)
- [ATS discovery](docs/methods/discovery.md)
- [Hiring velocity](docs/methods/hiring-velocity.md), [tech keywords](docs/methods/tech-keywords.md)
- [Scoring](docs/methods/scoring.md)
- [Publishing](docs/methods/publishing.md)

Numbers that exist today:

| Measure | Value | Measured | Caveat |
| --- | --- | --- | --- |
| Entity-resolution precision on production Form D links (hand-checked against SEC filings and company sites) | 0.50 (20 of 40; 1 more unclear) | 2026-10-14 | Measured before the Form D guards (R3 5 of 5 wrong, R2 15 of 35 wrong). Under the guards (ADR 0034) `tell resolve relink-formd` keeps exactly the 20 right links on a copy of that data (in-sample). Held out, on the 2026Q2 SEC data set (16,243 filings the guards were not designed on): the old rules made 21 decided auto-links, 8 right (0.38); the guarded rules made 7 decided, 7 right, plus 1 unclear (N is small: Wilson 95% interval 0.65 to 1.00), and review items fell from 625 to 91. See [docs/methods/entity-resolution.md](docs/methods/entity-resolution.md#production-measurement-2026-10-14). |
| Entity-resolution precision (name rules, 300-company gold set) | 1.0000 | 2026-10-14 | Not production precision: production measured 0.50 (row above), because no gold name is an unrelated company sharing a brand word. Gold set is a draft that a person has not yet spot-checked; the 300-row set is private, a 50-row sample is public (`tests/fixtures/gold_sample.csv`). |
| Entity-resolution recall on SEC-verified legal names (name rules, 36 of the 300) | 0.75 (27/36) | 2026-10-14 | Was 0.78 (28/36) before R3 became review-only for Form D. The all-300 figure (0.9433) is inflated: 290 of the 300 drafted legal names normalise to the brand and match trivially. The misses are issuers filed under a former corporate name (unlinked) and one former R3 link now sent to review. |
| Look-alike decoys auto-linked in the gold-set test (token supersets that fuzzy-score 100, e.g. "Blue Stripe Bakery" vs Stripe) | 0 of 180 | 2026-10-14 | Synthetic decoys only. Production's real look-alikes (Compass vs nCompass, Payload Inc. vs Payload CMS) were linked; see the first row. |
| Same measures on the public 50-row sample | precision 1.0000, recall 0.9200 | 2026-10-14 | Release gate is precision at least 0.97 and recall at least 0.85; both pass. Recall was 1.0000 before R3 became review-only for Form D. |

The backtest reports are generated by `tell backtest refresh` every Monday into [`tell-data/site/methods/`](https://github.com/karank2512/tell-data/tree/main/site/methods) (speed versus press, hiring velocity as a leading indicator, noise handling, operations; the workflow never commits to this repo, see [ADR 0025](docs/decisions/0025-weekly-reports-published-to-tell-data.md)). Their headline numbers (days between a Form D and the first press mention, the share of funding events preceded by a hiring spike, repost share, freshness p95, success rate, companies and postings tracked) are pending soak: they need calendar time of real collection after cutover, and nothing is quoted until a dated report with its N exists. A number with N under 30 is never quoted.

## Run health

[Run-health page](https://karank2512.github.io/tell-data/health.html): last success, 7-day success rate, items fetched and inserted in 24 hours and p95 freshness per source, with a red banner when a scheduled source has been silent for 24 hours. CI status is the badge at the top.

## Writing a collector

A collector is one module, `tell/collectors/<source>.py`, that exposes a module-level `COLLECTOR: CollectorSpec` (the contract is documented at the top of [tell/collectors/base.py](tell/collectors/base.py)). The shared contract and idempotency tests find it automatically, so adding a source never edits a shared test file.

`CollectorSpec` fields:

| Field | Meaning |
| --- | --- |
| `source` | The `events.source` value and the fixture folder name, such as `greenhouse`. |
| `kind` | `ats`, `filings`, `news` or `github`. ATS collectors drive `sync_board_postings`. |
| `response_model` | The pydantic v2 model that parses one raw provider response. It is the contract: a field that disappears must fail validation. |
| `fixture_dir` | `tests/fixtures/<source>`. |
| `drift_missing_field` | The field `drift.json` omits; the contract test asserts it appears in the validation error. |
| `run_fixture(session, case_path, *, run_id, now)` | Replays one fixture through the real parse and write path with no network, returning a `RunResult` (`fetched`, `inserted`, `skipped`, `events`). |

Rules that every collector follows:

- `event_id = sha256(source|external_id|version)`, where `version` is the provider's own change marker; a re-fetch of unchanged data inserts zero rows.
- Payloads hold provider fields only. No names, emails or phone numbers; keys that look like PII are rejected at construction.
- Fixtures live in `tests/fixtures/<source>/`: at least `normal`, `empty_board` (or `empty`) and a `drift` case that omits one field. Hand-trim each below 50 KB and scrub anything personal. Record once with polite rates, then freeze; if a provider's response changes, record a new fixture, update the contract test and say so in the PR.
- Ship `docs/sources/<source>.md`: endpoint, rate limit, legal note, sample response.
- Tests never touch the network (`respx` for httpx, `freezegun` for time).

PR checklist (also in the PR template): one ticket per PR naming the acceptance test it closes; `ruff`, `mypy --strict tell` and `pytest` green; fixture, contract test, idempotency test and source page for a new source; scoring changes ship with the explainability tests passing and a one-line `CHANGELOG.md` entry; no new paid dependency, hosted service or credit card; no secrets committed. Coverage floors: 90 for `collectors/`, `resolve/` and `signals/`, 80 overall. Full steps: [CONTRIBUTING.md](CONTRIBUTING.md#writing-a-collector).

## Limitations

- Coverage is biased toward companies on the four supported ATS vendors. A company on Workday, Oracle, Workable or a home-grown careers page is detected where possible but not read.
- Google News coverage is incomplete for small companies, and the feed's terms are an open question (see the data sources table).
- A company with no public job board and no SEC filing has no signals; `coverage` says so rather than guessing.
- Reposts and ghost jobs are filtered by heuristics (requisition fingerprints, a 30-day repost window, stale-posting weights), not by ground truth. The ghost-job vote counter is inert until 500 votes.
- The entity-resolution gold set is a draft that a person has not yet spot-checked, and the measured numbers are one day old. Backtest numbers are pending soak.
- Score weights and half-lives are hand-tuned defaults, revisited after at least 100 rated signals; no precision-of-signal number is quoted before then.
- The FastAPI v1 service is written and tested but not deployed; the static JSON on GitHub Pages is the API.

## Roadmap

- Cutover (the guided one-time setup): public repos, Neon database, push token, Slack webhook, SEC contact, PyPI release of `tell-mcp`, first supervised runs, then the unattended soak that produces the numbers above.
- Published backtest reports with real N, then the prospective hiring-velocity study once 60 days of collection exist, with a matched control group.
- GitHub org signals for developer-tool ICPs (v1.1; the collector exists and is config-gated).
- LLM-proposed answers for the review queue, with a human still clicking the button (v1.1).
- Deploy the FastAPI service when a consumer needs keys and quotas.
- Government-contract sources (USAspending, SAM.gov) when a public-sector seller asks (v2).

## License and contact

Apache-2.0, see [LICENSE](LICENSE). Questions, bugs and "watch these domains" requests: [GitHub issues](https://github.com/karank2512/tell-ai/issues). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
