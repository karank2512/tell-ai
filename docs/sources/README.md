# Data sources

One page per source: endpoint, rate limit, legal note, sample response, fixtures. Spec: `docs/SPEC.md` section 3. Every shipped source is a documented public endpoint or a public-domain government file. These notes are engineering summaries of what each provider publishes, not legal advice; where a note says "open item", a reviewed note is still owed.

## Index and legal notes

| Source | Page | Collector | Polled | Legal note (summary) |
| --- | --- | --- | --- | --- |
| Greenhouse Job Board API | [greenhouse.md](greenhouse.md) | `tell/collectors/greenhouse.py` | yes | Public data, read access needs no authentication; the only automated-access clause found binds job-seeker accounts, not the feed (SPEC 3.1). |
| Lever Postings API | [lever.md](lever.md) | `tell/collectors/lever.py` | yes | Docs say published postings are publicly viewable and may be scraped by third parties; site terms forbid scraping the app, which tell does not touch (SPEC 3.2). |
| Ashby Job Posting API | [ashby.md](ashby.md) | `tell/collectors/ashby.py` | yes | Documented public API, no authentication, same postings as `jobs.ashbyhq.com`; the SPEC gives no legal note, so a reviewed one is an open item. |
| SmartRecruiters Posting API | [smartrecruiters.md](smartrecruiters.md) | `tell/collectors/smartrecruiters.py` | yes | Documented public, unauthenticated API for published job ads; candidate and customer endpoints never touched; the SPEC row is missing, so a reviewed note is an open item. |
| SEC EDGAR (Form D, 8-K, S-1) | [edgar.md](edgar.md) | `tell/collectors/edgar.py`, `edgar_formd.py` | yes | US government work, public domain; fair-access policy followed (5 requests per second against a 10 limit, declared User-Agent). The Form D related-persons list is never stored. |
| Google News RSS and company feeds | [news.md](news.md) | `tell/collectors/news.py`, `news_classify.py` | yes | Google's feed is undocumented and its channel copyright says personal, non-commercial feed-reader use; only headline, URL, date and publisher are stored, never article text; whether the Google half ships enabled is an open maintainer decision. Company blog feeds are published for syndication. Leadership headlines are stored with the person's name redacted ([ADR 0016](../decisions/0016-news-headline-redaction.md)). |
| GitHub org repos and releases | [github.md](github.md) | `tell/collectors/github_org.py` | config-gated per company | Documented public REST API within published limits; org-level public data only (repo names, stars, release ids and tags), never user profiles or authors. |
| Company sites of Form D issuers | [issuer_sites.md](issuer_sites.md) | `tell/discovery/issuers.py` (`tell discover issuers`) | off until enabled (ADR 0035) | Homepage and legal pages only, `robots.txt` honoured, 1 request per second per host, User-Agent names the repo; only the domain, page path and matched legal-name span are stored, never page text; Form D `relatedPersonsList` never read. |
| YC public directory | [yc-directory.md](yc-directory.md) | `tell/discovery/` (`tell seed-yc`) | seed list only | Public company facts (name, website, batch, location), no personal data; the mirror's file is not redistributed. |

## Detected, never polled

Workday, Oracle and Workable career sites are recognised by ATS discovery (`docs/methods/discovery.md`) and recorded in `ats_boards` with `status = 'unsupported'`, so coverage reporting says "we cannot see this one" instead of implying there is nothing to see. No collector exists for them and nothing ever requests those hosts for postings ([ADR 0003](../decisions/0003-drop-workday-oracle-collectors.md)). They have no page here for that reason.

## Adding a source

A new source ships with a fixture, a schema-contract test, an idempotency test and a page in this folder (endpoint, rate limit, legal note, sample response), plus a row in the table above. See [CONTRIBUTING.md](../../CONTRIBUTING.md#writing-a-collector).
