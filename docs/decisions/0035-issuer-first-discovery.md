# 0035. Issuer-first discovery, rule R0, and the evidence rule for non-YC companies

- Status: proposed (2026-10-15; needs the owner's sign-off: it deviates from SPEC section 6
  and is a SPEC section 17-style business rule)
- Date: 2026-10-15

## Context

- **Too few Form D links to measure SPEC 13.1.** After the ADR 0034 guards, the YC seed
  (ADR 0009) yields about 18 trusted Form D links per 90 days. SPEC 13.1 wants N >= 100.
  Over 2026-07-15 to 2026-10-13 there were 10,059 original Form D filings; 1,068 (1,009
  distinct CIKs) were Delaware corporations incorporated within five years, about 360 a
  month, and only 8 of them were linked to a YC company.
- **The guard gap.** The ADR 0034 guards (c) and (d) read YC facts. A company without a YC
  status had no guard beyond (b), so the moment a non-YC company exists an exact R2 alias
  plus a Delaware corporation auto-links. Example: "Marble, Inc." would have linked to
  marble.studio, a studio in Paris.
- **Three designs were measured** (scratch measurements, read-only against a local copy of
  production):

  | | A: board-first | B: issuer-first | C: public lists |
  | --- | --- | --- | --- |
  | Correct Form D links per 90 days | about 50-100 | about 270 (150-430) | about 22 + 8 from review |
  | Precision seen | 6 of 6; 13 same / 0 different on the YC set | 11 of 11 at tiers A/B (Wilson lower bound 0.74) | 31 of 31 with evidence, not hand-checked to ADR 0034 standard |
  | Cost | about 150k requests to seed, about 380 MB of postings | about 30 requests per issuer, under 5 MB a quarter | close to nothing |

  B's link is exact by CIK, so its only precision question is whether a domain belongs to
  the issuer; A and C still rest on a name join.

## Decision

1. **Create companies from Form D issuers on tier A/B site evidence** (issuer-first
   discovery, `tell discover issuers`, shadow by default). Input: unlinked `form_d_filed`
   events of Delaware corporations with a year of incorporation, not pooled funds, whose CIK
   is not yet in `companies.cik`. A created company has `companies.source = 'issuer_first'`,
   its domain, brand name, legal name, CIK with `cik_source = 'issuer_first_a'` or
   `'issuer_first_b'`, and HQ city and state.
2. **Evidence tiers** (decided from the site's own pages: homepage plus terms, privacy and
   legal pages, robots honoured, 1 request per second per host):
   - **Tier A:** every token of the full legal name (descriptors included), in order,
     followed by a legal form (Inc, Corp, Corporation, PBC, Co.) with a word boundary after
     it; not preceded by `.`, `@` or `/`. Previous names from the filing count.
   - **Tier B:** the full brand plus an address in the Form D city or state, never a
     registered-agent city (Wilmington, Dover, Lewes, Newark DE, Sheridan WY).
   - **Always:** exactly one candidate domain qualifies; the site states no address in a
     different state; not a platform domain or a parked page. Text from separate page blocks
     is joined with a separator.
   - **Never evidence:** a shared core token plus the same city; a title alone; an ATS board
     name alone.
   - Tier C (weaker) goes to review only when the amount sold is at least $1M, else it is
     logged.
3. **Store evidence as path plus legal-name span only** (`issuer_discovery.evidence =
   {path, span}`), never surrounding text: a legal page can carry a phone number. Street
   addresses are compared in memory and never stored; only city and state are.
4. **Rule R0, exact CIK, confidence 0.99, evaluated before R1** (`tell/resolve/match.py`):
   an EDGAR event links when its CIK equals `companies.cik` and that CIK has a trusted
   `cik_source` (`issuer_first_a`, `issuer_first_b`, `r1`, `guarded_r2`, `manual`). A CIK
   of unknown origin (`cik_source` NULL) never fires R0. R0 is a trusted link (it writes
   `edgar` aliases and fills `legal_name`) and is not subject to the Form D guards, so
   amendments and later rounds of a company linked once link exactly.
