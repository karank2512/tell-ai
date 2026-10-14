# Method: entity resolution (name to domain)

Spec: `docs/SPEC.md` section 6 (normalization, rules, review queue, gold set), section 5
(`company_aliases`, `review_queue`), section 12 (Resolution test family). Code:
`tell/resolve/normalize.py`, `tell/resolve/aliases.py` (the alias rule, distinctive tokens),
`tell/resolve/guards.py` (Form D guards), `tell/resolve/match.py`, `tell/resolve/hygiene.py`
(alias and review-queue maintenance), `tell/resolve/relink.py` (Form D link repair),
`tell/resolve/review.py`, `tell/resolve/review_app.py` (Streamlit, optional). CLI: `tell
resolve run [--limit N] [--retry]`, `tell resolve backfill-aliases`, `tell resolve
review-export PATH`, `tell resolve review-import PATH`, `tell resolve stats`, `tell resolve
prune-aliases [--dry-run|--apply]`, `tell resolve close-junk-reviews [--dry-run|--apply]`,
`tell resolve relink-formd [--dry-run|--apply]`. Tests: `tests/resolve/` (alias rule, rules,
Form D guards on production's own links, resolver, hygiene, relink repair, review round trip,
CLI) and `tests/test_resolve.py` (gold-set precision and recall). Decisions that go beyond
the spec's wording: `docs/decisions/0018-entity-resolution-rule-refinements.md`,
`docs/decisions/0033-alias-hygiene.md`, `docs/decisions/0034-form-d-linking-guards.md`
(proposed).

## Production measurement 2026-10-14

**Read this before quoting any number below.** On 2026-10-14 production had auto-linked 41
Form D filings to the 6,246 YC companies. Each was checked by hand against the SEC filing
and the company's site: 20 same company, 20 different, 1 unclear. **Production precision
was 0.50 (20 of 40 decided)**, while the gold set reported 1.0000.

| Rule | Links | Same | Different | Unclear |
| --- | --- | --- | --- | --- |
| R3 (fuzzy >= 92 + state) | 5 | 0 | 5 | 0 |
| R2 (exact alias) | 36 | 20 | 15 | 1 |
| Total | 41 | 20 | 20 | 1 |

- R3: all 5 were one-word near misses (Compass vs nCompass, Inferra vs Infera, AlpacaX vs
  Alpaca, Sylvan vs Sylvian, Scaler AI vs Scale AI).
- R2: common-word brands after `Labs` / `Holdings` were stripped (Payload, Hub, Manatee,
  Echo, Modal Labs, Sunflower Labs, Overlord Labs, Protocol Labs, Vanta Holdings, Jasper
  Holdings LP); issuers that were not Delaware corporations (a Cayman LP, a Wyoming LLC,
  Canadian corporations, a North Carolina corporation); incorporation years inconsistent
  with the YC batch; companies listed Inactive or Acquired. 4 of the 15 were knock-ons of
  the wrong R3 links, which had written the issuer's name as an exact alias.
- All 20 right links: a Delaware corporation, an Active YC company, an incorporation year
  that fits the batch.
- Every wrong link also wrote `edgar` aliases and set `companies.legal_name` and
  `companies.cik`, which the backtest cohort trusts.
- 788 R5 review items were open: 405 funds and pooled vehicles, and most of the rest had a
  two-token alias such as `lsk technologies` or `ai insurance` as top candidate at 85.71,
  sharing only the descriptor word with the issuer.

