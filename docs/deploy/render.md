# Deploying the v1 API on Render (free tier)

> **Status: not deployed.** The FastAPI service (`tell/api`, spec section 10) is coded
> and tested; per ADR 0008 and PLAN Phase 9 it is deployed only when a consumer (a Clay
> user who needs query parameters the static files cannot answer) asks for it. Nothing in
> this repository deploys it automatically.

Everything the service answers is also in the static JSON on GitHub Pages
(`https://karank2512.github.io/tell-data/api/v0/...`), so an outage of the service
degrades to stale data, never to no data. Point every integration at the static JSON
first and add the service only for the queries it alone answers (`/v1/in-market` with
filters, `/v1/companies/{domain}/events?since=&types=`, writes).

## What the service is

| | |
| --- | --- |
| Image | `Dockerfile` at the repo root: `python:3.12-slim`, uv, `uvicorn tell.api.app:app --host 0.0.0.0 --port $PORT` |
| Routes | `GET /v1/companies/{domain}`, `GET /v1/in-market`, `GET /v1/companies/{domain}/events`, `POST /v1/watchlists`, `POST /v1/feedback`, `GET /healthz` (no auth) |
| Auth | `Authorization: Bearer tell_...`; keys stored as SHA-256 only (`api_keys`, ADR 0024) |
| Metering | `api_usage (key_id, day, calls)` upserted before every handler; 429 with `Retry-After` once the key's `daily_quota` (default 1000) is spent; resets 00:00 UTC |
| Errors | `application/problem+json` (`type`, `title`, `status`, `detail`, `errors` on 422) |
| CORS | Off. Browser pages on other origins cannot read responses; Clay and servers are unaffected |
| Database | The same Postgres the pipeline writes (Neon free). Never Render's free Postgres: it expires after 30 days |

## Render free tier, and what it means for callers

- **Spin-down after 15 minutes idle.** The first request after that waits for a cold
  start of up to about a minute.
- **Clay users: set the HTTP API column timeout to 60 s and retry once.** A cold start
  then costs one slow row, not a failed column. Fall back to the static URL
  `https://karank2512.github.io/tell-data/api/v0/companies/{{domain}}.json` (no auth)
  for anything the static file answers.
- **750 free instance hours per workspace per month.** A warm-up ping from GitHub
  Actions every 10 minutes during business hours keeps the service awake for about
  10 h x 22 working days = 220 hours a month, well inside 750. Outside those hours it
  sleeps and the first caller pays the cold start.
- 5 GB of bandwidth a month; Render may suspend a free service for unusually high
  outbound traffic. The service never polls anything: it only answers requests.
- The first dollar, if ever spent, buys a Render Starter instance (always on) and makes
  the warm-up ping unnecessary.

## Steps (when a consumer needs it)

1. **Migrate the database** from your machine or an Actions job, never from the
   service: `DATABASE_URL=<neon url> uv run alembic upgrade head` (migration `0007`
   creates `api_keys` and `api_usage`).
2. **Create the web service** on Render: *New > Web Service*, connect
   `karank2512/tell-ai`, runtime *Docker* (it finds the root `Dockerfile`), instance
   type *Free*, health check path `/healthz`.
3. **Environment variables** (Render dashboard, never committed): `DATABASE_URL` (the
   Neon pooled URL, `postgresql+psycopg://...?sslmode=require`), `TELL_ENV=prod`,
   optionally `TELL_REPO_URL` and `TELL_FEEDBACK_FORM_URL` so `generated_by` and links
   match the static site. Render sets `PORT` itself.
4. **Issue a key** for the consumer, locally against the same database:
   `DATABASE_URL=<neon url> uv run tell api create-key --owner <handle> --daily-quota 1000`.
   The key is printed once; only its hash is stored. Hand it over through a private
   channel. `owner` is a handle, never an email address.
5. **Warm-up ping** (add to `.github/workflows/` only after the service exists), every
   10 minutes, 13:00-23:00 UTC, Monday to Friday:

   ```yaml
   name: api-warmup
   on:
     schedule:
       - cron: "*/10 13-22 * * 1-5"
     workflow_dispatch:
   jobs:
     ping:
       runs-on: ubuntu-latest
       timeout-minutes: 3
       steps:
         - run: curl --fail --silent --show-error --max-time 90 --retry 1 "$TELL_API_URL/healthz" > /dev/null
           env:
             TELL_API_URL: ${{ vars.TELL_API_URL }}
   ```

   `/healthz` needs no key and is not metered. GitHub may delay scheduled runs by a few
   minutes; that only shortens the warm window.
6. **Smoke test**: `curl -H "Authorization: Bearer $KEY" "$TELL_API_URL/v1/companies/<domain>"`
   returns the same object as the static `companies/<domain>.json` for today.

## Operating it

- `tell api usage [--days 7] [--owner HANDLE]` prints calls per key and UTC day.
- `tell api revoke-key <id or 12-character prefix>`: the key answers 401 from then on.
  A lost key is revoked and replaced, never recovered.
- Raising a quota is a one-row update of `api_keys.daily_quota`.
- If the service misbehaves, suspend it on Render; consumers fall back to the static
  JSON with no change on tell's side.
