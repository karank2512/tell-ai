"""``tell api``: manage v1 API keys (typer sub-app, mounted into ``tell/cli.py``).

Commands:

* ``create-key --owner HANDLE [--daily-quota N]``: create a key and print it **once**
  (JSON on stdout: ``id``, ``key``, ``key_prefix``, ``owner``, ``daily_quota``). Only
  its SHA-256 is stored; a lost key is revoked and replaced, never recovered.
* ``revoke-key ID_OR_PREFIX``: revoke a key by id or 12-character display prefix.
* ``usage [--days N] [--owner HANDLE]``: calls per key and UTC day, one JSON object
  per line, newest day first (never the key or its hash).

``owner`` is a handle (``[a-z0-9._-]``), never an email address.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Annotated, Any

import typer

from tell.api.auth import DEFAULT_DAILY_QUOTA, create_key, revoke_key, usage_rows
from tell.collectors.base import iso_utc, utcnow
from tell.db import session_scope

app = typer.Typer(
    name="api",
    help="Manage FastAPI v1 keys and read their usage (spec section 10).",
    no_args_is_help=True,
    add_completion=False,
)


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True, default=str))


@app.command("create-key")
def create_key_command(
    owner: Annotated[str, typer.Option("--owner", help="Owner handle (never an email).")],
    daily_quota: Annotated[
        int, typer.Option("--daily-quota", min=0, help="Calls allowed per UTC day.")
    ] = DEFAULT_DAILY_QUOTA,
) -> None:
    """Create an API key and print it once; only its SHA-256 is stored."""
    try:
        with session_scope() as session:
            new = create_key(session, owner, daily_quota)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--owner") from exc
    _echo(
        {
            "id": new.id,
            "key": new.key,
            "key_prefix": new.key_prefix,
            "owner": new.owner,
            "daily_quota": new.daily_quota,
        }
    )
    typer.echo("Store this key now: it is shown once and cannot be recovered.", err=True)


@app.command("revoke-key")
def revoke_key_command(
    ref: Annotated[str, typer.Argument(help="Key id or its 12-character display prefix.")],
) -> None:
    """Revoke a key; it answers 401 from then on."""
    try:
        with session_scope() as session:
            row = revoke_key(session, ref, utcnow())
            record = {
                "id": row.id,
                "key_prefix": row.key_prefix,
                "owner": row.owner,
                "revoked_at": iso_utc(row.revoked_at) if row.revoked_at else None,
            }
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _echo(record)


@app.command("usage")
def usage_command(
    days: Annotated[int, typer.Option("--days", min=1, help="UTC days to show.")] = 7,
    owner: Annotated[str | None, typer.Option("--owner", help="Only this owner.")] = None,
) -> None:
    """Calls per key and UTC day, newest first (one JSON object per line)."""
    since = utcnow().date() - timedelta(days=days - 1)
    with session_scope() as session:
        rows = usage_rows(session, since, owner)
    for row in rows:
        _echo(row)
