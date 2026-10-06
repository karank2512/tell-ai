# tell-mcp

MCP server for [tell](https://github.com/karank2512/tell-ai), the open-source buying-signals
pipeline. It lets Claude Desktop, Cursor or any MCP client ask which accounts are in market
and why, with a receipt URL behind every signal (a job board, an SEC Form D filing, a news
item). It runs locally over stdio and reads tell's public static JSON: no key, no account.

## Quickstart

```sh
uvx tell-mcp --help
```

Claude Desktop (`claude_desktop_config.json`) or Cursor (`~/.cursor/mcp.json`):

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

Restart the app and ask: *"Which of these ten domains are in market and why?"*

## Tools

| Tool | Input | Output |
| --- | --- | --- |
| `watch_accounts` | `domains`, `watchlist` | v0: the intake form link prefilled with the domains; the watchlist appears after the nightly job |
| `in_market` | `watchlist`, `since_days` (7), `min_score` (6.0), `families` | `[{domain, name, score, heat, top_contributions[:3]}]`, highest score first |
| `explain` | `domain` | Every contribution with its receipt URL, plus "why not higher" (signals that decayed in the last 30 days) |
| `events` | `domain`, `since`, `types` | Contributions and counted postings, newest first, at most 200, each with a URL |
| `coverage` | `domains` | Per domain: job board, SEC CIK, news feed, or "not monitored" |

Prompt `why_now_email(domain)`: the two-sentence "why now" for an outreach email, built from
the top two signals with their receipt URLs. Tool descriptions ask the agent to cite the
`url` of every signal it mentions and to call `explain` before writing outreach copy.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELL_BASE_URL` | `https://karank2512.github.io/tell-data` | The static site to read |
| `TELL_API_KEY`, `TELL_API_URL` | unset | Both set: read companies and watchlists from the FastAPI service with a Bearer key |
| `TELL_INTAKE_FORM_URL` | unset | Intake form for `watch_accounts` (default: the repo's "Watch these domains" issue form) |

## v0 limitations

- `watch_accounts` returns a prefilled link; it does not create the watchlist.
- `in_market` reads public watchlists only (`api/v0/watchlists/{slug}.json`).
- `events` has no raw event endpoint to read: it lists the scored contributions and the
  counted postings behind them.
- Data refreshes with each ingest run (four a day); a domain quiet for 180 days has no file
  and reads as "not monitored".

When the FastAPI service is deployed, setting `TELL_API_KEY` and `TELL_API_URL` switches
company and watchlist reads to it without any other change.

Full docs: [docs/mcp.md](https://github.com/karank2512/tell-ai/blob/main/docs/mcp.md).
License: Apache-2.0.