5. **Guard (f) `no_site_evidence` for every Form D auto-link to a company with no YC
   status** (`tell/resolve/guards.py`): the link passes only when the issuer's CIK is the
   company's CIK with a trusted `cik_source`, or a `linked` tier A/B `issuer_discovery`
   record exists for the (CIK, company domain) pair. Otherwise it is R5 review; it never
   writes `edgar` aliases. Guard (f) alone also applies to the auto-links of every other
   EDGAR filing (S-1, 8-K), which carries no issuer facts for guards (b)-(d): otherwise an
   S-1 "Marble, Inc." would link marble.studio by exact name, hand it the CIK, and every
   later Form D would follow by R0. Only an R2 decided with the Form D guards writes
   `cik_source = 'guarded_r2'`; an R2 of another filing writes its CIK with `cik_source`
   NULL, which R0 does not trust.
6. **Provenance columns and table** (migration 0009): `companies.source` (`yc` /
   `issuer_first` / `board_first` / `manual`, default `yc`), `companies.cik_source` (null
   when unknown; backfilled where the events show a reviewer's link, an R1 link, or an R2
   Form D link that passes the ADR 0034 guards), and `issuer_discovery` (CIK, status, tier,
   domain, evidence, attempts, retry schedule). A trusted link that writes a CIK writes its
   source with it.
7. **Never read `relatedPersonsList`.** No person's name is fetched, stored or logged.
8. **Conflicts go to review.** A domain already in `companies` or a CIK held by another
   company opens a review item proposing the link; no write ever replaces an existing
   `companies.cik` or domain.

### Deviations from SPEC section 6

- A new rule R0 above R1.
- Companies created from a filing rather than from a domain list (SPEC section 5 seeds
  domains).
- Guard (f) is stricter than R2 for companies without YC facts.

## Consequences

- **The 13.1 report splits the cohort** (`tell/backtest/speed_vs_press.py`): "monitored
  before the filing" (a YC directory company, or any company that existed before the
  filing date) and "found from the filing" (created on or after it), each with its N, plus
  the pooled row. A found filing's alert time is the link time, so discovery latency
  counts. R0 is a trusted rule.
- **Press is sparse for these companies** (2 of 40 sampled issuers had a funding headline
  within the window), so the no-press share is large and the lag median needs two quarters
  pooled. One-word brands are flagged press-ambiguous; for a non-YC company whose legal name
  is longer, a funding headline counts only when it carries that name. `company_key` drops
  `PBC`.
- **13.2 (hiring velocity before a raise)** has no hiring history for issuer-first
  companies before their filing; only their later rounds count.
- **Recall gaps:** JavaScript-only sites, rebrands (one domain redirecting to another),
  sites that do not answer, generic or shell names.
- **Test fixtures model YC companies** with `yc_status = 'Active'`: a company with no YC
  status now needs site evidence for a Form D auto-link.

## Rejected

- **Common Crawl** as a board source: both index hosts disallow crawling in robots.txt.
- **Getro VC boards**: the terms forbid crawling.
- **QLever (Wikidata) as a primary source**: no published terms and no SLA.
- **Fuzzy rules for non-YC companies**: about 44 review items a week, against the target of
  under 20.

## Implementation notes (PR 3, PR 4, PR 6)

How the rules above were made concrete; every point is tested.

- **Evidence module** (`tell/resolve/evidence.py`, pure). Page text is kept as blocks, one
  per block-level element, joined with a newline that no pattern crosses (the "FastWay"
  case). Tier A and B text must be in a page body: a `<title>` or meta description alone
  is never evidence (`title_only`). Tier A's legal form is `Inc`, `Incorporated`, `Corp`,
  `Corporation`, `PBC` or `Co.` (with its period); up to three spaces or punctuation
  characters may sit between name tokens, so a stored span is never longer than the legal
  name plus its form.
