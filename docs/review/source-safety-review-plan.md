# Tell source safety: review plan and first narrow patch

16 November 2026. Base main: 90959626585053a91620ad8068f5071c9ef58d2b.

Status: REVIEW ONLY, NOT SAFE TO RESUME. Ingest and weekly remain manually disabled. No DB deletion, rewrite, migration, production run, merge, push, deployment or re-enable.

## What this patch does

Revised review candidate v2: replace weekly's unconditional enable loop with a READ-ONLY schedule-state audit. No automatic enable request exists, even for disabled_inactivity. The job token is reduced from actions:write to actions:read plus contents:read for checkout. Active/manual states are reported unchanged; inactivity, failed reads and unknown states are reported nonzero for owner review.

The earlier GET-then-enable v1 candidate is preserved separately but withdrawn as a pause-protection fix: its state read and enable write could race with a concurrent manual pause. V2 makes no write, so that race cannot re-enable anything. Tradeoff: GitHub inactivity suspension no longer automatically recovers. The owner must review any later recovery. This candidate changes no live state and is NOT safe to resume the wider pipeline.

Six offline shell tests use a mocked gh executable, with no network calls. They explicitly prove zero mutating requests across active/manual/inactivity/unknown/error states. Reducing token permission is defense in depth, not the sole safety test.

## Why a Google-fetch-only patch is insufficient

Read-only audit of current source:
1. `tell/collectors/news.py`: combined Google News and company feeds share source `news`. Stored payload includes `query`, match rule and publisher `source`; publisher labels alone are NOT provider provenance. Google News query and news_query rule are clues, not permission.
2. `tell/signals/scoring.py::_load_views`: reads historical Signal joined to EventRow without provider eligibility filtering. Disabling future fetches leaves old news contributions in fresh scores.
3. `tell/publish/data_repo.py::write_events_jsonl`: appends ALL observed events to the daily JSONL. `archive_events` likewise exports historical events and returns IDs used by pruning. Any exclusion here must not mark skipped rows as archived or eligible for deletion.
4. `tell/api/app.py::company_events`: returns raw events/receipt URLs/payload directly. A score-only filter would not cover this route.
5. `tell/publish/slack.py::crossed_accounts`: reads stored `tell` threshold-crossing events and their cached/derived data. A simple `source != news` misses these descendants. `new_signals_count` joins historical events too.
6. `tell/publish/json_api.py`: news presence uses EventRow.source==news and CompanyFeed counts; company pages build fresh scores but some callers pass precomputed results. Why-not/coverage/prefetch paths also need consistent eligibility.
7. `tell/publish/weekly.py`: important correction, top hiring and Form D lists explicitly do NOT read news. They are not automatically Google-derived. Its workflow still invokes publication/method artifacts and unconditional keepalive; inspect actual inputs rather than blanket-label the entire weekly report restricted.
8. Existing public tell-data exports, prior Slack posts, stored scores/crossings, backups and git history remain. This plan does not erase them or make them permitted.

## Conservative source-exclusion slice, not yet implemented

No feed-specific company RSS permission record was found in this audit. Do not invent one from a URL or a config toggle. The safest first slice is to disable the ENTIRE news collector by default, not silently fall back to unapproved company RSS, and exclude ALL news-origin contributions from read/publish paths until a reviewed per-feed permission model exists. This sacrifices otherwise possibly permitted news coverage but preserves independent EDGAR/ATS records. Unknown provenance must remain excluded and reported as unavailable coverage, never counted as zero accuracy.

A full implementation needs one shared eligibility boundary applied to:
- All live collector entry points, including direct news collect/ingest/cohort CLI calls, before network or DB side effects.
- Signal derivation and every fresh scoring read, excluding existing news-linked signals without deleting them or changing weights/thresholds.
- Cached Score/contribution consumers, stale threshold-crossing events, nudges/digests and CRM output, with a policy version or provenance proof. If old crossing lineage is insufficient, suppress the crossing rather than infer clean.
- Raw JSONL/API event output, public company JSON, CSV, methods/backtest reports and summary counts. Exclude descendants or stop that output if lineage is incomplete. Aggregates cannot be assumed clean.
- Archival/prune contract: retained excluded rows MUST NOT be reported as archived and MUST NOT become eligible for deletion. No production prune until this path is reviewed.
- Coverage messages that name the news restriction explicitly while EDGAR/ATS remain usable.

Do not implement separate ad hoc host-name filters at each output and assume agreement. A source field saying `news`, article publisher name, or URL redirect is insufficient evidence of licensed use. Keep permission evidence distinct from identity provenance.

## Predeclared fixture tests for that wider slice

