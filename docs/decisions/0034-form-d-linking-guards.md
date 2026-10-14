# 0034. Form D linking guards: R3 review-only, issuer and YC-status guards, trusted enrichment

- Status: accepted (proposed 2026-10-14; confirmed by Karan 2026-10-14)
- Date: 2026-10-14

## Context

On 2026-10-14 production had auto-linked 41 Form D filings to the 6,246 YC companies. Each
link was checked by hand against the SEC filing and the company's own site: 20 were the
same company, 20 were a different company and 1 was unclear. Precision was 50% (20 of 40
decided), against a gold-set precision of 1.0000. The gold set's legal names are mostly the
brand plus a suffix and none is a stranger that shares a brand word, so it could not see
this.

By rule:

| Rule | Links | Same | Different | Unclear |
| --- | --- | --- | --- | --- |
| R3 (fuzzy >= 92 + state) | 5 | 0 | 5 | 0 |
| R2 (exact alias) | 36 | 20 | 15 | 1 |

- **R3, 5 of 5 wrong**, all one-word near misses: Compass vs nCompass, Inferra vs Infera,
  AlpacaX vs Alpaca, Sylvan vs Sylvian, Scaler AI vs Scale AI.
- **R2, 15 of 35 decided wrong**: common-word brands left after `Labs` / `Holdings` are
  stripped (Payload, Hub, Manatee, Echo, Modal Labs, Sunflower Labs, Overlord Labs, Protocol
  Labs, Vanta Holdings, Jasper Holdings LP); issuers that are not Delaware corporations (a
  Cayman LP, a Wyoming LLC, two Canadian corporations, a North Carolina corporation);
  incorporation years that do not fit the YC batch; links to YC companies listed Inactive or
  Acquired. 4 of the 15 were knock-ons: the wrong R3 link had written the issuer's name as
  an `edgar` alias, so the issuer's next filing matched it exactly.
- **All 20 right links** were Delaware corporations linked to Active YC companies whose
  incorporation year fit the batch.
- Every wrong link also overwrote `companies.legal_name` and set `companies.cik`, which the
  speed-versus-press backtest cohort trusts (it accepts any event whose CIK equals the
  company's).

Separately, 788 R5 review items were open. 405 were funds, SPVs and other pooled vehicles;
most of the rest had a two-token alias with a descriptor word as top candidate (`lsk
technologies`, `ai insurance`, `h3x technologies`, `cb therapeutics`) at exactly 85.71
`token_set_ratio`, because the issuer shared only the descriptor.

## Decision

The rule order and confidences of SPEC section 6 are unchanged. The changes below apply to
Form D events (`form_d_filed`, `form_d_amendment`) only, except (4) and (5); news and ATS
linking are unchanged. Code: `tell/resolve/guards.py`, `tell/resolve/match.py`.

1. **(a) R3 is review-only for Form D.** A Form D that R3 would link becomes an R5 review
   item with `payload.match.guard_failed = ["r3_review_only", ...]`. This deviates from SPEC
   section 6 ("R3 ... auto-link, log") and needs the owner's sign-off. R3 still links news
   events.
2. **Guards on R2 and R4 auto-links.** A failing guard demotes the link to R5 review with
   `guard_failed` (the reasons), `demoted_from` and `proposed_company_id` in the audit
   record and on the review item; a guard never demotes to R6.
   - (b) the issuer's `entityType` is Corporation and its `jurisdictionOfInc` is DELAWARE
     (`entity_type_not_corporation`, `jurisdiction_not_delaware`); a missing value fails.
   - (c) the company's YC status, when known, is Active (`yc_status_not_active`; Inactive,
     Acquired and Public all fail). Companies not from the directory have no status and pass.
   - (d) the issuer's year of incorporation is not later than the YC batch year
     (`incorporated_after_batch`), and an issuer incorporated "over five years" ago is not
     linked to a batch from `filing year - 2` on (`over_five_years_for_recent_batch`; for
     2026 filings, batches from 2024 on). The collector stores `year_of_inc = null` for
     "over five years" (and the rare "yet to be formed"), so a `null` value under the key is
     read as "over five years"; a payload without the key says nothing. The rule is written
     relative to the filing year so it does not rot; with no filing date it uses 2024.
   - The ticket names R2 for (b); the guards are applied to R4 as well, since R4 is a weaker
     match than R2. R4 made no Form D link on 2026-10-14, and the extension only removes
     auto-links.