Why the gold set missed it: its legal names are mostly the brand plus a suffix, and no gold
name is an unrelated company that happens to share a brand word. That is the production
case. The rules that answer it (ADR 0034) are in [Form D guards](#form-d-guards-adr-0034)
below; the case table is a test fixture (`tests/resolve/formd_links_20261014.json`).

Under the new rules, on a local copy of production taken read-only the same day,
`tell resolve relink-formd --apply` keeps exactly the 20 links judged SAME, unlinks the
other 21 (20 to review, Jasper Holdings LP to R6), deletes the 7 `edgar` aliases the wrong
links minted, and resets 16 legal names and 16 CIKs; a second run changes nothing. `tell
resolve close-junk-reviews --apply` then closes 736 of 826 open items (395 pooled funds,
341 descriptor-only), leaving 90. Re-running the resolver over every unlinked EDGAR event
makes no new link.

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

### The alias rule (ADR 0033)

On 2026-10-13, production sent all 69 of one day's ambiguous Form D filings to review, and
all 69 were wrong. 28 matched `of`, the domain label of OfOne (`of.one`): every fund named
"X Fund I, a series of Y LLC" contains `of`, and `token_set_ratio` scores a token superset
100. The others matched `ai`, `ad`, `l`, `the`, and `apple`, the label of a YC company whose
directory website is an App Store page. So every alias now has to pass
`tell.resolve.aliases.alias_problem`:

- at least 3 letters or digits (spaces and hyphens not counted): `too_short` otherwise;
- not all digits: `numeric`;
- not made only of filler tokens: `generic`. Filler is `GENERIC_ALIAS_TOKENS` (function
  words `the`, `of`, `and`, ...; legal and structural forms `inc`, `co`, `group`,
  `holdings`, `hq`, ...; startup filler `ai`, `app`, `io`, `labs`, `tech`,
  `technologies`, ...; fund and SPV vocabulary `fund`, `capital`, `partners`, `ventures`,
  `series`, `spv`, ...; Roman numerals), any number, or a single character.

A **domain label** must also pass `label_alias_problem`. A label of 4 letters or fewer must
not be a common English word (`SHORT_ENGLISH_WORDS`, for example `ramp`, `flow`, `mesh`):
`short_word`. The domain must not be a platform's (`apple.com`, `*.vercel.app`, ...):
`platform_domain`. The company's *name* is unaffected by this test, so the company Ramp
keeps `ramp` through its name. `add_aliases` applies the rule for every source, and
`backfill_aliases` applies the label test before it writes a label.

Rows written before the rule are ignored by the index (next section). `tell resolve
prune-aliases` deletes the failing `manual` rows. It never deletes an `edgar`, `news` or
`ats_board` row, and never a `manual` alias that a review verdict wrote. It is a dry run
unless `--apply` is given.

Linking a filing, whether by the resolver or by a review verdict, goes through one function
(`link_event`): it sets `company_id` and, for a **trusted** link only, fills `companies.cik`
(when empty and not held by another company) and `companies.legal_name` (when empty), and
adds the issuer name and every `issuerPreviousNameList` entry as aliases (source `edgar`).
That is how renamed companies keep their history: a later filing under a former name is an
exact alias (R2). Trusted means R1, R2 after the Form D guards, or a human review verdict
(ADR 0034); an R4 link of an EDGAR event writes nothing but `company_id`, so a fuzzy link
can never mint the exact alias that links the issuer's next filing. A review verdict
additionally writes the queue item's name as a `manual` alias.

## Rules (`match_name`), top to bottom; the first that fires sets the confidence

| Rule | Condition | Confidence | Outcome |
| --- | --- | --- | --- |
| R1 domain | the source URL is on the watched domain (`on_domain_company_id`: board found by discovery, company RSS) | 0.98 | auto-link |
| R2 exact alias | normalized name, or its alt form, equals an alias held by exactly one company | 0.95 | auto-link |
| R3 name + geography | `rapidfuzz.fuzz.token_set_ratio(norm, alias) >= 92`, issuer state == `hq_state` (city == `hq_city` when either state is missing), the geography singles out **exactly one** company, and the superset guard holds | 0.90 | auto-link, logged; **review (R5) for a Form D** (ADR 0034) |
| R4 name only, unique | ratio >= 95, exactly one candidate company (ratio >= 85) in the **whole table**, and the superset guard holds | 0.85 | auto-link, logged |
| R5 ambiguous | ratio >= 85 with two or more candidate companies, or one candidate not linked above, and the candidate shares a distinctive token with the name (ADR 0034); or a Form D auto-link that failed a guard | 0.60 | review queue |
| R6 none | otherwise | 0 | unlinked event, kept with `company_id NULL` |

Candidates are scored with `token_set_ratio` against every alias in both the plain and the
alt form. Two `process.extract` passes per form: the top 20 aliases (so an R6 decision still
logs its nearest misses) and *every* alias at or above 85 (`limit=None, score_cutoff=85`),
collapsed to one entry per company. So R4's "exactly one candidate company in the whole
table" really is the whole table: a company holding 25 aliases cannot push a second
company's 88-ratio alias out of a truncated pool. Full US state names fold to the two-letter
codes Form D uses. Ten thousand aliases take milliseconds; the index is built once per run
and kept current as the run adds aliases.

### Candidate floor (ADR 0033)

- The index ignores any alias that fails the alias rule, even when the table holds it. It
  also ignores a stored `manual` alias that is the company's bare domain label and fails the
  label test (unless a review verdict wrote it). That applies to R2 as well, so the matcher
  behaves the same before and after `prune-aliases`.
- A **single-token alias** sits in a pool of its own. That pool is scored only for a name
  that is itself a single token (whole word against whole word, so `QuikNode` still finds
  `quicknode`), and the alias is also found by exact equality (R2, or a tie at 100). A
  one-word alias therefore never scores against a longer name: "Arboretum Apple a Series of
  CGF2026 LLC" is no candidate for `apple`, and "Blue Stripe Bakery" is no candidate for
  `stripe`. Multi-token aliases are scored exactly as before. A one-word *name* still finds
  a longer alias (`ramp` vs `ramp business` is a review item).

The ticket asked for "exactly that token as its whole name". Read literally, that drops the
gold set's correct R3 link `QuikNode, Inc.` -> QuickNode, so the whole-word comparison is
kept (ADR 0033). The rules' thresholds and confidences are unchanged.

### Form D guards (ADR 0034)

Applied when the event is a `form_d_filed` or `form_d_amendment`; the resolver passes the
issuer's facts from the payload (`tell.resolve.guards.IssuerFacts`) and the index holds each
company's `yc_status` / `yc_batch` (migration 0008, written by the YC seeder). News, ATS and
other EDGAR forms are matched without them. The rule order and confidences stay as above.

- **(e) Pooled vehicles are never candidates.** Industry group "Pooled Investment Fund", or a
  vehicle's legal-form token in the name (`fund`, `funds`, `L.P.`/`LP`, `SPV`, `Master`,
  `Feeder`, `Trust`, "a series of"): R6 at once, `excluded = "pooled_fund"` and
  `fund_signals` in the audit, no review item.
