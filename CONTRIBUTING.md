# Contributing to tell

Thanks for helping. `docs/SPEC.md` is the normative build spec and `docs/PLAN.md` the build plan; both are read-only references. Deviations are recorded in `docs/decisions/`.

## Setup

```
uv sync
make dev      # local Postgres via docker compose, if you have no local server
make test
```

macOS note: if the checkout lives under `~/Desktop` or `~/Documents` with iCloud Drive
sync on, iCloud marks every file under `.venv` hidden and Python 3.12 then ignores the
`.pth` files that make `tell` and `tell_mcp` importable. The `Makefile` detects that
location and keeps the environment at `~/.venvs/tell` instead (`make env` prints the path
in use). To get the same behaviour outside `make`, export
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

Each source directory under `tests/fixtures/` holds at least three recorded cases: `normal`, `empty_board`, and one `drift` case that deliberately omits a field so the contract test proves it fails loudly. Fixtures are trimmed by hand to under 50 KB and scrubbed of anything personal (no names, emails or phone numbers). Record them with `make refresh-fixtures` at polite rates, then freeze. If a provider's response changes, record a new fixture, update the contract test, and say so in the PR.

## Adding a source

1. Read `docs/SPEC.md` section 3 and an existing `docs/sources/*.md` page.
2. Implement `tell/collectors/<source>.py` exposing a `COLLECTOR` object that satisfies the protocol in `tell/collectors/base.py`, with pydantic response models as the contract.
3. Add fixtures: `normal`, `empty_board`, `drift`.
4. Add a contract test and an idempotency test (second run reports `inserted: 0`). Both discover collectors via `COLLECTOR`; do not edit shared test files.
5. Write `docs/sources/<source>.md`: endpoint, rate limit, legal note, sample response.
6. Add a `CHANGELOG.md` line.

## Coverage floors

90% for `tell/collectors/`, `tell/resolve/` and `tell/signals/`; 80% overall.

## No new paid dependency

Everything must run on free tiers and local tools. Do not add a paid API, hosted service, or anything requiring a credit card. Propose it in an issue first.

## PII

No column, fixture, log line or payload may contain a person's name, email or phone number.
