# Method: ATS discovery (domain to job board)

Spec: `docs/SPEC.md` section 4 (procedure), section 5 (`ats_boards`, `review_queue`) and
section 11 (budget). Code: `tell/discovery/ats.py`. CLI: `tell discover domain --domain
acme.com [--name "Acme"]`, `tell discover batch [--limit N] [--rps R] [--concurrency C]
[--max-minutes M]` and `tell discover reverify-guesses [--limit N] [--rps R]`
(`tell/discovery/reverify.py`). Tests: `tests/discovery/test_ats.py`,
`tests/discovery/test_deep_scan.py`, `tests/discovery/test_ownership.py` and
`tests/discovery/test_batch.py` (all HTTP mocked with `respx`, hand-crafted pages and
bundles under `tests/fixtures/discovery/`, plus the recorded public evidence of audited
guessed boards in `ownership_audit.json`).

Nothing in the ATS APIs maps a domain to a board token, so discovery is its own job: given a
domain, find the board or record that none was found.

Where this procedure departs from the SPEC section 4 wording (the deeper scan after the
first 200, no slug guesses after a hiring channel, and the further vendors and YC Work at a
Startup recorded as `unsupported`) is recorded in
[ADR 0030](../decisions/0030-discovery-deeper-scan-and-unsupported-destinations.md); the
ownership evidence required of slug guesses, the per-company key of a shared unsupported
destination and the request bounds below in
[ADR 0031](../decisions/0031-slug-guess-ownership-evidence.md).

## Procedure

1. **Normalise** the domain to its registrable form (`https://www.Acme.com/x` ->
   `acme.com`) and find or create its `companies` row (`name` = the given name, else the
   domain).
