# 0037. HubSpot write-back through the customer's private app: company properties plus a note

- Status: proposed
- Date: 2026-10-15

## Context

- SPEC section 9 delivers tell through a static site, JSON, a Slack digest, a CSV and
  webhooks "into Clay and HubSpot" (PLAN section 2), but no code writes to a CRM: the
  Clay recipe pulls `companies/{domain}.json` read-only and the CSV is a file a person
  uploads.
- The deep-research report of 2026-10-15 (`tell — Next steps research 2026-10-15.md`,
  kept with the plan documents) decided the next 90 days: tell becomes a nudge, not a
  feed, and the nudge ends in the customer's CRM with the receipt attached. Its findings
  for the destination:
  - HubSpot's CRM API and its hosted MCP server write Companies, Notes and Tasks on every
    tier, the Free CRM included.
  - The closest cheap competitor (Unify) ships Slack notifications and read-only CRM sync
    on its free and Pro tiers and paywalls read-write CRM behind its Business tier, so a
    free write-back is the wedge.
  - HubSpot app events are restricted to approved technology partners, and the
    mcp-server app component and Agent Builder need Professional or Enterprise plus
    HubSpot review; custom objects for a "signal" record are likely Enterprise-only.
  - Clay accepts webhook and HTTP inputs only on its Growth plan, so Clay stays pull-only.
- Constraints that bind: no new hosted service, no paid dependency, no personal data in
  any payload (SPEC section 1, CONTRIBUTING), and the ingest job already has every secret
  it is allowed (`DATABASE_URL`, `SLACK_WEBHOOK_URL`, `TELL_DATA_PUSH_TOKEN`; SPEC
  section 2 secrets policy).

## Decision

1. **Destination and route.** HubSpot first, through a private app or service key the
   customer creates in their own portal (`HUBSPOT_PRIVATE_APP_TOKEN`; HubSpot replaced
   private apps with service keys on new accounts in 2026, same bearer token and scope
   names, see `docs/crm/hubspot.md`), not a marketplace app, not HubSpot's MCP server,
   not app events. The token is the customer's; tell holds it only where the command
   runs.
2. **Objects.** Standard company objects and engagements only:
   - the company is found by `POST /crm/v3/objects/companies/search` on `domain`, then
     created (`name`, `domain`, the properties below) or patched (the properties only);
   - five company properties in the `companyinformation` group, created once by
     `tell crm hubspot setup`: `tell_score` (number), `tell_top_signal` (text),
     `tell_receipt_url` (text), `tell_scored_at` (date), `tell_profile` (text);
   - a note (`hs_note_body` plain text: score line, one `summary · date · url` line per
     contribution with a receipt, the digest's `Why now` sentence, the account page link)
     associated to the company with HubSpot-defined type `190`;
   - optionally a task (`--task`; type `192`, `NOT_STARTED`, due in 2 days, optional
     `hubspot_owner_id`).
   No custom object, no timeline event, no contact, no deal.
3. **Idempotency without a table.** A push is `unchanged` when the HubSpot company's
   `tell_scored_at` and `tell_score` already equal the score being pushed; `--force`
   overrides. The HubSpot record is the ledger, so no migration and nothing to keep in
   sync.
4. **Surface.** `tell crm hubspot check | setup | push DOMAIN [--profile] [--task]
   [--owner-id] [--dry-run] [--force]` (`tell/crm/`), one JSON line each, exit 1 with a
   token or scope hint on `401` / `403`. Nothing calls it from a workflow yet; the per
   event Slack nudge (a separate change) is the intended caller, on a click or
   automatically for the top tier.
5. **Politeness.** One `RateBudget` at 4 requests per second (Free and Starter private
   apps allow 100 per 10 seconds), the project User-Agent, the shared retry policy; a
   POST or PATCH is never retried after a transport error.

Interpretations made while implementing (flag if wrong):

- The scopes are `crm.objects.companies.read`, `crm.objects.companies.write`,
  `crm.schemas.companies.write`, and `crm.objects.contacts.read` /
  `crm.objects.contacts.write`, the last two because HubSpot's notes and tasks guides list
  them as the engagement endpoints' requirement. tell never calls a contacts endpoint;
  the token can read contacts, so the docs tell the customer so.
- A score's note is written once per scored day; a second push of the same day and value
  is `unchanged`. A later day with the same value writes a new note.
- `push` takes the newest stored score for the profile, whatever its date; the caller
  decides whether a stale score is worth pushing.
- The existing company's `name` is never overwritten; tell's name is used only when tell
  creates the record.
- The CRM search guide was not reachable on 2026-10-15; the search body follows HubSpot's
  documented `filterGroups` shape and the fixtures are hand-authored and declared as such
  (`tests/fixtures/hubspot/README.md`).

## Consequences

- No new hosted service, no credit card, no new dependency: `httpx` through
  `tell.http.fetch`, the customer's own HubSpot.
- No personal data leaves tell: company name, domain, score, profile, dates and public
  receipt URLs only; the PII guard's fixture scan covers `tests/fixtures/hubspot/`.
- Two new settings (`HUBSPOT_PRIVATE_APP_TOKEN`, a secret; `HUBSPOT_API_BASE`, for tests)
  and one new doc page (`docs/crm/hubspot.md`). SPEC section 2's secrets policy gains a
  fourth secret that lives only where a customer's push runs, never in the public repo's
  Actions.
- The acceptance test is the first real push against a Free portal (the page's setup
  section); until then the write path is proven only against hand-authored fixtures, and
  a `400` on the first run means a fixture update, not a silent patch.
- Salesforce, Attio and Clay write paths, when they come, follow the same shape
  (`tell/crm/<vendor>.py`, properties plus a note or its equivalent) and the same rule:
  the customer's token, company-level data, no tell-side ledger.
