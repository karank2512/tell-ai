# 0041. No public lead-time claim until a report with full follow-up marks it quotable

- Status: accepted
- Date: 2026-10-19

## Context

- **The speed-vs-press cohorts are censored.** A Form D filed 20 days ago has had 20 of its
  60 days to get press, so a round that is covered in week 7 counts as "no press" today. In
  the 2026-10-15 verified report 214 of 331 filings had under 60 days of follow-up; the
  lead-time medians and the filing-first share rest on the 44 filings with press, and the
  report marks them not quotable. The latest dated report has N=18.
- **The launch copy asked for them.** SPEC 15.3 has the LinkedIn bullet "[N] funding rounds
  a median [X] days before the first press mention", and 15.5 item 7 has the README quoting
  the section 13 headline numbers inline. `docs/SPEC.md` and `docs/PLAN.md` are read-only
  (CLAUDE.md), so the deviation is recorded here.

## Decision

1. **No public copy quotes a Form D lead-time median (or p75), a "days before press"
   figure or a "filing first" share** until a dated report with N of at least 100 and full
   60-day follow-up marks them quotable. Public copy is the README, `docs/launch/`, the
   published site, the `tell-mcp` prompt and tool text, a resume bullet, and anything posted
   or sent.
2. **Until then the only speed claims** are the two that the 2026-10-15 report supports,
   each quoted with its N: "of 331 Form D filings tracked, 44 (13.3%) had verified press
   within 60 days" and "29.1% of new Delaware-corporation issuers linked to a company at 59
   of 59 precision".
3. **Applied to the repo.** The LinkedIn draft's first bullet is the first claim above in
   place of SPEC 15.3's median bullet, through the placeholders `cohort_n`, `n` and
   `press_share_pct` in `docs/launch/numbers.yaml`; `median_lead_days` is gone; the README
   no longer lists the days between a Form D and its first press mention among the numbers
   to come; `docs/launch/README.md` states the rule. A resume bullet of the SPEC shape is
   dropped, or filled only from a report that meets (1).
4. **The measurement is unchanged.** `tell backtest speed-vs-press` and its templates keep
   printing the medians with their N and the `quotable` flag. The rule limits what is said,
   not what is measured.

## Consequences

- `Report.quotable` (`tell/backtest/common.py`) is N-based: a headline number with N under
  30 and, for 13.1, a cohort under 100 filings. It does not know the follow-up, so for a
  lead-time figure `quotable: true` is necessary but not sufficient: the person quoting
  checks the follow-up in the report's own cohort numbers. Teaching the report to flag it
  is a separate change.
- The two claims are quoted as worded. A paraphrase ("only 13% get press", "tell beats the
  news") is a new claim and needs its own report line.
