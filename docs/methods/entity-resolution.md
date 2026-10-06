# Method: entity resolution (name to domain)

Spec: `docs/SPEC.md` section 6 (normalization, rules, review queue, gold set), section 5
(`company_aliases`, `review_queue`), section 12 (Resolution test family). Code:
`tell/resolve/normalize.py`, `tell/resolve/match.py`, `tell/resolve/review.py`,
`tell/resolve/review_app.py` (Streamlit, optional). CLI: `tell resolve run [--limit N]
[--retry]`, `tell resolve backfill-aliases`, `tell resolve review-export PATH`, `tell resolve
review-import PATH`, `tell resolve stats`. Tests: `tests/resolve/` (rules, resolver, review
round trip, CLI) and `tests/test_resolve.py` (gold-set precision and recall).

The domain is the identity. ATS postings arrive already linked (discovery found the board on
the company's domain). Form D filings, other EDGAR filings and the occasional news item
arrive as names, and a name has to earn its link through deterministic, logged rules.

## Normalization (`normalize_name`, `normalize_name_alt`, `domain_label`)

Applied to every name, and to every stored alias, in this order:

1. Unicode NFKC, casefold, strip accents (`Société Générale` -> `societe generale`).
2. Remove legal suffixes at the **end** only: `inc`, `inc.`, `incorporated`, `llc`, `l.l.c.`,
   `ltd`, `limited`, `corp`, `corporation`, `co`, `company`, `plc`, `gmbh`, `s.a.`, `pbc`,
   `holdings`; `labs` only when another word precedes it (`Acme Labs` -> `acme`, `Labs` stays
   `labs`). Stripping repeats (`Acme Holdings, Inc.` -> `acme`) and never removes the last
   remaining word (`Corporation` stays `corporation`). Suffixes elsewhere are untouched
   (`Company Store` stays).
3. Remove punctuation except internal hyphens (`e-commerce` keeps its hyphen, `- Ltd` does
   not); apostrophes are deleted (`O'Reilly` -> `oreilly`), other punctuation becomes a
   space (`AT&T` -> `at t`); collapse whitespace; drop a leading `the`.
4. A second, *alt* form with digits spelled out (`3Scale` -> `threescale`) and
   `tech` / `technology` / `technologies` unified to `technologies`; `ai` is kept.
5. Domain label: `tldextract(domain).domain` with the same punctuation rules
   (`acme-labs.co.uk` -> `acme-labs`). `tldextract` runs on its bundled suffix list, never the
   network.

`slug_words` turns an ATS board identifier into words (`acme-labs-inc` -> `acme labs inc`)
so the same rules apply to board tokens (`-> acme`).

## Aliases

`company_aliases (company_id, alias, source)` holds normalized names. `tell resolve
backfill-aliases` seeds it (idempotent) with every company's normalized `name` and domain
label (source `manual`) and, for every linked Greenhouse / Lever / Ashby / SmartRecruiters
board, the identifier as words (source `ats_board`). Boards carry no display name in the
schema (discovery logs the name it verified against but does not store it), so the identifier
is the only board-side alias. Linking a filing adds the issuer name and every
`issuerPreviousNameList` entry (source `edgar`), which is how renamed companies keep their
history; a review verdict adds a `manual` alias.

## Rules (`match_name`), top to bottom; the first that fires sets the confidence

| Rule | Condition | Confidence | Outcome |
| --- | --- | --- | --- |
| R1 domain | the source URL is on the watched domain (`on_domain_company_id`: board found by discovery, company RSS) | 0.98 | auto-link |
| R2 exact alias | normalized name, or its alt form, equals an alias held by exactly one company | 0.95 | auto-link |
| R3 name + geography | `rapidfuzz.fuzz.token_set_ratio(norm, alias) >= 92` and issuer state == `hq_state` (city == `hq_city` when either state is missing) | 0.90 | auto-link, logged |
| R4 name only, unique | ratio >= 95 and exactly one candidate company (ratio >= 85) in the whole table | 0.85 | auto-link, logged |
| R5 ambiguous | ratio >= 85 with two or more candidate companies, or one candidate not linked above | 0.60 | review queue |
| R6 none | otherwise | 0 | unlinked event, kept with `company_id NULL` |

Candidates are the top 5 companies by `token_set_ratio`, scored against every alias in both
the plain and the alt form (`process.extract` over the whole alias list; ~10k aliases take
milliseconds, and the list is loaded once per run). Full US state names fold to the two-letter
codes Form D uses.

Two interpretation notes, both conservative:

- An alias shared by two companies makes R2 ambiguous; it falls through so geography (R3)
  can decide, otherwise the event goes to review (R5).
- The spec words R5 as "85 to 91 with one candidate". A single candidate at 92-94 whose
  geography does not match is not covered by any row; it is sent to review too rather than
  dropped as R6. If this is not wanted, lower `GEO_RATIO` or add an explicit R6 clause.

`token_set_ratio` scores a token subset as 100 (`acme technologies` vs `acme` = 100), so
R4's uniqueness requirement is what protects precision: a name that resembles two companies
is never auto-linked. If the review queue runs hot (spec target: under 20 open items a week),
tighten `REVIEW_RATIO` upward.

## Audit payload

Every decision writes `payload["match"]` on the event:

```json
{
  "rule": "R3",
  "ratio": 96.0,
  "candidates": [{"company_id": 12, "alias": "acme", "ratio": 96.0}],
  "confidence": 0.9,
  "resolved_at": "2026-10-05T12:00:00+00:00"
}
```

`candidates` is at most five entries. A news event keeps its collector-side rule under
`source_rule`; a pair a human already rejected is marked `"rejected": true` and left
unlinked. A review verdict rewrites `rule` to `manual` with `confidence` 1.0 and the
`review_id`, keeping the candidate list.

On auto-link the resolver also fills `companies.cik` (when empty and not held by another
company) and `companies.legal_name` (when empty) from an EDGAR filing.

## Resolver (`resolve_events`)

Scans `events` with `company_id NULL`: Form D (`issuer_name`, `issuer_state`,
`issuer_city`, `previous_names`, `cik`), other EDGAR forms (`company_name`), news events
(the company name in the Google News query). R5 opens one `review_queue` item
(`kind='entity_match'`, payload `{event_id, name, state, city, candidates}`) per event and
never a second one for the same `event_id`. Events that already carry an R1-R6 decision are
skipped, so a second run changes nothing; `--retry` re-evaluates them after aliases or
companies were added (the rejected-pair check and the one-item-per-event rule still hold).

## Review workflow

CSV is the default:

1. `tell resolve review-export review.csv` writes the open items: `id, kind, event_id, name,
   state, city, candidate_1_id, candidate_1_alias, candidate_1_ratio, ... candidate_3_*,
   verdict, company_id_or_domain, new_company_name`.
2. Fill `verdict` with `link` (plus a company id or domain), `create` (plus the new company's
   domain and optionally its name) or `reject`. Blank rows are skipped.
3. `tell resolve review-import review.csv` applies them: `link` sets the event's
   `company_id`, adds a `manual` alias so the pair never returns, marks the item `resolved`;
   `create` inserts the company (domain required) and then links; `reject` marks the item
   `rejected` and the resolver skips the same (name, candidates) pair from then on. Errors
   are reported per row and never stop the import.

The Streamlit page (`streamlit run tell/resolve/review_app.py`) offers the same three buttons
over the same functions. It refuses to open unless `TELL_REVIEW_PASSWORD` is set and compares
the typed password with `secrets.compare_digest`. It lives in the repo and is not deployed.

## Measured on the gold set

`tests/test_resolve.py` seeds companies from the gold CSV (`tests/fixtures/gold_sample.csv`
always; the full `tell-private/gold/companies.csv` when `TELL_GOLD_SET` points at it) and,
for every row with a supported board, an `ats_boards` row linked to the company as discovery
links it. It runs `backfill-aliases`, then feeds every legal name (with the row's state and
city) through `match_name` as a Form D issuer, and every board the way a job event arrives:
carrying its board's `company_id`, so it must come back through R1. Negatives for 30% of the
rows feed another company's legal name with this row's geography, which must not link to
this row. Besides the two floors the test requires zero wrong auto-links.

Board identifiers are deliberately *not* matched as free text. Production never does that
(collectors copy `board.company_id` onto the event), and the gold set holds near-duplicate
pairs where it would go wrong: one company's Ashby slug is a single word that is another
gold company's whole name, so the slug as free text hits R2 on the wrong company. Seeding the
board instead makes that word an alias of both companies, which R2 treats as ambiguous and R3
settles by geography.

Measured 2026-10-05 against the gold set built the same day (`build_gold_set.py`, seed
20261005; draft, not yet spot-checked by a person):

| Set | n | cases | negatives | precision | recall | by rule |
| --- | --- | --- | --- | --- | --- | --- |
| public sample (`gold_sample.csv`) | 50 | 83 | 15 | 1.0000 | 1.0000 | R1 33, R2 51, R3 9, R4 1, R5 4 |
| full private set (`companies.csv`) | 300 | 492 | 90 | 1.0000 | 0.9837 | R1 192, R2 336, R3 38, R4 6, R5 2, R6 8 |

Recall by group on the full set: board 355/360, no_board 60/60, hard:near_duplicate 17/17,
hard:suffix_punct_digit 18/18, hard:international 18/19, hard:renamed 16/18. The eight
misses are all Form D issuers whose legal name is a former corporate name sharing no token
with the current company name (ratios 52-78, so R6, unlinked and kept). That is the
renamed-company case the spec hands to `issuerPreviousNameList`: once one filing is linked
the previous names become aliases and later filings go through R2. No gold row was changed.

Gates: precision >= 0.97 and recall >= 0.85 pass on both sets. The numbers go into the README
verbatim once the gold set has been spot-checked.

## Deliberately not done

No LinkedIn lookups, no paid enrichment, no LLM in the auto-link path. An LLM may propose
answers for the review queue later, with the human still clicking the button.