2. **Fetch candidate pages** `https://{domain}/careers`, `/jobs`, `/join`,
   `/company/careers`, then the homepage `/`, in that order, **stopping at the first 200**.
   The scan uses that page only, even when it carries no board (as the spec says).
   - Redirects are followed by hand, **at most 2** per candidate; a third redirect abandons
     the candidate. A redirect whose `Location` is itself a board URL is a hit and is not
     fetched (common: `/careers` -> `jobs.lever.co/acme`, or `/careers` ->
     `ycombinator.com/companies/acme/jobs`). The same holds for a redirect to a hiring
     channel (LinkedIn jobs, Notion, Google Docs/Forms, see below): it is recorded and
     never fetched, so discovery never requests LinkedIn.
   - **robots.txt** is fetched once per origin through the same client and budget and read
     with `urllib.robotparser`. A disallowed candidate is skipped and recorded in
     `pages_checked` as `robots_blocked:/careers`; a redirect into a disallowed path stops
     there. Status rules follow RFC 9309: 2xx parsed; 401/403 disallow everything; other
     4xx mean no rules; 5xx, network errors and redirect loops mean "unreachable", treated
     as disallow-all.
   - Vendor APIs and vendor-hosted board pages (name lookups, guesses) are documented
     public endpoints and go through `tell.http.fetch` (429/5xx retries) without a robots
     check.
   - Company pages get one attempt each (no retries) with a **10-second** timeout (also for
     `robots.txt`; vendor APIs keep the client's 20 s): a flaky or dead site is a miss, not
     a reason to stall the run.
3. **Scan** the page: `<iframe src>`, `<script src>` and `<a href>` (parsed with
   `lxml.html`), then the raw HTML text (inline scripts, data attributes, JSON blobs; the
   JSON/JS slash escapes `\/` and `\u002F` are undone first), against the patterns below.
   Ranking: supported vendors, then unsupported ATS vendors, then listing sites (YC Work
   at a Startup), then `grnh.se` short links (see below). A page linking both a Workday
   site and a Lever board resolves to Lever; one linking its YC jobs page and an Ashby
   board resolves to Ashby. Within a class the first hit wins.
4. **Deeper scan**, only when the first answering candidate (step 2) named **no ATS**
   (nothing, only a YC listing, only a hiring channel, or only a `grnh.se` link). Added
   after the 2026-10-14 recall audit (below); it stops as soon as an ATS (supported or
   unsupported) is found, and every request in it counts against a hard cap of
   **12 requests per domain** (`MAX_DEEP_REQUESTS`; a redirect hop counts, and so does
   the `robots.txt` of every origin first reached in this step, such as `www.` or a page
   CDN; the hop limit shrinks when the cap is near, and a request past the cap is never
   sent):
   1. **Homepage**, unless it was the first answer (or a candidate already redirected to
      it). Recorded in `pages_checked` as `/`. The audit found a company whose
      `/careers` redirected to its YC jobs page while its Ashby board was linked only from
      the homepage (a slug guess had then picked another company's board).
   2. **Careers links, one hop.** Up to **2** (`MAX_LINKS`) same-site links (same
      registrable domain, so `careers.acme.com` counts) from the first page and the
      homepage whose anchor text or URL path says careers / jobs / join us / hiring /
      open roles / positions / vacancies / work with us (and a few non-English forms),
      not requested yet, in page order (`link:/path`). Links found on those pages are
      not followed. Off-site links are never fetched (known ATS hosts are already hits).
      The audit found a Comeet embed only on the position pages a careers page links to.
   3. **JavaScript bundles**, only when nothing at all (no board, no listing, no channel)
      was found: up to **5** (`MAX_SCRIPTS`) `<script src>` bundles of the careers pages
      (the first page if it was not the homepage, and the followed links), or of the
      homepage when there is no careers page, that are on the company's registrable
      domain or on a site-builder page CDN (`framerusercontent.com/sites/`). Third-party
      scripts (analytics, tag managers, CDNs of libraries) are never fetched. Ranked:
      careers/jobs-named bundles, then page and app bundles (`/pages/`, `/app/`,
      `index-`, `main-`, `_app`), then the rest, framework/polyfill/webpack/vendor
      bundles last. **2 MB in total** (`MAX_SCRIPT_BYTES`) is read per domain; a bundle
      is cut off at the remainder. One GET each, no redirects, no retries, and a **total
      wall-clock deadline of 15 s** per bundle (`SCRIPT_DEADLINE_S`): every request phase
      times out after 3 s (`SCRIPT_READ_TIMEOUT_S`) and no read starts after 12 s, so a
      host that drips bytes cannot hold a worker longer (DNS lookup excepted); what was
      read by then is scanned
      (`script:/path`). The audit found an Ashby API URL only in a careers page bundle.

   robots.txt is honoured for every page, link and bundle (`robots_blocked:link:/x`,
   `robots_blocked:script:/x`). **Hiring channels** are recorded, never fetched: a link to
   Notion (`*.notion.site`, `notion.so`), Google Forms (`docs.google.com/forms`,
   `forms.gle`), Google Docs/Sheets/Slides or LinkedIn jobs
   (`linkedin.com/company/{x}/jobs`, `linkedin.com/jobs/`) counts when it is on a careers
   page, or is a careers link (by its text or path) on the homepage, or is where a
   candidate page redirected. A homepage's LinkedIn company link or a "Join the
   waitlist" Google Form is not a channel.
5. **Slug guesses**, only when the scan found nothing (or its only hit was a `grnh.se`
   short link that did not resolve): from the domain label (`acme`),
   try `acme`, `acmeinc`, `acme-inc`, and for each slug Greenhouse, Lever, Ashby,
   SmartRecruiters in that order, with a small GET. A 200 with at least one job counts.
   **Skipped when a hiring channel was found and no board at all**: the company's careers
   link says where it hires, and on the audit's sample a guess could then only find
   another company's board. The slug list is unchanged (see "Recall audit" for why).
6. **Verify ownership** of a supported board: the board's own company name must reach
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
   the board activates it and resolves the item (marking it `resolved_by: discovery`).

   **Slug guesses also need ownership evidence** (ADR 0031). For a guessed board (0.60)
   the name is not enough: generic labels ("Stream", "Axle", "Tilt", "Chicory") match
   another company's board at 100. The board's own public data must point at the
   company's registrable domain: a URL on it whose registrable domain (eTLD+1) equals the
   company's, or, in a Greenhouse board intro, the domain written out as a host name
   (`acme.com`, `careers.acme.com`; not `acme.com.au` or `notacme.com`). Sources, read in
   this order and only until one points at the domain:

   | Vendor | Evidence that counts | Requests after a guess |
   | --- | --- | --- |
   | Greenhouse | (1) `content` of `GET /v1/boards/{token}` (the name request: links, URLs and the domain written out); (2) any job's `absolute_url` in the guess response `GET /v1/boards/{token}/jobs` (custom careers pages put `?gh_jid=` links on the company's domain); (3) any `<a href>` of the hosted board `job-boards.greenhouse.io/{token}` (its logo links the company site) | 0 extra when (1) or (2) shows the domain, else 1 (the hosted board) |
   | Lever | any `<a href>` of `jobs.lever.co/{site}` (the name request): the header logo link and the "Home Page" footer link | 0 extra |
   | Ashby | (1) `organization.publicWebsite` in the app data embedded in `jobs.ashbyhq.com/{name}` and that page's `<a href>` (the name request); (2) any job's `jobUrl` / `applyUrl` in the guess response | 0 extra |
   | SmartRecruiters | any `<a href>` of `careers.smartrecruiters.com/{id}` (the "Home Page" navigation link); the name comes from the guess response | 1 extra |

   A name-only match goes to review with `reason='guess_without_domain_evidence'` and
   the board is stored (or set, when an earlier run had activated it) `status='paused'`
   with `last_verified_at = now`. The payload adds `evidence` (the source that matched,
   `null` here), `evidence_checked`, `evidence_failed` (sources whose request failed) and
   `board_sites`: up to 5 other registrable domains the board points at (the vendors' own
   and page plumbing left out), which is usually the other company's site and lets a
   reviewer decide at a glance. Only domains are stored, never page text. A board a person
   re-activated after resolving such an item stays active on later runs (the item is
   resolved, not by discovery, and the board is `active` again). Page-linked boards
   (0.95) keep the name rule alone.
7. **Write `ats_boards`** with `vendor`, `identifier`, `discovered_from` (the URL of the page
   or bundle that carried the link, or the API URL that answered a guess), `confidence`,
   `status`, `last_verified_at = now`.

## Patterns

| Vendor | Shapes recognised | Identifier stored | Status |
| --- | --- | --- | --- |
| greenhouse | `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}`, `.../embed/job_board[/js]?for={token}` and `.../embed/job_app?for={token}` (iframe and script embeds), `boards-api.greenhouse.io/v1/boards/{token}` | token | active |
| greenhouse (short link) | `grnh.se/{code}`, resolved through its redirect (below) | the resolved token | active |
| lever | `jobs.lever.co/{site}`, `jobs.eu.lever.co/{site}`, `api[.eu].lever.co/v0/postings/{site}` | `site`, or `eu:{site}` | active |
| ashby | `jobs.ashbyhq.com/{name}` (incl. `/embed`), `api.ashbyhq.com/posting-api/job-board/{name}` | name (percent-decoded) | active |
| smartrecruiters | `careers.smartrecruiters.com/{company}`, `jobs.smartrecruiters.com/{company}/...` (not `oneclick-ui`), `api.smartrecruiters.com/v1/companies/{company}` | company | active |
| workday | `{tenant}.wd{1-5}.myworkdayjobs.com/[{locale}/]{site}` | `{tenant}.wd{n}/{site}` | unsupported |
| workable | `apply.workable.com/{sub}` (not `/j/`, `/api/`) | sub | unsupported |
| oracle | `{host}/hcmUI/CandidateExperience` | host | unsupported |
| dover | `app.dover.(com\|io)/(jobs\|apply)/{slug}` | slug | unsupported |
| gem | `jobs.gem.com/{slug}` | slug | unsupported |
| comeet | `comeet.(com\|co)/jobs/{slug}/...`; the careers-API embed `COMEET.init({... "company-uid": "{uid}" ...})` | slug, or the published company UID | unsupported |
| recruitee, personio, teamtailor, breezy, jazzhr, pinpoint, homerun, zohorecruit | `{sub}.recruitee.com`, `{sub}.jobs.personio.(de\|com)`, `{sub}.teamtailor.com`, `{sub}.breezy.hr`, `{sub}.applytojob.com`, `{sub}.pinpointhq.com`, `{sub}.homerun.co`, `{sub}.zohorecruit.(com\|eu\|in)` (not `www`, `app`, `api`, `help`, ... subdomains) | sub | unsupported |
| bamboohr | `{sub}.bamboohr.com/(careers\|jobs)` | sub | unsupported |
| rippling, jobvite, polymer, join | `ats.rippling.com/{slug}`, `jobs.jobvite.com/{slug}`, `jobs.polymer.co/{slug}`, `join.com/companies/{slug}` | slug | unsupported |
| icims | `(careers\|jobs)-{sub}.icims.com` | sub | unsupported |
| wellfound | `(wellfound.com\|angel.co)/company/{slug}/jobs` | slug | unsupported |
| yc_waas (listing site) | `ycombinator.com/companies/{slug}/jobs` (not the bare profile), `workatastartup.com/companies/{slug}` | YC slug | unsupported |

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
coverage reports say "we cannot see this one" instead of "no board". The further vendors
(Dover to Wellfound) and YC Work at a Startup (`yc_waas`) are recorded the same way, from
the 2026-10-14 recall audit, so coverage can say where a company hires. None of them is
fetched or polled; a collector for any of them needs an ADR (SPEC section 17). YC Work at
a Startup is a listing site, not an ATS: it ranks after every ATS and does not end the
deeper scan, because companies with a real ATS often link their YC page too.

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
| `unsupported` | an unsupported ATS (Workday, Oracle, Workable, Dover, Gem, Comeet, ...) or `yc_waas`, `status='unsupported'`; when that `(vendor, identifier)` is already another company's row (two companies linking one Gem board), this company's row is `identifier='<identifier>#<domain>'`, so neither outcome overwrites the other |
| `review` | `status='paused'` (new board, or a rejected guess, also when an earlier run had activated it) or unchanged (an existing page-linked board), plus an open `board_verify` item |
| `no_board` | `vendor='none'`, `identifier=<domain>`, `status='no_board'`, `discovered_from='checked:'` + the comma-joined `pages_checked` (e.g. `checked:robots_blocked:/careers,/jobs,/join,/company/careers,/` or `checked:/careers,/,link:/about/careers`), then `;via:` + the hiring channels seen, if any (e.g. `checked:/careers,/;via:linkedin`) |

When a later run finds a board, the domain's `none` marker row is deleted, so every company
has either a board row or a marker (the one exception: a board already owned by another
company, where only the open review item records the attempt). Coverage is therefore one query:

```sql
select status, count(distinct company_id) from ats_boards group by status;
```

and by destination, so the report can read "13.0% supported board, N% hiring only via YC,
N% via another ATS or channel, N% no public hiring signal":

```sql
select case
         when status = 'active' then 'supported:' || vendor
         when status = 'unsupported' then 'unsupported:' || vendor
         when status = 'no_board' and discovered_from like '%;via:%'
           then 'channel:' || split_part(discovered_from, ';via:', 2)
         else status
       end as destination,
       count(distinct company_id)
from ats_boards
group by 1
order by 2 desc;
```

The log line also carries `"channels": [...]` for every outcome.

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
18 on a full miss (robots, five pages, twelve guesses), so a miss needs about 4.5 s of the
4 rps budget on its own.

**Requests per domain, bounded** (after the 2026-10-14 deeper scan):

| Stage | Requests at most |
| --- | --- |
| `robots.txt` of the domain | 1 (once per origin per run, up to 6 with its own redirects; also once for any other origin a candidate's redirect enters, such as `www.`; in the deeper scan it is counted in the 12) |
| Candidate pages | 15 (5 candidates, each up to 3 with 2 redirect hops; stops at the first answer) |
| Deeper scan (homepage, 2 careers links, 5 bundles, 2 MB) | 12 (`MAX_DEEP_REQUESTS`, hard cap including redirect hops and the `robots.txt` of new origins) |
| `grnh.se` short link | 1 |
| Slug guesses | 12 (3 slugs x 4 vendors; skipped after a page hit, an unsupported ATS, a listing or a hiring channel) |
| Board name lookup and ownership evidence | 2 (the name request, plus for a guess the Greenhouse hosted board or the SmartRecruiters careers page; plus `tell.http` retries on 429/5xx from vendor APIs) |
| **Total** | **43**, plus `robots.txt` of origins a candidate's redirect enters and vendor-API retries |

Since ADR 0031 the deeper scan charges the `robots.txt` of every origin it enters to its
12. Outside it, each robots load follows up to 5 redirects (RFC 9309), so it is at most 6
requests, and in the strict worst case each of the 15 candidate requests enters a new
origin, which bounds a domain at 43 + 6 x 16 = 139 requests (the domain's own robots
counted once more; it was 210 before). In practice the extra origin is `www.` (1 to 6
requests). Every one of them draws from the shared 4 rps bucket, so the rate limit holds
whatever the count.

Typical costs: a board on `/careers` is about 3 requests (unchanged); a domain whose
candidates all fail is still about 18 (no page answered, so no deeper scan); a domain whose
`/careers` answers with nothing on it costs about 3 to 9 more than before for the deeper
scan, and 12 fewer when that scan finds an unsupported ATS, a YC listing or a hiring
channel (no guesses). On the 60 audit companies replayed offline (below) the estimated
requests per domain, pages plus guesses, went from 16.3 to 15.5.

**Concurrency.** Up to `TELL_DISCOVERY_CONCURRENCY` (default **6**) domains are in flight
at once on worker threads, and every one of their requests still draws from the one bucket,
so the request rate never exceeds `TELL_DISCOVERY_RPS` (SPEC section 11); what overlaps is
the waiting on slow or dead company hosts, which is what made the serial pass slow. Workers
only do HTTP. The calling thread is the single database writer: it records each domain as
soon as its worker finishes. `robots.txt` is cached per origin behind a per-origin lock, so
it is fetched once even when two workers reach the same origin together.

**Commit per domain.** Each domain is recorded in its own savepoint (a failure is logged,
counted in `errors` and the run continues) and then committed (the CLI passes
`Session.commit` as `discover_batch(commit=...)`), together with the run row's running
counters. A job killed by its timeout keeps every domain finished before the kill; the
`runs` row stays `running` with the counts so far, and the next run selects exactly the
domains that were not reached. No transaction is held open across the network calls.

**Wall-clock budget.** No new domain starts once `TELL_DISCOVERY_MAX_MINUTES` (default
**22**) have passed; in-flight domains finish and the rest are deferred to the next run.

The run writes one `runs` row (`source='discovery'`): `fetched` = domains processed,
`inserted` = `board` + `unsupported`, `skipped` = `review` + `no_board`, `notes` = the
outcome counts plus `new_domains`, `reverified`, `concurrency`, `budget_exhausted` and
`deferred`; `finished_at` is the real finish time (until 2026-10-14 it repeated
`started_at`). The CLI prints the same summary with `elapsed_s` and `seconds_per_domain`.

### Measured throughput

80 random active YC companies outside the 1,000-company snapshot sample (population as in
`docs/methods/snapshot-2026-10-13.md`, drawn with `random.Random(42)`), each run in a
throwaway database at 4 rps, 2026-10-14:

| Setting | Wall time | Seconds per domain | Outcomes |
| --- | --- | --- | --- |
| Serial (`--concurrency 1`), same 80 domains | 484.9 s | 6.06 | 14 board, 66 no_board, 0 errors |
| Six workers (default), before the deeper scan | 357.5 s | 4.47 | 14 board, 66 no_board, 0 errors |
| Six workers (default), with the deeper scan (same 80, 05:50 UTC) | 379.4 s | **4.74** | 13 board, 9 unsupported, 58 no_board (2 with a `;via:` channel), 0 errors |
| Serial snapshot runs, 2026-10-13 (1,000 domains in chunks of 150, reference) | about 880 s a chunk | 5.8 | |

Six workers cut the time per domain by 26 percent (6.06 s to 4.47 s, 1.36 times the throughput) with identical outcomes. The pass is now bound by the 4 rps budget itself (a full miss needs about 18
requests), not by slow hosts. The deeper scan, re-measured on the same 80 domains in a
throwaway database, costs 6 percent more (4.47 s to 4.74 s a domain): 40 of the 80 domains
ran it (`link:` or `script:` in `pages_checked`), and the 9 domains it records as
`unsupported` (Dover, Gem, BambooHR, Breezy, JazzHR, Personio, YC Work at a Startup 3)
skip the 12 slug guesses. One board fewer than the earlier run (13 vs 14) is either live-site
variance between the two runs 47 minutes apart or a guess now skipped after an
`unsupported` or channel hit; the earlier run's database was not kept, so this cannot tell
which. `ingest.yml` therefore keeps `--limit 250` (about 20 minutes at 4.74 s) under
the 22-minute budget and the 30-minute job timeout; a slower day defers the remainder.

## Recall audit (2026-10-14) and what changed

A private audit (`tell-private/reports/discovery-audit-2026-10-14.md`) checked 40 random
`no_board` companies and 20 found boards of the 2026-10-13 snapshot (1,000 YC companies:
161 boards, 820 `no_board`) by hand. Findings that shaped the code:

- **No supported board was missed** in the 40 (0/40, Wilson 95% upper bound 8.8%). The gap
  to the spec's 60-75% is the population (young, non-US, many hiring through YC Work at a
  Startup or other hosts), not discovery.
- **Where the `no_board` companies hire:** 9/40 through an unsupported ATS or channel
  (Notion 2, LinkedIn 2, Google Docs/Forms 2, Dover 1, Gem 1, Comeet 1), 6/40 through YC
  Work at a Startup only, 17/40 show no public hiring signal. Recording these is what
  the new vendors, `yc_waas` and the hiring channels do.
- **Wider slug guessing has negative value:** 27 slug variants per company found 0 real
  boards and 6 boards of other companies, so the slug list was **not** widened.
- **Guessed boards are the precision problem** (3 of 9 guessed boards in the sample
  belonged to another company; 0 of 11 page-linked ones). The deeper scan turned some
  guesses into page hits (below); ADR 0031 then added the ownership-evidence rule
  ("Precision: ownership evidence for guesses").

**Offline replay.** The audit's saved evidence (redirect chains, page bodies, and the
bundle findings) was replayed through the old and the new page scan without any network
(pages the audit never fetched answer 404, so the replay undercounts the new scan):