3. **(e) Pooled investment vehicles are never candidates.** An issuer whose industry group is
   Pooled Investment Fund, or whose name has a legal-form token of a vehicle (`fund`,
   `funds`, `L.P.` / `LP`, `SPV`, `Master`, `Feeder`, `Trust`, or the phrase "a series of"),
   goes straight to R6 with `excluded = "pooled_fund"` and the signals, and no review item.
   Two YC names contain such a token (Meter Feeder, AYC Fund); their own Form D filings are
   never auto-linked, which is the accepted cost.
4. **R5 needs a distinctive shared token** (all sources). A candidate becomes a review item
   only when one of its aliases at or above 85 shares a distinctive token with the name: a
   token that is not filler (`GENERIC_ALIAS_TOKENS`) and not a descriptor
   (`DESCRIPTOR_TOKENS`: technologies, therapeutics, insurance, health, bio, biosciences,
   robotics, energy, systems, solutions, software, labs, studio, network, data, analytics,
   security, payments, finance, capital, medical, diagnostics, pharma, foods and close
   variants). Tokens of 4 or more characters one edit apart count as shared, so one-letter
   typos (Acmee / Acme, Beakon / Beacon, Methodd / Method) still reach a human; that
   tolerance is an interpretation of "share a token". Without a shared distinctive token the
   decision is R6. This deviates from SPEC section 6 R5 ("ratio >= 85 ... review queue") in
   the same direction as ADR 0033: fewer review items, no new links. A guard-demoted
   auto-link (1, 2) is reviewed whether or not it shares one.
5. **Only trusted links write enrichment** (all EDGAR events). `edgar` aliases (issuer name,
   previous names), `companies.legal_name` and `companies.cik` are written only by R1, by an
   R2 link that passed every guard, or by a human review verdict. An R4 link (any EDGAR
   form) sets `company_id` and nothing else, so a fuzzy link can never mint the exact alias
   that makes the next filing an R2.
6. **Schema.** `companies.yc_status` and `companies.yc_batch` (text, nullable, migration
   0008), written by the YC seeder as listed in the directory; a re-seed updates them and
   counts the row as updated.
7. **Repair.** `tell resolve relink-formd [--dry-run|--apply]` (dry run by default)
   re-evaluates every linked Form D event under these rules, without the `edgar` aliases
   those events wrote unless a still-trusted link re-earns them. A failing link is unlinked
   (`payload.match.previous` keeps the old decision and company), an R5 decision opens one
   review item, and the `edgar` aliases, `legal_name` and `cik` traced to unlinked events
   and supported by no remaining linked event are removed. A link whose CIK is the
   company's stays when that CIK was not written only by failing links. Idempotent.
   `tell resolve close-junk-reviews` closes the open items the new candidate rules no
   longer produce (descriptor-only candidates, pooled funds).

## Consequences

- Measured on a local copy of production taken read-only on 2026-10-14 (17,936 EDGAR
  events, 6,242 companies, YC status and batch from the same day's directory):
  `relink-formd` keeps exactly the 20 links judged SAME and unlinks the other 21 (R5 20,
  R6 1: Jasper Holdings LP); it deletes 7 `edgar` aliases (`compass`, `scaler ai`,
  `inferra`, `alpacax`, `sylvan`, `polyester`, `bbot`) and resets 16 legal names and 16
  CIKs. A second run changes nothing. `close-junk-reviews` then closes 736 of 826 open
  items (395 pooled funds, 341 descriptor-only), leaving 90. A `resolve run --retry` over
  all 17,916 unlinked EDGAR events afterwards makes no new link and opens no new item.
- Gold set (Form D path, issuers as Delaware corporations): precision 1.0000 on both sets;
  recall 1.0000 -> 0.9200 on the public sample and 0.9733 -> 0.9433 on the 300 rows, all
  from the 9 R3 positives (near-duplicate pairs such as two companies named Rally) now
  going to review. SEC-verified slice: 28/36 -> 27/36. Gates still pass.
- Review volume per new filing goes up slightly for the near misses (every Compass, Inc.
  filing is a review item until a human rejects the pair) and down a lot for funds and
  descriptor-only matches.
- Recall for a real YC company that files as an LLC, outside Delaware, or after
  reincorporating is lower: those filings go to review instead of linking. A human verdict
  links them with full enrichment, and later filings under the same CIK follow.
- If the owner rejects (a), drop `R3_REVIEW_ONLY` from `_guarded` in `match.py`; R3 links
  then go through guards (b)-(d) like R2. The schema, the enrichment rule and the repair
  command stand on their own.
- Follow-up outside this change: the Form D collector should store the "over five years" /
  "yet to be formed" choice explicitly instead of a `null` year
  (`tell/collectors/edgar_formd.py`), and `docs/sources/yc-directory.md` should list
  `status` among the fields read.
