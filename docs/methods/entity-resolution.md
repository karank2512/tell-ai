# Method: entity resolution (name to domain)

Spec: `docs/SPEC.md` section 6 (normalization, rules, review queue, gold set), section 5
(`company_aliases`, `review_queue`), section 12 (Resolution test family). Code:
`tell/resolve/normalize.py`, `tell/resolve/match.py`, `tell/resolve/review.py`,
`tell/resolve/review_app.py` (Streamlit, optional). CLI: `tell resolve run [--limit N]
[--retry]`, `tell resolve backfill-aliases`, `tell resolve review-export PATH`, `tell resolve
review-import PATH`, `tell resolve stats`. Tests: `tests/resolve/` (rules, resolver, review
round trip, CLI) and `tests/test_resolve.py` (gold-set precision and recall). Decisions that
go beyond the spec's wording: `docs/decisions/0018-entity-resolution-rule-refinements.md`.

The domain is the identity. ATS postings arrive already linked (discovery found the board on
the company's domain). Form D filings, other EDGAR filings and the occasional news item
arrive as names, and a name has to earn its link through deterministic, logged rules.

## Normalization (`normalize_name`, `normalize_name_alt`, `domain_label`)

Applied to every name, and to every stored alias, in this order:

1. Unicode NFKC, casefold, strip accents (`Société Générale` -> `societe generale`).
2. Remove legal suffixes at the **end** only: the spec list `inc`, `inc.`, `incorporated`,
   `llc`, `l.l.c.`, `ltd`, `limited`, `corp`, `corporation`, `co`, `company`, `plc`,
   `gmbh`, `s.a.`, `pbc`, `holdings` (`SPEC_SUFFIXES`) plus the common non-US forms in
   `INTERNATIONAL_SUFFIXES` (`pte`, `pty`, `pvt`, `private`, `sdn bhd`, `ltda`, `sas`,
   `sarl`, `s.l.`, `srl`, `s.p.a.`, `s.a.p.i. de c.v.`, `ag`, `ug`, `ab`, `as`, `a/s`,
   `aps`, `oy`, `bv`, `nv`, `kk`, `llp`, `lp`; ADR 0018); `labs` only when another word
   precedes it (`Acme Labs` -> `acme`, `Labs` stays `labs`). Stripping repeats
   (`Acme Holdings, Inc.` -> `acme`, `Razorpay Private Limited` -> `razorpay`) and never
   removes the last remaining word (`Corporation` stays `corporation`, `AG` stays `ag`).
   Suffixes elsewhere are untouched (`Company Store` stays). Brackets and quotes are cut
   before the name is tokenised, so `Acme (Inc.)` and `"Acme Inc"` are `acme` too.
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

Why the international suffixes: the gold set's `hard:international` rows file as
`Pte. Ltd.`, `Private Limited`, `SAS`, `ApS`, `AG`, `AB`; a suffix left in the name is an
extra token, and the superset guard below (rightly) refuses to auto-link a name with an
extra token. With the list, those names are exact aliases (R2). Only forms that are not
ordinary English words are listed (`spa`, `se` are not).

## Aliases

`company_aliases (company_id, alias, source)` holds normalized names. `tell resolve
backfill-aliases` seeds it (idempotent) with every company's normalized `name` and domain
label (source `manual`) and, for every linked Greenhouse / Lever / Ashby / SmartRecruiters
board, the identifier as words (source `ats_board`). Boards carry no display name in the
schema (discovery logs the name it verified against but does not store it), so the identifier
is the only board-side alias.

Linking a filing, whether by the resolver or by a review verdict, goes through one function
(`link_event`): it sets `company_id`, fills `companies.cik` (when empty and not held by
another company) and `companies.legal_name` (when empty), and adds the issuer name and every
`issuerPreviousNameList` entry as aliases (source `edgar`). That is how renamed companies keep
their history: a later filing under a former name is an exact alias (R2). A review verdict
additionally writes the queue item's name as a `manual` alias.

## Rules (`match_name`), top to bottom; the first that fires sets the confidence

| Rule | Condition | Confidence | Outcome |
| --- | --- | --- | --- |
| R1 domain | the source URL is on the watched domain (`on_domain_company_id`: board found by discovery, company RSS) | 0.98 | auto-link |
| R2 exact alias | normalized name, or its alt form, equals an alias held by exactly one company | 0.95 | auto-link |
| R3 name + geography | `rapidfuzz.fuzz.token_set_ratio(norm, alias) >= 92`, issuer state == `hq_state` (city == `hq_city` when either state is missing), the geography singles out **exactly one** company, and the superset guard holds | 0.90 | auto-link, logged |
| R4 name only, unique | ratio >= 95, exactly one candidate company (ratio >= 85) in the **whole table**, and the superset guard holds | 0.85 | auto-link, logged |
| R5 ambiguous | ratio >= 85 with two or more candidate companies, or one candidate not linked above | 0.60 | review queue |
| R6 none | otherwise | 0 | unlinked event, kept with `company_id NULL` |

Candidates are scored with `token_set_ratio` against every alias in both the plain and the
alt form. Two `process.extract` passes per form: the top 20 aliases (so an R6 decision still
logs its nearest misses) and *every* alias at or above 85 (`limit=None, score_cutoff=85`),
collapsed to one entry per company. So R4's "exactly one candidate company in the whole
table" really is the whole table: a company holding 25 aliases cannot push a second
company's 88-ratio alias out of a truncated pool. Full US state names fold to the two-letter
codes Form D uses. Ten thousand aliases take milliseconds; the index is built once per run
and kept current as the run adds aliases.

### The superset guard (R3 and R4)

`token_set_ratio` scores any token superset 100: with only `stripe` in the table, "Blue
Stripe Bakery", "Anti Stripe Coalition" and "Stripe Press" all score 100 and, in Stripe's
state, the spec's R3 would auto-link them at 0.90 (R4 at 0.85 with no state). Production
Form D issuers routinely share a leading word with watched companies, and the gold
negatives (another gold company's legal name) never contain supersets, so measured precision
alone cannot see this.

R3 and R4 therefore also require a **guard ratio** of at least 85 (`GUARD_RATIO`) against one
of the company's aliases:

```
guard = min(token_sort_ratio(norm, alias), 100 * matched_chars / len(norm))
```

`token_sort_ratio` is the plain Indel ratio of the sorted tokens (`200 * M / (len(norm) +
len(alias))`); the second term is the share of the *query's* characters the alias explains.
The plain ratio alone catches short supersets (`acme robotics` vs `acme`: 47) but dilutes
with length (`domu technologies press` vs `domu technologies`: 85), which is what the
coverage cap fixes (74). A one-letter typo passes (`beakon health` vs `beacon health`: 92),
a short query against a long alias does not (`acme` vs `acme robotics`: 47). The guard is
computed for both the plain and the alt forms and the best value over the company's aliases
is kept as `sort_ratio` on the candidate.

A candidate that fails the guard falls to **R5 review**, never to a link: the guard only
removes auto-links, so it is consistent with the spec table. R2 is untouched (an exact alias
is an exact alias). The cost is review volume: every "<company> Press LLC" style issuer is a
queue item instead of a wrong link. The gold-set test feeds two supersets per sampled row and
counts any auto-link among them against precision.

### Interpretation notes (ADR 0018)

- An alias shared by two companies makes R2 ambiguous; it falls through so geography (R3)
  can decide, otherwise the event goes to review (R5).
- R3 with two companies at >= 92 that both match the issuer's geography is a tie (two
  companies both called "Method" in New York). The rule does not fire; the event goes to
  review with both candidates in the audit record. Insertion order never decides a link.
- The spec words R5 as "85 to 91 with one candidate". A single candidate at 92-94 whose
  geography does not match is not covered by any row; it is sent to review too rather than
  dropped as R6.
- Only EDGAR and news events are matched by name. A GitHub `repo_created` payload carries a
  repository `name` and a job payload a `title`; neither is a company name, so events from
  other sources are skipped (`no_name`) instead of being resolved by whatever `name` they
  carry.
- A rejected review item writes no alias (there is no negative alias in the schema). The
  resolver remembers the rejected (normalized name, candidate company set) and skips the pair
  while the current candidates are a subset of the rejected set; the pair legitimately comes
  back when a new company enters the candidate set.

If the review queue runs hot (spec target: under 20 open items a week), tighten
`REVIEW_RATIO` upward.

## Audit payload

Every decision writes `payload["match"]` on the event:

```json
{
  "rule": "R3",
  "ratio": 96.0,
  "candidates": [{"company_id": 12, "alias": "acme", "ratio": 96.0, "sort_ratio": 96.0}],
  "confidence": 0.9,
  "resolved_at": "2026-10-05T12:00:00+00:00"
}
```

`candidates` is at most five entries, each with the company's best alias, its
`token_set_ratio` and the guard value `sort_ratio`. A news event keeps its collector-side
rule under `source_rule`; a pair a human already rejected is marked `"rejected": true` and
left unlinked. A review verdict rewrites `rule` to `manual` with `confidence` 1.0 and the
`review_id`, keeping the candidate list.

R1 links are made by the collectors, not by the resolver: `sync_board_postings` copies
`board.company_id` onto every job and tech event of a linked board, and stamps
`payload["match"] = {"rule": "R1", "ratio": null, "candidates": [], "confidence": 0.98,
"via": "ats_board"}` (`tell.collectors.base.R1_MATCH`) at the same time, so every link in
the table is auditable. The news collector stamps its own `{"rule": "R1", "confidence":
0.98}` on items from a company's own feed. (GitHub events link through
`companies.github_org`, a configured identity rather than a match; they carry no record.)

