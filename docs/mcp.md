# tell-mcp: buying signals in Claude Desktop and Cursor

`tell-mcp` is a small [Model Context Protocol](https://modelcontextprotocol.io) server that
lets an AI agent ask tell which accounts are in market and why, with a receipt URL behind
every claim. It runs locally over stdio, reads tell's public static JSON API
(`docs/api/README.md`) and needs no key, no account and no server.

Spec: `docs/SPEC.md` section 10 ("MCP server"). Code: `packages/tell-mcp/` (a separate
distribution in the uv workspace, ADR 0012; only `tell-mcp` goes to PyPI, ADR 0010).

## Quickstart

```sh
uvx tell-mcp --help      # installs and runs the server's CLI; no clone needed
```

The agent app starts the server for you; you only add it to its config.

**Claude Desktop** (`claude_desktop_config.json`, Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "tell": {
      "command": "uvx",
      "args": ["tell-mcp"],
      "env": { "TELL_BASE_URL": "https://karank2512.github.io/tell-data" }
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "tell": {
      "command": "uvx",
      "args": ["tell-mcp"],
      "env": { "TELL_BASE_URL": "https://karank2512.github.io/tell-data" }
    }
  }
}
```

Restart the app, then ask: *"Which of these ten domains are in market and why?"* The answer
should cite a URL for every signal it mentions.

From a checkout: `uv sync`, then `uv run tell-mcp` (or point the config's `command` at
`uv` with `args: ["run", "--directory", "<checkout>", "tell-mcp"]`).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELL_BASE_URL` | `https://karank2512.github.io/tell-data` | Root of the static site (a fork's own Pages URL works too). |
| `TELL_API_KEY` | unset | Bearer key for the FastAPI service (v1, not deployed yet). |
| `TELL_API_URL` | unset | Base URL of the FastAPI service. Used only together with `TELL_API_KEY`. |
| `TELL_INTAKE_FORM_URL` | unset | Watchlist intake form; `{domains}` / `{watchlist}` placeholders are filled in. Without it, `watch_accounts` links the repo's "Watch these domains" issue form. |

HTTP: 20 s timeout, `User-Agent: tell-mcp/0.1 (+https://github.com/karank2512/tell-ai)`.

## Tools

Every tool description tells the agent to cite the `url` of any contribution it mentions
and to call `explain` before writing outreach copy. Domains are normalised first
(`https://www.Acme.com/careers` becomes `acme.com`).

| Tool | Input | Output |
| --- | --- | --- |
| `watch_accounts` | `domains: [string]`, `watchlist: string` | v0: the intake link prefilled with the domains (max 50 per request) and a note that the watchlist appears after the nightly job. Nothing is written. |
| `in_market` | `watchlist: string`, `since_days: int = 7`, `min_score: number = 6.0`, `families: [string]` (optional) | `[{domain, name, score, heat, top_contributions[:3]}]`, score descending. Kept when `score >= min_score` and the top contribution happened within `since_days` of the watchlist's `as_of`. `families` (for example `sales`, `engineering`) keeps accounts whose top `hiring_velocity` contribution in those role families is recent. |
| `explain` | `domain: string` | The full company object: every contribution with its receipt `url` (velocity with its counted postings), `why_not_higher` (signals that decayed to zero in the last 30 days), `sources`, plus `account_page`. Unknown domain: `monitored: false`. |
| `events` | `domain: string`, `since: date` (optional), `types: [string]` (optional) | Contributions and counted postings, newest first, at most 200, each with `url`; `types` matches a signal type or the kind `posting`. |
| `coverage` | `domains: [string]` | Per domain: `has_board` (`sources.ats`), `has_cik`, `has_feed` (`sources.news`) and a plain-language `note` on what tell cannot see; `status: "not monitored"` when tell has no file for it. |

Prompt: `why_now_email(domain)` fetches the company and returns a prompt with its top two
contributions inlined (type, date, summary, receipt URL), asking for exactly two sentences,
each ending with its receipt. With no data it tells the model to say so instead of
inventing a reason.

Errors: an unreachable site, a timeout or an HTTP 5xx comes back as a tool error with a
one-line message (the server keeps running). A 404 is not an error: it means "not
monitored", and the tools say so.

## v0 limitations

- **Read-only.** `watch_accounts` cannot create a watchlist; it returns a prefilled form
  link. A request shows up after a maintainer accepts it and the nightly job runs.
- **Public watchlists only.** `in_market` reads `api/v0/watchlists/{slug}.json`, which
  exists only for lists marked public (ADR 0022). For anything else, call `explain` or
  `coverage` per domain.
- **No raw event endpoint.** `events` returns the scored contributions and the counted
  postings behind them, not every event tell ingested; decayed signals are listed by
  `explain` under `why_not_higher`.
- **Freshness.** The static files are rebuilt by each ingest run (four a day) and GitHub
  Pages caches them for about 10 minutes.
- **Quiet domains look unknown.** A domain with no signal for 180 days has no file, so it
  reads as "not monitored" (the size budget, `docs/methods/publishing.md`).

## Later: the API

When the FastAPI service (`GET /v1/companies/{domain}`, `GET /v1/in-market`, Bearer keys
`tell_...`) is deployed, set both `TELL_API_KEY` and `TELL_API_URL`. `explain`, `events`,
`coverage`, the prompt and `in_market` then read from the service (company and watchlist
reads, with `Authorization: Bearer <key>`); weekly and health stay on the static files.
With only one of the two set, the server keeps using the static JSON, so nothing breaks
before the service exists. v1 will let `watch_accounts` create the watchlist through
`POST /v1/watchlists` instead of returning a link.

## Listing in the MCP Registry

The [official MCP Registry](https://modelcontextprotocol.io/registry/about) is the public
catalogue that MCP marketplaces pull server metadata from. It hosts metadata only (the code
stays on PyPI) and is still labelled "preview", so its API can change or be reset (checked
2026-10-19). `tell-mcp` is listed there as `io.github.karank2512/tell-mcp`: the
`tell-mcp-v0.1.0` tag published version 0.1.0 to PyPI and to the registry on 2026-10-20.
Merging the files below publishes nothing: the listing happens only when a `tell-mcp-v*` tag
runs `publish-mcp.yml`.

| File | Role |
| --- | --- |
| `packages/tell-mcp/server.json` | The registry entry (schema `2025-12-11`): name, description, repository, and the PyPI package `tell-mcp` at the current version over stdio, started with `uvx`. It documents `TELL_BASE_URL` (default `https://karank2512.github.io/tell-data`) and the optional `TELL_API_URL` / `TELL_API_KEY` (the v1 API, see "Later: the API"). |
| `packages/tell-mcp/README.md` | Ends with `<!-- mcp-name: io.github.karank2512/tell-mcp -->`. PyPI serves the README as the package description, and the registry refuses an entry unless the description of the exact version named in `server.json` contains that line (one space after the colon). It is a comment, so it does not render. |
| `.github/workflows/publish-mcp.yml` | The `registry` job described below. |
| `tests/mcp/test_registry_metadata.py` | Fails when the versions in `server.json`, `pyproject.toml` and `tell_mcp/__init__.py` differ, when the README loses the line above, or when `server.json` drifts from the code's defaults and this page. |

**How a tag triggers it.** Pushing `tell-mcp-vX.Y.Z` runs `publish-mcp.yml`: `build` checks
the tag against the package version and builds, `publish` uploads to PyPI by trusted
publishing, then the `registry` job downloads the registry's `mcp-publisher` binary and runs
`mcp-publisher login github-oidc` and `mcp-publisher publish packages/tell-mcp/server.json`.
The login uses the workflow's own GitHub OIDC token (`id-token: write`), so no secret is
stored, and it can publish only under `io.github.karank2512/`. The job is separate on purpose:
it starts after the PyPI upload succeeded (the registry reads the release from PyPI), and a
registry outage or rejection leaves the run red but the PyPI release untouched. "Re-run failed
jobs" retries just the listing; for a brand-new release the registry's own error says to wait
and retry, because PyPI can take a moment to serve it.

**Releasing a new version.** Change the version in `packages/tell-mcp/pyproject.toml`,
`tell_mcp/__init__.py` and both `version` fields of `server.json` (the server's and the
package's), then tag. The registry treats a published version as immutable, so a metadata fix
needs a new version string; the test above fails until the four values agree.

**Verify.** After the run:

```sh
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.karank2512/tell-mcp"
```

A listed server comes back as `{"servers":[{ ... "name":"io.github.karank2512/tell-mcp" ... }]}`;
an empty `servers` list means it is not listed (the search can take several seconds).

**Claude Connectors Directory.** That directory is a different thing and takes remote servers
only: its [submission page](https://claude.com/docs/connectors/building/submission) asks for an
`https://` URL and says a local server cannot be submitted on its own (it points local servers
to plugins). `tell-mcp` runs locally over stdio, so it is not a connector submission; that
would need a remote HTTPS deployment of the server.

## Development

```sh
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tell   # ADR 0011
uv run pytest tests/mcp -q                        # respx-served fixtures, no network
uv run ruff check packages/tell-mcp tests/mcp
uv run mypy --strict packages/tell-mcp/tell_mcp tests/mcp
```

The tests live in `tests/mcp/` so the root `pytest` run collects them; their fixtures
(`packages/tell-mcp/tests/fixtures/api/v0/`) were rendered from the publish scenario plus
one hand-written company, and a test validates every one against the published schemas.