- **"An address in the Form D city or state"** is `City, ST`, `City, State`, or the state
  with a ZIP code (`ST 12345`, `State 12345`), on the same page as the full brand. A state
  mentioned in prose ("seafood from Maine's working waterfront") is not an address. That
  demotes one of the design's eleven tier A/B domains (saltboxfish.com, which is also a
  "coming soon" page, so it is parked) to no evidence; the frozen hand check records it.
- **"An address in a different state"** is any ZIP-coded US address (`ST 12345`,
  `State 12345`) whose state is not the Form D state; for an issuer outside the US every US
  address counts. It disqualifies tier A and B alike (they become tier C).
- **Parked**: a parking or placeholder phrase (for sale, coming soon, under construction,
  ...) on a page with under 3,000 characters of text, or a page with no text whose title is
  empty or the domain itself.
- **Tier C** is the brand, or its distinctive words (filler and descriptors removed), in
  any title, description or body text when A and B fail. It is review-only.
- **Review items** reuse the `entity_match` kind on the issuer's newest filing, so the
  existing `link` / `create` / `reject` verdicts apply (a `link` writes the CIK with
  `cik_source = 'manual'`). Discovery's details sit under `payload.issuer_discovery`
  (`reason`: `domain_exists`, `several_domains` or `tier_c`; the CIK, tier, domains,
  evidence, amount sold); an item the resolver already opened for the event is updated,
  never duplicated.
- **Retries**: a `no_domain` issuer is due 30 and 90 days after its first try, and
  whenever an unlinked Form D event of its CIK was observed after its last try. A plain
  run takes new issuers first (newest filing first), then due retries; `--retry-due` takes
  only retries. New issuers and next-filing retries come from filings of the last 90 days
  (or `--since`); a scheduled retry (`next_retry_at` due) is selected from its CIK's newest
  eligible filing of any date, because the 90-day retry of an issuer first tried a day
  after its filing falls outside that window.
- **Review tooling**: `tell resolve close-junk-reviews` never closes an item with
  `payload.issuer_discovery` (it proposes a domain, not an alias candidate, so the
  matcher's R6 says nothing about it; the report counts `kept_issuer_discovery`). The CSV
  export (`discovery_reason`, `discovery_tier`, `discovery_domains`, `discovery_evidence`,
  `discovery_amount_sold`) and the Streamlit page show the proposal, since tier C and
  several-domains items carry no `candidates`.
- **Ingest links only a trusted CIK.** The EDGAR collector (daily index and `tell backfill
  formd`) links a new filing at write time only to a company whose `cik_source` is in the
  rule R0 set (`tell.collectors.edgar_formd.company_ids_by_cik`); before, any company
  holding the CIK got it, which was an R0 that trusted CIKs of unknown origin. A filing of
  a company whose CIK has an unknown origin is written unlinked and left for `tell resolve
  run` (name rules and guards). Such a company still counts as monitored for 8-Ks, so its
  8-Ks are still fetched, and left unlinked the same way. The SPEC 13.2 backtest
  (`tell/backtest/velocity_lead.py`) maps quarterly-data-set filings to companies through
  the same function, so it also counts only trusted CIKs.
- **Writes**: `--shadow` (the default) writes nothing, not even a `runs` row. `--apply`
  commits the selection before any request and then each issuer on its own, so no
  transaction is open across network I/O. A created company has no `ats_boards` row, so
  the next `tell discover batch` picks it up; its brand and domain label become `manual`
  aliases and the filing's names `edgar` aliases.
- **Workflow**: one step `tell discover issuers --apply` in the `filings` job after the
  EDGAR ingest, run only when the Actions variable `TELL_ISSUER_DISCOVERY` is `on` (unset
  by default, so production is unchanged until the owner turns it on).

## Production steps that need the owner's approval

Accept this ADR; run migration 0009 on production; start fetching company sites; run the
production backfill and the 2026Q2 backfill; enable the daily workflow step; publish or
quote any 13.1 number.
