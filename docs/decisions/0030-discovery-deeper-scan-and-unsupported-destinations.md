# 0030. Discovery: deeper scan past the first 200, no slug guesses after a hiring channel, more `unsupported` destinations

- Status: accepted
- Date: 2026-10-14

## Context

SPEC section 4 fixes the discovery procedure: fetch `/careers`, `/jobs`, `/join`,
`/company/careers` and the homepage "stopping at the first 200" (step 1), scan that page
for the listed board patterns (step 2), and "if nothing is found" try the slug guesses
(step 3). Unmatched domains are logged as `no_board`.

A recall audit of the 2026-10-13 snapshot (1,000 YC companies: 161 boards, 820
`no_board`) checked 40 random `no_board` companies and 20 found boards by hand. The full
report is private (`tell-private/reports/discovery-audit-2026-10-14.md`); the numbers
that matter are in `docs/methods/discovery.md` ("Recall audit"):

- 0 of the 40 had a supported board that discovery missed (Wilson 95% upper bound 8.8%).
- 9 of the 40 hire through an unsupported ATS or a hiring channel (Notion 2, LinkedIn 2,
  Google Docs/Forms 2, Dover 1, Gem 1, Comeet 1) and 6 only through YC Work at a Startup.
  All 15 were reported as plain `no_board`, as if they showed no hiring at all.
- Some boards sit one step past the first 200: one company's `/careers` redirected to its
  YC jobs page while its Ashby board was linked only from the homepage (a slug guess then
  picked another company's board); one Comeet embed was only on position pages a careers
  page links to; one Ashby API URL was only in a careers page's JavaScript bundle.
- Guessed boards are the precision problem: 3 of 9 guessed boards in the sample belonged to
  another company (0 of 11 page-linked ones). Trying 27 slug variants per company found 0
  real boards and 6 boards of other companies.

## Decision

Three deviations from the SPEC section 4 wording. Ownership verification (step 4), the
slug list, confidences and the 4 rps budget are unchanged.

**(a) Deeper scan past the first 200 (step 1).** When the first answering candidate names
no ATS (nothing, only a YC listing, only a hiring channel, or only a `grnh.se` link),
discovery also reads the homepage (if it was not the first answer), up to 2 same-site
careers links (one hop) and, if nothing at all was found, up to 5 same-site JavaScript
bundles (2 MB in total, 20 s wall-clock per bundle). The scan stops at the first ATS hit
and is capped at 12 requests per domain (`MAX_DEEP_REQUESTS`, redirect hops included).
robots.txt is honoured for every page, link and bundle; third-party scripts and off-site
links are never fetched. `robots.txt` of an origin first reached in this step is not
charged to the 12 (see Consequences).

**(b) No slug guesses after a hiring channel (step 3).** A careers link or redirect to
Notion, Google Docs/Forms or LinkedIn jobs is recorded on the `no_board` marker
(`discovered_from = 'checked:...;via:<channel>'`) and is never fetched. When such a channel
was found and no board at all, the slug guesses are skipped: the company's own page says
where it hires, and on the audit sample a guess could then only find another company's
board. Guesses still run when the scan found nothing.

**(c) More destinations recorded as `unsupported`.** Besides Workday, Oracle and Workable
(ADR 0003), discovery recognises 18 more ATS vendors (Dover, Gem, Comeet, Recruitee,
Personio, Teamtailor, BambooHR, Rippling, Breezy, Wellfound, Jobvite, JazzHR, Pinpoint,
iCIMS, Homerun, JOIN, Zoho Recruit, Polymer) and YC Work at a Startup (`yc_waas`, a
listing site that ranks after every ATS and does not end the deeper scan). They are
written with `status='unsupported'` like Workday, where the same domain used to end
`no_board`, and are never fetched further or polled. A collector for any of them needs its
own ADR (SPEC section 17). The values are listed in `tell/models.py` (`ATS_VENDORS`).

## Consequences

- **Public output changes.** `coverage_note` in `tell/publish/json_api.py` reads
  `unsupported:<vendor>` (for example `unsupported:yc_waas`, `unsupported:dover`) for
  companies that read `no_board` before, and the coverage numbers move from `no_board` to
  `unsupported` accordingly. On the audit's 40 `no_board` companies, replayed offline, 7
  move to `unsupported` (3 ATS, 4 YC) and 4 more stay `no_board` with a `;via:` channel
  recorded (channels do not change `coverage_note`). The supported-board share, the
  number SPEC section 4 sets at 60-75%, does not change: no supported board was missed.
- **Recall for guessed boards.** Two of the 20 audited boards that discovery had only
  guessed (0.60) are now found on the company's own pages (0.95), and the YC-redirect case
  above now finds the right board.
- **Request cost.** Per domain at most 42 requests (was 30) plus `robots.txt` of every
  further origin a redirect, link or bundle enters. That load is up to 6 requests (5
  redirects, RFC 9309) per origin and is not charged to `MAX_DEEP_REQUESTS`, so the strict
  worst case is higher than 42; in practice it is the `www.` host and at most one careers
  subdomain. Every request, robots included, still draws from the one 4 rps bucket, so the
  rate limit of SPEC section 11 holds. A domain whose `/careers` answers with nothing on
  it now costs about 3 to 9 more requests, and 12 fewer when the scan finds an unsupported
  ATS, a listing or a channel. Measured live on the same 80 YC domains (six workers,
  4 rps), the deeper scan raised the time per domain from 4.47 s to 4.74 s; 250 domains
  (`ingest.yml --limit 250`) take about 20 minutes, inside the 22-minute wall-clock budget
  (`docs/methods/discovery.md`, "Measured throughput").
- **Precision of guesses is still open.** Two wrong guessed boards in the sample have no
  careers page at all; only a stricter ownership rule for guesses would catch them. That
  changes SPEC section 4 step 4 (a business rule) and waits for Karan's decision.
