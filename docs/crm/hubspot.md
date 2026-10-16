# HubSpot write-back

[ADR 0037](../decisions/0037-hubspot-write-back.md). `tell crm hubspot` puts tell's score
and its receipt on the customer's own HubSpot company record: five `tell_*` company
properties and a timeline note with one dated, linked line per contribution. It writes
company objects, company properties, notes and tasks, and nothing else. It never reads or
writes a contact, and nothing it sends names a person.

The research of 2026-10-15 put HubSpot first because its CRM API writes companies and
notes on every tier, Free included, while the signal tools that serve the same seed-stage
buyer paywall CRM write-back; a private-app token is the route because marketplace apps
and app events need HubSpot partner approval.

## Setup (once per HubSpot account, about ten minutes)

1. **A HubSpot account.** The Free CRM is enough. Create one at hubspot.com if the design
   partner has none; the super admin of the account does the next step.
2. **A service key** (or, on an account that still offers them, a private app) with the
   scopes below. Both are the same kind of credential: a `pat-...` token sent as
   `Authorization: Bearer`, with the same scope names, and tell treats them identically.

   - **Service key** (what new accounts get; HubSpot's replacement for private apps,
     public beta since February 2026): in HubSpot go to Development, then Keys, then
     Service keys, click Create service key, name it `tell`, click Add new scope, tick
     the scopes below, Update, then Create. On the key's page click Show, then Copy.
     Only a super admin or a user with Developer tools access can do this. The key can
     be shown again later, is rotated with a 7-day grace period for the old value, and
     HubSpot suggests rotating it every six months. A service key cannot receive
     webhooks, which tell does not use.
   - **Private app** (older accounts): Settings, then Integrations, then Private Apps,
     or Development, then Legacy apps, then Create legacy app, then Private. Name it
     `tell`, add the scopes, create the app, open its Auth tab and copy the access
     token. An account that shows "Legacy apps aren't available on this account" uses
     the service key path above.

   | Scope | Why |
   | --- | --- |
   | `crm.objects.companies.read` | search a company by domain, read its `tell_*` values |
   | `crm.objects.companies.write` | create or update the company |
   | `crm.schemas.companies.write` | create the five `tell_*` properties (`setup`) |
   | `crm.objects.contacts.read`, `crm.objects.contacts.write` | HubSpot's notes and tasks guides list these as the engagement endpoints' scope requirement; tell never calls a contacts endpoint |

   If HubSpot shows the scopes as granular picks, choose the company and contact object
   read/write scopes plus company property settings write; `tell crm hubspot check` tells
   you which scope is missing when a call is refused.
3. **The token** goes in `.env` as `HUBSPOT_PRIVATE_APP_TOKEN=...` on the machine that
   runs the commands, or in a secret held by `tell-private`'s workflows. Never in this
   repo, never in an Actions variable, never in a log. The commands print the token
   nowhere; a 401 is reported as "check HUBSPOT_PRIVATE_APP_TOKEN".
4. `uv run tell crm hubspot check`, then `uv run tell crm hubspot setup`.

## Commands

| Command | Does | Prints |
| --- | --- | --- |
| `tell crm hubspot check` | one authenticated read (`GET /crm/v3/objects/companies?limit=1`), then looks up each `tell_*` property | `{"ok": true, "properties": {...}, "setup_needed": bool}`; exit 1 with a token or scope hint |
| `tell crm hubspot setup` | creates the `tell_*` company properties that do not exist (idempotent) | `{"created": [...], "existing": [...]}` |
| `tell crm hubspot push DOMAIN [--profile NAME] [--task] [--owner-id ID] [--dry-run] [--force]` | loads the newest stored score of `DOMAIN` under the profile (default `default`), searches HubSpot by `domain`, creates or updates the company, writes the note, optionally a task | `{"action": "created" \| "updated" \| "unchanged" \| "dry_run", "domain", "hubspot_company_id", "note_id", "task_id"}` |

`push` needs a score in the database first (`tell score --all-profiles`); a domain tell
does not know, or one with no score for the profile, exits 1 with a message that says
which command to run. `--dry-run` needs no token: it prints the three request bodies that
would be sent and sends nothing. The first real push against a Free portal is the
acceptance test of this page: `check`, `setup`, then
`push <a domain with a score> --task`, then open the company in HubSpot and see the
properties, the note and the task.

## What tell writes

**Company** (`POST /crm/v3/objects/companies` for a new record, `PATCH
/crm/v3/objects/companies/{id}` for an existing one, found by `POST
/crm/v3/objects/companies/search` with `domain` `EQ` the tell domain). A new company gets
`name` and `domain` plus the properties below; an existing company keeps every field it
has and only the `tell_*` properties move.

| Property | Type | Value |
| --- | --- | --- |
| `tell_score` | number | the stored score, one decimal |
| `tell_top_signal` | single-line text | summary of the largest contribution |
| `tell_receipt_url` | single-line text | its source URL |
| `tell_scored_at` | date | the scored day (`as_of`) |
| `tell_profile` | single-line text | the ICP profile |

