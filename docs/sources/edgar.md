# SEC EDGAR

Module: `tell/collectors/edgar.py` (daily index, HTTP, 8-K / S-1, tickers, `COLLECTOR`) and
`tell/collectors/edgar_formd.py` (Form D contract, XML parser, events, quarterly backfill).
Spec: SPEC §3.6, §5 rule 4, §13.2. Source value `edgar`, kind `filings`.

## Endpoints

| What | URL | Used for |
| --- | --- | --- |
| Daily form index | `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx` | New filings per day; kept form types `D`, `D/A`, `8-K`, `S-1` (exact match, so `8-K/A` and `S-1/A` are dropped) |
| Quarter listing | `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/index.json` (parents `{YYYY}/index.json`, `index.json`) | Which daily indexes the SEC has published; one request per quarter per run, before any index request |
| Form D document | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/primary_doc.xml` | Form D fields (below) |
| Filing folder | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/` | `events.url` (the receipt) |
| Submissions | `https://data.sec.gov/submissions/CIK{10 digits}.json` | `fileNumber` for Form D / D/A, `items` for 8-K (`filings.recent`, parallel arrays) |
| CIK to ticker | `https://www.sec.gov/files/company_tickers_exchange.json` | `load_ticker_map()` -> `{10-digit cik: (ticker, exchange, name)}` |
| Quarterly Form D data sets | `https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/{YYYY}q{n}_d.zip` from 2026 Q2, `https://www.sec.gov/files/structureddata/data/form-d-data-sets/{YYYY}q{n}_d.zip` through 2026 Q1 (listed on sec.gov/dera/data/form-d, checked 2026-10-13; TSV in ZIP) | `tell backfill formd` (`edgar_backfill.download_dataset()` -> `load_formd_quarterly()`), the backtest backfill |

The quarter in the index path comes from the date (`QTR1` = Jan-Mar ...). The CIK in archive
paths is unpadded; the accession number loses its dashes.

## Which days a run reads (catch-up)

All dates are on the New York calendar.

- **Scheduled run** (`tell ingest --source edgar`): every day from the one after
  `last_index_date` up to today, where `last_index_date` is the latest daily index a
  successful (`status = 'ok'`) `edgar` run read, taken from `runs.notes.last_index_date`
  of the recent runs. The window starts no earlier than **14 days back**
  (`MAX_CATCHUP_DAYS_BACK`), so an outage longer than two weeks needs `--since`. When
  today's index is already read, the run requests nothing.
- **First run ever** (no successful `edgar` run on record): today and the **3 days before
  it** (`FIRST_RUN_DAYS_BACK`), so a Monday-morning first run still reads Friday.
- **Runs recorded before `last_index_date` existed** kept only `notes.dates` (yesterday and
  today at run time); their earlier date is taken as read, so the first new run re-reads at
  most one day (cheap: see "Known filings").
- **Manual catch-up**: `tell ingest --source edgar --since YYYY-MM-DD` reads `since` through
  today, at most **120 days back** (`MAX_SINCE_DAYS_BACK`); older or future dates exit 2
  before any request or `runs` row. Older filings come from `tell backfill formd`.
- **Published days only.** The quarter `index.json` listing (one request per quarter per run)
  names the indexes that exist; only those are requested. sec.gov answers a request for a
  missing file (weekends, holidays such as Columbus Day 2026-10-12, today before the evening
  publication) with **403**, so asking first avoids those 403s entirely. A 403 on a listed
  index is a real block and fails the run. A 403 on a quarter listing counts as "nothing
  published yet" only when the parent listing (`{YYYY}/index.json`, then `index.json`) does
  not name that directory (the first days of a quarter or year); otherwise, or when the root
  itself is 403, the run fails. A 404 on a listing lists nothing.
- `runs.notes`: `dates` (requested), `indexes` (actually read), `last_index_date` (the
  later of the previous value and the latest index read: a run that reads nothing carries
  it forward), `previous_index_date`, `known`, `amended`, and `since` for a manual run.

## Known filings and transactions

