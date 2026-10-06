# Method: ATS discovery (domain to job board)

Spec: `docs/SPEC.md` section 4 (procedure), section 5 (`ats_boards`, `review_queue`) and
section 11 (budget). Code: `tell/discovery/ats.py`. CLI: `tell discover domain --domain
acme.com [--name "Acme"]` and `tell discover batch [--limit N] [--rps R]`. Tests:
`tests/discovery/test_ats.py` (all HTTP mocked with `respx`, hand-crafted pages under
`tests/fixtures/discovery/`).

Nothing in the ATS APIs maps a domain to a board token, so discovery is its own job: given a
domain, find the board or record that none was found.

## Procedure

1. **Normalise** the domain to its registrable form (`https://www.Acme.com/x` ->
   `acme.com`) and find or create its `companies` row (`name` = the given name, else the
   domain).
2. **Fetch candidate pages** `https://{domain}/careers`, `/jobs`, `/join`,
   `/company/careers`, then the homepage `/`, in that order, **stopping at the first 200**.
   The scan uses that page only, even when it carries no board (as the spec says).
   - Redirects are followed by hand, **at most 2** per candidate; a third redirect abandons
     the candidate. A redirect whose `Location` is itself a board URL is a hit and is not
     fetched (common: `/careers` -> `jobs.lever.co/acme`).
   - **robots.txt** is fetched once per origin through the same client and budget and read
     with `urllib.robotparser`. A disallowed candidate is skipped and recorded in
     `pages_checked` as `robots_blocked:/careers`; a redirect into a disallowed path stops
     there. Status rules follow RFC 9309: 2xx parsed; 401/403 disallow everything; other
     4xx mean no rules; 5xx, network errors and redirect loops mean "unreachable", treated
     as disallow-all.
   - Vendor APIs and vendor-hosted board pages (name lookups, guesses) are documented
     public endpoints and go through `tell.http.fetch` (429/5xx retries) without a robots
     check.
   - Company pages get one attempt each (no retries): a flaky site is a miss, not a reason
     to stall the run.
3. **Scan** the page: `<iframe src>`, `<script src>` and `<a href>` (parsed with
   `lxml.html`), then the raw HTML text (inline scripts, data attributes), against the
   patterns below. Supported vendors win over unsupported ones (a page linking both a
   Workday site and a Lever board resolves to Lever); within a class the first hit wins.
   `grnh.se` short links rank **last**, after unsupported vendors too (see below).
4. **Slug guesses**, only when the scan found nothing (or its only hit was a `grnh.se`
   short link that did not resolve): from the domain label (`acme`),
   try `acme`, `acmeinc`, `acme-inc`, and for each slug Greenhouse, Lever, Ashby,
   SmartRecruiters in that order, with a small GET. A 200 with at least one job counts.
