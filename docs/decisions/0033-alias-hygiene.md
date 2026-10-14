# 0033. Alias hygiene: an alias rule, one-word aliases match whole names only, no platform domains

- Status: proposed
- Date: 2026-10-14

## Context

A read-only check of production on 2026-10-13: of 236 Form D filings, 0 were linked and 69
went to the R5 review queue, all 69 wrong by hand.

- 28 matched the alias `of`. `backfill_aliases` adds every company's domain label as an
  alias; OfOne's domain is `of.one`, so its label is `of`. Every fund named "X Fund I, a
  series of Y LLC" contains the token `of`, and `token_set_ratio` scores any token
  superset of an alias 100.
- Most of the rest matched other one-token aliases: `ai`, `ad`, `l`, `the`, and `apple`. The
  last belongs to FameGame, a YC company whose directory website is an App Store link, so
  the YC seeder stored the domain `apple.com` and its label `apple` ("Arboretum Apple a
  Series of CGF2026 LLC").

The superset guard (ADR 0018) already stopped these from being auto-linked, but every one
became a review item. A quarter of the Form D backfill (about 12,000 filings) would open
thousands of them. SPEC section 6 sets a target of under 20 open items a week.

## Decision

1. **Alias rule** (`tell/resolve/aliases.py`). An alias is a normalized name. It must have at
   least 3 letters or digits (spaces and hyphens not counted), must not be all digits, and
   must not be made only of filler tokens. A filler token is a word in
   `GENERIC_ALIAS_TOKENS`, a number, or a single character. `GENERIC_ALIAS_TOKENS` has
   about 80 entries in five groups: function words (`the`, `of`, `and`, `a`, `de`, ...),
   legal and structural forms (`inc`, `co`, `llc`, `group`, `holdings`, `hq`, ...),
   startup filler (`ai`, `app`, `io`, `labs`, `tech`, `technologies`, ...), fund and SPV
   vocabulary (`fund`, `capital`, `partners`, `ventures`, `series`, `spv`, `trust`, ...),
   and Roman numerals (`ii`, ..., `xii`). A **domain label** must pass the same test and
   two more: when it has 4 letters or fewer it must not be a common English word
   (`SHORT_ENGLISH_WORDS`, about 1,000 words of 3-4 letters such as `ramp`, `flow`, `mesh`),
   and its domain must not be a platform's (item 3). A company's *name* is still an alias
   when the name itself is such a word: the company Ramp keeps `ramp` through its name.
   What the label test removes is a word taken from the domain alone. The rule applies
   wherever an alias is written (`add_aliases`, every source) and to the domain label in
   `backfill_aliases`.
2. **Candidate floor** (`AliasIndex`). Aliases that fail the rule are ignored even when the
   table holds them. This covers R2 as well as R3-R5. It also covers a stored `manual`
   alias that is the company's bare domain label and fails the label test, unless a review
   verdict wrote it. A **single-token alias** is scored only against a name that is itself
   a single token, whole word against whole word, or by exact equality. It never gets a
   `token_set_ratio` superset hit against a longer name. The R1-R6 thresholds and
   confidences are unchanged. The only change is which aliases can be candidates.
3. **Platform domains in the YC seed** (`tell/discovery/__init__.py`, `PLATFORM_DOMAINS`).
   This covers app stores and big consumer platforms (`apple.com`, `google.com`), social and
   profile pages (`linkedin.com`, `x.com`, `twitter.com`, `facebook.com`, `instagram.com`,
   `youtube.com`, ...), code hosting and per-project hosting (`github.com`, `github.io`,
   `vercel.app`, `netlify.app`, `herokuapp.com`, `pages.dev`, ...), site builders
   (`webflow.io`, `framer.website`, `wixsite.com`, `notion.site`, `notion.so`,
   `carrd.co`, ...), publishing and link hubs (`medium.com`, `substack.com`, `linktr.ee`,
   ...) and startup directories (`ycombinator.com`, `wellfound.com`, `crunchbase.com`, ...).
   A website is matched on its ICANN registrable domain, so `apps.apple.com` and
   `acme.vercel.app` are both caught. tell's identity key is the company's own domain, so
   such a company is **not seeded** and is counted as `skipped_platform_domain`. The
   platform's own company is exempt: `substack.com` stays the domain of a company named
   "Substack", but only on the bare domain.
4. **Maintenance commands.** All three are dry runs unless `--apply` is given, and all are
   idempotent.
   - `tell resolve prune-aliases [--dry-run|--apply]` deletes `company_aliases` rows that
     fail the rule. It deletes only rows with source `manual`. It never deletes `edgar`,
     `news` or `ats_board` rows; it counts them under `kept_other_sources`. It never
     deletes a `manual` alias a review verdict wrote (`link` / `create`); those are counted
     under `kept_human_review`.
   - `tell resolve close-junk-reviews [--dry-run|--apply]` re-runs the matcher on every open
     `entity_match` item. An item with no candidate left at or above 85 under the new
     rules is closed as `rejected`, with `payload.verdict = "reject"` and
     `payload.resolved_by = "alias_hygiene"`. Nothing is deleted and no event is linked.
     The resolver's rejected-pair memory then keeps the pair from reopening. Items that
     still have a real candidate stay open for a human.
   - `tell discover prune-platform-companies [--dry-run|--apply]` lists companies keyed by
     a platform domain. With `--apply` it deletes those with no events, boards, watchlist
     items, signals, feedback or non-zero scores, together with their aliases, feeds and
     zero scores. The others are listed with their blockers for a human.

### Deviation from the ticket, with the reason

The ticket worded the floor as "a single-token alias can only match when the issuer's
normalized name also has exactly that token as its whole name". Taken literally, only exact
equality would count. That reading lost a correct link in the full gold set: `QuikNode,
Inc.` -> QuickNode, a one-letter typo that R3 had linked. Both names are single tokens, so
whole word against whole word still meets the intent of the ticket (no token-superset
hits). The implemented rule is that a single-token alias is scored only against a
single-token name.

## Consequences

- Measured offline on the 1,000 YC companies in `tell_snapshot_20261014`, plus the two
  production rows the check named (OfOne `of.one`, FameGame `apple.com`). The alias state was
  rebuilt with the pre-change `backfill_aliases`, and 300 synthetic Form D issuer names were
  run through `match_name` (190 fund/SPV/series vehicles, 80 operating-company names, 30
  legal names of snapshot companies). R5 dropped from 184 to 6 and R6 rose from 74 to 254.
  All 30 true positives stayed linked and no correct link was lost. Auto-links went from 42
  to 40: the two removed were "The Apple Company" -> FameGame through `apple`. A rehearsal of
  the production cleanup on the same data: `close-junk-reviews` closed 178 of 184 open
  items and kept 6; `prune-aliases` deleted 12 rows; `prune-platform-companies` found both
  platform rows but deleted neither, because each had a board or events.
- Gold set: precision 1.0000 on both sets, the same recall (sample 1.0000, full 0.9733) and
  the same 8 misses. R5 on the full set fell from 182 to 30, almost all of them superset
  decoys that now go to R6.
- A company whose only names are short or generic (`H1`, `F2`, a company literally called
  "The Fund") can no longer be found by name; it still links through its domain (R1). A
  one-word alias gives a review item only for a near-identical one-word name, or a
  candidate in a tie.
- The remaining review noise comes from multi-token aliases that share one generic word
  with the issuer ("Nimbus Technologies" vs `h3x technologies`). `token_set_ratio` scores
  that 86. This ADR leaves it alone; the next lever is `REVIEW_RATIO`, as SPEC section 6
  says.
- Rows written before the rule are harmless once this code is deployed, because the index
  ignores them. Running the prune keeps the table honest.