- **(a) R3 is review-only.** A Form D that R3 would link is R5 with `guard_failed` starting
  `r3_review_only`.
- **(b) Delaware corporation.** An R2 or R4 link needs `entity_type` Corporation and
  `jurisdiction_of_inc` DELAWARE (`entity_type_not_corporation`, `jurisdiction_not_delaware`).
- **(c) Active YC company.** When the company's YC status is known it must be Active
  (`yc_status_not_active`; Inactive, Acquired and Public fail).
- **(d) Incorporation year fits the batch.** `year_of_inc` must not be later than the batch
  year (`incorporated_after_batch`). The payload's `year_of_inc` is `null` when the filer
  checked "over five years" (or "yet to be formed"); such an issuer is not linked to a batch
  from the filing year minus 2 on (`over_five_years_for_recent_batch`; for 2026 filings,
  batches from 2024 on).

A failed guard demotes the auto-link to **R5 review, never R6**: the audit record and the
review item carry `guard_failed`, `demoted_from` and `proposed_company_id`, so the reviewer
sees which company was proposed and why it was held back. A human verdict links it with full
enrichment.

### Distinctive-token floor for R5 (ADR 0034)

A candidate becomes a review item only if one of its aliases at or above 85 shares a
**distinctive** token with the name (`tell.resolve.aliases.shares_distinctive_token`): not
filler (`GENERIC_ALIAS_TOKENS`) and not a descriptor (`DESCRIPTOR_TOKENS`: `technologies`,
`therapeutics`, `insurance`, `health`, `bio`, `biosciences`, `robotics`, `energy`, `systems`,
`solutions`, `software`, `labs`, `studio`, `network`, `data`, `analytics`, `security`,
`payments`, `finance`, `capital`, `medical`, `diagnostics`, `pharma`, `foods` and close
variants). Tokens of 4 or more characters one edit apart count as shared, so a one-letter
typo (`acmee` / `acme`, `beakon health` / `beacon health`) still reaches a human. "Nimbus
Technologies" against `h3x technologies` (85.71) shares only `technologies` and is R6. The
floor applies to every source but only removes review items; the auto-link rules and the
guard demotions are untouched.

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

