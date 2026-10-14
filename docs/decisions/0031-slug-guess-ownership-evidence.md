# 0031. Slug-guessed boards need ownership evidence; shared destinations keyed per company

- Status: accepted (proposed 2026-10-14; confirmed by Karan 2026-10-14)
- Date: 2026-10-14

## Context

SPEC section 4 step 3 guesses slugs from the domain label (`acme`, `acmeinc`, `acme-inc`)
against the four documented public APIs, and step 4 verifies ownership by name only: the
board's own company name must reach `token_set_ratio >= 85` against the domain label or the
company name. For a generic label the name check proves nothing: "Stream", "Axle", "Tilt"
and "Chicory" match `stream`, `axle`, `tilt` and `chicory` at 100 whatever company owns the
board.

- The 2026-10-13 hand audit (private, `tell-private/reports/discovery-audit-2026-10-14.md`)
  found 3 of 20 found boards belonged to another company, all of them guesses (3 of 9
  guesses, 0 of 11 page-linked boards). Its wider slug search found 6 more wrong boards.
- The 2026-10-14 recount found 2 of 15 wrong, again guesses (Greenhouse `chicory` is
  another Chicory).
- Checking every guessed board of the 2026-10-14 snapshot against its own public data
  (`docs/methods/discovery.md`, "Precision: ownership evidence for guesses") showed the
  problem is larger than both samples suggested: of 52 active guessed boards, 21 belong to
  another company by hand review, 2 are unclear, 29 are the company's.

A wrong board is polled as if it were the company's, so another company's hiring enters
that company's signals.

Separately, an `unsupported` destination shared by two companies (two companies linking
one Gem board) was written over: `ats_boards` is unique on `(vendor, identifier)`, so the
second company's discovery rewrote the first company's `discovered_from`, `confidence` and
`last_verified_at` and left the second company with no row at all. It was picked again by
every batch (the 2026-10-14 snapshot's "one company has no recorded outcome").

## Decision

**(a) A slug guess (confidence 0.60) is activated only with ownership evidence.** Besides
the name rule, the board's own public data must point at the company's registrable domain:
a URL on it whose registrable domain (eTLD+1, as discovery normalises domains) equals the
company's, or, in a Greenhouse board intro, the domain written out. Evidence per vendor,
read in this order until one source points at the domain:

| Vendor | Sources | Extra requests per guess |
| --- | --- | --- |
| Greenhouse | `GET /v1/boards/{token}` `content` (the name request); every job's `absolute_url` (the guess response; custom careers pages put `?gh_jid=` links on the company's domain); the hosted board `job-boards.greenhouse.io/{token}` links (its logo links the company site) | 0, or 1 when the API shows none |
| Lever | `jobs.lever.co/{site}` links (the name request): header logo link and "Home Page" footer link | 0 |
| Ashby | `jobs.ashbyhq.com/{name}` (the name request): `organization.publicWebsite` in the embedded app data and the page's links; then every job's `jobUrl` / `applyUrl` (the guess response) | 0 |
| SmartRecruiters | `careers.smartrecruiters.com/{id}` links ("Home Page" in the navigation) | 1 |

A name-only match goes to `review_queue` (`kind='board_verify'`, payload `{domain, vendor,
identifier, board_name, ratio, discovered_from, reason: 'guess_without_domain_evidence',
evidence, evidence_checked, evidence_failed, board_sites}`, where `board_sites` lists up to
5 other registrable domains the board points at, vendors' own left out) and the board is
stored, or set, `status='paused'` with `last_verified_at = now`. That includes a board of
the same company that an earlier run activated under the name-only rule. A board a person
re-activated after resolving such an item (resolved, not by discovery itself, and the board
`active` again) is kept on later runs, so re-verification does not pause it every 30 days.
Page-linked boards (0.95) keep the name rule alone.

**(b) `tell discover reverify-guesses [--limit N] [--rps R]`** re-checks existing `active`
guessed boards under (a), least recently verified first: kept boards get
`last_verified_at = now`; failures become `paused` with the review item; a check where a
request failed and nothing pointed at the domain changes nothing (retried next run). Batch
re-verification applies (a) too, but only to boards 30 days past their last verification
and at the cost of a full rediscovery; the command applies it now, at about 2 requests per
board.

**(c) A shared unsupported destination is keyed per company.** When `(vendor,
identifier)` already belongs to another company's row, the current company's row is written
as `identifier = '<identifier>#<domain>'` (same vendor, `status='unsupported'`). The other
company's row is never written; `_write_board` now refuses to write over another company's
row. Unsupported rows are never fetched, so the key is only a record; `coverage_note` reads
the vendor, not the identifier.

**(d) Request bounds.** The deeper scan's `MAX_DEEP_REQUESTS` (12 per domain) now counts the
`robots.txt` of every origin first reached in that step (ADR 0030 left it uncharged), and a
request past the cap is never sent. One JavaScript bundle fetch has a total wall-clock
deadline of 15 s (3 s per request phase; no read starts after 12 s) instead of 20 s checked
only between chunks.

## Consequences

- **Precision.** On the 2026-10-14 snapshot's 52 active guessed boards (live check,
  2026-10-14, 1 request per second): 24 kept, 28 to review. Of the 28, 21 are other
  companies' boards, 5 are the company's own (no link to the company's domain on the board,
  or a different domain such as `sage.care` for `sagecare.ai`), 2 unclear. All 11 known
  wrong guesses from the audit and the recount are rejected; 0 wrong boards were kept.
  Recorded evidence: `tests/fixtures/discovery/ownership_audit.json`.
- **Coverage moves down.** Running `reverify-guesses` on that snapshot would move 28 of 158
  supported boards to `paused` (15.8% to 13.0%); up to 7 of them may be confirmed by a
  person. Public `coverage_note` reads `paused:<vendor>:<identifier>` for them.
- **Cost.** A guess costs 0 extra requests on Lever and Ashby, 1 on SmartRecruiters and 0
  or 1 on Greenhouse. `reverify-guesses` costs about 2 requests per board (104 for the 52
  boards).
- **Review load.** Name-only guesses become review items. A person resolves one by
  activating the board (it then stays active) or leaving it paused.
- **ADR 0030's request count** of 42 per domain becomes 43 (the extra evidence request);
  `robots.txt` beyond the domain's own is now bounded inside the 12.