## Resolver (`resolve_events`)

Scans `events` with `company_id NULL` from the EDGAR and news sources: Form D
(`issuer_name`, `issuer_state`, `issuer_city`, `previous_names`, `cik`), other EDGAR forms
(`company_name`), news events (the company name in the Google News query). R5 opens one
`review_queue` item (`kind='entity_match'`, payload `{event_id, name, state, city,
candidates}`) per event and never a second one for the same `event_id`. Events that already
carry an R1-R6 decision are skipped, so a second run changes nothing; `--retry` re-evaluates
them after aliases or companies were added (the rejected-pair check and the
one-item-per-event rule still hold).

## Review workflow

CSV is the default:

1. `tell resolve review-export review.csv` writes the open items: `id, kind, event_id, name,
   state, city, candidate_1_id, candidate_1_alias, candidate_1_ratio, ... candidate_3_*,
   verdict, company_id_or_domain, new_company_name`.
2. Fill `verdict` with `link` (plus a company id or domain), `create` (plus the new company's
   domain and optionally its name) or `reject`. Blank rows are skipped.
3. `tell resolve review-import review.csv` applies them: `link` links the event with the
   same enrichment an auto-link gets (`link_event`: `cik`, `legal_name`, issuer and
   previous-name aliases) plus a `manual` alias for the item's name, and marks the item
   `resolved`; `create` inserts the company (domain required) and then links; `reject` marks
   the item `rejected` and the resolver skips the same (name, candidates) pair from then on.
   Errors are reported per row and never stop the import.

