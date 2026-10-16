# HubSpot fixtures

Hand-authored, not recorded: they follow the response shapes HubSpot documents for the
properties v3, companies v3 (search, create, update), notes v3 and tasks v3 endpoints and
the error body shape of its `401 INVALID_AUTHENTICATION` and `403 MISSING_SCOPES`
answers (`docs/crm/hubspot.md` lists the pages). Record a real one against a Free
portal's private app only if a request shape stops working, trim it under 50 KB, scrub
`hubspot_owner_id` values and any contact field, then update these files and the tests
in `tests/crm/`. Every id, domain and timestamp here is synthetic; nothing names a person.

| File | Stands for |
| --- | --- |
| `property_tell_score.json` | `GET /crm/v3/properties/companies/tell_score` (exists) |
| `property_not_found.json` | the same GET for a property that does not exist (404) |
| `property_created.json` | `POST /crm/v3/properties/companies` |
| `companies_probe.json` | `GET /crm/v3/objects/companies?limit=1` |
| `search_empty.json`, `search_found.json`, `search_found_same_push.json`, `search_found_active.json` | `POST /crm/v3/objects/companies/search` by `domain`; the last one carries `hs_last_sales_activity_timestamp` / `notes_last_contacted` inside 90 days (gate rule 3, ADR 0040) |
| `companies_list_found.json`, `companies_list_more.json`, `companies_list_end.json` | `GET /crm/v3/objects/companies?limit=100&properties=...` pages: one holding the domain (mixed case, as a user may have typed it), one of other companies with a `paging.next.after` cursor, and the empty last page |
| `company_created.json`, `company_updated.json` | `POST` / `PATCH /crm/v3/objects/companies` |
| `note_created.json` | `POST /crm/v3/objects/notes` |
| `task_created.json` | `POST /crm/v3/objects/tasks` |
| `unauthorized_401.json`, `missing_scopes_403.json` | the two auth errors the CLI turns into hints |
