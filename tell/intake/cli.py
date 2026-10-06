"""``tell intake``: the intake typer sub-app (spec section 15.6, PLAN Phase 12).

Commands:

* ``sync --csv-url URL | --csv-file PATH [--private-dir DIR]``: read the intake form's
  published-as-CSV sheet (``--csv-url`` defaults to ``TELL_INTAKE_CSV_URL``), apply every
  new valid submission to ``watchlists`` / ``watchlist_items`` / ``companies``, commit,
  then write the contact details and webhooks to ``DIR/watchlists/<slug>.yaml``
  (default ``../tell-private``, relative to the working directory; the nightly workflow
  in ``docs/deploy/tell-private-intake.yml`` passes it explicitly).

Output is one JSON line of counts plus rejected row numbers and reasons, never a
name, email, company, URL or slug, so it is safe in a public Actions log. Exit 1 when
the sheet cannot be fetched or its columns drifted, when a private watchlist file does
not parse or validate, or when the private files cannot be written after the commit
(each message names the error type only: a YAML error could quote the file, and a file
name is a customer's slug); row-level problems exit 0 and are listed under
``rejected_rows``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml

from tell.collectors.base import utcnow
from tell.db import session_scope
from tell.intake.reader import (
    DEFAULT_PRIVATE_DIR,
    IntakeError,
    PrivateRecordError,
    check_private_dir,
    read_csv_source,
    sync_submissions,
    write_private_watchlists,
)

app = typer.Typer(
    name="intake",
    help="Turn intake-form submissions into watchlists (contacts stay in tell-private).",
    no_args_is_help=True,
    add_completion=False,
)


def _fail(message: str) -> typer.Exit:
    typer.echo(json.dumps({"source": "intake", "error": message}, sort_keys=True))
    return typer.Exit(code=1)


@app.command("sync")
def sync_command(
    csv_url: Annotated[
        str | None,
        typer.Option(
            "--csv-url",
            envvar="TELL_INTAKE_CSV_URL",
            show_envvar=True,
            show_default=False,
            help="Published-as-CSV URL of the intake form's response sheet (a secret).",
        ),
    ] = None,
    csv_file: Annotated[
        Path | None,
        typer.Option("--csv-file", exists=True, dir_okay=False, help="A downloaded copy."),
    ] = None,
    private_dir: Annotated[
        Path,
        typer.Option("--private-dir", file_okay=False, help="The tell-private checkout."),
    ] = DEFAULT_PRIVATE_DIR,
) -> None:
    """Sync new intake submissions into watchlists."""
    if (csv_url is None) == (csv_file is None):
        msg = "pass exactly one of --csv-url or --csv-file"
        raise typer.BadParameter(msg, param_hint="--csv-url / --csv-file")
    try:
        resolved = check_private_dir(private_dir)
        text = read_csv_source(csv_url=csv_url, csv_file=csv_file)
        with session_scope() as session:
            result = sync_submissions(session, text, private_dir=resolved, now=utcnow())
    except IntakeError as exc:
        raise _fail(str(exc)) from None
    except (PrivateRecordError, yaml.YAMLError) as exc:
        # Neither the message (it may quote the file) nor the file name (a customer's slug)
        # belongs in a log; run the sync locally against the private checkout to see which.
        raise _fail(f"invalid private watchlist file ({type(exc).__name__})") from None
    except ValueError as exc:
        # Anything else that failed validation (a profile, a row, the database layer): the
        # type is diagnostic enough for a log and never carries a value.
        raise _fail(f"intake sync failed ({type(exc).__name__})") from None
    try:
        written = write_private_watchlists(resolved, result.private_updates)
    except OSError as exc:
        # The database commit is done; the next sync re-applies the submission and writes
        # the file then (ADR 0027). The path would name a slug, so only the type is logged.
        raise _fail(f"could not write private watchlist files ({type(exc).__name__})") from None
    record = result.log_record()
    record["private_files_written"] = len(written)
    typer.echo(json.dumps(record, sort_keys=True))