- Before fetching anything for a day's rows, one query by `event_id`
  (`sha256("edgar|{accession}|{accession}")`) finds the accessions that already have an
  event. A known `D` costs no `primary_doc.xml` or submissions request, a known monitored
  `8-K` no submissions request; they count in `skipped` and `notes.known`. A `D/A` is always
  fetched, since it amends the original's payload (replaying it is a no-op). A rerun over
  the same days therefore costs one listing per quarter, one index per day, and the D/As.
- Days run oldest first, each in its own short transaction: two reads (known accessions,
  monitored CIKs), commit, the HTTP for that day, then one write and commit. No transaction
  is open during a request. A run that fails part way keeps the days already committed;
  the next run starts after the last successful run's `last_index_date` and skips them as
  known. (When `collect()` is handed a session that already has an open transaction, as in
  some tests, everything stays in that caller's transaction.)

## Rate limit, User-Agent, legal

- SEC fair-access policy: https://www.sec.gov/os/accessing-edgar-data — at most **10
  requests per second** and a `User-Agent` that declares who you are with a contact address.
- tell runs at **5 rps** (`TELL_RATE_EDGAR_RPS`), one `RateBudget` shared by `www.sec.gov`
  and `data.sec.gov`. Every request sends `User-Agent: $SEC_USER_AGENT` and
  `Accept-Encoding: gzip, deflate`; retries follow `tell/http.py` (429/5xx, 1-2-4-8 s).
- `SEC_USER_AGENT` defaults to a placeholder; the real contact address is set at cutover
  (Phase 13) as configuration, never committed. Without a contact address the SEC answers
  403 "Undeclared Automated Tool".
- EDGAR data is public domain (US government work).
- Backfill volume: one request per quarter (`tell backfill formd`), cached locally.
- Daily volume: one listing per quarter and one index per published day, plus, per new
  Form D and every D/A, one `primary_doc.xml` and one submissions JSON per distinct issuer CIK
  (cached within a run); per new monitored 8-K one submissions JSON. Roughly 2 requests per
  new Form D, so a busy day (~200 Form D) is ~400 requests (~80 s at 5 rps); a 120-day
  `--since` over ~84 business days is in the order of 30,000 requests, about 2 hours.

## Form D fields kept (SPEC §3.6, exactly)

`primaryIssuer` (`entityName`, `cik`, `issuerAddress` city / stateOrCountry / zipCode,
`jurisdictionOfInc`, `yearOfInc`, `entityType`), `issuerPreviousNameList` (names only, for
Phase 5 aliases), `offeringData.typeOfFiling.newOrAmendment.isAmendment`,
`typeOfFiling.dateOfFirstSale`, `industryGroup.industryGroupType`,
`offeringSalesAmounts.totalOfferingAmount` / `totalAmountSold` / `totalRemaining` (whole USD;
`Indefinite` -> `null` plus `totalOfferingAmountIndefinite` / `totalRemainingIndefinite`),
`investors.totalNumberAlreadyInvested`, `federalExemptionsExclusions`, and the filing's
`fileNumber` (from the submissions JSON). One addition: `newOrAmendment.previousAccessionNumber`,
used only as a fallback link when a D/A's file number is unknown.

## What is NOT stored

- The Form D officers-and-directors list (the `relatedPersonsList` element) is never read,
  parsed or stored: the parser only visits `primaryIssuer` and `offeringData`, the models
  forbid unknown keys, and `tests/test_pii_guard.py` fails if the element name appears in
  collector code outside a comment. The quarterly loader never opens `RELATEDPERSONS.tsv`.
- Issuer street lines and phone number, signature blocks, sales-compensation recipients.

## Events

| Form | Event type | Notes |
| --- | --- | --- |
| `D` | `form_d_filed` | `magnitude` = `totalOfferingAmount` (0 when undisclosed); `company_id` from `companies.cik` when known |
| `D/A` | none, or `form_d_amendment` | Merged into the original `form_d_filed` payload (see below); kept as `form_d_amendment` only when the original is not in `events` |
| `8-K` | `form_8k_filed` | Monitored companies only (`companies.cik` match); `items` plus `item_5_02`, `item_1_01`, `item_2_01` booleans |
| `S-1` | `form_s1_filed` | All filers; `company_id` when known |

Key: `edgar:{accession_number}` (`external_id` = `version` = accession number). `occurred_at`
is the index "Date Filed" at 00:00 UTC. Form D payloads carry the fields above in snake case
plus `is_amendment`, `file_number`, `issuer_name`, `issuer_city`, `issuer_state`, `issuer_zip`,
`previous_names` and `amendments: []`.

**Amendment rule (SPEC §5 rule 4).** A D/A looks up the original `form_d_filed` by
`payload.accession_number == previousAccessionNumber` first; when that is absent or unknown it
takes the latest original with the same `payload.file_number` whose `occurred_at` is on or
before the D/A's filing date (issuers reuse a Form D file number across later offerings, so the
oldest original under that number is usually a different round). It then calls `base.update_event_payload` with the new amounts and an appended `amendments` entry
(`accession_number`, `date_filed`, amounts). No event is written, so no second funding
signal can exist. Amounts are overwritten only by the latest amendment by filing date;
replaying an amendment already in the list is a no-op. In one run all new D filings are
written before any D/A is applied. A D/A whose original is unknown (including one filed
before every original under its file number) becomes `form_d_amendment`
(`is_amendment: true`), which Phase 7 never scores as funding.