| 40 `no_board` companies | Old scan | New scan |
| --- | --- | --- |
| Supported board found | 0 | 0 (none exists, per the audit) |
| Unsupported ATS recorded | 0 | 3 (Dover, Gem, Comeet) |
| YC Work at a Startup recorded | 0 | 4 |
| Hiring channel recorded (`via:`) | 0 | 4 (LinkedIn 2, Notion 1, Google Forms 1) |
| Still `no_board` with nothing recorded | 40 | 29 |

Of the audit's class (b) (unsupported ATS or channel, 9) the new scan records 7; one of the
other two is a Notion link only on the 404 page (not scanned), and the audit's Google Docs
evidence for the other turned out to be a consent-manager vendor list, not a job link. Of
class (c) (YC only, 6) it records 3; the other 3 never link their YC page (two have no
careers link, one points at an on-page anchor).

Among the 20 found boards, two that discovery had only guessed (0.60) are now found on the
company's own pages at 0.95: one through the Ashby API URL in its careers page bundle, one
through an inline `api.lever.co/v0/postings/...` call on `/careers` (the new `lever_api`
shape). The audit's wrong board caused by a YC redirect hiding the homepage's Ashby link
is fixed by the homepage step (`test_yc_redirect_does_not_hide_the_homepage_board`); the
other two wrong guessed boards in the sample have no careers page at all, so only a
stricter ownership rule would catch them. Class (a) misses: none existed, so none could be
recovered.