A Form D demoted by a guard records `"rule": "R5"` plus `"guard_failed": [...]`,
`"demoted_from"` and `"proposed_company_id"`; a pooled vehicle records `"rule": "R6"`,
`"excluded": "pooled_fund"` and `"fund_signals"`. An event changed by `relink-formd` also
carries `"previous"` (the old record plus the old `company_id`), `"relinked_by":
"relink_formd"` and `"relink_reasons"`; a later `resolve run --retry` keeps `previous`.

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

`tell resolve close-junk-reviews [--dry-run|--apply]` (ADR 0033, 0034) re-runs the matcher on
every open item with the current rules, including the Form D guards when the item's event is
a Form D. An item the rules no longer send to review (no candidate left at or above 85, only
descriptor-sharing candidates, or a pooled-fund issuer; `by_reason` counts them) is closed as
`rejected`, with `verdict = "reject"` and `resolved_by = "alias_hygiene"`. Its candidates
stay in the payload as the audit trail, and the rejected-pair memory keeps it from coming
back. Items that still have a real candidate stay open, even when the new rules would now
auto-link them; the command never links and never deletes. The report counts closed items
by the aliases that caused them and by reason (`too_short`, `single_token_superset`, ...).

`tell resolve relink-formd [--dry-run|--apply]` (ADR 0034) re-evaluates every linked Form D
event under the current rules. The alias index is built without the `edgar` aliases those
events could have written, unless a human-verified or non-Form D EDGAR link of the same
company carries the name; a kept trusted link puts its names back and the pass repeats until
nothing changes, so a renamed company's filings still find each other but a wrong link
cannot support itself. A link is kept when it is still an auto-link to the same company, or
when its CIK is the company's and was not written only by failing links. A failing link is
unlinked (or moved, when it is now an auto-link to another company), its old record goes
under `payload.match.previous`, an R5 decision opens one review item, and the enrichment it
wrote is removed: an `edgar` alias equal to an unlinked event's normalized issuer name or
previous name that no still-linked event of the company carries (never the company's own
name or domain label), and `legal_name` / `cik` when they are an unlinked event's and no
still-linked event has them. It prints the counts and every change as `(event_id, company,
reason)`; a second run changes nothing.

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

Gates: names precision >= 0.97 and recall >= 0.85 pass on both sets. The README quotes
these numbers with the caveat that the gold set is a draft a person has not yet
spot-checked; the caveat comes off, and the numbers may be quoted elsewhere, once the
spot-check is done.

### Re-measured 2026-10-14 after alias hygiene (ADR 0033)

Same gold files, same matcher thresholds:

| Set | precision | recall | by rule (names) |
| --- | --- | --- | --- |
| public sample, names | 1.0000 | 1.0000 | R2 57, R3 4, R5 12, R6 22 (was R5 34) |
| full private set, names | 1.0000 | 0.9733 | R2 371, R3 9, R5 30, R6 160 (was R5 182, R6 8) |