**Quarterly backfill (SPEC §13.2).** `load_formd_quarterly(zip_or_dir, run_id=, now=)` reads
`FORMDSUBMISSION.tsv`, `ISSUERS.tsv` and `OFFERING.tsv` (columns by header name; dates as
`DD-MON-YYYY` or ISO) and returns events with `payload.backfill = true`, the same keys as the
daily path (so overlaps dedupe), amendments merged in memory the same way (previous
accession first, else the latest original per file number filed on or before the D/A), no
`company_id`.

**`tell backfill formd` (`tell/collectors/edgar_backfill.py`).** The command that wires the
loader in, so the SPEC §13.1 cohort exists days after cutover instead of after 90 days of
daily collection:

```sh
uv run tell backfill formd --quarter 2026Q2 [--quarter 2026Q3 ...] [--since-days 120] [--cache-dir DIR]
uv run tell backfill formd --file ~/Downloads/2026q2_d.zip      # offline: a ZIP or an unpacked directory
uv run tell resolve                                              # link the new filings (R1-R4)
```

- **Download.** One GET per quarter through `SecClient` (`SEC_USER_AGENT`, gzip, the
  `TELL_RATE_EDGAR_RPS` budget and the shared retry policy) to the first of
  `dataset_urls(year, quarter)` that answers. The SEC moved the data sets: as linked on
  2026-10-13, 2026 Q2 and Q3 live under
  `https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/{YYYY}q{n}_d.zip`
  and 2026 Q1 and earlier under `.../files/structureddata/data/form-d-data-sets/...`
  (`FORMD_DATASET_URLS`, module constants with a test; 2008-2012 sets often end `_d_0.zip`
  and need `--file`). Quarters from 2026 Q2 try the new location first, older ones the old;
  a 404 or 403 moves to the other (sec.gov answers a missing file with either). The ZIP (a
  few MB) is cached as `{YYYY}q{n}_d.zip` in `../tell-private/cache/formd/` (or
  `--cache-dir`); a cached, readable ZIP is never fetched again. 404 at both means the
  quarter is not posted yet (`DatasetNotPublishedError`); a 403 at both is raised as such
  (not posted yet, or a User-Agent the SEC rejects). Either way, check the data-sets page
  and use `--file`; it is recorded as a `failed` run.
- **Contact guard.** While `SEC_USER_AGENT` still holds the placeholder default (it contains
  `example.invalid`), any download is refused before a request is made: exit 2, a message on
  stderr, no `runs` row. `--file` and cached quarters need no contact.
