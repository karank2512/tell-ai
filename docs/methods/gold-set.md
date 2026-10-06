# Method: the entity-resolution gold set

Spec: `docs/SPEC.md` section 6 ("Gold set and the metric"). Plan: `docs/PLAN.md` Phase 5.
Decision: `docs/decisions/0006-gold-set-location-and-public-sample.md`. Builder:
`scripts/build_gold_set.py`.

The gold set is the hand-checkable answer key that `tests/test_resolve.py` scores the
matcher against: for each company, its domain (the identity), display name, legal name, SEC
CIK when one exists, headquarters, and job board. Precision (correct links over asserted
links) and recall (links found over links that exist) are measured over it; the release
gate is precision >= 0.97 and recall >= 0.85.

## Where it lives

| File | Rows | Public | Purpose |
| --- | --- | --- | --- |
| `tell-private/gold/companies.csv` | 300 | no | The full set; the release gate runs on it on `main` after cutover (read-only deploy key). |
| `tests/fixtures/gold_sample.csv` | 50 | yes | Stratified sample so fork CI always runs the resolution tests. |

Both files have the same columns, UTF-8, sorted by `domain`:

| Column | Meaning |
| --- | --- |
| `domain` | Registrable domain, lowercase (`tell.discovery.registrable_domain`). |
| `name` | Display name as the YC directory lists it. |
| `legal_name` | Legal entity name; see `verification_source` for how sure it is. |
| `cik` | 10-digit zero-padded SEC CIK of the matched Form D issuer (or listed company); blank when none was found. |
| `hq_city`, `hq_state`, `hq_country` | From the YC location (US: `City, ST, USA`; elsewhere city plus ISO-3166 alpha-2 country, state blank); filled from the Form D business address when YC has none. |
| `board_vendor`, `board_identifier` | Job board found by `tell.discovery.ats.discover_domain` (Greenhouse, Lever, Ashby, SmartRecruiters), only when ownership verified (outcome `board`); blank otherwise. |
| `verification_source` | Where `legal_name` came from (below). |
| `notes` | `set=<group>` first, then short facts about the case (former name, the near-duplicate partner's domain, discovery outcome). Never anything about people. |

`verification_source` values:

- `edgar_fts`: a Form D issuer found by EDGAR full-text search whose normalised name equals
  the YC name (or one of its YC former names) and whose business state agrees with the YC
  state (city as fallback); or, for companies YC lists as public, the SEC
  `company_tickers_exchange.json` name. `cik` is filled.
- `discovery`: the job board's own company name carries a legal suffix and matches the
  name.
- `yc`: the YC directory name already carries a legal suffix (`Domu Technology Inc.`).
- `unverified`: best-effort legal form built from the name and country (`, Inc.` for the
  US and unknown countries, `Ltd` for GB, `GmbH` for DE, `Private Limited` for IN, ...).

## Selection

1. **Universe.** The public YC directory mirror `https://yc-oss.github.io/api/companies/all.json`
   (`tell.discovery.yc.YC_DIRECTORY_URL`; see `docs/sources/yc-directory.md`). Kept:
   companies with status `Active` or `Public` and a website; domains listed by two or more
   YC companies are dropped (the domain would not identify one company); a short
   exclusion list drops names that embed a person's name.
2. **Groups** (seeded shuffle, `--seed 20261005`):
   - `board` (60 %): hiring companies, discovered in order until 180 resolve to a
     supported board with verified ownership (outcome `board`). These exercise rule R1
     and the name rules with a known-good answer.
   - `no_board` (20 %): non-hiring companies, discovered in order until 60 come back
     `no_board`. These measure that nothing links where nothing should.
   - `hard:*` (20 %, 15 per kind; near-duplicates round down to whole pairs, the spare row goes to international): `renamed` (a YC former name unlike the current one),
     `near_duplicate` (pairs of different companies whose names start with the same word,
     both members kept), `suffix_punct_digit` (legal suffixes, `Labs`, `.ai`, digits,
     punctuation), `international` (non-US headquarters, one per country round-robin).
     Kept whatever discovery found.
3. **Discovery** runs against a throwaway database `tell_gold_<hex>` (created and migrated
   like `tests/conftest.py` does, dropped at exit); `tell_dev` is never touched.
4. **EDGAR.** One full-text query per name (`q="<name>"`, `forms=D`; former names too
   when the current name finds nothing) plus the ticker file once. A match must be
   unique; several plausible issuers leave the row `unverified`. Investment vehicles that
   merely mention the company (`<Fund> <Company> LLC`, `<Company> Seed a Series of ...`)
   do not normalise to the company name and are rejected.

## Counts (build of 2026-10-05)

| Group | Rows | With a verified board | With a CIK |
| --- | --- | --- | --- |
| `board` | 180 | 180 | 28 |
| `no_board` | 60 | 0 | 3 |
| `hard:renamed` | 15 | | |
| `hard:near_duplicate` (7 pairs) | 14 | | |
| `hard:suffix_punct_digit` | 15 | | |
| `hard:international` (16 countries) | 16 | | |
| hard, total | 60 | 12 | 5 |
| **total** | **300** | **192** | **36** |

By `verification_source`: `edgar_fts` 36, `unverified` 259, `yc` 4, `discovery` 1.

Boards by vendor: Ashby 145, Greenhouse 27, Lever 16, SmartRecruiters 4 (all found live on
2026-10-05). Discovery looked at 781 domains in all (542 `no_board`, 219 `board`, 15
`review`, 5 `unsupported`); hiring YC companies resolved to a verified board about one time
in three. `hq_country`: US 220, blank 15 (YC lists only "Remote" or a bare country), 21
other countries.

32 rows carry a note `same-name form d issuer(s) elsewhere, not linked`: a Form D issuer
with exactly the company's normalised name exists but in another state or city. These are
the natural negative cases for rule R3 and the first rows to spot-check.

Public sample (`tests/fixtures/gold_sample.csv`): 50 rows, 30 `board`, 10 `no_board`, 10
hard (2 near-duplicate pairs, 2 renamed, 2 suffix/punctuation/digit, 2 international); 33
with a board, 6 with a CIK.

## Caveats

- **Legal names are mostly unverified.** Most early-stage companies have no Form D under
  their brand name, or one under a name the conservative match rejects. Unverified legal
  names are a plausible form, not a fact; the matcher's precision/recall is scored on
  `name` against `domain`, and `legal_name` matters only for rows with a CIK.
- **Form D matches can still be wrong** when an unrelated issuer with the same name sits in
  the same state; those rows are the first thing to spot-check.
- **Board columns are a snapshot.** Boards move (Lever to Ashby is common); discovery was
  run once on the build date. `no_board` means discovery found none, not that the company
  has no careers page: YC-hosted job pages and vendors tell does not support are invisible.
- **Selection bias.** YC companies only, skewed to US software startups; the `hard` group
  is deliberately over-weighted relative to real traffic.
- **SEC access.** The builder sends `SEC_USER_AGENT` (a placeholder until cutover). If
  sec.gov answers 403 it stops calling sec.gov and leaves CIKs blank; on this build
  sec.gov was reachable and never answered 403 (about 495 requests at 2 per second with
  the placeholder User-Agent), so the CIK column is as complete as the matching rule allows.
- **No people.** Only company facts are read from the YC directory (no founders, no
  descriptions); notes name companies and domains only.

## Refreshing

```sh
uv run python scripts/build_gold_set.py \
    --out ../tell-private/gold/companies.csv \
    --sample-out tests/fixtures/gold_sample.csv \
    --limit 300 --sample-size 50 --cache-dir /path/to/cache
```

Flags: `--seed` (selection), `--seed-file` (a saved `all.json` instead of fetching),
`--rps` (per-host request rate, default 2), `--workers` (parallel discovery, default 8),
`--no-edgar`, `--keep-db`. Discovery and EDGAR answers are cached as JSON lines in
`--cache-dir`, so a rerun with the same cache repeats no request and reproduces the same
files. A refreshed set must be spot-checked by hand before the gate uses it.