Every positive is decided exactly as before: R2 and R3 counts are unchanged, and the same 8
renamed issuers are missed. The R5 that disappeared are superset decoys ("Blue <name>
Bakery") against one-word aliases, which now go to R6 instead of review. The strict reading
of the ticket (a single-token alias matches only by equality) lost `QuikNode, Inc.` ->
QuickNode (R3), which is why the whole-word comparison was kept.

Offline Form D measurement (no production access). Setup: the 1,000 YC companies and their
boards from `tell_snapshot_20261014`, plus OfOne (`of.one`) and FameGame (`apple.com`), with
aliases rebuilt by the pre-change `backfill_aliases`. Input: 300 synthetic issuer names (99
"X Fund I, a series of Y", 51 "X Y a Series of Z LLC", 40 other fund names, 80
operating-company names, 30 legal names of snapshot companies).

| | R2 | R5 | R6 | true positives linked |
| --- | --- | --- | --- | --- |
| before | 42 | 184 | 74 | 30/30 |
| after (code only, old alias rows) | 40 | 6 | 254 | 30/30 |
| after (code + `prune-aliases`) | 40 | 6 | 254 | 30/30 |

Under the old code, 94 of the 184 R5 items had `of` as top candidate. Under the new code,
all 6 remaining R5 items are operating-company names: one-word typos ("Lad" vs `clad`), or
multi-token aliases sharing one generic word ("Nimbus Technologies" vs `h3x technologies`).
The two R2 links that disappeared were "The Apple Company" -> FameGame, made through the
platform label `apple`. The rest of the R2 links are synthetic names equal to a YC
company's name, which R2 links by design.

### Re-measured 2026-10-14 after the Form D guards (ADR 0034)

`tests/test_resolve.py` now feeds every name as a Form D issuer: a Delaware corporation of
unknown age (the gold CSV has no entity type or jurisdiction; guards (b)-(d) are tested on
production's own links instead), against companies with no YC status or batch. So the gold
set measures the name rules plus R3 review-only, the fund exclusion and the R5 floor.

| Set | precision | recall | by rule (names) |
| --- | --- | --- | --- |
| public sample, names, before | 1.0000 | 1.0000 | R2 57, R3 4, R5 12, R6 22 |
| public sample, names, after | 1.0000 | 0.9200 | R2 57, R5 16, R6 22 |
| full private set, names, before | 1.0000 | 0.9733 | R2 371, R3 9, R5 30, R6 160 |
| full private set, names, after | 1.0000 | 0.9433 | R2 371, R5 39, R6 160 |
| SEC-verified slice (36 rows), before | 1.0000 | 0.78 (28/36) | |
| SEC-verified slice (36 rows), after | 1.0000 | 0.75 (27/36) | |

Every lost positive is a former R3 link that is now a review item: mostly the
near-duplicate pairs (two companies called Rally, Roger, Flint or Method, separated only by
geography) and `QuikNode, Inc.` -> QuickNode. No decision moved from review to R6 on the gold
set, and the gates (precision >= 0.97, recall >= 0.85) still pass. Remember the production
measurement above: gold-set precision is not production precision.

### Re-measured 2026-10-13: how much of the 300-row recall is real

The gold set's legal names were mostly drafted by a script: 259 of 300 rows are marked
`unverified`, and 290 of the 300 legal names normalise to the company's display name, so
R2 matches them trivially. The 0.9733 recall above therefore mostly measures that the
normaliser strips `, Inc.`. Split by how the legal name was obtained (same matcher, all 300
companies seeded so every name competes with the full alias index):

| Slice | positives | found | recall | auto-links | wrong | precision |
| --- | --- | --- | --- | --- | --- | --- |
| Legal name verified against SEC EDGAR (`edgar_fts`) | 36 | 28 | 0.78 | 28 | 0 | 1.00 |
| All verified (`edgar_fts`, `yc`, `discovery`) | 41 | 33 | 0.80 | 33 | 0 | 1.00 |
| Unverified (script-drafted legal forms) | 259 | 259 | 1.00 | 259 | 0 | 1.00 |

Even the verified recall is flattering: most verified legal names are also the brand plus a
suffix. On the 9 SEC-verified issuers whose legal name differs from the brand (former
corporate names such as `InterviewStreet Inc` for HackerRank), the name rules found 1. The
misses stay unlinked, never mislinked: a wrong link is a false alert to a seller, so the
rules are precision-first, and renamed issuers are left to `issuerPreviousNameList` aliases.

The result that does not depend on the labels is the decoy test: 0 of the 180 token-superset
names (for example `Blue Stripe Bakery` against `Stripe`, which `token_set_ratio` scores
100) were auto-linked, and there were 0 wrong links among all 380 auto-links. Quote that,
and the verified-slice recall with its N, rather than 0.9733, until the gold set has been
re-verified against EDGAR and spot-checked by a person.

## Deliberately not done

No LinkedIn lookups, no paid enrichment, no LLM in the auto-link path. An LLM may propose
answers for the review queue later, with the human still clicking the button.