The Streamlit page (`streamlit run tell/resolve/review_app.py`) offers the same three buttons
over the same functions. It refuses to open unless `TELL_REVIEW_PASSWORD` is set and compares
the typed password with `secrets.compare_digest`. It lives in the repo and is not deployed.

## Measured on the gold set

`tests/test_resolve.py` seeds companies from the gold CSV (`tests/fixtures/gold_sample.csv`
always; the full `tell-private/gold/companies.csv` when `TELL_GOLD_SET` points at it) and,
for every row with a supported board, an `ats_boards` row linked to the company as discovery
links it. It runs `backfill-aliases`, then reports two tallies:

- **names** (the headline metric, the spec's "runs the matcher over the gold set's names"):
  every legal name, with the row's state and city, through `match_name` as a Form D issuer.
  Only the name rules R2-R6 can fire. Negatives for 30% of the rows: another company's legal
  name with this row's geography (must not link to this row, asserted) and two token
  supersets of this row's name (`<name> Press LLC`, `Blue <name> Bakery`, with this row's
  geography; any auto-link counts as a wrong link). The gate (precision >= 0.97, recall >=
  0.85, zero wrong links) is applied to this tally.
- **boards**: every board the way a job event arrives, carrying its board's `company_id`,
  so it must come back through R1. These cases are tautological (the expected id is the
  input); they verify the production path and are asserted to be 100% R1, but they are kept
  out of the headline numbers.

