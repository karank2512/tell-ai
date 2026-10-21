# 0044. Public site: a neutral Form D digest and an opt-out

- Status: proposed
- Date: 2026-10-20

## Context

- **ADR 0042 published "Rounds filed, not announced"** (PR #14, merged 2026-10-19): a weekly
  page, a section of the weekly edition and `api/v0/rounds-not-announced.json` listing, company
  by company, the original Form D filings of the last two weeks with no funding headline in
  tell's news data yet.
- **The framing, not the data, was the problem.** Form D filings are public SEC records, and
  listing them is normal. Listing them as "not announced" singles out the one thing each founder
  chose: not to announce yet. The companies on that list are the seed-stage founders tell sells
  to (many seeded from the YC directory, ADR 0009).
- **The founder decided (2026-10-20):** the public page becomes a neutral weekly digest of Form
  D filings; the "not announced" framing leaves the public site; any company can opt out of the
  public site entirely. The sales value of "filed, not announced" stays in the private outputs
  (per-watchlist Slack nudges and the CSV), which do not change.
- **The SPEC has neither.** SPEC section 9 describes no Form D digest (ADR 0042 added the list)
  and no opt-out: it publishes an account page and JSON for every scored company within the
  size budget, and the weekly page's top 25 lists cover "the whole monitored set".
  `docs/SPEC.md` is read-only (CLAUDE.md), so the deviation is recorded here.

## Decision

1. **The public list becomes "Form D filings", a neutral weekly digest.** This supersedes ADR
   0042 for the public outputs; its cohort rules stand.
   - Page `form-d-filings.html`, JSON `api/v0/form-d-filings.json` (`FILINGS_SCHEMA` in
     `tell/publish/json_api.py`, rows under `filings`) and the weekly edition's section
     `<h2 id="form-d-filings">Form D filings</h2>`. They replace `rounds-not-announced.html`
     and `api/v0/rounds-not-announced.json`, which were one day old and had no consumer but a
     README link (updated).
   - Kept from ADR 0042: the window (the 14 days ending with the edition's week); the cohort
     (original filings, no amendments, no pooled funds, a link the speed-vs-press cohort
     trusts, a company with a published account page, `companies.source` not `manual`); the
     columns (company linked to its account page, domain, filing date, offering and sold
     amounts as the payload states them, the sec.gov receipt); newest first; one method
     sentence, with no percentage.
   - Removed: the press join. Every qualifying filing appears whether or not a funding headline
     exists, and nothing on the page or in the JSON says whether press was found. The schema's
     objects are closed (`additionalProperties: false`), and the tests fail if the page, the
     weekly section or the JSON says "press", "news", "headline", "announced" or "stealth".
     `NEWS_FUNDING`, `PRESS_LOOKBACK_DAYS` and the `_press` helper are deleted; nothing else
     called them. `as_of` stays: the day the list was read.
   - The retired output leaves the site on the first publish. Every write of the digest
     deletes `rounds-not-announced.html` and `api/v0/rounds-not-announced.json`; the JSON
     would otherwise stop the publish, because `validate_site_tree` rejects a file it has no
     schema for. An archived weekly page that still carries the old section loses it, along
     with its JSON-link clause (the archive step in 4). Only `weekly/2026-W42.html` can carry
     it, and only if this lands after that edition is archived on Monday 2026-10-26.
2. **What stays private.** The "filed, not announced" read keeps its value where only the
   customer who asked sees it: the per-watchlist Slack nudges, the HubSpot push and the CSV.
   None of them changes here. The speed-vs-press report keeps its aggregate press numbers.
3. **The opt-out mechanism.**
   - **Request:** a public GitHub issue on `karank2512/tell-ai` titled `Opt out: <domain>`,
     from `.github/ISSUE_TEMPLATE/opt-out.md`. It has two fields: the domain, and anything that
     shows the request comes from the company. No email address appears anywhere, because none
     is public. Every account page and the digest page carry the line "A company that would
     rather not appear on this site can ask to be removed: open a GitHub issue on
     karank2512/tell-ai titled 'Opt out: \<domain>'. The next publish removes it." Its link
     opens the template, with the title prefilled with the domain on an account page.
   - **Check:** the operator confirms the request comes from the company before acting. tell
     verifies nothing itself.
   - **Record:** `tell publish opt-out add DOMAIN` sets `companies.public_opt_out = true` and
     `companies.public_opted_out_at` (migration 0014: boolean not null default false,
     timestamptz null). `remove DOMAIN` clears both. `list` prints the opted-out companies.
     Each prints one JSON line. `add` and `remove` are idempotent (a second `add` keeps the
     first date) and exit 1 for a domain no company has.
   - **No `--note`.** The only place the codebase keeps free text about a command is
     `runs.notes`, and `runs` is the pipeline's job log. It feeds the health page, the success
     rates and the dead-man check. No maintenance command records a run (`tell resolve
     review-import`, `relink-formd`, `tell api`), and an opt-out run would have to join
     `NON_PIPELINE_SOURCES` to stay out of the success rate. A free-text note would also be the
     one place a requester's name or email could reach the database (CLAUDE.md: no PII). The
     issue is the record of who asked, why, and how it was checked.
   - **A domain tell does not know:** `add` refuses it. Nothing about the domain is published,
     and recording the opt-out would need a `companies` row, which the pipeline would then
     start to monitor. If tell adds the company later, it is not opted out. The operator either
     keeps the issue open until then or answers that there is nothing to remove.
4. **What the opt-out does,** from the next publish on (the nightly `tell publish` in
   `ingest.yml`; `tell weekly` on Mondays):
   - No account page and no `api/v0/companies/{domain}.json`. `published_company_ids` leaves the
     company out, so `render_site` deletes the files earlier publishes wrote. It does this with
     the clean-up it already had (`_remove_others`, which deletes the files of companies that
     leave the 180-day size budget), so no new deletion was needed for these files.
   - No row in the weekly top lists (`top_hiring` and `top_funding`: the weekly page,
     `api/v0/weekly/latest.json`, the index excerpt), in the Form D filings, among the index's
     top movers, or in a public watchlist's `api/v0/watchlists/{slug}.json`. A watchlist's heat
     is then a percentile among the members shown.
   - **Archived weekly pages** (`weekly/{earlier week}.html`, never rendered again) lose the
     rows that name the company. `scrub_archive` in `tell/publish/site.py` removes each `<li>`
     or `<tr>` whose muted cell holds an opted-out domain, keeps the rest of the page (heading
     counts such as "Top 25" are left as printed), and leaves a page with nothing to remove
     untouched. This is the one deletion this ADR adds. Without it the archive would keep
     naming the company, and the footer's "The next publish removes it" would be false.
5. **What the opt-out does not affect.**
   - **Private outputs:** the Slack nudges and digests, the HubSpot push (by command or with the
     nudge) and the CSV export read neither column. `tests/publish/test_opt_out.py` pins that
     their output is the same before and after an opt-out, and that the nudge is still sent.
     Their "Account page" link then leads to a page that no longer exists, as it already does
     for a company outside the size budget. Changing that is left to a separate decision.
   - **The keyed v1 API** (`tell/api`, coded, not deployed): it serves key holders, not the
     public site.
   - **Counts that name no company.** The weekly coverage numbers (`companies_monitored`,
     `boards_polled`, `filings_read`), the run-health page and `api/v0/health.json` (fetched,
     inserted, freshness), and the methods reports in `tell-data/site/methods/` (speed vs
     press, velocity lead, noise, operations) still count opted-out companies. tell still reads
     their public sources for the private outputs, and these numbers describe what tell reads.
   - **Monitoring:** collection, resolution and scoring go on as before.
   - **The `tell-data` system of record:** `events/*.jsonl` (append-only, SPEC section 9; each
     line's `raw_ref` is its address) and the board snapshots keep the company's events.
     Rewriting the append-only log is out of scope. The git history of `tell-data` keeps every
     page as it was published before the opt-out.
6. **SPEC deviation.** SPEC section 9 lists the site's pages and the weekly page's contents and
   describes neither the Form D digest nor an opt-out. With this ADR the site also publishes
   `form-d-filings.html` and `api/v0/form-d-filings.json`. An opted-out company gets no account
   page or JSON although the size budget would keep it. The weekly top 25 lists skip it,
   although SPEC says "the whole monitored set". Archived weekly pages are edited after
   publication to remove it.

## Consequences

- The digest is longer than the old list (no press filter) and steadier: a filing stays on it
  for two editions, and a row changes only when a link, an account page or an opt-out changes.
- The opt-out is manual (issue, check, command). It takes effect on the next nightly publish.
  Undoing it brings back the company's pages and rows from then on, but not the archived rows
  already removed.
- Rewriting archived pages is new. They were never touched after publication before, and now a
  page is re-serialized by lxml when a row is removed: the content is the same, but character
  references may be re-encoded.
- ADR 0042's status line reads "superseded by 0044 for the public outputs". Its cohort rules and
  its tests' link cases carry over to `tests/publish/test_form_d_filings.py`.
- The weekly post drafts (`docs/launch/weekly-filings-post.md`) follow the same line: company
  names stay on the page, never in a post, and no public copy says "not announced" or "stealth".