- **Window.** `--since-days N` (default 120) writes only filings whose filing date is on or after
  today (UTC) minus N days; the rest are counted in `notes.outside_window`. The 13.1 cohort is
  the last 90 days; the 30-day margin covers a report dated a little later. Use a larger N for
  the 13.2 retrospective (2025 Q4 to 2026 Q2).
- **Write.** Events keep `source = 'edgar'`, `payload.backfill = true` and the daily key, inside
  one `edgar_backfill` run (`runs.notes`: `datasets`, `since_days`, `cutoff`, `outside_window`,
  `linked_by_cik`, `by_type`). A rerun, or a filing the daily index already delivered, inserts
  nothing (`skipped`). An issuer whose CIK is already a company's `cik` is linked at write
  time, as the daily path does; every other filing waits for `tell resolve`. A D/A whose
  original is in an earlier quarter stays a `form_d_amendment` event (never scored as funding).
- **Health.** Backfilled events are left out of the freshness p95 (`health.json`, the ops
  report), like first-poll baselines: they measure the archive's age, not tell's lag.
  `edgar_backfill` is not a scheduled or health source.
- **Posting lag.** SPEC §3.6 notes the data sets were posted through 2026 Q1 as of October
  2026. If the newest posted quarter ends before the 90-day cohort window, evaluate the report
  at the end of that quarter (`tell backtest speed-vs-press --as-of YYYY-MM-DD`) and collect the
  press with matching windows (`docs/sources/news.md`, "Cohort runs").

## Fixtures (`tests/fixtures/edgar/`)

**Provenance: hand-crafted.** A one-off live request to the daily index with the project
User-Agent (`tell/0.1 (+https://github.com/karank2512/tell-ai)`, no contact address) was refused
with 403 "Undeclared Automated Tool", so every fixture was written by hand to the documented
EDGAR shapes with fictional issuers (CIKs 999xxxx) and re-recording is a cutover task once
`SEC_USER_AGENT` holds a real contact (`docs/CUTOVER_CHECKLIST.md`, step 5). The ticker file uses real,
public CIK/ticker pairs of well-known listed companies.

Cutover checklist: re-record the hand-crafted EDGAR fixtures once `SEC_USER_AGENT` carries a
real contact address, and diff them. `make refresh-fixtures` is a placeholder (there is no
automated recorder); the manual procedure is `docs/CUTOVER_CHECKLIST.md`, step 5.

| File | What |
| --- | --- |
| `form.20261001.idx` | Daily index, 11 rows incl. `10-K`, `424B2`, `8-K/A`, `S-1/A`, `SC 13G` |
| `formd_1.xml`, `formd_2.xml` | Form D `primary_doc.xml` (X0708); `formd_2` has `Indefinite` amounts |
| `formd_amendment.xml` | D/A for `formd_1` (file number `021-555001`), higher `totalAmountSold` |
| `drift.xml` | `formd_1` without `totalOfferingAmount` |
| `normal.json`, `amendment.json`, `empty.json`, `drift.json` | JSON renderings (`FormDFilings`) the generic contract / idempotency tests and `run_fixture` read |
| `submissions_CIK.json`, `submissions_formd_issuer.json` | Trimmed `data.sec.gov/submissions` documents (8-K filer, Form D issuer) |
| `company_tickers_exchange.json` | 11 rows of the ticker file |
| `quarterly/*.tsv` | Synthetic 3-row DERA data set (two D, one D/A) |

Sample `normal.json` element (abridged):

```json
{"accessionNumber": "0009990001-26-000001", "cik": "9990001", "formType": "D",
 "dateFiled": "2026-10-01", "fileNumber": "021-555001",
 "primaryIssuer": {"cik": "0009990001", "entityName": "Example Robotics, Inc.",
   "issuerAddress": {"city": "SAN FRANCISCO", "stateOrCountry": "CA", "zipCode": "94107"}, "...": "..."},
 "offeringData": {"offeringSalesAmounts": {"totalOfferingAmount": 18000000,
   "totalAmountSold": 12500000, "totalRemaining": 5500000, "...": "..."}, "...": "..."}}
```