5. **Verify ownership** of a supported board: the board's own company name must reach
   `rapidfuzz.fuzz.token_set_ratio >= 85` (lowercased, punctuation stripped) against the
   domain label, the given company name, or the stored company name.

   | Vendor | Name source |
   | --- | --- |
   | Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}` -> `name` |
   | Lever | `<title>` of `jobs.lever.co/{site}` (`jobs.eu.lever.co` for `eu:` sites) |
   | Ashby | `<title>` of `jobs.ashbyhq.com/{name}` (e.g. "Ramp Jobs") |
   | SmartRecruiters | `content[0].company.name` of `GET /v1/companies/{id}/postings?limit=1` (taken from the guess response when the board came from a guess) |

   A name that cannot be fetched scores 0. Below 85, or when the board row already belongs
   to another company, a `review_queue` item is written (`kind='board_verify'`, payload
   `{domain, vendor, identifier, board_name, ratio, discovered_from, reason}` with
   `reason` = `name_mismatch` or `owned_by_other_company`) and **the board is not
   activated**: a new board is stored with `status='paused'`, which no collector polls,
   until a human resolves the item. Reruns reuse the open item. A later run that verifies
   the board activates it and resolves the item.
6. **Write `ats_boards`** with `vendor`, `identifier`, `discovered_from` (the URL of the page
   that carried the link, or the API URL that answered a guess), `confidence`, `status`,
   `last_verified_at = now`.

## Patterns

| Vendor | Shapes recognised | Identifier stored | Status |
| --- | --- | --- | --- |
| greenhouse | `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}`, `.../embed/job_board[/js]?for={token}` (iframe and script embeds), `boards-api.greenhouse.io/v1/boards/{token}` | token | active |
| greenhouse (short link) | `grnh.se/{code}`, resolved through its redirect (below) | the resolved token | active |
| lever | `jobs.lever.co/{site}`, `jobs.eu.lever.co/{site}` | `site`, or `eu:{site}` | active |
| ashby | `jobs.ashbyhq.com/{name}` (incl. `/embed`), `api.ashbyhq.com/posting-api/job-board/{name}` | name (percent-decoded) | active |
| smartrecruiters | `careers.smartrecruiters.com/{company}` | company | active |
| workday | `{tenant}.wd{1-5}.myworkdayjobs.com/[{locale}/]{site}` | `{tenant}.wd{n}/{site}` | unsupported |
| workable | `apply.workable.com/{sub}` (not `/j/`, `/api/`) | sub | unsupported |
| oracle | `{host}/hcmUI/CandidateExperience` | host | unsupported |

Hosts must start at a boundary, so look-alikes such as `notboards.greenhouse.io` do not
match.

`grnh.se/{code}` links are Greenhouse's per-posting short links: the code is an opaque hash,
not a board token. They rank after every other hit, and a chosen one is resolved with
**one GET of `https://grnh.se/{code}` with redirects not followed** (no retries, through the
run's rate budget). Only a `Location` that starts with `https://boards.greenhouse.io/{token}`
or `https://job-boards.greenhouse.io/{token}` resolves; the token is then verified like any
other page hit (confidence 0.95, `discovered_from` = the company page). Anything else (no
redirect, an error, a redirect to the company's own site such as `/careers?gh_jid=...`)
falls through to the slug guesses; the short-link code itself is never sent to review or
stored as an identifier.

Workday, Oracle and Workable are recorded with `status='unsupported'` and are never fetched
further or polled (`docs/decisions/0003-drop-workday-oracle-collectors.md`); they exist so
coverage reports say "we cannot see this one" instead of "no board".

## Confidence

| How found | `confidence` |
| --- | --- |
| Link, embed or redirect on the company's own page | 0.95 |
| Slug guess answered by a vendor API with at least one job | 0.60 |
| Nothing found (`vendor='none'` marker row) | 0.00 |

## Outcomes and coverage reporting

Each domain ends in exactly one outcome, logged as one JSON line on the `tell.discovery`
logger: `{"domain", "outcome", "vendor", "identifier", "confidence", "pages_checked"}`.

| Outcome | `ats_boards` row |
| --- | --- |
| `board` | supported vendor, `status='active'` |
| `unsupported` | Workday / Oracle / Workable, `status='unsupported'` |
| `review` | `status='paused'` (new board) or unchanged (existing board), plus an open `board_verify` item |
| `no_board` | `vendor='none'`, `identifier=<domain>`, `status='no_board'`, `discovered_from='checked:'` + the comma-joined `pages_checked` (e.g. `checked:robots_blocked:/careers,/jobs,/join,/company/careers,/`) |

When a later run finds a board, the domain's `none` marker row is deleted, so every company
has either a board row or a marker (the one exception: a board already owned by another
company, where only the open review item records the attempt). Coverage is therefore one query:

```sql
select status, count(distinct company_id) from ats_boards group by status;
```

Expectation (spec section 4): **60 to 75 percent of venture-backed US software companies
resolve to one of the documented vendors**; the rest are `unsupported`, `review` or
`no_board`, each with the pages checked. Phase 3's gate is at least 7 of 10 sample
companies resolved.

## Budget (spec section 11)

`tell discover batch` takes at most `TELL_DISCOVERY_BUDGET` (default **300**) new domains
per run: companies with no `ats_boards` row and no open `board_verify` item, oldest
`created_at` first. Boards due for re-verification follow, capped separately (default: the
same number). Every request of the run (robots, pages, vendor APIs, name lookups) shares one
token bucket at `TELL_DISCOVERY_RPS` (default **4** per second, burst 1). A domain costs
about 3 requests on a page hit (robots, one or two pages, one name lookup) and up to about
20 on a full miss (robots, five pages, twelve guesses), so 5,000 domains is roughly 15,000
requests: about an hour at 4 rps, spread over the first week at 300 domains a run.

Each domain runs in its own savepoint; a failure is logged, counted in `errors` and the run
continues. The run writes one `runs` row (`source='discovery'`): `fetched` = domains
processed, `inserted` = `board` + `unsupported`, `skipped` = `review` + `no_board`,
`notes` = the outcome counts.

## Re-verification

`reverify_due(board, now)` is true when:

- `last_verified_at` is missing or 30 days old or older; or
- the board has answered **404 twice in a row** and has been polled since discovery last
  looked at it: `status='dead'` and `last_verified_at < last_polled_at`. Discovery does not
  read a 404 counter: every ATS collector already sets `status='dead'` on the second
  consecutive 404 (Greenhouse, Ashby and SmartRecruiters infer the streak from
  `last_polled_at` > `last_verified_at`; Lever counts it in an open `review_queue` item),
  so `dead` is the marker discovery reads.

`paused` boards are never due (they wait for their review item). Re-verification reruns the
whole procedure for the company's domain: a dead board found and verified again becomes
`active`; a renamed board is written as a new row.

**Every outcome stamps the company's dead boards.** After any outcome for a company
(`board`, `unsupported`, `review`, `no_board`), discovery sets `last_verified_at = now` on
that company's `dead` boards and on its `vendor='none'` marker row, if one remains. A dead
board that discovery could not revive (the company moved to another board, or has none) is
therefore checked again only after 30 days, or sooner if a collector polls it again after
that stamp; before this rule a dead board was due on every batch forever, because no
outcome ever touched its row.

Note: the collectors also set `last_verified_at` on every successful poll, so for polled
boards the 30-day rule rarely fires; it matters for `unsupported` and `no_board` rows, which
are re-checked monthly.
