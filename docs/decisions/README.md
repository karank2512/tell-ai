# Decision records

Template: [0000-template.md](0000-template.md). Each record covers one locked decision from `docs/PLAN.md`.

| ADR | Decision |
| --- | --- |
| [0001](0001-license-apache-2.0.md) | License is Apache-2.0 |
| [0002](0002-repos-under-personal-account.md) | Repos live under the personal GitHub account |
| [0003](0003-drop-workday-oracle-collectors.md) | Drop the Workday and Oracle collectors |
| [0004](0004-ci-postgres-service-container.md) | CI uses a Postgres 16 service container |
| [0005](0005-local-first-dev-database.md) | Local-first dev database |
| [0006](0006-gold-set-location-and-public-sample.md) | Gold set location and public sample |
| [0007](0007-collectors-fresh-from-spec.md) | Collectors are built fresh from the spec |
| [0008](0008-optional-code-included-not-deployed.md) | Optional code is included but not deployed |
| [0009](0009-yc-directory-only-seed.md) | Seed domains come from the YC directory only |
| [0010](0010-pypi-tell-mcp-only.md) | Publish only tell-mcp to PyPI |
| [0011](0011-venv-outside-icloud.md) | Keep the uv environment outside iCloud-synced folders |
| [0012](0012-tell-mcp-as-workspace-package.md) | tell-mcp is a uv workspace package, not `tell/mcp/server.py` |
| [0013](0013-postings-version-marker.md) | `postings.version_marker` holds the provider change marker |
| [0014](0014-company-feeds-table.md) | `company_feeds` keeps discovered company RSS/Atom feeds |
| [0015](0015-ats-404-and-reverification.md) | One 404 / dead-board rule for every ATS collector |
| [0016](0016-news-headline-redaction.md) | Leadership news headlines are stored with the person's name redacted (accepted 2026-10-06) |
| [0017](0017-posting-ghost-votes.md) | `posting_ghost_votes` stores anonymous job-seeker ghost-job votes (inert until 500) |
| [0018](0018-entity-resolution-rule-refinements.md) | Entity-resolution refinements beyond the SPEC wording: R3 tie to review, R4 over the whole table, token-superset guard, international suffixes, R1 audit record, one link function, name-only headline metric |
| [0019](0019-velocity-cold-start-and-reposts.md) | Hiring velocity: first-poll events flagged and never counted, `vanished_within_30d` repost rule, capped walk-back marks the whole episode |
| [0020](0020-taxonomy-interpretations.md) | Role-family taxonomy interpretations: keyword masking, shared department hints, stale-weighted small-company count, non-English stop words, short-keyword suffixes, location code collision kept |
| [0021](0021-scoring-interpretations.md) | Scoring interpretations: size band gates alerts not storage, 7-day cooldown + 0.5 re-arm hysteresis, end-of-day scoring instant, `default` scores every active company, `news_funding` and first-poll tech events carry no weight, heat ties count half |
| [0022](0022-public-watchlists.md) | `watchlists.slug` and `watchlists.is_public` mark the lists published as `api/v0/watchlists/{slug}.json` |
| [0023](0023-publishing-interpretations.md) | Publishing interpretations: rounding that adds up, heat only through public watchlists, code-commit `generated_by`, weekly edition = last complete ISO week, first-poll events out of freshness, `never_run` sources, digest hour 7 |
| [0024](0024-api-keys-tables.md) | `api_keys` and `api_usage` back the FastAPI v1 service: SHA-256 keys with prefix and revocation, metering before the handler, v1 route interpretations |
| [0025](0025-weekly-reports-published-to-tell-data.md) | `weekly.yml` publishes refreshed methods reports to `tell-data/site/methods/` and never commits to `tell-ai` |
| [0026](0026-workflow-keepalive-via-api.md) | Scheduled workflows are kept alive by re-enabling them through the API, not by commits |
| [0027](0027-intake-contact-storage.md) | Intake contacts and webhooks live only in tell-private; the DB keeps a random per-watchlist owner handle and a company+profile slug |
| [0028](0028-operations-interpretations.md) | Operations interpretations: separate `discover` job and per-run prune in `ingest.yml`, 5-hour poll gap, dead-man vs health `never_run`, prune keeps signal evidence, `runs.notes.manual` written by the run lifecycle (`tell --manual` / `TELL_RUN_MANUAL`) |
| [0029](0029-ingest-runs-per-day.md) | `ingest.yml` runs once a day by default (private repo, metered minutes) and four times a day when the `TELL_RUNS_PER_DAY` variable is `4` |
| [0030](0030-discovery-deeper-scan-and-unsupported-destinations.md) | Discovery deviations from SPEC section 4: deeper scan past the first 200, no slug guesses after a hiring channel, 18 more ATS vendors and YC Work at a Startup recorded as `unsupported` (changes `coverage_note`) |
| [0031](0031-slug-guess-ownership-evidence.md) | Slug-guessed boards are activated only when the board points at the company's registrable domain (else `paused` + `board_verify` `guess_without_domain_evidence`), `tell discover reverify-guesses`, shared unsupported destinations keyed `<identifier>#<domain>`, deep-scan robots counted in the 12, 15 s bundle deadline (accepted 2026-10-14) |
| [0032](0032-news-feed-discovery-cache.md) | News: company-feed discovery at most once per company per 30 days (misses, including unreachable sites, remembered in `runs.notes.no_feed`, never errors), company sites on 6 workers at 1 rps per host with 10 s timeouts separate from Google News, one commit per company |
| [0033](0033-alias-hygiene.md) | Alias hygiene: an alias rule (>= 3 letters/digits, not all digits, not only filler tokens; domain labels also not a short English word or a platform domain), one-word aliases match whole one-word names only, YC companies on platform domains are not seeded, `tell resolve prune-aliases` / `close-junk-reviews` and `tell discover prune-platform-companies` (dry run by default) (proposed) |
| [0034](0034-form-d-linking-guards.md) | Form D linking guards: R3 review-only for Form D, R2/R4 need a Delaware corporation, an Active YC company and an incorporation year that fits the batch (else review), pooled funds straight to R6, R5 needs a shared distinctive token, only R1 / guarded R2 / human links write aliases, legal name and CIK, `companies.yc_status` / `yc_batch` (migration 0008), `tell resolve relink-formd` (dry run by default) (accepted 2026-10-14) |
| [0035](0035-issuer-first-discovery.md) | Issuer-first discovery: companies created from Form D issuers on tier A/B site evidence (`companies.source`, `issuer_discovery`, evidence stored as `{path, span}` only), rule R0 (exact CIK with a trusted `companies.cik_source`, 0.99, above R1), guard (f) `no_site_evidence` for Form D auto-links to companies without a YC status, migration 0009, 13.1 split into monitored-before and found-from-the-filing cohorts (accepted 2026-10-15) |
