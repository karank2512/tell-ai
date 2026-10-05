"""``tell discover``: the discovery typer sub-app (mounted into ``tell/cli.py`` in Phase 9).

Commands:

* ``domain --domain acme.com [--name "Acme"]``: run spec section 4 for one domain.
* ``batch [--limit N] [--rps R]``: one budgeted pass (defaults ``TELL_DISCOVERY_BUDGET``
  and ``TELL_DISCOVERY_RPS``) over new domains and boards due for re-verification.
* ``seed-yc --file PATH | --fetch``: seed ``companies`` from the YC directory.

Each command commits on success. Results print as JSON lines on stdout.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from tell.collectors.base import utcnow
from tell.db import session_scope
from tell.discovery.ats import discover_batch, discover_domain
from tell.discovery.yc import fetch_directory, seed_companies, seed_from_file
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
) -> None:
    """One budgeted discovery pass (spec section 11: <= 300 new domains at 4 rps)."""
    with session_scope() as session, make_client() as client:
        batch = discover_batch(session, client, now=utcnow(), limit=limit, rps=rps)
        summary = {
            "run_id": batch.run_id,
            "new_domains": batch.new_domains,
            "reverified": batch.reverified,
            "errors": len(batch.errors),
            "outcomes": batch.outcomes,
        }
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