## Precision: ownership evidence for guesses (2026-10-14)

The rule of step 6 was checked live on 2026-10-14 (1 request per second overall, the
discovery User-Agent, the same `ownership_evidence` code `reverify-guesses` runs, no
database writes) against every guessed board of the 2026-10-14 snapshot
(`tell_snapshot_20261014`: 52 active, 2 already paused) and the 8 further wrong guesses
the audit's wider slug search had found. Rejected boards were then judged by hand from the
company's homepage and the board's job titles and descriptions.

| 52 active guessed boards | Count |
| --- | ---: |
| Kept: the board points at the company's domain | 24 |
| - by source: Ashby app data 9, SmartRecruiters careers page 7, Greenhouse hosted board 3, Lever hosted page 3, Greenhouse intro 1, Greenhouse job URL 1 | |
| Sent to review (`guess_without_domain_evidence`; all 28 passed the name rule at 100) | 28 |
| - another company's board, by hand review | 21 |
| - the company's own board (no link to its domain, or another domain such as `sage.care` for `sagecare.ai`) | 5 |
| - unclear | 2 |

- **Wrong guesses rejected: 21 of 21** in the snapshot, plus **11 of 11** known wrong
  guesses from the audit and the recount (`stream`, `tilt`, `axle`, `chicory`, `earth`,
  `stacker`, `seer`, `spire`, `symphony` on Greenhouse and SmartRecruiters,
  `palettelabs`), the 3 snapshot ones among them. **Wrong guesses kept: 0.**
