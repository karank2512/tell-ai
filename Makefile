.PHONY: dev dev-down test lint fmt migrate refresh-fixtures env

# Where uv keeps the project environment. On macOS, iCloud Drive's Desktop & Documents
# sync marks every file under a dot-directory as hidden, and CPython 3.12 skips hidden
# .pth files, so a .venv inside ~/Desktop or ~/Documents silently loses its editable
# installs (tell, tell_mcp) and `make test` fails on the tell-mcp stub. When the checkout
# lives in one of those folders and UV_PROJECT_ENVIRONMENT is not already set, keep the
# environment outside the synced tree. Everywhere else uv's default .venv is used.
# See docs/decisions/0011-venv-outside-icloud.md.
ICLOUD_SYNCED :=
ifeq ($(shell uname -s),Darwin)
ifneq ($(or $(filter $(HOME)/Desktop/% $(HOME)/Documents/%,$(CURDIR)),$(findstring $(HOME)/Library/Mobile Documents/,$(CURDIR))),)
ICLOUD_SYNCED := 1
endif
endif
ifeq ($(origin UV_PROJECT_ENVIRONMENT),undefined)
ifeq ($(ICLOUD_SYNCED),1)
export UV_PROJECT_ENVIRONMENT := $(HOME)/.venvs/tell
endif
endif

# Print the environment path `uv run` will use (empty means uv's default .venv).
env:
	@echo "UV_PROJECT_ENVIRONMENT=$(UV_PROJECT_ENVIRONMENT)"

# Local Postgres 16 for contributors without a server (port 5433; see docker-compose.yml).
dev:
	docker compose up -d

dev-down:
	docker compose down

# Coverage data and the JSON report are per invocation (suffixed with this make's PID) so
# concurrent `make test` runs in one checkout never collide; both are removed afterwards.
test:
	@id=$$$$; status=0; \
	COVERAGE_FILE=.coverage.$$id uv run pytest --cov=tell --cov-report=json:coverage.$$id.json --cov-report=term-missing -q \
	  && uv run python scripts/check_coverage.py coverage.$$id.json || status=$$?; \
	rm -f .coverage.$$id coverage.$$id.json; \
	exit $$status

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict tell

fmt:
	uv run ruff check --fix .
	uv run ruff format .

migrate:
	uv run alembic upgrade head

# Phase 2 replaces this with the supervised fixture recorder (real calls, polite rates, diff for review).
refresh-fixtures:
	@echo "refresh-fixtures: no recorder yet (lands with the first collector in Phase 2)."