- Collector entry points: request spy proves ZERO HTTP calls; DB spy proves no real source writes when restricted, including manual/cohort paths.
- Mixed historical fixtures: EDGAR-only and ATS-only unchanged; Google news, company RSS with no recorded permission and unknown news provenance excluded. No score-weight or threshold diff.
- Descendant fixture: source=tell crossing whose top contribution is news excluded; incomplete lineage fails closed; independent clean crossing remains eligible.
- Output matrix: JSONL/API/CSV/company JSON/Slack/nudge/CRM/method summaries expose no restricted headline/URL/contribution/count. Precomputed score passed by a caller cannot bypass.
- Archive fixture: excluded event never in `present`/archived ID return, never pruned, original row unchanged.
- Weekly fixture: direct ATS/EDGAR lists stay intact; only actual restricted lineage is suppressed. Do not hide unrelated source data without cause.
- Existing outputs fixture: explicitly report historical artifacts unchanged, not a deletion-success assertion.
- Gate failures: missing permission/provenance does not become a config bypass or a successful zero-result run. Coverage is unavailable with reason.

Only after exact candidate review, offline test matrix, full required checks and a separate owner merge/resume decision may any resumption be proposed. This first keepalive patch alone is NOT that candidate.

## Tests performed on first patch

Linux offline standard-library unit tests: six test methods, including four unknown/empty-state subcases, all pass. `bash -n` passes. Additional Mac checks and any missing full-suite/type/style checks are recorded in the parent report, not implied by this plan.

## Sources

Code paths above inspected at the stated base. Live workflow states rechecked 07:32 NZDT: ingest376131172 and weekly376131174 disabled_manually; CI and publish-mcp active unchanged.
https://github.com/karank2512/tell-ai/blob/main/.github/workflows/ingest.yml
https://github.com/karank2512/tell-ai/blob/main/.github/workflows/weekly.yml
Google News RSS copyright notice, previously fetched from actual XML: https://news.google.com/rss/search?q=Tell. Personal non-commercial feed-reader rendering does not establish Tell's storage/publication permission. This patch changes no source permission.

## Independent review follow-up and enable-path audit (08:20 NZDT)

The v2 exact-archive reviewer found no current shell/workflow defect, but requested stronger invocation tests and broader path auditing. The mock now rejects every gh argv except the exact three read-only `gh api repos/fixture/repo/actions/workflows/<file> --jq .state` shapes. Logged calls must exactly equal those three reads; `gh workflow enable`, `--method PUT`, unexpected paths/options or any other call fail the test even if the shell catches the nonzero exit.

AppleDouble metadata was present in the prior archive. The final archive excludes `._*` via COPYFILE_DISABLE and exclusion patterns. Original user files are not deleted. This is a packaging correction, not a source deletion.

Read-only repo audit of tracked .github/scripts/docs/Makefile found the prior weekly enable loop and human runbook/ADR references; current script contains no enable endpoint or mutating invocation. The deployment runbook and cutover checklist are updated to stop recommending blind enable. ADR0026 remains historical rather than rewritten; this review supersedes its auto-recovery behavior if accepted. Manual workflow_dispatch and CLI commands remain possible paths and are NOT authorization to run them. No repo-wide absolute pause-protection claim is made.

User crontab contained an unrelated indexer only. No Tell-named launch files, matching launch labels, or active Tell collector/publish CLI processes were found in the bounded user/standard LaunchAgent inspection at08:19. This does not prove no differently named scheduler, process, remote worker or future manual invocation exists. No scheduler was altered or disabled. Collector-level exclusion is still required before ingest may resume.

Additional wider-source surfaces that must be covered before resumption:
- Weekly backtest refresh/methods reports can republish historical news-influenced results, although direct hiring/FormD lists do not read news.
- The MCP package and any deployed MCP/static API consumers; publish-mcp is a package/tag publication path, not a daily dataset refresh, but still requires surface/contract review.
- Daily Slack digest, source/runlog counts and freshness/coverage messages. PR26 merged07Nov, https://github.com/karank2512/tell-ai/pull/26, changes news collector/runlog/Slack coverage and stop reasons. Do not report a restricted collector as successful zero coverage or retain stale Google success messages.
- Existing Mac manual collectors, cron/launchd or other local jobs and unexamined remote workers may bypass GitHub schedule pause. A full safety boundary must be in collector and downstream code, not only YAML.
- GDELT or any replacement needs independent source provenance and permitted-use evidence. It is not covered by excluding Google or by a generic news switch.

Explicit resume gates: source-specific permitted-use evidence or collector-level fail-closed exclusion BEFORE any ingest resume; historical score/export/API/descendant/archive filtering including cached crossings and public methods; targeted and complete tests, style/types; independent exact-candidate review; then a separate owner merge/resume decision. Backups, Neon database, tell-data git history and prior messages remain unchanged. No erasure or safe-resumption claim.
