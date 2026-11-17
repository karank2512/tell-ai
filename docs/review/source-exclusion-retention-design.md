# Source exclusion and retention: candidate design (offline)

17 November 2026. Base: `origin/main` 6890a10 (PR 29 merged; this worktree's tree is identical).

Status: **REVIEW CANDIDATE, NOT WIRED, NOT SAFE TO RESUME.** Ingest and weekly stay
`disabled_manually`. This candidate changes no workflow, reads no production data, fetches
nothing and deletes nothing. It adds one pure module that nothing imports, its synthetic
tests and this document. It does not replace `docs/review/source-safety-review-plan.md` (PR 29).
It narrows that plan's "shared eligibility boundary" into a concrete, tested decision
function and separates four concerns that the plan left bundled together.

Files:

- `tell/ops/source_eligibility.py`: the pure decision module. Standard library only; no DB,
  HTTP or environment access, and a test enforces that.
- `tests/ops/test_source_eligibility.py`: 91 synthetic test cases, offline.

## Facts this design rests on (read from code at the base, not from memory)

| Fact | Where |
| --- | --- |
| Google News and company feeds share `events.source = 'news'` | `tell/collectors/news.py` `SOURCE`, `derive_events` |
| Every news row carries `payload.match.rule` (`news_query` for Google, `R1` or `company_feed` for a company feed), `payload.query` (the request URL: Google search URL or the feed URL) and `url` (Google: a `news.google.com/rss/articles/...` redirect) | `derive_events`; present since the collector's first commit 8a9c7cc (`git log -S'"query"'`) |
| `payload.source` is the publisher's or feed's own label | `docs/sources/news.md` "Fields used". **Not provenance**, and never used |
| `source = 'tell'` carries exactly two types: `hiring_velocity` (from ATS postings only) and `score_crossed_threshold` (from stored scores) | `tell/signals/velocity.py`, `tell/signals/scoring.py` `emit_crossings` |
| Lineage: event → `signals.event_id` (FK) → `scores.contributions[]` (`signal_id`, plus a copied `url` and `summary`) → crossing `payload.top_contributions[:2]` → Slack / nudges / gate / HubSpot | `scoring.py` `_contribution_dict`, `emit_crossings` |
| `scores` rows are upserted per `(company, profile, as_of)` (`update_columns=["score","contributions"]`): a rescore overwrites that day's lineage | `scoring.py` `score_companies` |
| `prune` = `archive_events` (appends every event older than 180 days to **public** `tell-data` JSONL) then deletes archived rows that no signal references | `tell/ops/prune.py`, `tell/publish/data_repo.py` |
| `write_events_jsonl` appends every event observed that day to public `tell-data` | `data_repo.py` |
| News aliases (`company_aliases.source = 'news'`) come from the company name tell put in its own query, not from Google content | `tell/resolve/match.py` `_subject`, `_alias_source` |

Not verified: production row counts, and whether any production `news` row was written by
anything other than `derive_events` (manual SQL, an older local build). The design does not
need either. A row whose markers are missing or disagree is `unknown` and excluded.

## 1. Exclusion is not deletion, and neither is a retention policy

Three different questions, three different answers:

| Question | Answer in this candidate | Function |
| --- | --- | --- |
| May this row feed scores, exports, notifications or reports? | **Exclusion**, decided at read time. The row is never changed | `classify_event`, `classify_crossing` |
| What may the size-budget prune do with it? | Eligible rows: today's behavior, unchanged. Excluded rows: **HOLD**: never archived to public `tell-data`, never deleted, at any age | `archive_disposition` |
| How long is a held row kept, and where may it go? | **Not decided.** Owner choice R1 below. HOLD is a holding state that destroys nothing; it is not a policy | none |

Why HOLD and not "archive then delete" for excluded rows: in today's code, archiving means
publishing to the public `tell-data` repo. That is continued use of the very content being
excluded. Deleting without archiving is a lossy erasure that the owner has not authorized.
HOLD is the only option that is both non-destructive and non-publishing.

Exclusion rules (`POLICY_VERSION = "source-exclusion/1"`):

| Row | Decision |
| --- | --- |
| `edgar`, `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `github`; `tell`/`hiring_velocity` | eligible (`independent_source`), unchanged |
| `news` with all three markers Google (rule `news_query`, query host and item host `*.google.com`) | excluded `google_news_excluded`. No permit can lift it; that needs a reviewed code change |
| `news` with company-feed rule and no Google marker | excluded `no_feed_permit` unless a `FeedPermit` for that exact feed URL is in `PERMITS` (empty; a code constant, never a setting) |
| `news` with some but not all Google markers | excluded `provenance_conflict`. It is **not** labeled Google |
| `news` with a missing or unparsable marker or an unknown rule | excluded `provenance_unknown` |
| `tell`/`score_crossed_threshold` | excluded `lineage_required` until `classify_crossing` clears it (section 2) |
| any other source or `tell` type | excluded `unrecognized_source` |

A `FeedPermit` must name an https feed URL with no credentials, on a host that is not
Google, and an `evidence_ref` that is a repo path under `docs/` (a URL is refused). The
evidence document itself must exist and be reviewed. The candidate cannot check that, so
adding a permit is a reviewed code change by construction.

Collector gate: `news_fetch_plan()` returns `google_news=False` always, and only permitted
feed URLs. With no permits, `runs` is False: the news collector should not start at all.
This is a fail-closed pause of the whole news component, not a fallback to unapproved
company RSS.

## 2. Lineage and auditability

- Every `Decision` carries `provenance`, `reason` and `policy_version`, and holds no row
  content, so it is safe to persist or log.
- Crossings: the payload keeps only two contributions, so `classify_crossing` reads the
  full set from the stored `scores` row (`StoredScore`). That row can be overwritten by a
  rescore, so it is trusted only if its score still equals the crossing's score and it
  still contains the cited top signal ids. Otherwise the crossing is `lineage_incomplete`
  and excluded, never inferred clean. One restricted contributor, however small, makes the
  crossing `lineage_restricted`: without it the threshold might not have been crossed.
  The candidate suppresses such crossings; it does not recompute them (see R4).
- Score comparison accepts finite numbers only (defect fix, 17 November 2026). The first
  candidate cleared a crossing (`lineage_clean`) when the crossing's score or the stored
  score was NaN, or when both were the same infinity. NaN compares false with everything and
  `inf - inf` is NaN, so the tolerance check never failed. It also raised instead of deciding
  for an int too large for a float, or a non-float stored score such as `Decimal`. Now both
  scores must be finite int or float values (not bool), otherwise the crossing is
  `lineage_incomplete`. Tolerance, scoring, weights, thresholds and policy are unchanged. A
  future caller must pass `scores.score` as a float: the column is `Numeric`, so it arrives
  as a `Decimal`, which this boundary now rejects (fails closed) rather than converting.
- Fresh scores: `eligible_signal_ids` is the read filter. Weights, half-lives and
  thresholds are untouched; a score moves only because restricted inputs are dropped.
- `cached_output_trusted(version)`: every stored score, crossing and export made before
  this policy has no version and is untrusted. It must be recomputed from eligible inputs
  or cleared through lineage before it is shown or sent again.
- `ExclusionAudit` keeps excluded event ids in memory for a private audit artifact. Where
  that artifact lives is open (R6); it is never in the log line.

## 3. Reprocessing semantics

- Exclusion is a pure function of (stored row, `PERMITS`, `POLICY_VERSION`). It is
  deterministic and idempotent, and it never mutates its input (tested).
- It is reversible without touching data. Adding or withdrawing a permit flips eligibility
  at the next evaluation. One feed's permit changes no other feed and never Google (tested).
- When wired, reprocessing must:
  - not derive new `signals` from excluded events (`build_signals` would otherwise keep
    processing restricted content); existing signals stay in place and are filtered on read;
  - not rescore past `as_of` days in place. `score_companies` upserts, so a filtered rescore
    of history would overwrite the stored lineage that `classify_crossing` needs. Filtered
    history should be computed at read time, or written with a version (needs a migration,
    a phase-owned shared file: proposal only);
  - recognise that dropping restricted inputs changes future crossings through hysteresis
    (`_rearmed` reads stored scores). Expected, and named in the changelog when wired;
  - never re-emit a historical Slack/HubSpot action. Suppression applies to new sends only.
- Prune with HOLD: excluded rows stay in Neon past 180 days. That costs storage against
  SPEC section 5's 1 GB budget, by an amount unknown without a production count (read-only;
  owner to authorize).

## 4. Secrets and log hygiene

- The candidate needs no secret and reads none (no `os`, `environ`, `getenv` or `open`;
  tested by AST).
- `ExclusionAudit.to_log_line()` emits a fixed key set: policy version, counts by reason and
  provenance, coverage state and permit count. A test feeds rows whose payloads hold a
  synthetic DB URL, a synthetic webhook URL and a headline, and asserts that none of them,
  no item or query URL, and no event id reaches the line (no `://`, no `@`).
- Coverage is derived from policy, not from row counts: with no permits it reports
  `news_coverage: unavailable` (`google_news_excluded;no_company_feed_permits`). A run with
  zero news rows is never reported as zero news activity or as full coverage.
- Existing note for the wiring review: news `runs.notes.blocked_by.url` stores a Google
  request URL (it holds a company name, no person and no secret). Under exclusion no new
  ones are written; old ones stay (R2).

## Wiring plan (not done here; each item is its own reviewed change)

PR 29's plan predeclares these tests. Each surface calls this one module, with no ad hoc host filter:

1. `news.ingest`/`collect`, including cohort and explicit `company_ids`: refuse to start
   when `news_fetch_plan().runs` is False; zero HTTP calls (respx spy) and no DB writes;
   the run log reports `unavailable` with its reason.
2. `build_signals`: skip excluded events. `_load_views`/`score_companies`: filter through
   `eligible_signal_ids`.
3. `emit_crossings` consumers (`slack.crossed_accounts`, nudges, gate, HubSpot): only
   `classify_crossing`-clean crossings.
4. `write_events_jsonl`, `archive_events`, the API `company_events`, `json_api`, CSV,
   weekly methods and backtests (13.1 speed-vs-press reads `news_funding`, so it becomes
   unavailable): eligible rows only.
5. `prune`: `archive_disposition`. HOLD rows are not in the archive's returned ids and
   are not deleted; `PruneResult` reports them separately.

## Owner choices (open; nothing here is decided)

- **R1, retention of held rows.** Pick one: (a) keep in the database indefinitely until a
  later decision (today's HOLD); (b) delete after a duration the owner names, with no public
  archive; (c) move to a private archive (for example `tell-private`), which still stores
  Google-derived text, then delete; (d) delete now. No duration is proposed here: none is
  grounded in the SPEC, the ADRs or the Google notice.
- **R2, already-published copies.** Rows already in public `tell-data` JSONL and its git
  history, `runs.notes`, prior Slack posts, HubSpot records, Neon restore history and local
  backups. Choose between stopping new copies only, or also removing old ones. Removal from
  git history is a destructive force-push.
- **R3, the company-feed evidence standard.** What counts as feed-specific permission
  (explicit site/feed terms, written consent, other). Until then `PERMITS` stays empty.
- **R4, crossings with restricted lineage.** Suppress (this candidate), or recompute
  counterfactually (changes hysteresis history and is a scoring-behavior change).
- **R5, `news` aliases.** Keep them as tell-originated (the query name; this candidate's
  reading), or treat them as restricted descendants.
- **R6, the private audit artifact.** Where excluded-id lists live (for example a
  `runs.notes` count only, or a file in `tell-private`), and how long that list is kept.

## Limits

- Unwired: no collector, score, publish or prune behavior changes yet, and nothing is safe to resume.
- No production count of Google, company-feed or unknown rows; identifiability is argued
  from the collector code since its first commit plus fail-closed for anything else.
- Not legal advice. Excluding Google does not make prior copies permitted (R2).
- GDELT or any replacement source needs its own provenance and permitted-use review;
  `unrecognized_source` keeps it excluded by default.
