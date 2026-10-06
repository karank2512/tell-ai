# Contributing to tell

Thanks for helping. `docs/SPEC.md` is the normative build spec and `docs/PLAN.md` the build plan; both are read-only references. Deviations are recorded in `docs/decisions/`.

## Setup

```
uv sync
make dev      # local Postgres via docker compose, if you have no local server
make test
```

### Database URL

Every database access goes through `DATABASE_URL` (environment variable or a `.env` file
in the checkout; `cp .env.example .env` is a valid starting point because empty values
mean "use the default").

- Local PostgreSQL server (the default, ADR 0005): nothing to set. The default is
  `postgresql+psycopg://karankapur@localhost:5432/tell_dev`; point it at your own
  server and user if they differ, e.g. `DATABASE_URL=postgresql+psycopg://$USER@localhost:5432/tell_dev`.
- `docker-compose.yml` users (`make dev`): the container listens on port **5433** with user,
  password and database all `tell`, so set
  `DATABASE_URL=postgresql+psycopg://tell:tell@localhost:5433/tell_dev`.

Tests create and drop their own `tell_test_<hex>` database on the same server, using
`DATABASE_URL` with the database name swapped for `postgres` (or `TELL_TEST_ADMIN_URL` when
you need a different maintenance connection). The test suite is hermetic otherwise: every
other `TELL_*` variable and the `.env` file are ignored while tests run.

### iCloud-synced checkouts (macOS)

If the checkout lives under `~/Desktop` or `~/Documents` with iCloud Drive sync on, iCloud
marks every file under `.venv` hidden and Python 3.12 then ignores the `.pth` files that
make `tell` and `tell_mcp` importable. The `Makefile` detects that location and keeps the
environment at `~/.venvs/tell` instead (`make env` prints the path in use). To get the
same behaviour when calling `uv run ...` directly, export
`UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell` in your shell, or clone somewhere that iCloud
does not sync. See `docs/decisions/0011-venv-outside-icloud.md`.

## PR rules

1. One ticket per PR; the ticket names the acceptance test it closes.
2. `ruff`, `mypy --strict` on `tell/`, and `pytest` green in CI. No `# type: ignore` without an explanatory comment.
3. No network calls in tests. Test adapters against recorded fixtures under `tests/fixtures/<source>/` (use `respx`).
4. A new source ships with a fixture, a schema-contract test, an idempotency test, and a `docs/sources/<source>.md` page.
5. A scoring change ships with the explainability tests passing and a one-line `CHANGELOG.md` entry saying what moved and why.
6. Secrets only via GitHub Actions secrets; `.env.example` lists every variable with a comment; never commit a value.
7. No new paid dependency, no new hosted service, no credit card anywhere. If a change needs one, stop and open an issue.

## Standing per-PR checklist

- CI green.
- No new paid service.
- `CHANGELOG.md` line present.
- The PR description names the acceptance test it closes and lists anything you were unsure about.

## Fixture discipline

Each source directory under `tests/fixtures/` holds at least three recorded cases: `normal`, `empty_board`, and one `drift` case that deliberately omits a field so the contract test proves it fails loudly. Fixtures are trimmed by hand to under 50 KB and scrubbed of anything personal (no names, emails or phone numbers). Record them by hand at polite rates (`make refresh-fixtures` is a placeholder that points at the procedure in `docs/CUTOVER_CHECKLIST.md`, step 5), then freeze. If a provider's response changes, record a new fixture, update the contract test, and say so in the PR.

## Writing a collector

A collector is one module, `tell/collectors/<source>.py`. The contract is documented at the top of `tell/collectors/base.py` ("Collector registration contract"); read it and one existing collector (for example `greenhouse.py` for an ATS, `edgar.py` for filings) first.

1. Read `docs/SPEC.md` section 3 and an existing `docs/sources/*.md` page. If the source is undocumented or its terms are unclear, open an issue before writing code.
2. Write pydantic v2 response models for the provider's JSON. They are the contract: a field the provider removes must fail validation.
3. Expose a module-level `COLLECTOR: CollectorSpec` with `source`, `kind` (`ats`, `filings`, `news` or `github`), `response_model`, `fixture_dir` (`tests/fixtures/<source>`), `drift_missing_field` and `run_fixture(session, case_path, *, run_id, now) -> RunResult`. The shared tests `tests/test_contracts.py` and `tests/test_idempotency.py` find it through `discover_collectors()`; never edit a shared test file to add a source.
4. Build events with `base.Event`: `event_id = sha256(source|external_id|version)` where `version` is the provider's own change marker, timezone-aware datetimes (`iso_utc`), provider fields only in the payload. ATS collectors hand postings to `sync_board_postings` and follow the one board-liveness rule (`record_board_ok` / `record_board_not_found`, ADR 0015). Use `tell/http.py` for every request and one `RateBudget` per host; send the project User-Agent; never retry a block.
5. Add fixtures under `tests/fixtures/<source>/` (see Fixture discipline): `normal`, `empty_board` (or `empty`) and `drift`, plus `updated.json` for ATS sources.
6. Make sure the contract test fails loudly on `drift` (the error names the missing field) and the idempotency test passes (a second run reports `inserted: 0`).
7. Write `docs/sources/<source>.md`: endpoint, rate limit, legal note, sample response, fixtures; add a row to the table in `docs/sources/README.md`.
8. Add a `CHANGELOG.md` line, then fill in the PR template.

### PR checklist for a collector

- One ticket, and the PR description names the acceptance test it closes.
- `ruff check`, `ruff format --check`, `mypy --strict tell` and `pytest` pass; no `# type: ignore` without a comment saying why.
- Fixture (three cases), contract test, idempotency test and source page are all present.
- No network in tests (`respx`, `freezegun`); no names, emails or phone numbers in code, fixtures, logs or payloads.
- Coverage stays at or above the floors below.
- A provider response that differs from the fixture means a new fixture, an updated contract test and a sentence in the PR; never patch around it silently.

## Local development notes

- `UV_PROJECT_ENVIRONMENT`: when you call `uv run` directly from a checkout under an iCloud-synced folder, export `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell` (ADR 0011). `make` targets set it for you. If you iterate with a fixed environment, `UV_NO_SYNC=1` stops `uv run` from re-syncing it.
- `TELL_COLLECTORS_ONLY`: `TELL_COLLECTORS_ONLY=greenhouse,lever uv run pytest tests/test_contracts.py tests/test_idempotency.py -q --no-cov` narrows collector discovery to the named modules, so you can iterate on one source without waiting for the others. Unset it before the final run.
- Fast loop: `uv run pytest <files> -q -p no:cacheprovider --no-cov`. Before a PR: `make lint` and `make test` (coverage floors enforced by `scripts/check_coverage.py`).
- Tests create and drop their own temporary database on your local server (or on the `make dev` container); see "Database URL" above.

## Coverage floors

Enforced by `make test` and CI: 90% for `tell/collectors/`, `tell/resolve/` and `tell/signals/`; 80% overall.

## No new paid dependency

Everything must run on free tiers and local tools. Do not add a paid API, hosted service, or anything requiring a credit card. Propose it in an issue first. A PR that needs a paid API, a hosted service or a credit card stops and asks; it does not add it.

## PII

No column, fixture, log line or payload may contain a person's name, email or phone number.
