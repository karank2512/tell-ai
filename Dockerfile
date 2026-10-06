# tell FastAPI v1 service (spec section 10). Coded and tested, NOT deployed:
# see docs/deploy/render.md. Build: docker build -t tell-api .
# Run:   docker run -e DATABASE_URL=postgresql+psycopg://... -e PORT=8000 -p 8000:8000 tell-api
FROM python:3.12-slim

# uv, pinned to the version the lockfile was produced with.
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Dependencies first, in their own cached layer: the lockfile plus every workspace
# member's metadata (tell depends on the tell-mcp workspace package).
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages/tell-mcp/pyproject.toml packages/tell-mcp/README.md packages/tell-mcp/
RUN uv sync --frozen --no-dev --no-install-workspace

# The code. tell is installed editable from /app because tell.publish.json_api reads
# docs/api/company.schema.json relative to the checkout.
COPY tell ./tell
COPY packages/tell-mcp ./packages/tell-mcp
COPY alembic ./alembic
COPY alembic.ini ./
COPY docs/api ./docs/api
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home tell
USER tell

EXPOSE 8000
# Render (and most hosts) inject PORT; the service never runs migrations itself.
CMD ["sh", "-c", "exec uvicorn tell.api.app:app --host 0.0.0.0 --port $PORT"]
