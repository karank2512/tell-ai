"""``tell discover``: the discovery typer sub-app (mounted into ``tell/cli.py`` in Phase 9).

Commands:

* ``domain --domain acme.com [--name "Acme"]``: run spec section 4 for one domain.
* ``batch [--limit N] [--rps R] [--concurrency C] [--max-minutes M]``: one budgeted pass
  (defaults ``TELL_DISCOVERY_BUDGET``, ``TELL_DISCOVERY_RPS``,
  ``TELL_DISCOVERY_CONCURRENCY`` and ``TELL_DISCOVERY_MAX_MINUTES``) over new domains and
  boards due for re-verification. It commits after every domain, so a killed job keeps
  everything finished so far.
* ``reverify-guesses [--limit N] [--rps R]``: re-check active slug-guessed boards (0.60)
  under the ownership-evidence rule (ADR 0031); failures become ``paused`` with a
  ``board_verify`` item. Commits after every board; idempotent.
* ``seed-yc --file PATH | --fetch``: seed ``companies`` from the YC directory.
* ``prune-platform-companies [--dry-run | --apply]``: list companies keyed by a platform
  domain (``apple.com``, ``*.vercel.app``, ...; ADR 0033); ``--apply`` deletes those with
  no events, boards, watchlist items, signals or feedback. Dry run by default.
* ``issuers [--since DATE] [--limit N] [--shadow | --apply] [--retry-due]
  [--concurrency C] [--max-minutes M]``: issuer-first discovery (ADR 0035,
  :mod:`tell.discovery.issuers`): find the own site of each new Form D issuer (a Delaware
  corporation no company holds yet) and, on tier A/B site evidence, create the company and
  link its filings by CIK (rule R0). Shadow by default (writes nothing, prints what it
  would do); ``--apply`` writes and commits after every issuer. ``--retry-due`` takes only
  the ``no_domain`` issuers whose retry is due. One JSON line per issuer, then the run line.

The other commands commit on success. Results print as JSON lines on stdout.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from tell.collectors.base import utcnow
from tell.db import session_scope
from tell.discovery.ats import discover_batch, discover_domain
from tell.discovery.issuers import (
    DEFAULT_CONCURRENCY,
    DEFAULT_LIMIT,
    DEFAULT_MAX_MINUTES,
    DEFAULT_WINDOW_DAYS,
    discover_issuers,
)
from tell.discovery.reverify import reverify_guesses
from tell.discovery.yc import (
    fetch_directory,
    prune_platform_companies,
    seed_companies,
    seed_from_file,
)
from tell.http import make_client

app = typer.Typer(
    name="discover",
    help="Find job boards for company domains and seed companies.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("domain")
def domain_command(
    domain: Annotated[str, typer.Option("--domain", help="Company domain, e.g. acme.com.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Company name used for verification.")
    ] = None,
) -> None:
    """Discover the job board for one domain."""
    with session_scope() as session, make_client() as client:
        try:
            result = discover_domain(session, client, domain, name, now=utcnow())
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--domain") from exc
        record = result.log_record()
        record["discovered_from"] = result.discovered_from
        typer.echo(json.dumps(record, sort_keys=True))


@app.command("batch")
def batch_command(
    limit: Annotated[
        int | None, typer.Option("--limit", min=0, help="Max new domains (default: budget).")
    ] = None,
    rps: Annotated[
        float | None, typer.Option("--rps", min=0.01, help="Requests per second (default 4).")
    ] = None,
    concurrency: Annotated[
        int | None,
        typer.Option(
            "--concurrency", min=1, help="Domains in flight at once, sharing --rps (default 6)."
        ),
    ] = None,
    max_minutes: Annotated[
        float | None,
        typer.Option(
            "--max-minutes", min=0.01, help="Start no new domain after this (default 22)."
        ),
    ] = None,
) -> None:
    """One budgeted discovery pass (spec section 11: <= 300 new domains at 4 rps)."""
    with session_scope() as session, make_client() as client:
        batch = discover_batch(
            session,
            client,
            now=utcnow(),
            limit=limit,
            rps=rps,
            concurrency=concurrency,
            max_minutes=max_minutes,
            commit=Session.commit,
        )
        processed = batch.processed
        summary = {
            "run_id": batch.run_id,
            "new_domains": batch.new_domains,
            "reverified": batch.reverified,
            "errors": len(batch.errors),
            "outcomes": batch.outcomes,
            "concurrency": batch.concurrency,
            "budget_exhausted": batch.budget_exhausted,
            "deferred": batch.deferred,
            "elapsed_s": round(batch.elapsed_s, 1),
            "seconds_per_domain": round(batch.elapsed_s / processed, 2) if processed else None,
        }
        typer.echo(json.dumps(summary, sort_keys=True))


@app.command("reverify-guesses")
def reverify_guesses_command(
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=0, help="Max boards to check (default: all)."),
    ] = None,
    rps: Annotated[
        float | None, typer.Option("--rps", min=0.01, help="Requests per second (default 4).")
    ] = None,
) -> None:
    """Re-check active slug-guessed boards for ownership evidence (ADR 0031)."""
    with session_scope() as session, make_client() as client:
        result = reverify_guesses(
            session, client, now=utcnow(), limit=limit, rps=rps, commit=Session.commit
        )
        for check in result.checks:
            typer.echo(json.dumps(check.log_record(), sort_keys=True))
        summary = {"checked": len(result.checks), "outcomes": result.outcomes}
        typer.echo(json.dumps(summary, sort_keys=True))


@app.command("seed-yc")
def seed_yc_command(
    file: Annotated[
        Path | None,
        typer.Option("--file", exists=True, dir_okay=False, help="Saved directory JSON."),
    ] = None,
    fetch: Annotated[
        bool, typer.Option("--fetch", help="Download the public directory instead.")
    ] = False,
) -> None:
    """Seed companies from the public YC directory (``--file`` or ``--fetch``, not both)."""
    if (file is None) == (not fetch):
        raise typer.BadParameter("pass exactly one of --file PATH or --fetch")
    with session_scope() as session:
        if file is not None:
            stats = seed_from_file(session, file)
        else:
            with make_client() as client:
                stats = seed_companies(session, fetch_directory(client))
        typer.echo(json.dumps(asdict(stats), sort_keys=True))


@app.command("prune-platform-companies")
def prune_platform_companies_command(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="List only (default), or delete the unreferenced platform-domain companies.",
        ),
    ] = True,
) -> None:
    """List (or with --apply delete) companies whose domain is a platform's (ADR 0033)."""
    with session_scope() as session:
        report = prune_platform_companies(session, apply=not dry_run)
        typer.echo(json.dumps(report, sort_keys=True))