- **Correct guesses kept: 24 of 29 to 31** (77% to 83%, counting the 2 unclear either
  way). The 5 to 7 others wait in review.
- The samples underestimated the problem: about 21 to 23 of the 52 active guessed boards
  (40% to 44%) belong to another company, against 3 of 9 in the 2026-10-13 audit and 2 of
  15 in the recount. On the snapshot, supported-board coverage would move from 15.8%
  (158) to 13.0% (130) once `reverify-guesses` runs, plus up to 7 boards a reviewer may
  confirm.
- **Cost.** 104 requests for the 52 boards in `reverify-guesses` mode (no guess response
  to reuse: Greenhouse 48 for 17 boards, Ashby 31 for 20, SmartRecruiters 20 for 10,
  Lever 5 for 5). In discovery, after a guess, the evidence costs 0 extra requests on
  Lever and Ashby, 1 on SmartRecruiters and 0 or 1 on Greenhouse (15 of the 17 Greenhouse
  boards here needed the hosted board).
- `tests/fixtures/discovery/ownership_audit.json` keeps the minimal public evidence of
  the 11 wrong guesses and 8 correct ones (page title or board name, one link per site the
  board links, one job URL per host; no page text, no people), and
  `tests/discovery/test_ownership.py` shows the rule rejects every wrong one and keeps
  every correct one.

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
`active`; a renamed board is written as a new row; a board found again only by a slug guess
must show ownership evidence (step 6) or it is set `paused`.

