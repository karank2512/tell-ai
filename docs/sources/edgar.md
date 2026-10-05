# SEC EDGAR

Module: `tell/collectors/edgar.py` (daily index, HTTP, 8-K / S-1, tickers, `COLLECTOR`) and
`tell/collectors/edgar_formd.py` (Form D contract, XML parser, events, quarterly backfill).
Spec: SPEC §3.6, §5 rule 4, §13.2. Source value `edgar`, kind `filings`.

## Endpoints

| What | URL | Used for |
| --- | --- | --- |
| Daily form index | `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx` | New filings per day; kept form types `D`, `D/A`, `8-K`, `S-1` (exact match, so `8-K/A` and `S-1/A` are dropped) |
| Form D document | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/primary_doc.xml` | Form D fields (below) |
| Filing folder | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-no-dashes}/` | `events.url` (the receipt) |
| Submissions | `https://data.sec.gov/submissions/CIK{10 digits}.json` | `fileNumber` for Form D / D/A, `items` for 8-K (`filings.recent`, parallel arrays) |
| CIK to ticker | `https://www.sec.gov/files/company_tickers_exchange.json` | `load_ticker_map()` -> `{10-digit cik: (ticker, exchange, name)}` |
| Quarterly Form D data sets | sec.gov/dera/data/form-d (TSV in ZIP) | `load_formd_quarterly()` backtest backfill |

The quarter in the index path comes from the date (`QTR1` = Jan-Mar ...). The CIK in archive
paths is unpadded; the accession number loses its dashes. A missing daily index (404:
weekends, holidays, today before publication) is skipped, not an error. By default a run
reads yesterday's and today's index on the New York calendar.

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
- Daily volume: one index per day plus, per Form D / D/A, one `primary_doc.xml` and one
  submissions JSON per distinct issuer CIK (cached within a run); per monitored 8-K filer one
  submissions JSON. Roughly 2 requests per Form D, so a busy day (~200 Form D) is ~400 requests.

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
`payload.file_number` (fallback: `payload.accession_number == previousAccessionNumber`) and
calls `base.update_event_payload` with the new amounts and an appended `amendments` entry
(`accession_number`, `date_filed`, amounts). No event is written, so no second funding
signal can exist. Amounts are overwritten only by the latest amendment by filing date;
replaying an amendment already in the list is a no-op. In one run all new D filings are
written before any D/A is applied. A D/A whose original is unknown becomes
`form_d_amendment` (`is_amendment: true`), which Phase 7 never scores as funding.

**Quarterly backfill (SPEC §13.2).** `load_formd_quarterly(zip_or_dir, run_id=, now=)` reads
`FORMDSUBMISSION.tsv`, `ISSUERS.tsv` and `OFFERING.tsv` (columns by header name; dates as
`DD-MON-YYYY` or ISO) and returns events with `payload.backfill = true`, the same keys as the
daily path (so overlaps dedupe), amendments merged in memory the same way, no `company_id`.

## Fixtures (`tests/fixtures/edgar/`)

**Provenance: hand-crafted.** A one-off live request to the daily index with the project
User-Agent (`tell/0.1 (+https://github.com/karankapur/tell)`, no contact address) was refused
with 403 "Undeclared Automated Tool", so every fixture was written by hand to the documented
EDGAR shapes with fictional issuers (CIKs 999xxxx) and re-recording is a cutover task once
`SEC_USER_AGENT` holds a real contact (`make refresh-fixtures`). The ticker file uses real,
public CIK/ticker pairs of well-known listed companies.

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
