# 0038. Enrichment gate v0: a five-rule verdict on every nudge, and a ledger of the advice

- Status: proposed
- Date: 2026-10-16

## Context

- **tell never enriches.** No personal data is held (ADR 0027), so tell cannot be the tool
  that finds an email or a phone number. The 2026-10-15 next-steps research (section 5,
  "The enrichment gate") concluded that the useful thing tell can do at that moment is
  tell the person whether the crossing is worth one of their own enrichment credits, and
  record that advice so its precision can be measured. Credits are the cost that matters
  to the first buyer: a Clay waterfall is 10 to 25 credits per row, Hunter's free plan is
  50 credits a month, and single-event alerts were the failure mode the workflow lane found
  most often.
- **The nudge already carries a one-rule gate.** ADR 0036 shipped `Worth enriching: yes ·
  {a} + {b}` / `not yet · one signal family` (two distinct signal families with receipts)
  as the contract line, with the full gate deferred to the CRM write path.
- **The CRM write path exists but is a command.** ADR 0037's `tell crm hubspot push` is
  run by hand; the automatic "nudge, then push when the gate says so" needs a per-watchlist
  CRM token in `tell-private`, which is its own decision.

## Decision

1. **`tell/signals/gate.py` decides `enrich`, `skip` or `hold`** from the report's five
   rules, every rule evaluated and recorded even after one fails:
   1. two distinct signal families with a receipt URL, else `skip` (`one_family`); the
      profile threshold and the size band are already implied by the crossing;
   2. the coverage note is not `dead:*` or `unsupported:*`, else `hold`
      (`thin_coverage:<note>`); `no_board` passes (see interpretations);
   3. the CRM is not checked in v0; `crm_unchecked` is recorded;
   4. no `enrich` verdict for the same company and watchlist in the last 90 days, else
      `skip` (`enriched_recently`); per watchlist, never across customers;
   5. `enrich` verdicts this UTC month below the watchlist's monthly budget, else `hold`
      (`budget_spent`); the budget is `credit_budget_monthly` in the watchlist's
      `tell-private` record, default 100 (the free Clay data-credit allowance the report
      lists as unverified; a hand-edited field, never asked on the intake form).
   `skip` wins over `hold`; `enrich` needs every rule to pass.
2. **The `enrichment_verdicts` ledger (migration 0011).** One row per (crossing event,
   watchlist): `verdict`, `reasons` (the rule tokens in order), `families`, `decided_at`,
   `company_id`. Written when the nudge is built, before the POST; a resend after a failed
   send reads the stored verdict back, so the Slack line never changes between attempts and
   the budget is never charged twice. Rules 4 and 5 read this table. Nothing in it names a
   person and tell stores no enrichment result: it is a ledger of advice, and the
   denominator of the acted-on rate once feedback arrives.
3. **The nudge line.** `Worth enriching: yes · {a} + {b}`, `skip · one signal family`,
   `skip · a credit went to this company in the last 90 days`, `hold · board unreadable
   (dead:lever:acme)`, `hold · this month's credit budget is spent`. The notify JSON line
   and the `slack_nudge` run's notes carry `verdicts: {enrich, skip, hold}` counts.
4. **Where the gate runs.** In `send_nudges`, per (watchlist, crossing), with the coverage
   note from `company_facts_many` and the budget from `load_private_budgets`. Pure callers
   without a session (`build_nudge_blocks` in tests, future `push_to_crm`) get `decide()`
   over the contributions alone, as if coverage, history and budget were fine.

Interpretations made while implementing (flag if wrong):

- `no_board` is not thin coverage. A seed company without a public job board is the
  normal case for Form D issuers, which are tell's own discoveries; treating them as `hold`
  would silence the gate exactly where tell adds something. Thin means tell knows of a
  board it cannot read.
- Rule 4 is per watchlist. Two design partners watching the same company each get their
  own advice; one customer's spend never changes another's verdict.
- Rule 5 counts the gate's `enrich` verdicts, not credits the person actually spent (tell
  cannot see those). Feedback will say how far apart the two are.
- The budget month is the UTC calendar month.

## Consequences

- Every nudge now explains its verdict in words a seller can act on, and every verdict is
  on record. Precision per rule can be read off the ledger joined to `feedback` once labels
  exist, which is what tuning the rules needs.
- One operator step: run migration 0011 on production before the next ingest run that has
  a private webhook (the ledger is written on every nudge).
- A design partner's budget is set by editing their `tell-private` record
  (`credit_budget_monthly: 50`); the intake form does not ask for it.
- Not in this ADR: rule 3 (the CRM check) and the automatic HubSpot push on `enrich`,
  which need a per-watchlist HubSpot token in `tell-private`; a `tell gate explain` command.
