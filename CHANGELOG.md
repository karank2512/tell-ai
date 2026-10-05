# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 0 repository scaffold: Apache-2.0 `LICENSE`, `CLAUDE.md` agent conventions, `CHANGELOG.md`, `CONTRIBUTING.md`, pull request template, "Watch these domains" issue form.
- Architecture decision records `docs/decisions/0001` to `0010` for the locked decisions, plus template and index.
- `docs/README.md` map of the documentation tree.
- Phase 1 schema and event model: `tell/models.py` (the thirteen tables of SPEC section 5, field for field), alembic initial migration `0001_initial`, `tell/collectors/base.py` (`Event`, idempotent `write_events`, `update_event_payload` for D/A amendments, run lifecycle with one JSON log line, ATS posting state machine, `CollectorSpec` registration contract), taxonomy stubs, and the parity, idempotency, contract and PII-guard test families.

### Fixed

- Phase 1 review: the generic idempotency test is kind-aware (an ATS rerun emits no events rather than skipping them); the fixture PII guard bounds URL tokens at quotes/delimiters and walks JSON strings so compact JSON cannot hide an `@`; `collectors/base.py` rejects PII-named keys at any nesting depth and refuses description-like keys or strings over 500 characters in `ObservedPosting.extra`; the `Makefile` keeps uv's environment outside iCloud-synced folders (ADR 0011) so `make test` passes from `~/Desktop`.
