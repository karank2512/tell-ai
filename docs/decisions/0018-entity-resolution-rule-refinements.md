# 0018. Entity-resolution rule refinements beyond the SPEC section 6 wording

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 6 gives the normalization steps, the rules R1-R6 with their confidences and
the gold-set gate (precision >= 0.97, recall >= 0.85). Two independent verifications of the
Phase 5 build found places where the literal wording either under-specifies a case or, when
followed exactly, auto-links the wrong company:

1. **R3 geography tie.** Two companies sharing an alias (two "Method"s), both in New York;
   a Form D from "Method Inc", NY. The rule as worded fires for the first candidate at
   >= 92 whose state matches, so the lower `company_id` was linked purely by ordering.
2. **R4 "exactly one candidate company in the whole table"** was evaluated over a
   20-alias `process.extract` pool. A company holding 20+ aliases could hide a second
   company's 88-ratio alias, and R4 fired where the spec says R5.
3. **Token-superset hazard.** `rapidfuzz.fuzz.token_set_ratio` scores any token superset
   100. With only `stripe` in the table, "Blue Stripe Bakery", "Anti Stripe Coalition" and
   "Stripe Press" in Stripe's state all satisfy R3 (0.90), and R4 (0.85) without a state.
   The gold negatives (another gold company's legal name) never contain supersets, so the
   measured precision of 1.0 could not see this. Production Form D issuers routinely share
   a leading word with watched companies.
4. **R1 links had no audit record.** Collectors copy `board.company_id` onto job and tech
   events; the resolver only scans `company_id IS NULL`. The spec says every decision
   writes `{rule, ratio, candidates[:5]}` into the payload.
5. **Review verdicts skipped the Form D enrichment.** A `link` / `create` verdict set
   `company_id` and a `manual` alias only; previous names, `cik` and `legal_name` were not
   applied, so a later filing under a former name went R6, contradicting "each previous
   name becomes an alias when a filing is linked".
6. **The headline metric counted tautological cases.** 192 of 492 full-set cases fed the
   expected `company_id` in as `on_domain_company_id` (R1 boards), inflating recall from
   0.9733 to 0.9837.
7. Minor: `payload["name"]` of any source was treated as a company name (GitHub repository
   names); a suffix glued to a bracket survived step 2 (`Acme (Inc.)` -> `acme inc`); a
   `reject` verdict writes no alias although the spec says "a resolved item writes an
   alias".

Orchestrator guidance for the judgement calls is recorded in the Phase 5/6 fix task and
followed here.

## Decision

Rules stay the spec's rules with the spec's confidences and thresholds; none is weakened.
The refinements only remove auto-links or add audit data:

- **R3 fires only when the geography singles out exactly one company.** Two or more
  companies at >= 92 in the issuer's state is a tie; the event goes to review (R5) with
  both candidates in the audit record.
- **R4's uniqueness is evaluated over every alias.** Candidate scoring runs two
  `process.extract` passes per form: the top 20 (so R6 decisions log their nearest misses)
  and every alias at or above 85 (`limit=None, score_cutoff=85`), collapsed per company.
- **Superset guard for R3 and R4.** Besides `token_set_ratio`, the company must have an
  alias with guard ratio >= 85, where
  `guard = min(token_sort_ratio(norm, alias), 100 * matched_chars / len(norm))`: the plain
  sorted-token ratio capped by the share of the *query* the alias explains (plain
  `token_sort_ratio` dilutes with length: "Domu Technologies Press" vs "domu technologies"
  scores 85; the coverage term gives 74). A candidate failing the guard falls to R5 review,
  never to R6. R2 is untouched. This is the orchestrator's option (f) with the coverage
  cap added after the gold set showed the plain ratio passing 4-word supersets.
- **International legal suffixes** are stripped like the spec's list: `pte`, `pty`, `pvt`,
  `private`, `sdn bhd`, `ltda`, `sas`, `sarl`, `s.l.`, `srl`, `s.p.a.`, `s.a.p.i. de c.v.`,
  `ag`, `ug`, `ab`, `as`, `a/s`, `aps`, `oy`, `bv`, `nv`, `kk`, `llp`, `lp`
  (`INTERNATIONAL_SUFFIXES`). The spec list already holds `gmbh` and `s.a.`; without the
  rest, "Peakflo Pte. Ltd." keeps `pte` as an extra token and the guard sends it to review.
  Only forms that are not English words are listed; stripping is end-only and never
  removes the last word. Brackets and quotes are cut before suffix stripping.
- **R1 audit record.** `sync_board_postings` stamps
  `payload["match"] = {"rule": "R1", "ratio": null, "candidates": [], "confidence": 0.98,
  "via": "ats_board"}` (`tell.collectors.base.R1_MATCH`) on every event whose
  `company_id` it copies from the board. (The news collector already stamps `{"rule":
  "R1", "confidence": 0.98}` on company-feed items.)
- **One link function.** `tell.resolve.match.link_event` applies the enrichment (`cik`
  when free, `legal_name` when empty, issuer name and previous names as `edgar` aliases)
  and is called by the resolver and by review `link` / `create` verdicts alike.
- **Only EDGAR and news events are matched by name** (`NAME_SOURCES`); other sources are
  counted as `no_name`.
- **Reject writes no alias.** `company_aliases` has no polarity column, so a rejected
  pair cannot be stored as an alias without inventing a negative one. The resolver instead
  remembers (normalized name, candidate company set) of rejected items and skips the pair
  while the current candidates are a subset of the rejected set. The pair legitimately
  returns when a new company enters the candidate set. Accepted as the reading of "a
  resolved item writes an alias so the same pair never comes back"; a negative-alias
  column would be a schema deviation for little gain.
- Two earlier interpretations are confirmed: a shared alias makes R2 ambiguous (R3 or R5
  decide); a single candidate at 92-94 without matching geography goes to review, not R6.
- **The headline metric is the name rules only.** `tests/test_resolve.py` reports two
  tallies: `names` (legal names through R2-R6; gated) and `boards` (R1 path; asserted
  100% but not in the headline). The names tally also feeds two token supersets per
  sampled row (`<name> Press LLC`, `Blue <name> Bakery`) with the row's geography; any
  auto-link among them counts against precision.

## Consequences

- Measured 2026-10-05 on the name rules: public sample precision 1.0000 / recall 1.0000
  (n=50); full private set precision 1.0000 / recall 0.9733 (n=300, 292/300; the eight
  misses are former corporate names that only `issuerPreviousNameList` can bridge). Zero
  wrong auto-links; all 210 superset negatives go to review. Boards: 33/33 and 192/192 R1.
- More review-queue items: every "<company> Press LLC" style issuer is a queue item
  instead of a wrong link. The spec target (under 20 open items a week by week 4) is the
  check; `REVIEW_RATIO` is the knob.
- The audit record gains `sort_ratio` per candidate; R1 events gain `payload["match"]`.
  Consumers that assume job payload keys are only provider fields see one more key.
- `docs/methods/entity-resolution.md` carries the formulas, the measured table and the
  before/after numbers; the README takes the names-only numbers once the gold set is
  spot-checked.
