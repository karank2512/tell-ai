# 0020. Role-family taxonomy and location interpretations

- Status: accepted
- Date: 2026-10-05

## Context

SPEC section 7 gives the role-family keyword table ("title contains (any)", first family
wins, `exec` last, department hints), the fingerprint (`location_norm` keeps city and
country) and the rule that non-English titles fall to `other`. Building it raised choices
the table does not settle, and the Phase 6 verification asked for them to be recorded:

- `revenue` (sales) always matches before `revenue operations` (gtm_ops), so the gtm_ops
  keyword could never win on a literal first-match reading.
- Two department hints are shared by two families (`marketing`: gtm_ops and marketing;
  `data`: ml_ai and data).
- "Fewer than 50 open postings" in the small-company rule: raw count or stale-weighted?
- Non-English detection covered only non-Latin scripts, so "Développeur Backend" was
  `engineering` and "Responsable Marketing" was `marketing`.
- The keyword suffix rule (`s`, `es`, `ing`, `er`, `ers`) made "Barista (20 hrs)"
  `finance_ops` through `hr` + `s`.
- `location_norm` read a two-letter country code that is also a US state or Canadian
  province code as the state ("Toronto, CA" -> `toronto, us`), and the " or " split
  dropped the state in "Portland, OR 97201".

## Decision

- **Masking.** A keyword match lying strictly inside a longer match from another family is
  ignored, so "Revenue Operations Manager" is `gtm_ops` while "Chief Revenue Officer" stays
  `sales`. Otherwise first family in table order wins.
- **Shared department hints go to the broader family** (`marketing`, `data`): a department
  alone cannot tell the ops or ML variant from the core function. Hints are consulted only
  when no title keyword of any family (including `exec`) matched.
- **The small-company count is stale-weighted** (`open_postings`): a board full of
  200-day-old postings does not make a company "large", consistent with the spec's stale
  weights applying to "any magnitude it feeds".
- **Non-English titles.** Non-Latin scripts are detected by letter share; Latin-script
  titles are flagged by a short stop-word list of unmistakably non-English role words
  (`NON_ENGLISH_WORDS`: French, Spanish, Portuguese, German, Italian role nouns such as
  "développeur", "ingeniero", "responsable", "entwickler", "praktikant"). Words that are
  also English words are not listed; an accent alone does not decide; an English title
  with a German gender marker ("Software Engineer (m/w/d)") is English. This is not
  language detection and will miss titles made only of cognates; it stands until the
  translation table the spec anticipates exists.
- **Short keywords take only the plural.** Keywords of three letters or fewer (`ae`,
  `sdr`, `bi`, `ux`, `vp`, ...) accept `s` only; `hr` accepts no suffix ("hrs" is hours).
  "HRBP" stays a miss (`other`), not a false positive.
- **Location codes.** The " or " split applies only when the word before it is not
  followed by a comma, so state codes survive ("Portland, OR 97201" -> `portland, us`).
  The state-versus-country collision is **kept as is**: "City, XX" with `XX` a US state
  or Canadian province code is read as that state. ATS boards write "City, ST" far more
  often than "City, CC", each board formats its locations consistently, and
  `location_norm` exists only to make the requisition fingerprint stable per board; it is
  never used as geography (entity resolution uses Form D state against `hq_state`). A
  wrong country therefore neither splits nor merges requisitions. Documented rather than
  "fixed" because any heuristic (city lists, board-level signals) adds a dependency or a
  guess for no fingerprint benefit.

## Consequences

- `role_family` sends listed-word titles to `other`; `tests/signals/test_taxonomy.py` and
  the 60-title acceptance list pin the behaviour.
- `docs/methods/hiring-velocity.md` section 1 and 2 describe every rule above.
- If velocity is ever broken down by country, the location collision must be revisited
  first.
