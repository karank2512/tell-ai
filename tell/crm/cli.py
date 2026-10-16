"""``tell crm``: CRM write-back commands (typer sub-app, mounted into ``tell/cli.py``).

``tell crm hubspot ...`` (ADR 0037, ``docs/crm/hubspot.md``):

* ``check``: one authenticated read and which ``tell_*`` company properties exist. Exit 1
  with a scope or token hint when HubSpot refuses the token.
* ``setup``: create the missing ``tell_*`` company properties (idempotent).
* ``push DOMAIN [--profile NAME] [--task] [--owner-id ID] [--dry-run] [--force]``: upsert
  the company by domain with the latest stored score, write the receipt as a note and,
  with ``--task``, a follow-up task. ``--dry-run`` prints the payloads and sends nothing;
  a score already pushed (same day and value) is reported ``unchanged`` unless ``--force``.

Every command prints one JSON object. The token comes from ``HUBSPOT_PRIVATE_APP_TOKEN``
and is never printed.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from tell.collectors.base import utcnow
from tell.config import get_settings
from tell.crm.hubspot import (
    HubSpotClient,
    HubSpotError,
    check,
    ensure_properties,
    load_receipt,
    push_company,
)
from tell.db import session_scope
from tell.signals.profiles import DEFAULT_PROFILE_NAME, ProfileNotFoundError, get_profile

app = typer.Typer(
    name="crm",
    help="Write tell's score and receipt onto the customer's CRM records (ADR 0037).",
    no_args_is_help=True,
    add_completion=False,
)
hubspot_app = typer.Typer(
    name="hubspot",
    help="HubSpot write-back: company properties plus a note with the receipt.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(hubspot_app, name="hubspot")


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True, default=str))


def _fail(message: str) -> typer.Exit:
    typer.echo(json.dumps({"source": "hubspot", "error": message}, sort_keys=True))
    return typer.Exit(code=1)


def _client() -> HubSpotClient:
    secret = get_settings().hubspot_private_app_token
    if secret is None:
        raise _fail("HUBSPOT_PRIVATE_APP_TOKEN is not set (docs/crm/hubspot.md)")
    return HubSpotClient(secret.get_secret_value())


@hubspot_app.command("check")
def check_command() -> None:
    """Confirm the token works and report which tell properties exist."""
    try:
        with _client() as client:
            _echo(check(client))
    except HubSpotError as exc:
        raise _fail(exc.hint) from None


@hubspot_app.command("setup")
def setup_command() -> None:
    """Create the tell_* company properties that are missing (idempotent)."""
    try:
        with _client() as client:
            _echo(ensure_properties(client))
    except HubSpotError as exc:
        raise _fail(exc.hint) from None


@hubspot_app.command("push")
def push_command(  # noqa: PLR0917 - typer options are positional by construction
    domain: Annotated[str, typer.Argument(help="Company domain as tell knows it.")],
    profile: Annotated[
        str, typer.Option("--profile", help="ICP profile whose score is pushed.")
    ] = DEFAULT_PROFILE_NAME,
    task: Annotated[bool, typer.Option("--task", help="Also create a follow-up task.")] = False,
    owner_id: Annotated[
        str | None, typer.Option("--owner-id", help="HubSpot owner id for the task.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the payloads; send nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Write the note even if this score was pushed.")
    ] = False,
) -> None:
    """Upsert the company by domain and write the receipt as a note."""
    settings = get_settings()
    try:
        icp = get_profile(profile)
    except ProfileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--profile") from None
    normalized = domain.strip().lower().removeprefix("www.").rstrip(".")
    try:
        with session_scope() as session:
            receipt = load_receipt(session, normalized, icp)
    except LookupError as exc:
        raise _fail(str(exc)) from None
    now = utcnow()
    if dry_run:
        outcome = push_company(
            None,
            receipt,
            now=now,
            base_url=settings.tell_base_url,
            threshold=icp.alert_threshold,
            task=task,
            owner_id=owner_id,
            dry_run=True,
        )
        _echo(outcome.as_record())
        return
    try:
        with _client() as client:
            outcome = push_company(
                client,
                receipt,
                now=now,
                base_url=settings.tell_base_url,
                threshold=icp.alert_threshold,
                task=task,
                owner_id=owner_id,
                force=force,
            )
    except HubSpotError as exc:
        raise _fail(exc.hint) from None
    _echo(outcome.as_record())
