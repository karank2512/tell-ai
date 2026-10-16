# 0040. HubSpot with the nudge: rule 3 reads the customer's portal, `enrich` pushes to it

- Status: proposed
- Date: 2026-10-17

## Context

- **The loop had a gap in the middle.** ADR 0036 nudges, ADR 0038 says whether the account
  is worth a credit, ADR 0037 writes the account to HubSpot, but only by hand (`tell crm
  hubspot push`). The research's flow ("the moment tell spots a signal, it nudges the
  person, checks whether the lead is worth enriching, and it lands in the CRM") needs the
  three joined without a command in between.
- **Rule 3 was `crm_unchecked`.** The gate's "not already active in the CRM" rule needs a
  token for the customer's portal, which tell did not hold anywhere.
- **The first live portal showed a trap.** After tell's own push, the company's
  `notes_last_updated` ("Last Activity Date") was set by tell's note while
  `hs_last_sales_activity_timestamp` ("Last Engagement Date") stayed empty (2026-10-16).
  A rule 3 that read the note date would skip every company tell had touched.

## Decision

1. **The token lives in the watchlist's `tell-private` record**, beside the webhook:
   `hubspot_private_app_token` (a private-app or service-key token; ADR 0037's scopes) and
   an optional `hubspot_owner_id` for the task. Hand-edited by the operator, never asked on
   the intake form, never stored in the database or printed. `tell-private`'s "no secrets
   except webhooks" rule gains this second exception.
2. **Rule 3 reads sales activity, never tell's note.** With a token, the nudge run looks
   the company up by domain (search, then the object-store fallback of #6) and reads
   `hs_last_sales_activity_timestamp` and `notes_last_contacted`: either inside 90 days is
   `active` and the verdict is `skip` ("already in HubSpot with sales activity in the last
   90 days"); present without is `quiet`, absent is `not_in_crm`, both pass. A HubSpot
   error leaves the rule `crm_unchecked` rather than blocking the nudge.
3. **`enrich` pushes before the message goes out.** The push is ADR 0037's, with the task:
   company upsert, five properties, the receipt note, a task assigned to the owner id. The
   nudge gains a line: `HubSpot: added · note + task`, `updated · note + task`, `already
   current` or `push failed (401)`. The ledger row (migration 0013: `crm_action`,
   `crm_company_id`, `crm_pushed_at`) records it; a resend after a failed Slack POST shows
   the stored line and never pushes twice; a `failed` push is retried on the next attempt.
   A push failure never stops the nudge.
4. **Counts.** `tell notify` prints `nudges.crm` (`created` / `updated` / `unchanged` /
   `failed`) and the `slack_nudge` run's notes carry the same.

Interpretations made while implementing (flag if wrong):

- Only `enrich` pushes. A `hold` (thin coverage, budget) and a `skip` leave HubSpot alone;
  the person can still run `tell crm hubspot push` by hand.
- The task is always created on an automatic push (the nudge is the "do something now"
  moment), due in 2 days, assigned to `hubspot_owner_id` when set.
- One token per watchlist, not per customer: two lists of the same company share a token
  by having the same value in both records.

## Consequences

- With a token in their record, a design partner's flow is complete without a command:
  crossing, verdict, HubSpot record with the receipt and a task, Slack nudge that says all
  of it. Without a token nothing changes.
- Operator steps: migration 0013 on production; for a partner, add
  `hubspot_private_app_token` (and `hubspot_owner_id`) to their `tell-private` record and
  run `tell crm hubspot setup` once against their portal with that token (the five
  properties must exist; the push does not create them).
- The per-watchlist client runs at the same 4 requests per second; a run with N candidates
  on a tokened list costs at most N lookups plus up to 5 requests per push (plus the
  listing pages when the search index misses).
- Not in this ADR: Slack buttons (claim, snooze), Attio or Salesforce, a push on `hold`.