@app.command("issuers")
def issuers_command(  # noqa: PLR0917 - typer maps each parameter to one CLI option
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help=f"Filings on or after this date (default: the last {DEFAULT_WINDOW_DAYS} days).",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=0, help="Max issuers this run (new first, then retries)."),
    ] = DEFAULT_LIMIT,
    shadow: Annotated[
        bool,
        typer.Option(
            "--shadow/--apply",
            help="Shadow (default): write nothing, print the decisions. --apply writes them.",
        ),
    ] = True,
    retry_due: Annotated[
        bool,
        typer.Option("--retry-due", help="Only the no_domain issuers whose retry is due."),
    ] = False,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", min=1, max=8, help="Issuers in flight at once."),
    ] = DEFAULT_CONCURRENCY,
    max_minutes: Annotated[
        float,
        typer.Option("--max-minutes", min=0.01, help="Start no new issuer after this."),
    ] = DEFAULT_MAX_MINUTES,
) -> None:
    """Create companies from new Form D issuers on site evidence (ADR 0035)."""
    start = since.replace(tzinfo=UTC) if since is not None else None
    with session_scope() as session, make_client() as client:
        run = discover_issuers(
            session,
            client,
            now=utcnow(),
            apply=not shadow,
            since=start,
            limit=limit,
            retry_due=retry_due,
            concurrency=concurrency,
            max_minutes=max_minutes,
            commit=Session.commit,
        )
        for result in run.results:
            typer.echo(json.dumps(result.log_record(), sort_keys=True))
        typer.echo(json.dumps(run.run_line(), sort_keys=True))
