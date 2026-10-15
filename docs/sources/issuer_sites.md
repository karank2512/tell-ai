# Company sites of Form D issuers (issuer-first discovery)

Decision: [ADR 0035](../decisions/0035-issuer-first-discovery.md) (proposed). Command:
`tell discover issuers [--since DATE] [--limit N] [--shadow | --apply] [--retry-due]
[--concurrency C] [--max-minutes M]`. Code: `tell/discovery/issuers.py` (network and
database), `tell/resolve/evidence.py` (the pure evidence rule). Tests:
`tests/discovery/test_issuers.py`, `tests/resolve/test_evidence.py`. Fixtures:
`tests/fixtures/issuer_sites/{normal,empty_board,drift}`, and the frozen hand check
`tests/resolve/issuer_evidence_20261014.json`.

This is not a collector: it writes no events. It reads the Form D events EDGAR already
stored, looks for each new issuer's own website, and creates the company only when the
site itself names the issuer. The filing then links exactly by CIK (rule R0).

## Endpoints

Per issuer, in this order (all `GET`, through the run's one `httpx` client):

| Call | URL | Bound |
| --- | --- | --- |
| ATS slug probes | `boards-api.greenhouse.io/v1/boards/{slug}/jobs`, `api.lever.co/v0/postings/{slug}?mode=json&limit=1`, `api.ashbyhq.com/posting-api/job-board/{slug}`, `api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1` for the slugs `{joined}`, `{hyphenated}`, `{core}` of the normalized issuer name | at most 12, plus 1-3 to read a found board's own name and links (`ownership_evidence`; its vendor-hosted board pages, such as `jobs.ashbyhq.com/{slug}`, are robots-checked like a company site) |
| DNS | an address lookup for each domain guess (`{core}.com`, `.ai`, `.io`, `.co`, `get{core}.com`, `{core}hq.com`, `{joined}.com`, `{joined}.ai`) | at most 8 guesses, plus the found board's first linked site |
| `robots.txt` | `https://{domain}/robots.txt`, and for every redirect target's origin | once per origin per run |
| Homepage | `https://{domain}/`, redirects followed by hand (at most 3 hops, each robots-checked); the company's key is the final registrable domain | 1 page |
| Legal pages | the homepage's terms/privacy/legal links on the same site, then `/terms`, `/privacy`, `/terms-of-service`, `/privacy-policy`, `/legal`; only when the homepage names the brand; stops at the first page with the legal name | at most 8 per domain |

Measured on a 40-issuer sample (design pass, 2026-10-14): about 25-35 HTTP requests per
issuer. About 12 new issuers a day, so a few hundred requests a day, under the 1,000-a-day
ceiling in ADR 0035's acceptance criteria. `runs.notes.requests` records the count.

## Rate limit and identity

- One request per second per host (`HOST_RPS`), enforced by a token bucket per host
  installed as an `httpx` request hook for the whole run, so the up to 8 issuers in flight
  (`--concurrency`, default 8) never exceed it on any host, vendor APIs included.
- Company sites get one attempt with a 10 s timeout; vendor APIs follow the shared retry
  policy (`tell/http.py`: 429 and 5xx with backoff).
- User-Agent: `TELL_HTTP_USER_AGENT`, `tell/0.1 (+https://github.com/karank2512/tell-ai)`.
  The SEC is not contacted by this command.
- No new issuer starts after `--max-minutes` (default 12); the workflow step has a
  15-minute timeout.

## Legal note

- **Company sites** are read like a visitor would: `robots.txt` honoured per origin (a
  401/403 or unreachable `robots.txt` means keep out, RFC 9309), only the homepage and the
  legal pages, at one request per second, with a User-Agent that names the project. No
  login, no form, no search engine, no LinkedIn, no purchased list.
- **What is stored** is only the domain, the page path and the matched legal-name span
  (`issuer_discovery.evidence = {path, span}`, for example `{"path": "/terms", "span":
  "Uviquity, Inc."}`), never surrounding page text: a legal page can carry a street
  address or a phone number. Street addresses are compared in memory only; the company
  gets the Form D city and state.
- **SEC data** is public domain and already stored by the EDGAR collector. The Form D
  `relatedPersonsList` is never read.
- **ATS vendor APIs** are the documented public ones of SPEC sections 3.1-3.4. The
  vendor-hosted board pages `ownership_evidence` reads for a found board's name and links
  (Greenhouse, Lever and Ashby hosted boards, the SmartRecruiters careers page) are not
  APIs, so their `robots.txt` is honoured: a disallowed page is not fetched.

These are engineering notes, not legal advice. Starting this source in production needs
the owner's approval (ADR 0035, "Production steps").

## Sample

A terms page (`tests/fixtures/issuer_sites/normal/terms.html`, fictional company):

```html
<p>These Terms of Service govern your use of the website at quillfeather.com (the
   &ldquo;Site&rdquo;), operated by Quillfeather Robotics, Inc. (&ldquo;Quillfeather&rdquo;).</p>
<p>Notices to us must be sent in writing to our office in Austin, TX 78701.</p>
```

What the run prints for that issuer (one JSON line; the run line follows):

```json
{"boards": [], "candidates": ["quillfeather.com", "quillfeather.ai", "..."],
 "cik": "0002990001", "company_id": 1, "domain": "quillfeather.com", "domains": [],
 "evidence": {"path": "/legal/terms-of-service", "span": "Quillfeather Robotics, Inc."},
 "issuer": "Quillfeather Robotics, Inc.", "linked_events": 1, "outcome": "linked",
 "retry": false, "review_id": null, "tier": "A"}
```

## Outcomes

| Outcome | When | Writes (`--apply`) |
| --- | --- | --- |
| `linked` | exactly one domain at tier A or B | a company (`source = 'issuer_first'`, `cik_source = issuer_first_a/b`), `edgar` aliases of the issuer's names, every unlinked EDGAR event of the CIK linked by R0, `issuer_discovery.status = 'linked'` |
| `conflict` | the domain already belongs to a company, or two domains qualify | an `entity_match` review item on the filing (`payload.issuer_discovery.reason` = `domain_exists` / `several_domains`); nothing on any company changes |
| `tier_c` | only weaker evidence (the brand in a title or description) | a review item when the amount sold is at least $1M, else nothing but the record |
| `no_domain` | nothing found | retried 30 and 90 days after the first try (selected from the issuer's newest eligible filing whatever its date, so the plain daily step runs the 90-day retry although that filing is then outside the 90-day window), and on the issuer's next filing |
| `known_cik` | a company took the CIK while the network half ran | nothing |

`--shadow` (the default) writes nothing at all and prints the same lines, for the hand
checks in ADR 0035's measurement plan.

## Fixtures

`tests/fixtures/issuer_sites/`: `normal` (legal name on the terms page, tier A),
`empty_board` (the guessed domain belongs to someone else), `drift` (the legal entity line
moved into a script and the title; the contract test fails loudly). Under 5 KB in all, no
person's data. The real excerpts of the design's hand check are in
`tests/resolve/issuer_evidence_20261014.json`.
