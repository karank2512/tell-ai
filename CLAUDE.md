# CLAUDE.md — agent conventions for tell

tell is an open-source buying-signals pipeline. Normative references: `docs/SPEC.md` (build spec), `docs/PLAN.md` (build plan). Both are READ-ONLY: never edit `docs/SPEC.md` or `docs/PLAN.md`. Record deviations in `docs/decisions/` instead.

## Conventions (SPEC §1)

- Read `docs/SPEC.md` and the relevant `docs/sources/*.md` before touching a source.
- Prefer boring code: standard library, `httpx`, `pydantic`, `sqlalchemy`, `alembic`, `rapidfuzz`, `feedparser`, `lxml`. No new framework without a note in `docs/decisions/`.
- When a provider's response differs from the fixture, record a new fixture, update the contract test, and say so in the PR. Never patch around it silently.
- Log every run as one JSON line per source: `run_id`, `source`, `started_at`, `finished_at`, `fetched`, `inserted`, `skipped`, `errors`.
- Ask before any decision in SPEC §17; never guess a business rule.

## Rules for every PR (SPEC §1)

1. One ticket per PR; the ticket names the acceptance test it closes (SPEC §12).
2. `ruff`, `mypy --strict` on `tell/`, and `pytest` green in CI. No `# type: ignore` without a comment explaining why.
3. No network calls in tests. Adapters are tested against recorded fixtures under `tests/fixtures/<source>/` (use `respx` for httpx).
4. Any new source ships with: fixture, schema-contract test, idempotency test, and a `docs/sources/<source>.md` page (endpoint, rate limit, legal note, sample response).
5. Any change to scoring ships with the explainability test passing and a one-line `CHANGELOG.md` entry saying what moved and why.
6. Secrets only via GitHub Actions secrets. `.env.example` lists every variable with a comment; never commit a value.
7. No new paid dependency, no new hosted service, no credit card anywhere. A PR that needs one stops and asks.

Standing checks on every PR: CI green; no new paid service; `CHANGELOG.md` line present; the PR description names the acceptance test it closes and lists anything unsure.

## Hard constraints

- Python >=3.12, managed with uv. Coverage floors: 90 for `collectors/`, `discovery/`, `resolve/`, `signals/`; 80 overall (enforced by `scripts/check_coverage.py`).
- No PII: no column, fixture, log line or payload holds a person's name, email or phone (e.g. never store Form D `relatedPersonsList`).
- Do not add dependencies to `pyproject.toml` outside Phase 0; report the need instead.

## Local build conventions (PLAN)

- Repo root on this machine: `/Users/karankapur/Desktop/Tell/tell`. Siblings `../tell-data` and `../tell-private` are generated locally and become their own repos at cutover.
- Dev database: Homebrew PostgreSQL 14 at `postgresql+psycopg://karankapur@localhost:5432/tell_dev`, no password. Tests create and drop their own temporary database per session on the same server. `docker-compose.yml` (Postgres 16) is for contributors without a local server. CI uses a Postgres 16 service container.
- Parallel agents share the checkout and own disjoint files. Shared files (`pyproject.toml`, `uv.lock`, `tell/cli.py`, `tell/models.py`, alembic migrations) are edited only by the phase that owns them.
- Agents do not run `git commit`; the orchestrator commits.

## Running tests

- `make test` runs the full suite (pytest with coverage). Also: `make dev`, `make migrate`. `make refresh-fixtures` only prints the supervised manual re-recording procedure; it does not record fixtures or call the network.
- `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell` when calling `uv run` directly from this checkout (it lives under iCloud-synced `~/Desktop`; `make` sets it for you, see ADR 0011).
- Gates before finishing: `ruff check`, `ruff format --check`, `mypy --strict tell/`, `make test`.

## Fixtures

- Live under `tests/fixtures/<source>/`; collector fixtures cover normal, empty and drift cases (a drift case omits a field so the contract test fails loudly). ATS boards use `empty_board`; EDGAR, GitHub and news use `empty`. The shared collector contract test (`tests/test_contracts.py`) reads `normal*.json` and `empty*.json`, requires at least one matching parsing fixture and requires `drift.json`. Discovery fixtures such as `issuer_sites/` use their own layout.
- Hand-trimmed to under 50 KB and scrubbed of anything personal. Fixture origins vary: some were recorded from live responses, others hand-crafted or derived. Keep them frozen and follow each source's fixture documentation and contract tests. `docs/CUTOVER_CHECKLIST.md` step 5 covers the supervised EDGAR procedure only; other source refreshes need a source-specific plan and review, not an assumed automated recorder.
- Public gold-set sample: `tests/fixtures/gold_sample.csv` (50 rows). The full gold set lives in `tell-private`.

## Collector discovery contract

Each collector module exposes a `COLLECTOR` object conforming to the protocol in `tell/collectors/base.py`. Contract and idempotency tests discover collectors automatically through these objects, so adding a source never edits a shared test file.

## Decisions

Locked decisions are in `docs/decisions/` (index in `docs/decisions/README.md`). Workday and Oracle collectors are dropped; discovery records them as `unsupported` only.