All five live in HubSpot's `companyinformation` group with labels prefixed `tell`.

**Note** (`POST /crm/v3/objects/notes`, `hs_timestamp` = now, `hs_note_body` plain text,
associated to the company with the HubSpot-defined association type `190`, note to
company):

```
tell score 30.1 (default) as of 2026-10-06
Form D: $10,000,000 offering · 2026-10-01 · https://www.sec.gov/Archives/edgar/data/.../0001.txt
4 new sales roles in 7 days vs 0.5 baseline · 2026-10-03 · https://boards.greenhouse.io/...
Why now: Form D: $10,000,000 offering and 4 new sales roles in 7 days vs 0.5 baseline.
Account page: https://karank2512.github.io/tell-data/companies/example.com.html
```

Only contributions with a receipt URL are listed (the receipt rule of the site and the
CSV). The body is capped under HubSpot's 65,536-character limit.

**Task** (only with `--task`; `POST /crm/v3/objects/tasks`, association type `192`, task
to company): subject `tell: {company} crossed {threshold}`, body the `Why now` sentence,
`hs_task_status` `NOT_STARTED`, `hs_task_priority` `MEDIUM`, `hs_task_type` `TODO`, due in
2 days (`hs_timestamp`), assigned to `--owner-id` when given.

**Idempotency.** When the HubSpot company already carries the same `tell_scored_at` and
`tell_score`, the push reports `unchanged` and writes no second note or task; `--force`
writes them anyway. A new day's score is a new note. No tell table records pushes; the
HubSpot record is the ledger.

## What tell never writes

No contact, no deal, no ticket, no person's name, email or phone. The company's existing
`name` is not overwritten. Form D `relatedPersonsList` is never read in the first place.
The note carries public receipts (a filing URL, a job board URL, a press URL) and tell's
own account page.

## Rate limits and failures

A Free or Starter private app allows 100 requests per 10 seconds per app and 250,000 per
day per account; HubSpot states that a service key follows the same limits as a privately
distributed app. The client paces itself at 4 requests per second through one shared
budget, retries 429 and 5xx with the project's backoff schedule, and never retries a POST
or PATCH after a transport error (a timed-out create may have landed). A `push` is at most
five requests: search, create or update, note, task.

| Answer | Reported as |
| --- | --- |
| `401` | `HubSpot rejected the token (401): check HUBSPOT_PRIVATE_APP_TOKEN` |
| `403 MISSING_SCOPES` | `the private app lacks a scope (403); grant: <the scopes HubSpot names>` (the wording covers a service key too) |
| other `4xx` / exhausted retries | HubSpot's message, exit 1 |

## HubSpot pages this was written against (fetched 2026-10-15)

- Service keys (fetched 2026-10-16): creation steps, `Authorization: Bearer pat-na1-...`,
  same limits as privately distributed apps, no webhooks:
  <https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/account-service-keys>
  and the beta announcement <https://developers.hubspot.com/changelog/service-keys>
- Private apps: creation steps, `Authorization: Bearer`, rate limits per tier:
  <https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/overview>
- Scopes (`crm.objects.companies.read` / `.write`, `crm.schemas.companies.write`):
  <https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/scopes>
- Companies v3: create with `name` / `domain` (domain is "the primary unique identifier"),
  `PATCH` to update: <https://developers.hubspot.com/docs/api-reference/crm-companies-v3/guide>
- Properties v3: `POST /crm/v3/properties/companies` with `name`, `label`, `type`,
  `fieldType`, `groupName`; the `number`/`number`, `string`/`text`, `date`/`date` pairs:
  <https://developers.hubspot.com/docs/api-reference/crm-properties-v3/guide>
- Notes: `POST /crm/v3/objects/notes`, `hs_timestamp` required, `hs_note_body` up to
  65,536 characters, `associations` with `HUBSPOT_DEFINED` and type `190`:
  <https://developers.hubspot.com/docs/guides/api/crm/engagements/notes>
- Tasks: `POST /crm/v3/objects/tasks`, `hs_task_*` properties, `hubspot_owner_id`:
  <https://developers.hubspot.com/docs/guides/api/crm/engagements/tasks>
- Association type ids (note to company `190`, task to company `192`):
  <https://developers.hubspot.com/docs/api-reference/crm-associations-v4/guide>

Not reachable on 2026-10-15 (404 at the documented address): the CRM search guide, so the
search request body (`filterGroups` / `filters` / `propertyName` / `operator` `EQ`) and
the `companyinformation` group name follow HubSpot's long-standing documented shapes and
are covered by the hand-authored fixtures in `tests/fixtures/hubspot/`. If the first real
`push` answers `400`, record the response, fix the shape and update the fixture, as the
fixture rule requires.
