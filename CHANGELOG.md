# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 5 entity resolution: `tell/resolve` (five-step name normalization, rules R1-R6 with the spec's confidences and `payload["match"]` audit record, `resolve_events` linking Form D / EDGAR / news events with previous-name aliases and `cik` / `legal_name` enrichment, `backfill-aliases`, review-queue CSV round trip plus the optional password-gated Streamlit page, `tell resolve` sub-app), gold-set precision/recall test `tests/test_resolve.py`, `docs/methods/entity-resolution.md`.
- Phase 2-4 fix batch: single ATS 404 rule with `ats_boards.not_found_count`/`first_not_found_at` (migration 0004, ADR 0015); Form D amendments link by `previousAccessionNumber`, else the latest original on or before the D/A; leadership news headlines redacted (ADR 0016, provisional); news per-run budget, 24h/weekly rotation, bare `funding` rule, blocked runs fail; discovery re-verifies dead boards once per 30 days and resolves `grnh.se` short links; GitHub runs stop on 429 / exhausted rate limit; repo references point at karank2512/tell-ai and karank2512/tell-data.
- Phase 0 repository scaffold: Apache-2.0 `LICENSE`, `CLAUDE.md` agent conventions, `CHANGELOG.md`, `CONTRIBUTING.md`, pull request template, "Watch these domains" issue form.
- Phase 0 code scaffold: `pyproject.toml` with the uv workspace (`packages/tell-mcp` as a member, ADR 0012), typed `tell/config.py` settings, `tell/http.py` polite HTTP layer (retries, `Retry-After`, per-host rate budgets), `tell/db.py` engine and upsert helpers, `tell/cli.py` (typer), the `tell-mcp` stub, `tests/conftest.py` temporary per-session database, `scripts/check_coverage.py` floors gate, GitHub Actions CI with a Postgres 16 service container, pre-commit hooks (ruff, mypy, gitleaks), `docker-compose.yml` Postgres for contributors without a local server.
- Architecture decision records `docs/decisions/0001` to `0013` for the locked decisions, plus template and index.
- `docs/README.md` map of the documentation tree.
- Phase 1 schema and event model: `tell/models.py` (the thirteen tables of SPEC section 5, field for field), alembic initial migration `0001_initial`, `tell/collectors/base.py` (`Event`, idempotent `write_events`, `update_event_payload` for D/A amendments, run lifecycle with one JSON log line, ATS posting state machine, `CollectorSpec` registration contract), taxonomy stubs, and the parity, idempotency, contract and PII-guard test families.
- `postings.version_marker` (migration `0002`, ADR 0013): the provider change marker last seen per posting, set on insert and every poll; `job_updated` now compares against it instead of looking earlier events up, so the 180-day event prune can never cause a spurious `job_updated`. Event payloads no longer carry the database-internal `board_id` (vendor and board identifier remain).
- `TELL_COLLECTORS_ONLY` (comma-separated module names) narrows `discover_collectors()` for parallel local work; unset means everything.
- Coverage floors live in `pyproject.toml` under `[tool.tell.coverage]`; `scripts/check_coverage.py` reads them (defaults otherwise) and `make test` writes per-invocation coverage files so concurrent runs do not collide.
- Phase 2 ATS collectors: Greenhouse, Lever, Ashby and SmartRecruiters, each with recorded fixtures, contract tests, idempotency tests and a `docs/sources` page.
- Phase 3 discovery: `tell/discovery` careers-page scan, ATS slug guesses, board ownership verification, unsupported-vendor detection and a per-batch request budget; YC company directory seeder; `docs/methods/discovery.md`.
- Phase 4 collectors: `edgar` (8-K, S-1) and `edgar_formd` (Form D, including the quarterly bulk loader), `news` with `news_classify` (Google News RSS plus company feeds), and `github_org`; `company_feeds` table via migration `0003` (ADR 0014).

### Changed

- `tell/http.py`: the full backoff schedule is used (first attempt plus four retries at 1, 2, 4, 8 s; five attempts in total); transport errors are retried only for idempotent methods (GET, HEAD, OPTIONS) and propagate for POST and friends; non-finite `Retry-After` values (`nan`, `inf`) fall back to the normal backoff; `RateBudget` / `HostRateBudgets` are thread-safe.
- `tell/config.py` ignores empty environment values (`env_ignore_empty`), so `cp .env.example .env` yields the defaults with every secret `None`.
- CI runs gitleaks in its own always-on job and declares `workflow_dispatch`; pre-commit pins ruff to the `uv.lock` version and gives the mypy hook the project's typed dependencies.

### Fixed

- Phase 1 review: the generic idempotency test is kind-aware (an ATS rerun emits no events rather than skipping them); the fixture PII guard bounds URL tokens at quotes/delimiters and walks JSON strings so compact JSON cannot hide an `@`; `collectors/base.py` rejects PII-named keys at any nesting depth and refuses description-like keys or strings over 500 characters in `ObservedPosting.extra`; the `Makefile` keeps uv's environment outside iCloud-synced folders (ADR 0011) so `make test` passes from `~/Desktop`.
- Tests are hermetic: an autouse fixture strips ambient `TELL_*` / `SEC_USER_AGENT` / `SLACK_WEBHOOK_URL` / `GITHUB_TOKEN` variables, blocks the checkout's `.env` and clears the settings cache around every test. The PII guard tokenizes collector sources (only `#` comments are exempt) and flags an `@` in a URL's userinfo.