Board identifiers are deliberately *not* matched as free text. Production never does that
(collectors copy `board.company_id` onto the event), and the gold set holds near-duplicate
pairs where it would go wrong: one company's Ashby slug is a single word that is another
gold company's whole name, so the slug as free text hits R2 on the wrong company. Seeding the
board instead makes that word an alias of both companies, which R2 treats as ambiguous and R3
settles by geography.

Measured 2026-10-05 against the gold set built the same day (`build_gold_set.py`, seed
20261005; draft, not yet spot-checked by a person), after the ADR 0018 refinements:

| Set | n | name cases | negatives (of which supersets) | precision | recall | by rule |
| --- | --- | --- | --- | --- | --- | --- |
| public sample (`gold_sample.csv`), names | 50 | 50 | 45 (30) | 1.0000 | 1.0000 | R2 57, R3 4, R5 34 |
| full private set (`companies.csv`), names | 300 | 300 | 270 (180) | 1.0000 | 0.9733 | R2 371, R3 9, R5 182, R6 8 |
| public sample, boards (R1 check) | 50 | 33 | 0 | 1.0000 | 1.0000 | R1 33 |
| full private set, boards (R1 check) | 300 | 192 | 0 | 1.0000 | 1.0000 | R1 192 |

Name-rule recall by group on the full set: board 175/180, no_board 60/60,
hard:near_duplicate 14/14, hard:suffix_punct_digit 15/15, hard:international 15/16,
hard:renamed 13/15. The eight misses are all Form D issuers whose legal name is a former
corporate name sharing no token with the current company name (ratios 52-78, so R6, unlinked
and kept). That is the renamed-company case the spec hands to `issuerPreviousNameList`: once
one filing is linked the previous names become aliases and later filings go through R2. The
R5 count is almost entirely the 180 superset negatives sent to review by the guard. Zero
wrong auto-links on either set. No gold row was changed.

Before ADR 0018 the headline was reported as 0.9837 recall on the full set; that number
included the 192 tautological R1 board cases. The name-rule recall was 0.9733 then and is
0.9733 now. On the way there the superset guard alone sent 27 international-suffix names
(`Pte. Ltd.`, `Private Limited`, `SAS`, `ApS`, `AG`, ...) to review (recall 0.8833): a
suffix the spec list does not know is an extra token, which is exactly what the guard
refuses. The international suffix list makes those names exact aliases (R2), which is why
R2 grew from 254 to 283 positive cases and R3 shrank from 38 to 9.

Gates: names precision >= 0.97 and recall >= 0.85 pass on both sets. The numbers go into the
README verbatim once the gold set has been spot-checked.

## Deliberately not done

No LinkedIn lookups, no paid enrichment, no LLM in the auto-link path. An LLM may propose
answers for the review queue later, with the human still clicking the button.