**`tell discover reverify-guesses [--limit N] [--rps R]`** applies the evidence rule to the
existing `active` guessed boards (0.60, supported vendor) without a full rediscovery,
least recently verified first, at `TELL_DISCOVERY_RPS` unless `--rps` is given: about 2
requests per board (Greenhouse: board API, job list, hosted board, stopping at the first
evidence; Lever: hosted page; Ashby: hosted page, then the posting API; SmartRecruiters:
postings for the name, then the careers page).

| Result | What changes |
| --- | --- |
| `kept` | evidence found and the name still matches (or a person re-activated the board after its review item): `last_verified_at = now`; open discovery items about the board are resolved |
| `paused` | `guess_without_domain_evidence` or `name_mismatch`: `status='paused'`, `last_verified_at = now`, one open `board_verify` item (reused on reruns) |
| `error` | a request failed and nothing pointed at the domain, or the name could not be fetched: nothing changes; the next run tries again |

It commits after every board and prints one JSON line per board plus a summary
(`{"checked", "outcomes": {"kept", "paused", "error"}}`). Rerunning it is idempotent:
paused boards are no longer selected and kept boards only get a newer stamp.

**Every outcome stamps the company's dead boards.** After any outcome for a company
(`board`, `unsupported`, `review`, `no_board`), discovery sets `last_verified_at = now` on
that company's `dead` boards and on its `vendor='none'` marker row, if one remains. A dead
board that discovery could not revive (the company moved to another board, or has none) is
therefore checked again only after 30 days, or sooner if a collector polls it again after
that stamp; before this rule a dead board was due on every batch forever, because no
outcome ever touched its row.

Note: the collectors never write `last_verified_at` (discovery owns it), so every board,
polled or not, is re-checked by discovery 30 days after its last verification.
