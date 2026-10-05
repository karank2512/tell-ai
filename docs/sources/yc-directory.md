# YC company directory (seed list)

Spec: `docs/SPEC.md` section 4 ("Seed lists for the first 5,000 domains"). Decision:
`docs/decisions/0009-yc-directory-only-seed.md`. Code: `tell/discovery/yc.py`. CLI:
`tell discover seed-yc --fetch` or `tell discover seed-yc --file PATH`. Tests:
`tests/discovery/test_yc.py`, `tests/discovery/test_cli.py`.

## Source chosen

| Option | What it is | Verdict |
| --- | --- | --- |
| `https://yc-oss.github.io/api/companies/all.json` | Community mirror (github.com/yc-oss/api). A GitHub Actions job exports the public Algolia index behind ycombinator.com/companies once a day and publishes it as static JSON on GitHub Pages. | **Used.** One static GET, no key, no JavaScript, no scraping of ycombinator.com. |
| ycombinator.com/companies (Algolia) | The official directory page queries Algolia with a search-only key embedded in the page. | Not used. The key is an implementation detail of YC's site, it rotates, and paging the index means many requests against a service we have no agreement with. Fallback only if the mirror disappears. |
| ycombinator.com/companies HTML | Server-rendered company pages. | Not used: about 6,000 page fetches to get what the mirror gives in one. |

Checked on 2026-10-05: `meta.json` reported 6,269 launched companies across 51 batches,
last updated the same morning (03:07 UTC). The mirror lists **launched companies only**
(those with a public page on ycombinator.com), so it is a lower bound on the YC portfolio.

## Endpoint

`GET https://yc-oss.github.io/api/companies/all.json` returns one JSON array (about 12 MB).
Per-batch files (`/api/batches/winter-2024.json`) and `meta.json` exist too; tell reads only
`all.json`.

- Rate limit: none published (GitHub Pages static hosting). One request per seed run; the
  shared HTTP layer (`tell.http.get`) sends the project User-Agent and retries 429/5xx.
- Auth: none.

## Fields read

The pydantic model `YcRecord` declares four fields and drops everything else at parse
time, so nothing else is held in memory past validation, let alone stored.

| Field | Becomes |
| --- | --- |
| `name` | `companies.name` (whitespace collapsed) |
| `website` | `companies.domain`: registrable domain via `tldextract` (bundled suffix list, private suffixes such as `vercel.app` kept), lowercase, no `www`, path dropped. Records with an empty or unusable website are skipped and counted. |
| `all_locations` | `hq_city`, `hq_state`, `hq_country` from the **first** `;`-separated location, only when it has the US shape `City, ST, USA` (two-letter state); `hq_country` is stored as `US`. Any other shape leaves all three `NULL`. |
| `batch` | returned by `fetch_directory` (e.g. `Winter 2012`) but not stored: `companies` has no column for it. |

Never read or stored: founder names or anything else about people, descriptions
(`one_liner`, `long_description`), application answers, logos.

## Write rules

`INSERT ... ON CONFLICT (domain) DO UPDATE` in chunks of 500. `name` takes the YC value;
the `hq_*` columns keep an existing value when YC has none; the row (and `updated_at`) is
touched only when a value actually changes, so re-seeding the same file is a no-op
(asserted by `test_reseed_is_idempotent`). Within one seed the first record for a domain
wins; later records with the same domain are counted as `duplicates`.

## Legal note

The YC directory is public, factual information about companies. tell copies four facts per
company (name, website, batch, location) and no personal data. The mirror repository
declares no licence (GitHub reports none as of 2026-10-05) and states it is unofficial and
built from YC's public Algolia index; YC's own site terms govern the underlying data. We do
not redistribute the mirror's file: the published dataset only ever carries the derived
company rows. If YC or the mirror's maintainers object, switch the seed off; nothing else in
tell depends on it.

## Fixture

`tests/fixtures/yc/sample.json`: 21 real public records recorded live from the mirror on
2026-10-05 (20 from `companies/top.json`, plus `Arcane` from `batches/winter-2024.json`
because its `website` is empty), trimmed to `name`, `website`, `batch`, `all_locations`. It
covers `www.` and path-bearing websites, trailing slashes, multi-location strings,
`; Remote`, an empty location, non-US locations and a two-part location.

## Sample response (one record, trimmed to the fields read)

```json
{
  "name": "Gusto",
  "website": "https://gusto.com",
  "batch": "Winter 2012",
  "all_locations": "San Francisco, CA, USA"
}
```
